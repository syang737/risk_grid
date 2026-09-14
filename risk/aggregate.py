"""Server-side pivot over the scenario matrix.

One composable request replaces what the incumbent exposes as four fixed
toolbar tabs. A pivot is an ordered list of dimensions, a path of already
expanded ancestors, filters, and a set of measures; expanding a node means
grouping by the next dimension with the path applied as filters.

The client never receives positions. It receives the children of whichever node
was expanded -- typically tens or hundreds of rows -- so cost scales with the
data under that node, not with the book.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field, replace

import polars as pl
import pyarrow.ipc as ipc

# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Dimension:
    """A column a user can group or drill on."""

    name: str
    label: str
    # Account-side and instrument-side dimensions are both drillable and freely
    # interleavable; the split is only used to offer sensible default orders.
    axis: str = "instrument"


DIMENSIONS: dict[str, Dimension] = {
    d.name: d
    for d in (
        Dimension("firm", "Firm", "account"),
        Dimension("desk", "Desk", "account"),
        Dimension("master_account", "Master Account", "account"),
        Dimension("account", "Account", "account"),
        Dimension("sector", "Product", "instrument"),
        Dimension("underlying", "Instrument", "instrument"),
        Dimension("instrument_type", "Type", "instrument"),
        Dimension("expiry", "Expiry", "instrument"),
        Dimension("strike", "Strike", "instrument"),
        Dimension("right", "Right", "instrument"),
        Dimension("contract", "Contract", "instrument"),
    )
}

# Defaults matching the incumbent's two main tabs, as starting points only --
# the user composes any order they like.
ACCOUNT_DRILL = ("desk", "master_account", "account", "underlying", "contract")
INSTRUMENT_DRILL = ("sector", "underlying", "contract", "account")

GREEK_COLUMNS = ("market_value", "und_qty", "delta", "gamma", "vega", "theta", "rho")


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

TEXT_OPS = {"equals", "notEqual", "contains", "notContains", "startsWith", "endsWith", "blank", "notBlank"}
NUMBER_OPS = {"equals", "notEqual", "greaterThan", "greaterThanOrEqual",
              "lessThan", "lessThanOrEqual", "inRange"}


@dataclass(frozen=True)
class Filter:
    """One column predicate. Hashable so requests can be cache keys."""

    column: str
    op: str
    value: object = None
    value2: object = None

    def expr(self) -> pl.Expr:
        col = pl.col(self.column)
        op, v, v2 = self.op, self.value, self.value2

        if op == "in":
            return col.is_in(list(v))
        if op == "equals":
            return col == v
        if op == "notEqual":
            return col != v
        if op == "contains":
            return col.cast(pl.Utf8).str.contains(str(v), literal=True)
        if op == "notContains":
            return ~col.cast(pl.Utf8).str.contains(str(v), literal=True)
        if op == "startsWith":
            return col.cast(pl.Utf8).str.starts_with(str(v))
        if op == "endsWith":
            return col.cast(pl.Utf8).str.ends_with(str(v))
        if op == "blank":
            return col.is_null()
        if op == "notBlank":
            return col.is_not_null()
        if op == "greaterThan":
            return col > v
        if op == "greaterThanOrEqual":
            return col >= v
        if op == "lessThan":
            return col < v
        if op == "lessThanOrEqual":
            return col <= v
        if op == "inRange":
            return (col >= v) & (col <= v2)
        raise ValueError(f"unsupported filter op: {op}")


# --------------------------------------------------------------------------
# Requests and results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PivotRequest:
    """Everything needed to produce one block of grid rows.

    Frozen and built from tuples so it can be used directly as a cache key.
    """

    dimensions: tuple[str, ...]
    path: tuple[tuple[str, object], ...] = ()
    filters: tuple[Filter, ...] = ()
    measures: tuple[str, ...] = GREEK_COLUMNS
    # Dimensions to render as value-or-count cells. Each costs ~15-20ms per
    # million rows, so pass only what is actually displayed.
    detail_dimensions: tuple[str, ...] = ()
    sort: tuple[tuple[str, bool], ...] = ()  # (column, descending)
    offset: int = 0
    limit: int | None = None
    include_worst_scenario: bool = False

    @property
    def depth(self) -> int:
        return len(self.path)

    @property
    def is_leaf(self) -> bool:
        """True when every dimension has been expanded and only positions remain."""
        return self.depth >= len(self.dimensions)

    @property
    def group_column(self) -> str | None:
        return None if self.is_leaf else self.dimensions[self.depth]

    def cache_key(self) -> tuple:
        """Identity of the *aggregate*, ignoring presentation-only fields.

        Sorting and paging are applied to the cached frame, so two requests
        differing only in sort or offset share one aggregation.

        Only dimensions up to the current depth are included: deeper ones do not
        affect this level's rows. That makes the totals request (which truncates
        to the root dimension) hash identical to the root display request, so
        both read one aggregate.

        That sharing is correctness, not just speed. Float summation is not
        associative, so recomputing the same aggregate by a different route can
        land a group on the other side of a post-aggregation filter -- which
        once put an entire sector in the totals but not in the rows.
        """
        return (
            self.dimensions[: self.depth + 1], self.path, self.filters,
            self.measures, self.detail_dimensions, self.include_worst_scenario,
        )

    def child(self, value: object) -> PivotRequest:
        """The request for the children of `value` at the current level."""
        if self.is_leaf:
            raise ValueError("cannot descend below the leaf level")
        return replace(
            self,
            path=self.path + ((self.group_column, value),),
            offset=0,
        )


@dataclass
class PivotResult:
    rows: pl.DataFrame
    total_rows: int
    group_column: str | None
    is_leaf: bool
    detail_dimensions: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Scenario helpers
# --------------------------------------------------------------------------


def attach_scenarios(positions: pl.DataFrame, pnl, labels: tuple[str, ...]) -> pl.DataFrame:
    """Widen the position frame with one column per scenario."""
    if pnl.shape != (len(positions), len(labels)):
        raise ValueError(f"pnl shape {pnl.shape} does not match {(len(positions), len(labels))}")
    return positions.with_columns(
        [pl.Series(f"s{i:03d}", pnl[:, i]) for i in range(len(labels))]
    )


def scenario_columns(df: pl.DataFrame) -> list[str]:
    return [c for c in df.columns if c.startswith("s") and c[1:].isdigit()]


def detail_count_column(dim: str) -> str:
    """Companion column holding the distinct count behind a value-or-count cell."""
    return f"{dim}__n"


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


# Columns that only exist once rows have been aggregated.
AGGREGATE_ONLY = frozenset({"positions", "worst", "worst_idx"})


def _split_filters(
    request: PivotRequest, scen: list[str]
) -> tuple[list[Filter], list[Filter]]:
    """Separate filters that narrow the book from filters that narrow the rows.

    A filter on a dimension or a raw position attribute (iv, dte) selects
    positions and must run before the groupby. A filter on a measure, a
    scenario or Max Risk refers to the *aggregate* a user is looking at, so it
    must run after: applying `delta > 1000` to individual positions would drop
    rows and silently change every remaining group's sum, which is not what was
    asked for.
    """
    pre, post = [], []
    for f in request.filters:
        target = post if (f.column in AGGREGATE_ONLY or f.column in request.measures or f.column in scen) else pre
        target.append(f)
    return pre, post


def _filter_exprs(request: PivotRequest, pre: list[Filter]) -> list[pl.Expr]:
    return [pl.col(dim) == value for dim, value in request.path] + [f.expr() for f in pre]


def _apply_sort(df: pl.DataFrame, sort: tuple[tuple[str, bool], ...], default: str | None) -> pl.DataFrame:
    usable = [(c, d) for c, d in sort if c in df.columns]
    if usable:
        return df.sort([c for c, _ in usable], descending=[d for _, d in usable])
    if default and default in df.columns:
        return df.sort(default)
    return df


def aggregate(df: pl.DataFrame, request: PivotRequest) -> PivotResult:
    """One level of the pivot tree, sorted and paged."""
    return present(aggregate_frame(df, request), request)


def present(result: PivotResult, request: PivotRequest) -> PivotResult:
    """Apply sort and paging to a (possibly cached) aggregate.

    Split from `aggregate_frame` because this half is microseconds while that
    half is the expensive groupby -- so re-sorting or paging a result already
    computed costs nothing. See `risk/batch.py`.
    """
    frame = _apply_sort(result.rows, request.sort, result.group_column or "position_id")
    return replace(result, rows=_slice(frame, request))


def aggregate_frame(df: pl.DataFrame, request: PivotRequest) -> PivotResult:
    """Produce one level of the pivot tree, unsorted and unpaged.

    At the leaf level (every dimension expanded) the raw positions under the
    path are returned instead of an aggregate.
    """
    scen = scenario_columns(df)
    pre, post = _split_filters(request, scen)

    lf = df.lazy()
    for e in _filter_exprs(request, pre):
        lf = lf.filter(e)

    if request.is_leaf:
        return _leaf(lf, request, scen, post)

    group_col = request.group_column
    measures = [m for m in request.measures if m in df.columns]

    # Dimensions already pinned by the path are constant within every group, so
    # computing distinct counts for them would be wasted work.
    pinned = {dim for dim, _ in request.path} | {group_col}
    details = [d for d in request.detail_dimensions if d in df.columns and d not in pinned]

    agg = (
        [pl.len().alias("positions")]
        + [pl.col(m).sum().alias(m) for m in measures]
        + [pl.col(c).sum().alias(c) for c in scen]
    )
    for d in details:
        agg.append(pl.col(d).n_unique().alias(detail_count_column(d)))
        agg.append(pl.col(d).first().alias(d))

    out = lf.group_by(group_col).agg(agg)

    if scen:
        # Worst case at any grouping level is worst-of-sum, never sum-of-worsts:
        # the latter assumes every position hits its worst simultaneously.
        out = out.with_columns(pl.min_horizontal(scen).alias("worst"))
        if request.include_worst_scenario:
            out = out.with_columns(pl.concat_list(scen).list.arg_min().alias("worst_idx"))

    # A detail cell shows its value only when the group holds exactly one.
    for d in details:
        out = out.with_columns(
            pl.when(pl.col(detail_count_column(d)) == 1).then(pl.col(d)).otherwise(None).alias(d)
        )

    for f in post:
        out = out.filter(f.expr())

    frame = out.collect()
    return PivotResult(
        rows=frame,
        total_rows=frame.height,
        group_column=group_col,
        is_leaf=False,
        detail_dimensions=tuple(details),
    )


def _leaf(
    lf: pl.LazyFrame, request: PivotRequest, scen: list[str], post: list[Filter]
) -> PivotResult:
    """Raw positions under a fully expanded path."""
    schema = lf.collect_schema().names()
    keep = [c for c in ("position_id", *DIMENSIONS, *request.measures) if c in schema]
    keep += [c for c in scen if c not in keep]

    lf = lf.select(keep)
    if scen:
        lf = lf.with_columns(pl.min_horizontal(scen).alias("worst"))
    # At the leaf there is no aggregation, but Max Risk is still derived, so
    # these filters can only be applied once it exists.
    for f in post:
        lf = lf.filter(f.expr())

    frame = lf.collect()
    return PivotResult(rows=frame, total_rows=frame.height, group_column=None, is_leaf=True)


def _slice(frame: pl.DataFrame, request: PivotRequest) -> pl.DataFrame:
    if request.offset or request.limit is not None:
        return frame.slice(request.offset, request.limit)
    return frame


def totals_request(request: PivotRequest) -> PivotRequest:
    """The root-level request whose rows the totals summarise.

    Drilling expands in place, so the root rows stay on screen and the grand
    total is always the total of the root level -- not of the subtree the user
    happens to have open.
    """
    # detail_dimensions is preserved deliberately: it is part of the cache key,
    # and keeping it makes this request hash identical to the root display
    # request so both read the same aggregate.
    return replace(request, path=(), dimensions=request.dimensions[:1],
                   sort=(), offset=0, limit=None)


def totals(df: pl.DataFrame, request: PivotRequest) -> dict:
    """Grand total row over the rows the grid is showing.

    Built from the root-level aggregate so that post-aggregation filters (Max
    Risk below a threshold, say) are reflected: a total that ignored them would
    sit above a filtered list describing a different population.

    Max Risk is recomputed as the minimum across the *summed* scenarios. Summing
    each group's own worst would assume every group blows up in the same
    scenario at once, which is both wrong and alarming.
    """
    return totals_from(
        aggregate_frame(df, totals_request(request)).rows, request, scenario_columns(df)
    )


def totals_from(root: pl.DataFrame, request: PivotRequest, scen: list[str]) -> dict:
    """Fold a root-level aggregate into the grand total row.

    Separate from `totals()` so a caller holding a cached root aggregate (see
    `risk/batch.py`) can reuse it rather than recompute.
    """
    measures = [m for m in request.measures if m in root.columns]
    if not root.height:
        out = {"positions": 0, **{m: 0.0 for m in measures}, **{c: 0.0 for c in scen}}
    else:
        out = root.select(
            [pl.col("positions").sum().alias("positions")]
            + [pl.col(m).sum().alias(m) for m in measures]
            + [pl.col(c).sum().alias(c) for c in scen]
        ).to_dicts()[0]

    if scen:
        out["worst"] = min(out[c] for c in scen)
    return out


# --------------------------------------------------------------------------
# Convenience + wire format
# --------------------------------------------------------------------------


def pivot(
    df: pl.DataFrame,
    group_by: list[str],
    filters: dict[str, object] | None = None,
    measures: tuple[str, ...] = GREEK_COLUMNS,
) -> pl.DataFrame:
    """Aggregate to `group_by`, optionally restricted to a subtree.

    Low-level helper kept for spikes and tests; the grid goes through
    `aggregate()`.
    """
    lf = df.lazy()
    for col, val in (filters or {}).items():
        lf = lf.filter(pl.col(col) == val)

    scen = scenario_columns(df)
    present = [m for m in measures if m in df.columns]

    out = lf.group_by(group_by).agg(
        [pl.len().alias("positions")]
        + [pl.col(m).sum().alias(m) for m in present]
        + [pl.col(c).sum().alias(c) for c in scen]
    ).sort(group_by).collect()

    if scen:
        out = out.with_columns(
            pl.min_horizontal(scen).alias("worst"),
            pl.concat_list(scen).list.arg_min().alias("worst_idx"),
        )
    return out


def to_arrow_ipc(df: pl.DataFrame) -> bytes:
    """Serialize a frame for bulk transfer (export), not for grid blocks.

    Grid blocks are ~100 rows, where JSON is a few tens of KB and needs no Arrow
    dependency in the browser. Arrow earns its keep on full-book export.
    """
    table = df.to_arrow()
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()
