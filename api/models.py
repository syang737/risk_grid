"""Request and response models for the grid API.

The grid endpoint speaks AG Grid's server-side row model shape so the browser
datasource stays thin: AG Grid hands us its request almost verbatim and we
translate it into a `PivotRequest`.
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl
from pydantic import BaseModel, Field

from risk.aggregate import DIMENSIONS, Filter

# --------------------------------------------------------------------------
# AG Grid request shapes
# --------------------------------------------------------------------------


class SortItem(BaseModel):
    colId: str
    sort: str = "asc"


class ColumnVO(BaseModel):
    """AG Grid's column value object; only `id` matters to us."""

    id: str
    displayName: str | None = None
    field: str | None = None


class GridRequest(BaseModel):
    """AG Grid SSRM request, plus the fields this product adds."""

    startRow: int = 0
    endRow: int = 100
    rowGroupCols: list[ColumnVO] = Field(default_factory=list)
    groupKeys: list[Any] = Field(default_factory=list)
    filterModel: dict[str, Any] = Field(default_factory=dict)
    sortModel: list[SortItem] = Field(default_factory=list)

    # risk_grid additions
    firm_id: str | None = None
    batch_id: str | None = None
    template: str | None = None
    dimensions: list[str] | None = None
    detail_dimensions: list[str] = Field(default_factory=list)
    include_totals: bool = True

    def dimension_names(self) -> tuple[str, ...]:
        names = self.dimensions if self.dimensions is not None else [c.id for c in self.rowGroupCols]
        unknown = [n for n in names if n not in DIMENSIONS]
        if unknown:
            raise ValueError(f"unknown dimensions: {unknown}")
        return tuple(names)

    def sort_tuples(self) -> tuple[tuple[str, bool], ...]:
        return tuple((s.colId, s.sort.lower() == "desc") for s in self.sortModel)


class GridResponse(BaseModel):
    rows: list[dict[str, Any]]
    lastRow: int
    groupColumn: str | None
    isLeaf: bool
    detailDimensions: list[str]
    totals: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Filter translation
# --------------------------------------------------------------------------

# AG Grid names these differently from our Filter ops in only a couple of
# places; everything else passes through.
_TEXT_ALIASES = {"notEqual": "notEqual", "equals": "equals"}


def _one_condition(column: str, spec: dict) -> list[Filter]:
    kind = spec.get("filterType", "text")

    if kind == "set":
        values = spec.get("values") or []
        # An empty set filter in AG Grid means "nothing selected", which should
        # match nothing rather than being ignored.
        return [Filter(column, "in", tuple(values))]

    op = spec.get("type")
    if op is None:
        return []
    if op in ("blank", "notBlank"):
        return [Filter(column, op)]

    if kind == "number":
        value = spec.get("filter")
        if op == "inRange":
            return [Filter(column, "inRange", value, spec.get("filterTo"))]
        return [Filter(column, op, value)]

    return [Filter(column, op, spec.get("filter"))]


def translate_filters(model: dict[str, Any]) -> tuple[Filter, ...]:
    """AG Grid `filterModel` -> our `Filter` tuple.

    Combined conditions are flattened to AND. OR is not supported yet and is
    rejected loudly rather than silently narrowing a risk view.
    """
    out: list[Filter] = []
    for column, spec in model.items():
        if not isinstance(spec, dict):
            continue
        if "operator" in spec:
            if str(spec["operator"]).upper() != "AND":
                raise ValueError(f"only AND-combined filters are supported (column {column!r})")
            for key in ("condition1", "condition2"):
                if isinstance(spec.get(key), dict):
                    out.extend(_one_condition(column, spec[key]))
            continue
        out.extend(_one_condition(column, spec))
    return tuple(out)


def build_path(
    dimensions: tuple[str, ...], group_keys: list[Any], source: pl.DataFrame | pl.LazyFrame
) -> tuple[tuple[str, Any], ...]:
    """AG Grid `groupKeys` -> a typed pivot path.

    Keys arrive as strings from the browser; numeric dimensions such as strike
    must be cast back or the equality filter silently matches nothing.
    """
    if len(group_keys) > len(dimensions):
        raise ValueError("more group keys than dimensions")

    schema = source.schema if isinstance(source, pl.DataFrame) else source.collect_schema()
    path = []
    for dim, raw in zip(dimensions, group_keys):
        path.append((dim, _coerce(raw, schema.get(dim))))
    return tuple(path)


def _coerce(value: Any, dtype: pl.DataType | None) -> Any:
    if dtype is None or value is None or not isinstance(value, str):
        return value
    if dtype.is_integer():
        return int(value)
    if dtype.is_float():
        return float(value)
    if dtype == pl.Boolean:
        return value.lower() == "true"
    return value


# --------------------------------------------------------------------------
# JSON sanitising
# --------------------------------------------------------------------------


def jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace non-finite floats with null.

    NaN and infinity are not valid JSON, and an expired deep-OTM option can
    produce either. Emitting them makes the browser's parser throw on a payload
    that is otherwise fine.
    """
    for row in rows:
        for key, value in row.items():
            if isinstance(value, float) and not math.isfinite(value):
                row[key] = None
    return rows
