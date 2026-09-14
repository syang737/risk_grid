"""Pivot correctness.

The performance story is worthless if the aggregation is wrong, and a
groupby-sum over a precomputed matrix is exactly the kind of thing that looks
right while quietly dropping or double-counting rows.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pyarrow.ipc as ipc
import pytest

from risk.aggregate import attach_scenarios, children, pivot, scenario_columns, to_arrow_ipc
from risk.engine import base_valuation, scenario_pnl
from risk.scenarios import pct_grid, sigma_grid
from risk.synthetic import generate_book


@pytest.fixture(scope="module")
def book():
    df, sigma = generate_book(20_000, seed=42)
    df = base_valuation(df)
    grid = sigma_grid()
    pnl = scenario_pnl(df, grid, sigma_daily=sigma, dtype=np.float64)
    return attach_scenarios(df, pnl, grid.labels), pnl, grid


def test_pivot_totals_match_raw_matrix(book):
    df, pnl, grid = book
    agg = pivot(df, ["desk"])
    scen = scenario_columns(df)

    assert agg["positions"].sum() == len(df)
    # Summing the pivot back up must reproduce the raw column totals exactly.
    for i, col in enumerate(scen):
        np.testing.assert_allclose(agg[col].sum(), pnl[:, i].sum(), rtol=1e-9)


def test_pivot_partitions_the_book(book):
    """Every position lands in exactly one group, at every grain."""
    df, _, _ = book
    for keys in (["desk"], ["underlying"], ["desk", "account"], ["instrument_type", "expiry"]):
        assert pivot(df, keys)["positions"].sum() == len(df)


def test_filtered_pivot_matches_manual_filter(book):
    df, _, _ = book
    desk = df["desk"][0]
    agg = pivot(df, ["account"], filters={"desk": desk})
    manual = df.filter(pl.col("desk") == desk)

    assert agg["positions"].sum() == len(manual)
    np.testing.assert_allclose(agg["delta"].sum(), manual["delta"].sum(), rtol=1e-9)


def test_children_descends_one_level(book):
    df, _, _ = book
    top = pivot(df, ["desk"]).sort("positions", descending=True)["desk"][0]
    kids = children(df, {"desk": top}, ("desk", "account"))

    assert "account" in kids.columns
    assert kids["positions"].sum() == len(df.filter(pl.col("desk") == top))
    assert len(kids) < len(df)


def test_children_rejects_descent_past_leaf(book):
    df, _, _ = book
    with pytest.raises(ValueError):
        children(df, {"desk": "DESK00", "account": "ACCT00001"}, ("desk", "account"))


def test_worst_case_is_the_min_across_scenarios(book):
    df, _, _ = book
    agg = pivot(df, ["desk"])
    scen = scenario_columns(df)
    expected = agg.select(scen).to_numpy().min(axis=1)
    np.testing.assert_allclose(agg["worst"].to_numpy(), expected, rtol=1e-9)

    # worst_idx must actually point at the worst scenario.
    rows = np.arange(len(agg))
    picked = agg.select(scen).to_numpy()[rows, agg["worst_idx"].to_numpy()]
    np.testing.assert_allclose(picked, expected, rtol=1e-9)


def test_arrow_roundtrip(book):
    df, _, _ = book
    node = pivot(df, ["desk"])
    payload = to_arrow_ipc(node)

    table = ipc.open_stream(payload).read_all()
    assert table.num_rows == len(node)
    assert set(table.column_names) == set(node.columns)
    np.testing.assert_allclose(table.column("worst").to_numpy(), node["worst"].to_numpy())


def test_equity_rows_have_unit_delta_and_no_option_greeks():
    df, _ = generate_book(5_000, seed=3)
    df = base_valuation(df)
    eq = df.filter(~pl.col("is_option"))
    np.testing.assert_allclose(eq["delta"].to_numpy(), eq["qty"].to_numpy())
    for g in ("gamma", "vega", "theta", "rho"):
        np.testing.assert_allclose(eq[g].to_numpy(), 0.0)


def test_flat_pct_grid_shocks_every_underlying_identically():
    df, _ = generate_book(2_000, seed=5)
    df = base_valuation(df)
    grid = pct_grid(moves=np.array([-0.10, 0.10]), vol_shocks=np.array([0.0]))
    pnl = scenario_pnl(df, grid, dtype=np.float64)

    eq = (~df["is_option"].to_numpy())
    spot = df["underlying_price"].to_numpy()[eq]
    qty = df["qty"].to_numpy()[eq]
    np.testing.assert_allclose(pnl[eq, 0], -0.10 * spot * qty, rtol=1e-6)
    np.testing.assert_allclose(pnl[eq, 1], 0.10 * spot * qty, rtol=1e-6)
