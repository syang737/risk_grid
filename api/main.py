"""FastAPI service behind the grid.

One endpoint does the work: `POST /api/grid/rows` takes an AG Grid server-side
row model request and returns one block of pivot rows. Everything else is
metadata -- batches, dimensions, templates, configs.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from risk.aggregate import DIMENSIONS, PivotRequest
from risk.batch import Batch, BatchStore, build_batch
from risk.synthetic import generate_book
from risk.templates import ColumnTemplate, ShockConfig, TemplateStore

from .models import GridRequest, GridResponse, build_path, jsonable, translate_filters

DEFAULT_POSITIONS = int(os.environ.get("RISK_GRID_POSITIONS", 250_000))
TEMPLATE_ROOT = Path(os.environ.get("RISK_GRID_TEMPLATES", Path.home() / ".risk_grid"))

batches = BatchStore()
templates = TemplateStore(TEMPLATE_ROOT)


def _seed_batch(n: int = DEFAULT_POSITIONS, config_name: str = "Exposure") -> Batch:
    """Build a synthetic batch so the app is usable with no data feed attached."""
    config = templates.get_config(config_name)
    positions, sigma = generate_book(n, seed=7)
    batch = build_batch(
        positions, sigma, grid=config.to_grid(),
        label="US WBL IntraDay", shock_config=config.name,
    )
    batch.warm()
    return batches.put(batch)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _seed_batch()
    yield


app = FastAPI(title="risk_grid", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _batch(batch_id: str | None) -> Batch:
    try:
        return batches.get(batch_id) if batch_id else batches.latest()
    except KeyError:
        raise HTTPException(404, f"no such batch: {batch_id}")


def _template(name: str | None) -> ColumnTemplate:
    try:
        return templates.get_template(name) if name else templates.list_templates()[0]
    except (KeyError, IndexError):
        raise HTTPException(404, f"no such template: {name}")


# --------------------------------------------------------------------------
# Grid
# --------------------------------------------------------------------------


@app.post("/api/grid/rows", response_model=GridResponse)
def grid_rows(request: GridRequest) -> GridResponse:
    batch = _batch(request.batch_id)
    template = _template(request.template)

    try:
        dimensions = request.dimension_names()
        path = build_path(dimensions, request.groupKeys, batch.frame)
        filters = translate_filters(request.filterModel)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if not dimensions:
        raise HTTPException(400, "at least one dimension is required")

    limit = max(request.endRow - request.startRow, 0) or None
    pivot = PivotRequest(
        dimensions=dimensions,
        path=path,
        filters=filters,
        measures=template.measures(),
        detail_dimensions=tuple(request.detail_dimensions),
        sort=request.sort_tuples(),
        offset=request.startRow,
        limit=limit,
    )

    try:
        result = batch.aggregate(pivot)
        totals = batch.totals(pivot) if request.include_totals else None
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))

    return GridResponse(
        rows=jsonable(result.rows.to_dicts()),
        lastRow=result.total_rows,
        groupColumn=result.group_column,
        isLeaf=result.is_leaf,
        detailDimensions=list(result.detail_dimensions),
        totals=jsonable([totals])[0] if totals else None,
    )


@app.get("/api/grid/columns")
def grid_columns(batch_id: str | None = None, template: str | None = None) -> dict:
    """Resolve a column template against a batch's scenario grid."""
    batch = _batch(batch_id)
    tpl = _template(template)
    config = templates.get_config(batch.shock_config)
    resolved = tpl.resolve(config.to_grid())
    return {
        "template": tpl.name,
        "config": config.name,
        "columns": [
            {"field": c.field, "label": c.label, "format": c.format,
             "width": c.width, "missing": c.missing}
            for c in resolved
        ],
    }


@app.get("/api/grid/values/{column}")
def grid_values(column: str, batch_id: str | None = None, limit: int = 1000) -> dict:
    """Distinct values of a column, to populate a set filter."""
    batch = _batch(batch_id)
    try:
        return {"column": column, "values": batch.distinct(column, limit)}
    except KeyError:
        raise HTTPException(404, f"no such column: {column}")


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


@app.get("/api/dimensions")
def list_dimensions() -> list[dict]:
    return [{"name": d.name, "label": d.label, "axis": d.axis} for d in DIMENSIONS.values()]


@app.get("/api/batches")
def list_batches() -> list[dict]:
    return [
        {"id": b.id, "label": b.label, "displayName": b.display_name,
         "timestamp": b.timestamp.isoformat(), "positions": b.positions,
         "shockConfig": b.shock_config, "scenarios": list(b.scenario_labels),
         "cache": b.cache_stats()}
        for b in batches.list()
    ]


class CreateBatch(BaseModel):
    positions: int = DEFAULT_POSITIONS
    config: str = "Exposure"


@app.post("/api/batches")
def create_batch(body: CreateBatch) -> dict:
    """Rebuild a synthetic batch, e.g. after changing the shock config."""
    try:
        batch = _seed_batch(body.positions, body.config)
    except KeyError:
        raise HTTPException(404, f"no such config: {body.config}")
    return {"id": batch.id, "displayName": batch.display_name, "positions": batch.positions}


@app.get("/api/templates")
def list_templates() -> list[dict]:
    return [t.to_dict() for t in templates.list_templates()]


@app.put("/api/templates")
def save_template(body: dict) -> dict:
    try:
        return templates.save_template(ColumnTemplate.from_dict(body)).to_dict()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/templates/{name}")
def delete_template(name: str) -> dict:
    templates.delete_template(name)
    return {"deleted": name}


@app.get("/api/configs")
def list_configs() -> list[dict]:
    return [c.to_dict() for c in templates.list_configs()]


@app.put("/api/configs")
def save_config(body: dict) -> dict:
    try:
        return templates.save_config(ShockConfig.from_dict(body)).to_dict()
    except NotImplementedError as exc:
        raise HTTPException(400, str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/configs/{name}")
def delete_config(name: str) -> dict:
    templates.delete_config(name)
    return {"deleted": name}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "batches": len(batches)}
