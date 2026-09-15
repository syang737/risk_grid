"""Persistence, rollups, and serving a batch from Parquet.

The point of persisting a batch is that reloading it must be indistinguishable
from having built it, so most of these compare the two.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from risk.aggregate import Filter, PivotRequest, aggregate, totals
from risk.batch import (
    DERIVED_COLUMNS,
    build_batch,
    load_batch,
    load_positions,
    write_batch,
)
from risk.scenarios import sigma_grid
from risk.storage import (
    ARTIFACT_VERSION,
    BatchLayout,
    LocalStore,
    Manifest,
    delete_batch,
    list_batches,
    read_manifest,
)
from risk.synthetic import generate_book
from risk.templates import default_shock_config

DIMS = ("sector", "underlying", "contract")
REQUEST = PivotRequest(dimensions=DIMS, detail_dimensions=("account", "desk"))


@pytest.fixture(scope="module")
def built():
    positions, sigma = generate_book(20_000, seed=21)
    return build_batch(
        positions, sigma, grid=sigma_grid(), label="US WBL IntraDay",
        batch_id="b1", firm_id="acme",
    )


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield LocalStore(tmp)


@pytest.fixture
def persisted(store, built):
    write_batch(built, store)
    return store, built


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_loaded_batch_aggregates_identically(persisted):
    store, built = persisted
    loaded = load_batch(store, "acme", "b1")

    assert loaded.positions == built.positions
    assert loaded.display_name == built.display_name
    assert loaded.scenario_labels == built.scenario_labels

    fresh = aggregate(built.frame, REQUEST).rows.sort("sector")
    served = loaded.aggregate(REQUEST).rows.sort("sector")
    shared = [c for c in fresh.columns if c in served.columns]
    assert fresh.select(shared).equals(served.select(shared))


def test_drilling_and_leaf_work_against_parquet(persisted):
    store, built = persisted
    loaded = load_batch(store, "acme", "b1")

    root = loaded.aggregate(REQUEST)
    child = loaded.aggregate(REQUEST.child(root.rows["sector"][0]))
    assert child.group_column == "underlying"
    assert child.total_rows > 0

    leaf_path = REQUEST.child(root.rows["sector"][0]).child(child.rows["underlying"][0])
    contract = loaded.aggregate(leaf_path)
    leaf = loaded.aggregate(leaf_path.child(contract.rows["contract"][0]))
    assert leaf.is_leaf and "position_id" in leaf.rows.columns


def test_totals_match_after_reload(persisted):
    store, built = persisted
    loaded = load_batch(store, "acme", "b1")
    before = totals(built.frame, REQUEST)
    after = loaded.totals(REQUEST)

    assert before["positions"] == after["positions"]
    # The scenario matrix is float32, so summing tens of thousands of values in
    # a different order moves the result by ~1e-7 relative. Tolerance is set to
    # the data's precision, not float64's.
    np.testing.assert_allclose(after["worst"], before["worst"], rtol=1e-6)


def test_filters_survive_the_round_trip(persisted):
    store, built = persisted
    loaded = load_batch(store, "acme", "b1")
    filtered = dataclasses.replace(
        REQUEST, filters=(Filter("instrument_type", "equals", "OPTION"),)
    )
    assert (
        loaded.aggregate(filtered).rows["positions"].sum()
        == aggregate(built.frame, filtered).rows["positions"].sum()
    )


def test_eager_and_lazy_loads_agree(persisted):
    store, _ = persisted
    lazy = load_batch(store, "acme", "b1")
    eager = load_batch(store, "acme", "b1", eager=True)
    assert isinstance(lazy.frame, pl.LazyFrame)
    assert isinstance(eager.frame, pl.DataFrame)
    assert lazy.aggregate(REQUEST).rows.equals(eager.aggregate(REQUEST).rows)


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


def test_positions_artifact_is_the_raw_book(persisted):
    """Cold retention keeps this instead of the full batch, so it must reprice."""
    store, built = persisted
    raw = load_positions(store, "acme", "b1")

    assert raw.height == built.positions
    assert not [c for c in DERIVED_COLUMNS if c in raw.columns]
    assert not [c for c in raw.columns if c.startswith("s") and c[1:].isdigit()]
    # Everything scenario_pnl needs is still present.
    assert {"underlying_price", "strike", "dte", "iv", "qty", "multiplier"} <= set(raw.columns)


def test_positions_artifact_is_much_smaller(persisted):
    store, _ = persisted
    manifest = read_manifest(store, BatchLayout("acme", "b1"))
    assert manifest.sizes["positions"] * 3 < manifest.sizes["full"]


def test_manifest_records_what_built_it(persisted):
    store, built = persisted
    manifest = read_manifest(store, BatchLayout("acme", "b1"))
    assert manifest.version == ARTIFACT_VERSION
    assert manifest.firm_id == "acme" and manifest.batch_id == "b1"
    assert manifest.positions == built.positions
    assert manifest.rollups and "write_frames" in manifest.timings


def test_future_artifact_version_is_refused():
    """A newer layout must fail loudly, not be half-read."""
    raw = Manifest(
        version=ARTIFACT_VERSION + 1, firm_id="acme", batch_id="b1", label="",
        timestamp="2026-01-01T00:00:00+00:00", positions=0, scenario_labels=[],
        shock_config="",
    ).to_json()
    with pytest.raises(ValueError, match="cannot be read"):
        Manifest.from_json(raw)


def test_batches_are_namespaced_by_firm(store, built):
    write_batch(built, store)
    other = dataclasses.replace(built, firm_id="globex", id="b1")
    write_batch(other, store)

    assert list_batches(store, "acme") == ["b1"]
    assert list_batches(store, "globex") == ["b1"]
    # Same batch id, different firms, different keys.
    assert BatchLayout("acme", "b1").full != BatchLayout("globex", "b1").full

    delete_batch(store, BatchLayout("globex", "b1"))
    assert list_batches(store, "globex") == []
    assert list_batches(store, "acme") == ["b1"]


def test_store_refuses_keys_that_escape_its_root(store):
    with pytest.raises(ValueError, match="escapes store root"):
        store.put("../../escape.txt", b"nope")


# --------------------------------------------------------------------------
# Rollups
# --------------------------------------------------------------------------


def test_rollup_matches_the_on_demand_pivot(built):
    """The whole point: a rollup is the same answer, precomputed."""
    batch = dataclasses.replace(built, rollups={}, _aggregates=type(built._aggregates)())
    on_demand = batch.aggregate(REQUEST).rows.sort("sector")

    batch.materialise_rollups()
    from_rollup = batch.aggregate(REQUEST).rows.sort("sector")

    shared = [c for c in on_demand.columns if c in from_rollup.columns]
    assert on_demand.select(shared).equals(from_rollup.select(shared))


def test_rollups_skip_high_cardinality_dimensions(built):
    batch = dataclasses.replace(built, rollups={})
    built_dims = batch.materialise_rollups(max_groups=100)

    assert "sector" in built_dims and "desk" in built_dims
    # Contract is one row per tradeable line; far past any sane threshold.
    assert "contract" not in built_dims and "contract" not in batch.rollups
    assert all(count <= 100 for count in built_dims.values())


def test_rollup_serves_any_template_as_a_column_subset(built):
    """One artifact per dimension, whatever measures a template asks for."""
    batch = dataclasses.replace(built, rollups={})
    batch.materialise_rollups()

    narrow = dataclasses.replace(
        REQUEST, measures=("market_value",), detail_dimensions=("desk",)
    )
    result = batch.aggregate(narrow)
    assert result.detail_dimensions == ("desk",)
    assert "market_value" in result.rows.columns
    assert "account__n" not in result.rows.columns
    assert "delta" not in result.rows.columns


def test_filtered_and_drilled_requests_bypass_rollups(built):
    """A rollup only knows the whole book at one grain."""
    batch = dataclasses.replace(built, rollups={}, _aggregates=type(built._aggregates)())
    batch.materialise_rollups()

    before = batch.cache_stats()["entries"]
    batch.aggregate(REQUEST)                      # rollup hit, no cache entry
    assert batch.cache_stats()["entries"] == before

    batch.aggregate(dataclasses.replace(
        REQUEST, filters=(Filter("instrument_type", "equals", "OPTION"),)))
    assert batch.cache_stats()["entries"] == before + 1


def test_rollups_reload_with_the_batch(persisted):
    store, built = persisted
    loaded = load_batch(store, "acme", "b1")
    manifest = read_manifest(store, BatchLayout("acme", "b1"))
    assert set(loaded.rollups) == set(manifest.rollups)
    assert loaded.cache_stats()["rollups"] == len(manifest.rollups)


def test_writing_without_materialising_stores_no_rollups(store, built):
    batch = dataclasses.replace(built, rollups={})
    manifest = write_batch(batch, store, materialise=False)
    assert manifest.rollups == {}
    assert load_batch(store, "acme", "b1").rollups == {}


def test_write_batch_needs_a_materialized_frame(store, persisted):
    lazy = load_batch(store, "acme", "b1")
    with pytest.raises(TypeError, match="materialized"):
        write_batch(lazy, store)
