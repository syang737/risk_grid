"""Admin API: onboarding, ingestion, saved views, reports and alerts.

Firm admins administer their own firm. That is not a convenience -- it is the
difference between onboarding scaling and every new customer costing us a week.
Platform admins can reach any firm but must name one.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from alerting import AlertSpec, evaluate_spec
from control.auth import Forbidden, Principal
from control.models import (
    AlertEvent,
    AlertMode,
    AlertRule,
    IngestionProfile,
    IngestionRun,
    Report,
    SavedView,
)
from control.service import audit
from ingest.connectors import describe_connectors
from ingest.mapping import (
    IngestionProfile as RuntimeProfile,
    MappingError,
    apply_profile,
    describe_transforms,
    read_source,
    suggest_mappings,
)
from ingest.schema import describe as describe_schema
from ingest.validate import validate
from .deps import administer, batch_for, get_session, principal, resolve, templates_for

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Enough rows to see whether a mapping is right, few enough to stay instant.
PREVIEW_ROWS = 20


_administer = administer


# --------------------------------------------------------------------------
# Catalogues
# --------------------------------------------------------------------------


@router.get("/catalogue")
def catalogue(who: Principal = Depends(principal)) -> dict:
    """Everything the mapping screen needs to render itself."""
    return {
        "fields": describe_schema(),
        "transforms": describe_transforms(),
        "connectors": describe_connectors(),
    }


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------


class SampleRequest(BaseModel):
    """A sample file, base64-encoded, plus how to read it."""

    filename: str = "sample.csv"
    content_base64: str
    file_format: str = "csv"
    read_options: dict = Field(default_factory=dict)
    mappings: list[dict] | None = None


def _write_sample(body: SampleRequest, directory: Path) -> Path:
    path = directory / body.filename
    try:
        path.write_bytes(base64.b64decode(body.content_base64))
    except Exception:
        raise HTTPException(400, "content_base64 is not valid base64")
    return path


@router.post("/mapping/inspect")
def inspect_sample(body: SampleRequest, who: Principal = Depends(principal),
                   firm_id: str | None = None) -> dict:
    """Read a sample file and guess a mapping.

    Pre-filling the form is most of why onboarding can be hours rather than
    weeks. The guesses are shown for confirmation, never applied silently.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_sample(body, Path(tmp))
        try:
            frame = read_source(path, body.file_format, body.read_options)
        except Exception as exc:
            raise HTTPException(400, f"could not read the file: {exc}")

    head = frame.head(5)
    return {
        "rows": frame.height,
        "columns": [
            {"name": name, "samples": [str(v) for v in head[name].to_list() if v is not None][:3]}
            for name in frame.columns
        ],
        "suggested": [
            {"field": m.field, "source": m.source, "transform": m.transform, "params": m.params}
            for m in suggest_mappings(list(frame.columns))
        ],
    }


@router.post("/mapping/preview")
def preview_mapping(body: SampleRequest, who: Principal = Depends(principal),
                    firm_id: str | None = None) -> dict:
    """Apply a candidate mapping to a sample and show what comes out.

    Returns validation findings too: seeing that a strike column needs scaling
    is far cheaper here than after a batch has been built from it.
    """
    if body.mappings is None:
        raise HTTPException(400, "mappings are required to preview")

    with tempfile.TemporaryDirectory() as tmp:
        path = _write_sample(body, Path(tmp))
        try:
            raw = read_source(path, body.file_format, body.read_options)
        except Exception as exc:
            raise HTTPException(400, f"could not read the file: {exc}")

    profile = RuntimeProfile.from_dict({
        "firm_id": firm_id or "preview", "file_format": body.file_format,
        "read_options": body.read_options, "mappings": body.mappings,
    })

    try:
        mapped = apply_profile(raw, profile)
    except MappingError as exc:
        # Not an HTTP error: an incomplete mapping is the normal state of a form
        # being filled in, and the problems are what the user needs to see.
        return {"ok": False, "problems": exc.problems, "rows": [], "findings": []}

    report = validate(mapped)
    preview = mapped.head(PREVIEW_ROWS)
    return {
        "ok": True,
        "problems": [],
        "columns": list(preview.columns),
        "rows": [
            {k: (None if v is None else (v if isinstance(v, (int, float, bool)) else str(v)))
             for k, v in row.items()}
            for row in preview.to_dicts()
        ],
        "findings": report.to_dict()["findings"],
        "validates": report.ok,
    }


# --------------------------------------------------------------------------
# Ingestion profiles and runs
# --------------------------------------------------------------------------


class ProfileBody(BaseModel):
    name: str = "default"
    connector_kind: str = "local"
    connector_settings: dict = Field(default_factory=dict)
    file_format: str = "csv"
    read_options: dict = Field(default_factory=dict)
    mappings: list[dict] = Field(default_factory=list)
    schedule: str = ""
    grace_minutes: int = 30
    shock_config: str = "Exposure"
    label: str = "IntraDay"


def _profile_json(profile: IngestionProfile) -> dict:
    return {
        "id": profile.id, "name": profile.name, "version": profile.version,
        "active": profile.active, "connectorKind": profile.connector_kind,
        "connectorSettings": profile.connector_settings, "fileFormat": profile.file_format,
        "readOptions": profile.read_options, "mappings": profile.mappings,
        "schedule": profile.schedule, "graceMinutes": profile.grace_minutes,
        "shockConfig": profile.shock_config, "label": profile.label,
    }


@router.get("/profiles")
def list_profiles(firm_id: str | None = None, who: Principal = Depends(principal),
                  session: Session = Depends(get_session)) -> list[dict]:
    firm = _administer(who, firm_id)
    rows = session.scalars(
        select(IngestionProfile)
        .where(IngestionProfile.firm_id == firm)
        .order_by(IngestionProfile.name, desc(IngestionProfile.version))
    )
    return [_profile_json(p) for p in rows]


@router.post("/profiles")
def save_profile(body: ProfileBody, firm_id: str | None = None,
                 who: Principal = Depends(principal),
                 session: Session = Depends(get_session)) -> dict:
    """Create the next version of a profile.

    Never edits in place. A firm changing their export must not silently
    reinterpret the batches already built under the old mapping.
    """
    firm = _administer(who, firm_id)

    existing = session.scalars(
        select(IngestionProfile)
        .where(IngestionProfile.firm_id == firm, IngestionProfile.name == body.name)
        .order_by(desc(IngestionProfile.version))
    ).all()

    for old in existing:
        old.active = False

    profile = IngestionProfile(
        firm_id=firm, name=body.name,
        version=(existing[0].version + 1) if existing else 1,
        connector_kind=body.connector_kind, connector_settings=body.connector_settings,
        file_format=body.file_format, read_options=body.read_options,
        mappings=body.mappings, schedule=body.schedule, grace_minutes=body.grace_minutes,
        shock_config=body.shock_config, label=body.label,
    )
    session.add(profile)
    session.flush()
    audit(session, "profile.save", actor=who.email, firm_id=firm,
          target=body.name, version=profile.version)
    return _profile_json(profile)


@router.get("/runs")
def list_runs(firm_id: str | None = None, limit: int = 50,
              who: Principal = Depends(principal),
              session: Session = Depends(get_session)) -> list[dict]:
    """What arrived, when, and what happened to it."""
    firm = _administer(who, firm_id)
    rows = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.firm_id == firm)
        .order_by(desc(IngestionRun.started_at))
        .limit(limit)
    )
    return [
        {
            "id": r.id, "fileName": r.file_name, "status": r.status.value, "rows": r.rows,
            "batchId": r.batch_id, "error": r.error, "quarantineKey": r.quarantine_key,
            "findings": (r.findings or {}).get("findings", []),
            "startedAt": r.started_at.isoformat() if r.started_at else None,
            "finishedAt": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# Saved views
# --------------------------------------------------------------------------


class ViewBody(BaseModel):
    name: str
    description: str = ""
    spec: dict = Field(default_factory=dict)


def _view_json(view: SavedView) -> dict:
    return {"id": view.id, "name": view.name, "description": view.description,
            "spec": view.spec, "createdBy": view.created_by}


@router.get("/views")
def list_views(firm_id: str | None = None, who: Principal = Depends(principal),
               session: Session = Depends(get_session)) -> list[dict]:
    firm = resolve(firm_id, who)
    rows = session.scalars(select(SavedView).where(SavedView.firm_id == firm).order_by(SavedView.name))
    return [_view_json(v) for v in rows]


@router.post("/views")
def save_view(body: ViewBody, firm_id: str | None = None,
              who: Principal = Depends(principal),
              session: Session = Depends(get_session)) -> dict:
    firm = resolve(firm_id, who)
    view = session.scalars(
        select(SavedView).where(SavedView.firm_id == firm, SavedView.name == body.name)
    ).one_or_none()
    if view is None:
        view = SavedView(firm_id=firm, name=body.name, created_by=who.email)
        session.add(view)
    view.description = body.description
    view.spec = body.spec
    session.flush()
    return _view_json(view)


@router.delete("/views/{view_id}")
def delete_view(view_id: int, firm_id: str | None = None,
                who: Principal = Depends(principal),
                session: Session = Depends(get_session)) -> dict:
    firm = _administer(who, firm_id)
    view = session.get(SavedView, view_id)
    if view is None or view.firm_id != firm:
        raise HTTPException(404, "no such view")
    attached = session.scalars(select(Report).where(Report.view_id == view_id)).all()
    if attached:
        raise HTTPException(
            409, f"{len(attached)} report(s) still use this view: "
                 + ", ".join(r.name for r in attached)
        )
    session.delete(view)
    return {"deleted": view_id}


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


class ReportBody(BaseModel):
    name: str
    view_id: int
    schedule: str = ""
    recipients: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=lambda: ["image", "csv"])
    active: bool = True


@router.get("/reports")
def list_reports(firm_id: str | None = None, who: Principal = Depends(principal),
                 session: Session = Depends(get_session)) -> list[dict]:
    firm = _administer(who, firm_id)
    rows = session.scalars(select(Report).where(Report.firm_id == firm).order_by(Report.name))
    return [
        {"id": r.id, "name": r.name, "viewId": r.view_id, "schedule": r.schedule,
         "recipients": r.recipients, "formats": r.formats, "active": r.active,
         "lastRunAt": r.last_run_at.isoformat() if r.last_run_at else None,
         "lastError": r.last_error}
        for r in rows
    ]


@router.post("/reports")
def save_report(body: ReportBody, firm_id: str | None = None,
                who: Principal = Depends(principal),
                session: Session = Depends(get_session)) -> dict:
    firm = _administer(who, firm_id)
    view = session.get(SavedView, body.view_id)
    if view is None or view.firm_id != firm:
        raise HTTPException(404, "no such view")

    report = session.scalars(
        select(Report).where(Report.firm_id == firm, Report.name == body.name)
    ).one_or_none()
    if report is None:
        report = Report(firm_id=firm, name=body.name)
        session.add(report)
    report.view_id = body.view_id
    report.schedule = body.schedule
    report.recipients = body.recipients
    report.formats = body.formats
    report.active = body.active
    session.flush()
    audit(session, "report.save", actor=who.email, firm_id=firm, target=body.name)
    return {"id": report.id, "name": report.name}


@router.post("/reports/{report_id}/run")
def run_report_now(report_id: int, firm_id: str | None = None, batch_id: str | None = None,
                   who: Principal = Depends(principal),
                   session: Session = Depends(get_session)) -> dict:
    """Send a report immediately. What a schedule is worth testing against."""
    from notify.base import default_notifier
    from notify.deliver import run_report

    firm = _administer(who, firm_id)
    report = session.get(Report, report_id)
    if report is None or report.firm_id != firm:
        raise HTTPException(404, "no such report")

    batch = batch_for(firm, batch_id, session)
    try:
        message = run_report(session, batch, report, templates_for(firm), default_notifier())
    except Exception as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")
    return {
        "sent": message.to,
        "attachments": [
            {"filename": a.filename, "bytes": len(a.content)} for a in message.attachments
        ],
    }


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


class AlertBody(BaseModel):
    name: str
    description: str = ""
    spec: dict = Field(default_factory=dict)
    mode: str = AlertMode.transition.value
    recipients: list[str] = Field(default_factory=list)
    active: bool = True


def _alert_json(rule: AlertRule) -> dict:
    return {
        "id": rule.id, "name": rule.name, "description": rule.description,
        "spec": rule.spec, "mode": rule.mode.value, "recipients": rule.recipients,
        "active": rule.active,
    }


@router.get("/alerts")
def list_alerts(firm_id: str | None = None, who: Principal = Depends(principal),
                session: Session = Depends(get_session)) -> list[dict]:
    firm = _administer(who, firm_id)
    rows = session.scalars(select(AlertRule).where(AlertRule.firm_id == firm).order_by(AlertRule.name))
    return [_alert_json(r) for r in rows]


@router.post("/alerts")
def save_alert(body: AlertBody, firm_id: str | None = None,
               who: Principal = Depends(principal),
               session: Session = Depends(get_session)) -> dict:
    firm = _administer(who, firm_id)
    try:
        spec = AlertSpec.from_dict(body.spec)
        spec.to_pivot_request()  # refuse a rule that would fire on everything
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc))

    rule = session.scalars(
        select(AlertRule).where(AlertRule.firm_id == firm, AlertRule.name == body.name)
    ).one_or_none()
    if rule is None:
        rule = AlertRule(firm_id=firm, name=body.name)
        session.add(rule)
    rule.description = body.description or spec.describe()
    rule.spec = spec.to_dict()
    rule.mode = AlertMode(body.mode)
    rule.recipients = body.recipients
    rule.active = body.active
    session.flush()
    audit(session, "alert.save", actor=who.email, firm_id=firm, target=body.name)
    return _alert_json(rule)


@router.delete("/alerts/{rule_id}")
def delete_alert(rule_id: int, firm_id: str | None = None,
                 who: Principal = Depends(principal),
                 session: Session = Depends(get_session)) -> dict:
    firm = _administer(who, firm_id)
    rule = session.get(AlertRule, rule_id)
    if rule is None or rule.firm_id != firm:
        raise HTTPException(404, "no such alert")
    session.delete(rule)
    return {"deleted": rule_id}


@router.post("/alerts/test")
def test_alert(body: AlertBody, firm_id: str | None = None, batch_id: str | None = None,
               who: Principal = Depends(principal),
               session: Session = Depends(get_session)) -> dict:
    """Evaluate a rule against a batch without saving or sending it.

    A threshold nobody has checked against real data is a threshold that either
    never fires or fires on everything.
    """
    firm = _administer(who, firm_id)
    try:
        spec = AlertSpec.from_dict(body.spec)
        spec.to_pivot_request()
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc))

    batch = batch_for(firm, batch_id, session)
    try:
        breaches = evaluate_spec(batch, spec)
    except Exception as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")

    return {
        "description": spec.describe(),
        "batch": batch.display_name,
        "breaches": len(breaches),
        "rows": [
            {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v))
             for k, v in b.row.items()}
            for b in breaches[:20]
        ],
    }


@router.get("/alerts/events")
def list_alert_events(firm_id: str | None = None, limit: int = 50,
                      who: Principal = Depends(principal),
                      session: Session = Depends(get_session)) -> list[dict]:
    firm = _administer(who, firm_id)
    rows = session.scalars(
        select(AlertEvent).where(AlertEvent.firm_id == firm)
        .order_by(desc(AlertEvent.fired_at)).limit(limit)
    )
    return [
        {"id": e.id, "ruleId": e.rule_id, "batchId": e.batch_id,
         "firedAt": e.fired_at.isoformat(), "rows": e.rows,
         "notified": e.notified, "notifyError": e.notify_error}
        for e in rows
    ]
