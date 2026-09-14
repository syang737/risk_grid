"""Server-side pivot over the scenario matrix.

The client never receives positions. It receives the children of whichever tree
node the user expanded -- typically tens or hundreds of rows -- serialized as
Arrow IPC. Expanding a node is one filter plus one groupby on a single column,
so cost scales with the data under that node, not with the book.
"""

from __future__ import annotations

import io

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.ipc as ipc

# The dimensions a user can pivot on, in the order a risk screen usually nests
# them. Account level and instrument level are the two main axes.
ACCOUNT_LEVELS = ("firm", "desk", "account")
INSTRUMENT_LEVELS = ("instrument_type", "underlying", "expiry", "strike")

GREEK_COLUMNS = ("market_value", "delta", "gamma", "vega", "theta", "rho")


def attach_scenarios(
    positions: pl.DataFrame, pnl: np.ndarray, labels: tuple[str, ...]
) -> pl.DataFrame:
    """Widen the position frame with one column per scenario."""
    if pnl.shape != (len(positions), len(labels)):
        raise ValueError(f"pnl shape {pnl.shape} does not match {(len(positions), len(labels))}")
    return positions.with_columns(
        [pl.Series(f"s{i:03d}", pnl[:, i]) for i in range(len(labels))]
    )


def scenario_columns(df: pl.DataFrame) -> list[str]:
    return [c for c in df.columns if c.startswith("s") and c[1:].isdigit()]


def pivot(
    df: pl.DataFrame,
    group_by: list[str],
    filters: dict[str, object] | None = None,
    measures: tuple[str, ...] = GREEK_COLUMNS,
) -> pl.DataFrame:
    """Aggregate to `group_by`, optionally restricted to a subtree.

    Scenario columns are summed; `worst` and `worst_scenario` fold across them,
    which is the number a stress screen actually leads with.
    """
    lf = df.lazy()
    for col, val in (filters or {}).items():
        lf = lf.filter(pl.col(col) == val)

    scen = scenario_columns(df)
    present = [m for m in measures if m in df.columns]

    agg = (
        [pl.len().alias("positions")]
        + [pl.col(m).sum().alias(m) for m in present]
        + [pl.col(c).sum().alias(c) for c in scen]
    )
    out = lf.group_by(group_by).agg(agg).sort(group_by).collect()

    if scen:
        out = out.with_columns(
            pl.min_horizontal(scen).alias("worst"),
            pl.concat_list(scen)
            .list.arg_min()
            .alias("worst_idx"),
        )
    return out


def children(
    df: pl.DataFrame,
    path: dict[str, object],
    levels: tuple[str, ...],
    measures: tuple[str, ...] = GREEK_COLUMNS,
) -> pl.DataFrame:
    """Children of the node identified by `path`, one level deeper.

    `path` maps already-expanded levels to their values; the next level in
    `levels` becomes the grouping column.
    """
    depth = len(path)
    if depth >= len(levels):
        raise ValueError("already at leaf level")
    return pivot(df, [levels[depth]], filters=path, measures=measures)


def to_arrow_ipc(df: pl.DataFrame) -> bytes:
    """Serialize a node page for the wire."""
    table = df.to_arrow()
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()
