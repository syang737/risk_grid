"""Batch identity, the aggregate cache, and eviction."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from risk.aggregate import INSTRUMENT_DRILL, Filter, PivotRequest, totals_request
from risk.batch import Batch, BatchStore, build_batch
from risk.synthetic import generate_book
from risk.templates import default_shock_config


@pytest.fixture(scope="module")
def batch() -> Batch:
    positions, sigma = generate_book(10_000, seed=9)
    return build_batch(
        positions, sigma, grid=default_shock_config().to_grid(),
        label="US WBL IntraDay",
        timestamp=datetime(2026, 8, 13, 14, 16, 42, tzinfo=timezone.utc),
    )


@pytest.fixture
def req() -> PivotRequest:
    return PivotRequest(dimensions=INSTRUMENT_DRILL, detail_dimensions=("account",))


def test_display_name_matches_the_incumbent_format(batch):
    assert batch.display_name == "US WBL IntraDay 2026-08-13 14:16:42"
    assert batch.positions == 10_000
    assert len(batch.scenario_columns) == 20


def test_repeat_aggregate_is_served_from_cache(batch, req):
    before = batch.cache_stats()["entries"]
    first = batch.aggregate(req)
    after_first = batch.cache_stats()["entries"]
    second = batch.aggregate(req)

    assert after_first == before + 1
    assert batch.cache_stats()["entries"] == after_first  # no new entry
    assert first.rows.equals(second.rows)


def test_sorting_and_paging_reuse_one_aggregate(batch, req):
    batch.aggregate(req)
    entries = batch.cache_stats()["entries"]

    batch.aggregate(dataclasses.replace(req, sort=(("worst", False),)))
    batch.aggregate(dataclasses.replace(req, offset=2, limit=3))

    # Presentation-only changes must not force a recompute.
    assert batch.cache_stats()["entries"] == entries


def test_totals_share_the_root_aggregate(batch, req):
    """Totals must not add a cache entry beyond the root rows."""
    batch.aggregate(req)
    entries = batch.cache_stats()["entries"]
    result = batch.aggregate(req)
    t = batch.totals(req)

    assert batch.cache_stats()["entries"] == entries
    assert t["positions"] == result.rows["positions"].sum()


def test_totals_request_hashes_like_the_root_request(req):
    assert totals_request(req).cache_key() == req.cache_key()

    drilled = req.child("Banking")
    # Drilled in, totals still describe the root level, so they differ from the
    # drilled rows but match the root.
    assert totals_request(drilled).cache_key() == req.cache_key()


def test_different_filters_get_different_entries(batch, req):
    batch.aggregate(req)
    entries = batch.cache_stats()["entries"]
    batch.aggregate(dataclasses.replace(req, filters=(Filter("instrument_type", "equals", "OPTION"),)))
    assert batch.cache_stats()["entries"] == entries + 1


def test_cache_evicts_by_bytes(batch, req):
    small = dataclasses.replace(batch, aggregate_budget=4096)
    object.__setattr__(small, "_aggregates", type(small._aggregates)())
    object.__setattr__(small, "_bytes", 0)

    for dims in (("sector",), ("underlying",), ("account",), ("desk",)):
        small.aggregate(dataclasses.replace(req, dimensions=dims))

    stats = small.cache_stats()
    # Always keeps at least one, never grows past the budget with more than one.
    assert stats["entries"] >= 1
    assert stats["entries"] == 1 or stats["bytes"] <= small.aggregate_budget


def test_warm_precomputes_root_levels(batch):
    fresh = build_batch(*generate_book(2_000, seed=4), grid=default_shock_config().to_grid())
    assert fresh.cache_stats()["entries"] == 0
    fresh.warm()
    assert fresh.cache_stats()["entries"] == 2


def test_distinct_values(batch):
    assert sorted(batch.distinct("instrument_type")) == ["EQUITY", "OPTION"]
    with pytest.raises(KeyError):
        batch.distinct("nope")


def test_store_evicts_least_recently_used():
    store = BatchStore(max_batches=2)
    made = []
    for i in range(3):
        b = build_batch(*generate_book(500, seed=i), batch_id=f"b{i}",
                        grid=default_shock_config().to_grid())
        made.append(b)
        store.put(b)

    assert len(store) == 2
    assert "b0" not in store and "b2" in store
    assert store.latest().id == "b2"


def test_store_get_refreshes_recency():
    store = BatchStore(max_batches=2)
    for i in range(2):
        store.put(build_batch(*generate_book(500, seed=i), batch_id=f"b{i}",
                              grid=default_shock_config().to_grid()))

    store.get("b0")  # b0 is now most recent
    store.put(build_batch(*generate_book(500, seed=5), batch_id="b2",
                          grid=default_shock_config().to_grid()))

    assert "b0" in store and "b1" not in store


def test_missing_batch_raises():
    with pytest.raises(KeyError):
        BatchStore().get("nope")
    with pytest.raises(KeyError):
        BatchStore().latest()
