"""The query service.

Serves pivots for persisted batches. It does not build them -- that is
`python -m risk.build` or `python -m ingest.worker` -- so a rebuild never
competes with the queries it is about to invalidate, and the two can be sized
for the machines they suit.

Isolation is two independent layers. The principal's firm is checked on every
call, and the process refuses any firm other than `RISK_GRID_FIRM` when that is
set. Deployed, each firm gets its own container, so position data never shares
a process and a missing check is not by itself a leak.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from control.auth import Principal, served_firm
from control.db import get_database
from control.service import list_batches as list_batch_records
from risk.aggregate import DIMENSIONS, PivotRequest
from risk.templates import ColumnTemplate, ShockConfig

from .admin import router as admin_router
from .deps import (
    batch_for,
    get_session,
    principal,
    resolve,
    store_uri,
    templates_for,
)
from .models import GridRequest, GridResponse, build_path, jsonable, translate_filters


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_database().create_all()
    yield


app = FastAPI(title="risk_grid query service", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(admin_router)


def _firm(requested: str | None, who: Principal) -> str:
    return resolve(requested, who)


def _template(firm_id: str, name: str | None) -> ColumnTemplate:
    store_ = templates_for(firm_id)
    try:
        return store_.get_template(name) if name else store_.list_templates()[0]
    except (KeyError, IndexError):
        raise HTTPException(404, f"no such template: {name}")


# --------------------------------------------------------------------------
# Grid
# --------------------------------------------------------------------------


@app.post("/api/grid/rows", response_model=GridResponse)
def grid_rows(
    request: GridRequest,
    who: Principal = Depends(principal),
    session: Session = Depends(get_session),
) -> GridResponse:
    firm_id = _firm(request.firm_id, who)
    batch = batch_for(firm_id, request.batch_id, session)
    template = _template(firm_id, request.template)

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
def grid_columns(
    firm_id: str | None = None,
    batch_id: str | None = None,
    template: str | None = None,
    who: Principal = Depends(principal),
    session: Session = Depends(get_session),
) -> dict:
    firm = _firm(firm_id, who)
    batch = batch_for(firm, batch_id, session)
    tpl = _template(firm, template)
    try:
        config = templates_for(firm).get_config(batch.shock_config)
    except KeyError:
        raise HTTPException(404, f"no such shock config: {batch.shock_config}")

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
def grid_values(
    column: str,
    firm_id: str | None = None,
    batch_id: str | None = None,
    limit: int = 1000,
    who: Principal = Depends(principal),
    session: Session = Depends(get_session),
) -> dict:
    firm = _firm(firm_id, who)
    batch = batch_for(firm, batch_id, session)
    try:
        return {"column": column, "values": batch.distinct(column, limit)}
    except KeyError:
        raise HTTPException(404, f"no such column: {column}")


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


@app.get("/api/dimensions")
def list_dimensions(who: Principal = Depends(principal)) -> list[dict]:
    return [{"name": d.name, "label": d.label, "axis": d.axis} for d in DIMENSIONS.values()]


@app.get("/api/batches")
def list_batches(
    firm_id: str | None = None,
    limit: int = 50,
    who: Principal = Depends(principal),
    session: Session = Depends(get_session),
) -> list[dict]:
    firm = _firm(firm_id, who)
    return [
        {
            "id": r.batch_id,
            "firmId": r.firm_id,
            "label": r.label,
            "displayName": f"{r.label} {r.timestamp:%Y-%m-%d %H:%M:%S}",
            "timestamp": r.timestamp.isoformat(),
            "positions": r.positions,
            "shockConfig": r.shock_config,
            "scenarios": r.manifest.get("scenario_labels", []),
            "rollups": list(r.manifest.get("rollups", {})),
        }
        for r in list_batch_records(session, firm, limit=limit)
    ]


@app.get("/api/templates")
def list_templates(
    firm_id: str | None = None, who: Principal = Depends(principal)
) -> list[dict]:
    return [t.to_dict() for t in templates_for(_firm(firm_id, who)).list_templates()]


@app.put("/api/templates")
def save_template(
    body: dict, firm_id: str | None = None, who: Principal = Depends(principal)
) -> dict:
    firm = _firm(firm_id, who)
    try:
        return templates_for(firm).save_template(ColumnTemplate.from_dict(body)).to_dict()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/templates/{name}")
def delete_template(
    name: str, firm_id: str | None = None, who: Principal = Depends(principal)
) -> dict:
    templates_for(_firm(firm_id, who)).delete_template(name)
    return {"deleted": name}


@app.get("/api/configs")
def list_configs(firm_id: str | None = None, who: Principal = Depends(principal)) -> list[dict]:
    return [c.to_dict() for c in templates_for(_firm(firm_id, who)).list_configs()]


@app.put("/api/configs")
def save_config(
    body: dict, firm_id: str | None = None, who: Principal = Depends(principal)
) -> dict:
    firm = _firm(firm_id, who)
    try:
        return templates_for(firm).save_config(ShockConfig.from_dict(body)).to_dict()
    except NotImplementedError as exc:
        raise HTTPException(400, str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/configs/{name}")
def delete_config(
    name: str, firm_id: str | None = None, who: Principal = Depends(principal)
) -> dict:
    templates_for(_firm(firm_id, who)).delete_config(name)
    return {"deleted": name}


@app.get("/api/health")
def health() -> dict:
    """Unauthenticated on purpose: load balancers do not carry credentials."""
    return {"ok": True, "firm": served_firm(), "store": store_uri()}
