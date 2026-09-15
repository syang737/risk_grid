"""Batches: named, timestamped snapshots of a book.

The incumbent labels every screen with one (`US WBL IntraDay 2026-08-13
14:16:42`), and post-trade stress is inherently snapshot-shaped, so the batch is
a first-class object rather than an implicit global.

A batch is built once and read many times, so there are three tiers between a
request and the data, cheapest first:

  rollups      pre-aggregated root levels, materialised at build time. Every
               dimension below the cardinality threshold costs ~1.5MB combined
               and serves in about a millisecond.
  _aggregates  computed aggregates keyed on request identity, excluding sort
               and paging, so scrolling and re-sorting a computed level is free.
  source       the batch itself -- a frame in memory, or a lazy scan over
               Parquet. Serving from Parquet measures within ~1.5x of RAM and
               faster when few scenario columns are needed.

BatchStore sits above all of it, holding loaded batches across requests.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import polars as pl

from .aggregate import (
    ACCOUNT_DRILL,
    DIMENSIONS,
    GREEK_COLUMNS,
    INSTRUMENT_DRILL,
    PivotRequest,
    PivotResult,
    Source,
    aggregate_frame,
    as_lazy,
    attach_scenarios,
    column_names,
    detail_count_column,
    present,
    scenario_columns,
    totals_from,
    totals_request,
)
from .engine import base_valuation, scenario_pnl
from .scenarios import ScenarioGrid, sigma_grid
from .storage import (
    ARTIFACT_VERSION,
    BatchLayout,
    Manifest,
    ObjectStore,
    _write_parquet,
    read_manifest,
    scan,
    write_manifest,
)

# Aggregates are capped by estimated bytes, not by count: a contract-level
# aggregate on a large book is a million rows wide with 50 scenario columns,
# while a desk-level one is 25 rows. Counting entries would be meaningless.
DEFAULT_AGGREGATE_BUDGET = 1 * 1024**3
DEFAULT_MAX_BATCHES = 3

# Above this many groups a rollup stops being cheap. Measured at 2M positions:
# every dimension except contract totals 1.57MB, while contract alone is 82MB
# across 343k groups -- so contract is computed on demand and cached instead.
ROLLUP_MAX_GROUPS = 50_000

# Columns `base_valuation` and `scenario_pnl` derive. Dropping them leaves the
# raw book, which is 9x smaller and enough to reprice from.
DERIVED_COLUMNS = ("mark", "market_value", "und_qty", "delta", "gamma", "vega", "theta", "rho")


@dataclass
class Batch:
    """A loaded snapshot: positions, greeks and the scenario P&L matrix."""

    id: str
    label: str
    timestamp: datetime
    frame: Source
    scenario_labels: tuple[str, ...]
    shock_config: str = "default"
    firm_id: str = "default"
    n_positions: int = 0
    rollups: dict[str, pl.DataFrame] = field(default_factory=dict)
    aggregate_budget: int = DEFAULT_AGGREGATE_BUDGET

    _aggregates: OrderedDict = field(default_factory=OrderedDict, repr=False)
    _bytes: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.n_positions and isinstance(self.frame, pl.DataFrame):
            self.n_positions = self.frame.height

    # -- identity -----------------------------------------------------------

    @property
    def display_name(self) -> str:
        return f"{self.label} {self.timestamp:%Y-%m-%d %H:%M:%S}"

    @property
    def positions(self) -> int:
        return self.n_positions

    @property
    def scenario_columns(self) -> list[str]:
        return scenario_columns(self.frame)

    @property
    def dimensions(self) -> list[str]:
        available = set(column_names(self.frame))
        return [d for d in DIMENSIONS if d in available]

    # -- queries ------------------------------------------------------------

    def aggregate(self, request: PivotRequest) -> PivotResult:
        """One level of the pivot tree, from the cheapest tier that can serve it."""
        rolled = self._from_rollup(request)
        if rolled is not None:
            return present(rolled, request)

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
        """Grand total, folded from the root aggregate.

        Goes through `self.aggregate` so it reads whatever the root rows read --
        free, and identical to the rows it sits above rather than merely close
        to them.
        """
        root = self.aggregate(totals_request(request))
        return totals_from(root.rows, request, self.scenario_columns)

    def distinct(self, column: str, limit: int = 1000) -> list:
        """Distinct values of a column, for populating set filters."""
        if column not in column_names(self.frame):
            raise KeyError(column)
        return (
            as_lazy(self.frame)
            .select(pl.col(column).unique().sort().head(limit))
            .collect()[column]
            .to_list()
        )

    # -- rollups ------------------------------------------------------------

    def _from_rollup(self, request: PivotRequest) -> PivotResult | None:
        """Serve a root-level request from a materialised rollup, if one fits.

        Rollups are built with every measure, every scenario column and every
        detail dimension, so a narrower request is a column selection rather
        than a different aggregation. Anything filtered or drilled falls
        through -- a rollup only knows the whole book at one grain.
        """
        if request.depth or request.filters or request.include_worst_scenario:
            return None

        dimension = request.group_column
        rollup = self.rollups.get(dimension)
        if rollup is None:
            return None

        details = [
            d for d in request.detail_dimensions
            if d != dimension and detail_count_column(d) in rollup.columns
        ]
        keep = [dimension, "positions"]
        keep += [m for m in request.measures if m in rollup.columns]
        keep += [c for c in rollup.columns if c.startswith("s") and c[1:].isdigit()]
        if "worst" in rollup.columns:
            keep.append("worst")
        for d in details:
            keep += [d, detail_count_column(d)]

        frame = rollup.select(list(dict.fromkeys(keep)))
        return PivotResult(
            rows=frame,
            total_rows=frame.height,
            group_column=dimension,
            is_leaf=False,
            detail_dimensions=tuple(details),
        )

    def materialise_rollups(self, max_groups: int = ROLLUP_MAX_GROUPS) -> dict[str, int]:
        """Pre-aggregate every root level cheap enough to be worth storing.

        Cardinality is measured first, in one pass, so a dimension that would
        produce a huge rollup is skipped rather than computed and discarded.
        """
        dims = self.dimensions
        if not dims:
            return {}

        counts = (
            as_lazy(self.frame)
            .select([pl.col(d).n_unique().alias(d) for d in dims])
            .collect()
            .to_dicts()[0]
        )

        detail = tuple(dims)
        built: dict[str, int] = {}
        for dim in dims:
            if counts[dim] > max_groups:
                continue
            request = PivotRequest(
                dimensions=(dim,), measures=GREEK_COLUMNS, detail_dimensions=detail
            )
            self.rollups[dim] = aggregate_frame(self.frame, request).rows
            built[dim] = counts[dim]
        return built

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
            return {
                "entries": len(self._aggregates),
                "bytes": self._bytes,
                "rollups": len(self.rollups),
            }

    def warm(self, drills: tuple[tuple[str, ...], ...] = (ACCOUNT_DRILL, INSTRUMENT_DRILL)) -> None:
        """Precompute root levels so the first screen is instant.

        A no-op for dimensions already covered by a rollup. Only roots: deeper
        levels are already fast because they sit behind a filter, and
        precomputing them would be guessing at where the user will click.
        """
        for dims in drills:
            self.aggregate(PivotRequest(dimensions=dims, limit=1))


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def build_batch(
    positions: pl.DataFrame,
    sigma_daily: np.ndarray,
    grid: ScenarioGrid | None = None,
    label: str = "Synthetic IntraDay",
    batch_id: str | None = None,
    horizon_days: float = 1.0,
    shock_config: str = "default",
    timestamp: datetime | None = None,
    firm_id: str = "default",
) -> Batch:
    """Value a book and build its scenario matrix into a Batch.

    Rollups are not materialised here: that is a build-worker concern, and
    doing it on every in-process build would make tests pay for it.
    """
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
        firm_id=firm_id,
    )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def write_batch(
    batch: Batch,
    store: ObjectStore,
    max_groups: int = ROLLUP_MAX_GROUPS,
    materialise: bool = True,
) -> Manifest:
    """Write a batch's artifacts and return its manifest.

    Requires a materialized frame -- this is the build path, and the batch was
    just computed in memory.
    """
    if not isinstance(batch.frame, pl.DataFrame):
        raise TypeError("write_batch needs a materialized batch frame")

    layout = BatchLayout(batch.firm_id, batch.id)
    sizes: dict[str, int] = {}
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    scen = scenario_columns(batch.frame)
    raw = batch.frame.drop([c for c in (*DERIVED_COLUMNS, *scen) if c in batch.frame.columns])
    sizes["positions"] = _write_parquet(store, layout.positions, raw)
    sizes["full"] = _write_parquet(store, layout.full, batch.frame)
    timings["write_frames"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    built = batch.materialise_rollups(max_groups) if materialise else {}
    for dim, frame in batch.rollups.items():
        sizes[f"rollup_{dim}"] = _write_parquet(store, layout.rollup(dim), frame)
    timings["rollups"] = time.perf_counter() - t0

    manifest = Manifest(
        version=ARTIFACT_VERSION,
        firm_id=batch.firm_id,
        batch_id=batch.id,
        label=batch.label,
        timestamp=batch.timestamp.isoformat(),
        positions=batch.positions,
        scenario_labels=list(batch.scenario_labels),
        shock_config=batch.shock_config,
        rollups=built,
        sizes=sizes,
        timings=timings,
    )
    write_manifest(store, layout, manifest)
    return manifest


def load_batch(store: ObjectStore, firm_id: str, batch_id: str, eager: bool = False) -> Batch:
    """Load a persisted batch.

    Lazy by default: the grid scans Parquet rather than holding the book in
    RAM, which is the difference between sizing a box for one firm and for
    thirty. Pass `eager=True` where a materialized frame is genuinely needed.
    """
    layout = BatchLayout(firm_id, batch_id)
    manifest = read_manifest(store, layout)

    source: Source = scan(store, layout.full)
    if eager:
        source = source.collect()

    rollups = {
        dim: pl.read_parquet(store.uri(layout.rollup(dim)), **store.scan_options())
        for dim in manifest.rollups
        if store.exists(layout.rollup(dim))
    }

    return Batch(
        id=manifest.batch_id,
        label=manifest.label,
        timestamp=manifest.built_at,
        frame=source,
        scenario_labels=tuple(manifest.scenario_labels),
        shock_config=manifest.shock_config,
        firm_id=manifest.firm_id,
        n_positions=manifest.positions,
        rollups=rollups,
    )


def load_positions(store: ObjectStore, firm_id: str, batch_id: str) -> pl.DataFrame:
    """The raw book from a batch, for repricing it under a different config."""
    return pl.read_parquet(
        store.uri(BatchLayout(firm_id, batch_id).positions), **store.scan_options()
    )


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


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
