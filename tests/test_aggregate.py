"""Pivot correctness.

The performance story is worthless if the aggregation is wrong, and a
groupby-sum over a precomputed matrix is exactly the kind of thing that looks
right while quietly dropping or double-counting rows.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import polars as pl
import pyarrow.ipc as ipc
import pytest

from risk.aggregate import (
    Filter,
    PivotRequest,
    aggregate,
    attach_scenarios,
    detail_count_column,
    pivot,
    scenario_columns,
    to_arrow_ipc,
    totals,
)
from risk.engine import base_valuation, scenario_pnl
from risk.scenarios import pct_grid, sigma_grid
from risk.synthetic import generate_book

DIMS = ("sector", "underlying", "contract", "account")


@pytest.fixture(scope="module")
def book():
    df, sigma = generate_book(20_000, seed=42)
    df = base_valuation(df)
    grid = sigma_grid()
    pnl = scenario_pnl(df, grid, sigma_daily=sigma, dtype=np.float64)
    return attach_scenarios(df, pnl, grid.labels), pnl, grid


@pytest.fixture(scope="module")
def req():
    return PivotRequest(dimensions=DIMS, detail_dimensions=("account", "sector", "underlying"))


# --------------------------------------------------------------------------
# Totals and partitioning
# --------------------------------------------------------------------------


def test_pivot_totals_match_raw_matrix(book):
    df, pnl, _ = book
    agg = pivot(df, ["sector"])
    assert agg["positions"].sum() == len(df)
    for i, col in enumerate(scenario_columns(df)):
        np.testing.assert_allclose(agg[col].sum(), pnl[:, i].sum(), rtol=1e-9)


@pytest.mark.parametrize("dims", [
    ("sector",), ("account",), ("desk", "account"),
    ("underlying", "account"), ("account", "underlying"),
    ("instrument_type", "expiry"), ("contract",),
])
def test_any_dimension_order_partitions_the_book(book, dims):
    """Every position lands in exactly one group, whatever the order."""
    df, _, _ = book
    result = aggregate(df, PivotRequest(dimensions=dims))
    assert result.rows["positions"].sum() == len(df)
    assert result.group_column == dims[0]


def test_drill_path_narrows_to_the_same_rows(book, req):
    """Descending a level equals filtering the frame by hand."""
    df, _, _ = book
    root = aggregate(df, req)
    sector = root.rows["sector"][0]

    child = aggregate(df, req.child(sector))
    manual = df.filter(pl.col("sector") == sector)

    assert child.group_column == "underlying"
    assert child.rows["positions"].sum() == len(manual)
    np.testing.assert_allclose(child.rows["delta"].sum(), manual["delta"].sum(), rtol=1e-9)


def test_leaf_level_returns_positions(book, req):
    df, _, _ = book
    path = req
    for dim in DIMS:
        result = aggregate(df, path)
        assert not result.is_leaf
        path = path.child(result.rows[dim][0])

    leaf = aggregate(df, path)
    assert leaf.is_leaf and leaf.group_column is None
    assert "position_id" in leaf.rows.columns
    assert leaf.total_rows >= 1


def test_cannot_descend_past_leaf(req):
    path = req
    for _ in DIMS:
        path = path.child("x")
    with pytest.raises(ValueError):
        path.child("y")


# --------------------------------------------------------------------------
# Distinct-value cells (the [21] fix)
# --------------------------------------------------------------------------


def test_detail_cell_shows_value_only_when_unique(book, req):
    df, _, _ = book
    result = aggregate(df, req)
    count_col = detail_count_column("account")

    assert count_col in result.rows.columns
    for n, value in zip(result.rows[count_col], result.rows["account"]):
        # Exactly one account -> show it; more than one -> null, so the client
        # renders a chip instead of a meaningless number.
        assert (value is not None) == (n == 1)


def test_detail_counts_match_manual_n_unique(book, req):
    df, _, _ = book
    result = aggregate(df, req)
    for sector, n in zip(result.rows["sector"], result.rows[detail_count_column("account")]):
        expected = df.filter(pl.col("sector") == sector)["account"].n_unique()
        assert n == expected


def test_pinned_dimensions_get_no_detail_columns(book, req):
    """A dimension fixed by the path is constant, so counting it is wasted work."""
    df, _, _ = book
    root = aggregate(df, req)
    child = aggregate(df, req.child(root.rows["sector"][0]))
    assert "sector" not in child.detail_dimensions
    assert detail_count_column("sector") not in child.rows.columns


def test_chip_drill_equals_direct_filter(book, req):
    """Clicking an account chip must land on the accounts actually holding it."""
    df, _, _ = book
    by_underlying = PivotRequest(dimensions=("underlying", "account"),
                                 detail_dimensions=("account",))
    root = aggregate(df, by_underlying)
    und = root.rows["underlying"][0]
    expected_n = root.rows[detail_count_column("account")][0]

    drilled = aggregate(df, by_underlying.child(und))
    assert drilled.group_column == "account"
    assert drilled.total_rows == expected_n
    assert set(drilled.rows["account"]) == set(
        df.filter(pl.col("underlying") == und)["account"].unique()
    )


# --------------------------------------------------------------------------
# Worst-of-sum
# --------------------------------------------------------------------------


def test_worst_is_min_across_summed_scenarios(book, req):
    df, _, _ = book
    result = aggregate(df, req)
    scen = scenario_columns(df)
    expected = result.rows.select(scen).to_numpy().min(axis=1)
    np.testing.assert_allclose(result.rows["worst"].to_numpy(), expected, rtol=1e-9)


def test_totals_are_worst_of_sum_not_sum_of_worsts(book, req):
    """The distinction that keeps Max Risk from being wildly overstated."""
    df, _, _ = book
    result = aggregate(df, req)
    t = totals(df, req)

    scen = scenario_columns(df)
    np.testing.assert_allclose(t["worst"], min(t[c] for c in scen), rtol=1e-9)

    sum_of_worsts = result.rows["worst"].sum()
    # Groups bottom out in different scenarios, so the portfolio's worst single
    # scenario is strictly better than adding up each group's own worst.
    assert t["worst"] > sum_of_worsts


def test_totals_describe_exactly_the_rows_they_sit_above(book, req):
    """Same population, not merely a similar one.

    Position count is exact because it is an integer; the sums are compared to
    tolerance because Polars reduces a multi-column select differently from a
    single Series, which moves the last bit. What must never differ is *which*
    groups are counted -- a float-driven disagreement there once put a whole
    sector in the totals and not in the rows.
    """
    df, _, _ = book
    for filters in ((), (Filter("instrument_type", "equals", "OPTION"),)):
        request = dataclasses.replace(req, filters=filters)
        root = aggregate(df, request)
        t = totals(df, request)
        assert t["positions"] == root.rows["positions"].sum()
        for col in scenario_columns(df):
            np.testing.assert_allclose(t[col], root.rows[col].sum(), rtol=1e-12)


def test_totals_match_ungrouped_aggregation(book, req):
    df, _, _ = book
    t = totals(df, req)
    assert t["positions"] == len(df)
    np.testing.assert_allclose(t["delta"], df["delta"].sum(), rtol=1e-9)


def test_worst_scenario_index_points_at_the_worst(book):
    df, _, _ = book
    agg = pivot(df, ["sector"])
    scen = scenario_columns(df)
    values = agg.select(scen).to_numpy()
    picked = values[np.arange(len(agg)), agg["worst_idx"].to_numpy()]
    np.testing.assert_allclose(picked, values.min(axis=1), rtol=1e-9)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def test_dimension_filter_narrows_the_book(book, req):
    df, _, _ = book
    filtered = dataclasses.replace(req, filters=(Filter("instrument_type", "equals", "OPTION"),))
    result = aggregate(df, filtered)
    assert result.rows["positions"].sum() == df.filter(pl.col("is_option")).height


def test_measure_filter_applies_after_aggregation(book, req):
    """`delta > x` means the group's delta, not each position's."""
    df, _, _ = book
    unfiltered = aggregate(df, req)
    threshold = float(unfiltered.rows["delta"].median())

    filtered = aggregate(df, dataclasses.replace(
        req, filters=(Filter("delta", "greaterThan", threshold),)))

    expected = unfiltered.rows.filter(pl.col("delta") > threshold)
    assert filtered.total_rows == expected.height
    np.testing.assert_allclose(
        sorted(filtered.rows["delta"].to_list()), sorted(expected["delta"].to_list()), rtol=1e-9
    )


def test_worst_filter_applies_after_aggregation(book, req):
    df, _, _ = book
    unfiltered = aggregate(df, req)
    threshold = float(unfiltered.rows["worst"].median())
    filtered = aggregate(df, dataclasses.replace(
        req, filters=(Filter("worst", "lessThan", threshold),)))
    assert filtered.total_rows == unfiltered.rows.filter(pl.col("worst") < threshold).height


def test_totals_reflect_post_aggregation_filters(book, req):
    """A total above a filtered list must describe that list."""
    df, _, _ = book
    # Midway between two adjacent values, never equal to one: float summation is
    # not associative, so a threshold sitting exactly on a group's value is a
    # coin toss rather than a test.
    ranked = sorted(aggregate(df, req).rows["worst"].to_list())
    mid = len(ranked) // 2
    threshold = (ranked[mid - 1] + ranked[mid]) / 2
    filtered = dataclasses.replace(req, filters=(Filter("worst", "lessThan", threshold),))

    t = totals(df, filtered)
    result = aggregate(df, filtered)
    assert t["positions"] == result.rows["positions"].sum()
    assert t["positions"] < len(df)


def test_text_and_range_filters(book, req):
    df, _, _ = book
    contains = aggregate(df, dataclasses.replace(
        req, filters=(Filter("sector", "contains", "Real"),)))
    assert set(contains.rows["sector"]) == {s for s in df["sector"].unique() if "Real" in s}

    in_range = aggregate(df, dataclasses.replace(
        req, dimensions=("account",), filters=(Filter("strike", "inRange", 10.0, 50.0),)))
    manual = df.filter((pl.col("strike") >= 10.0) & (pl.col("strike") <= 50.0))
    assert in_range.rows["positions"].sum() == manual.height


# --------------------------------------------------------------------------
# Sorting, paging, wire format
# --------------------------------------------------------------------------


def test_sort_and_page_do_not_change_the_aggregate(book, req):
    df, _, _ = book
    full = aggregate(df, req)
    ranked = aggregate(df, dataclasses.replace(req, sort=(("worst", False),)))

    assert ranked.total_rows == full.total_rows
    assert ranked.rows["worst"].to_list() == sorted(full.rows["worst"].to_list())

    page = aggregate(df, dataclasses.replace(req, sort=(("worst", False),), offset=2, limit=3))
    assert page.rows["sector"].to_list() == ranked.rows["sector"].to_list()[2:5]
    assert page.total_rows == full.total_rows  # total is the unpaged count


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

    eq = ~df["is_option"].to_numpy()
    spot = df["underlying_price"].to_numpy()[eq]
    qty = df["qty"].to_numpy()[eq]
    np.testing.assert_allclose(pnl[eq, 0], -0.10 * spot * qty, rtol=1e-6)
    np.testing.assert_allclose(pnl[eq, 1], 0.10 * spot * qty, rtol=1e-6)


def test_arrow_roundtrip(book):
    df, _, _ = book
    node = pivot(df, ["sector"])
    table = ipc.open_stream(to_arrow_ipc(node)).read_all()
    assert table.num_rows == len(node)
    assert set(table.column_names) == set(node.columns)
