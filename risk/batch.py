"""Batches: named, timestamped snapshots of a book.

The incumbent labels every screen with one (`US WBL IntraDay 2026-08-13
14:16:42`), and post-trade stress is inherently snapshot-shaped, so the batch is
a first-class object rather than an implicit global. Modelling it now is what
makes batch-vs-batch comparison possible later.

Two caches live here, for different reasons:

  BatchStore       holds loaded batches across requests. A grid session fires
                   dozens of pivots against one batch and a 5M-row batch is
                   several GB, so it can neither be rebuilt per request nor
                   held without bound.

  Batch._aggregates  holds computed aggregates keyed on the request identity,
                   ignoring sort and paging. Scrolling and re-sorting an
                   already-computed level then costs microseconds instead of
                   repeating the groupby.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import polars as pl

from .aggregate import (
    ACCOUNT_DRILL,
    INSTRUMENT_DRILL,
    PivotRequest,
    PivotResult,
    aggregate_frame,
    attach_scenarios,
    present,
    scenario_columns,
    totals_from,
    totals_request,
)
from .engine import base_valuation, scenario_pnl
from .scenarios import ScenarioGrid, sigma_grid

# Aggregates are capped by estimated bytes, not by count: a contract-level
# aggregate on a large book is a million rows wide with 50 scenario columns,
# while a desk-level one is 25 rows. Counting entries would be meaningless.
DEFAULT_AGGREGATE_BUDGET = 1 * 1024**3
DEFAULT_MAX_BATCHES = 3


@dataclass
class Batch:
    """A loaded snapshot: positions, greeks and the scenario P&L matrix."""

    id: str
    label: str
    timestamp: datetime
    frame: pl.DataFrame
    scenario_labels: tuple[str, ...]
    shock_config: str = "default"
    aggregate_budget: int = DEFAULT_AGGREGATE_BUDGET

    _aggregates: OrderedDict = field(default_factory=OrderedDict, repr=False)
    _bytes: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- identity -----------------------------------------------------------

    @property
    def display_name(self) -> str:
        return f"{self.label} {self.timestamp:%Y-%m-%d %H:%M:%S}"

    @property
    def positions(self) -> int:
        return self.frame.height

    @property
    def scenario_columns(self) -> list[str]:
        return scenario_columns(self.frame)

    # -- queries ------------------------------------------------------------

    def aggregate(self, request: PivotRequest) -> PivotResult:
        """One level of the pivot tree, served from cache where possible."""
        key = request.cache_key()

        with self._lock:
            entry = self._aggregates.get(key)
            if entry is not None:
                self._aggregates.move_to_end(key)

        if entry is None:
            result = aggregate_frame(self.frame, request)
            self._store(key, result)
        else:
            result, _ = entry

        return present(result, request)

    def totals(self, request: PivotRequest) -> dict:
        """Grand total, folded from the cached root aggregate.

        Goes through `self.aggregate` so it hits the same cache entry as the
        root rows on screen -- free after the first call, and identical to the
        rows it sits above rather than merely close to them.
        """
        root = self.aggregate(totals_request(request))
        return totals_from(root.rows, request, self.scenario_columns)

    def distinct(self, column: str, limit: int = 1000) -> list:
        """Distinct values of a column, for populating set filters."""
        if column not in self.frame.columns:
            raise KeyError(column)
        return self.frame[column].unique().sort().head(limit).to_list()

    # -- cache --------------------------------------------------------------

    def _store(self, key: tuple, result: PivotResult) -> None:
        size = result.rows.estimated_size()
        # A single aggregate larger than the whole budget is not worth evicting
        # everything else for; serve it and move on.
        if size > self.aggregate_budget:
            return
        with self._lock:
            if key in self._aggregates:
                return
            self._aggregates[key] = (result, size)
            self._bytes += size
            while self._bytes > self.aggregate_budget and len(self._aggregates) > 1:
                _, (_, evicted) = self._aggregates.popitem(last=False)
                self._bytes -= evicted

    def cache_stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._aggregates), "bytes": self._bytes}

    def warm(self, drills: tuple[tuple[str, ...], ...] = (ACCOUNT_DRILL, INSTRUMENT_DRILL)) -> None:
        """Precompute root levels so the first screen is instant.

        Only the root of each drill order: deeper levels are already fast
        because they sit behind a filter, and precomputing them would be
        guessing at where the user will click.
        """
        for dims in drills:
            self.aggregate(PivotRequest(dimensions=dims, limit=1))


def _get_aggregates(batch: Batch) -> OrderedDict:
    return batch._aggregates


def build_batch(
    positions: pl.DataFrame,
    sigma_daily: np.ndarray,
    grid: ScenarioGrid | None = None,
    label: str = "Synthetic IntraDay",
    batch_id: str | None = None,
    horizon_days: float = 1.0,
    shock_config: str = "default",
    timestamp: datetime | None = None,
) -> Batch:
    """Value a book and build its scenario matrix into a Batch."""
    grid = grid or sigma_grid()
    valued = base_valuation(positions)
    pnl = scenario_pnl(valued, grid, sigma_daily=sigma_daily, horizon_days=horizon_days)
    frame = attach_scenarios(valued, pnl, grid.labels)

    ts = timestamp or datetime.now(timezone.utc)
    return Batch(
        id=batch_id or f"{label.lower().replace(' ', '-')}-{ts:%Y%m%d-%H%M%S}",
        label=label,
        timestamp=ts,
        frame=frame,
        scenario_labels=grid.labels,
        shock_config=shock_config,
    )


class BatchStore:
    """Process-level cache of loaded batches, evicted least-recently-used."""

    def __init__(self, max_batches: int = DEFAULT_MAX_BATCHES) -> None:
        self.max_batches = max_batches
        self._batches: OrderedDict[str, Batch] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, batch: Batch) -> Batch:
        with self._lock:
            self._batches[batch.id] = batch
            self._batches.move_to_end(batch.id)
            while len(self._batches) > self.max_batches:
                self._batches.popitem(last=False)
        return batch

    def get(self, batch_id: str) -> Batch:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            self._batches.move_to_end(batch_id)
            return batch

    def latest(self) -> Batch:
        with self._lock:
            if not self._batches:
                raise KeyError("no batches loaded")
            return next(reversed(self._batches.values()))

    def list(self) -> list[Batch]:
        with self._lock:
            return list(reversed(self._batches.values()))

    def __contains__(self, batch_id: object) -> bool:
        with self._lock:
            return batch_id in self._batches

    def __len__(self) -> int:
        with self._lock:
            return len(self._batches)
