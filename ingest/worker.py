"""The ingestion pipeline.

    fetch -> map -> validate -> reprice -> write -> register -> alert -> report

Each step is recorded on an `IngestionRun` whether or not it succeeds, because
the question an ops person actually asks is "what happened to the 9:30 file",
and "nothing in the logs" is not an answer.

Validation errors quarantine the file rather than producing a partial batch. A
batch that loads and is wrong is worse than no batch: a risk screen nobody can
trust is worse than a risk screen nobody has.

    python -m ingest.worker --firm acme --once
"""

from __future__ import annotations

import argparse
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from alerting import evaluate_batch
from control.db import Database, get_database, set_database
from control.models import BatchRecord, BatchStatus, IngestionProfile, IngestionRun, RunStatus
from control.service import audit, register_batch
from notify.base import Notifier, default_notifier
from notify.deliver import deliver_alerts, run_reports
from risk.batch import build_batch, load_batch, write_batch
from risk.build import open_store
from risk.storage import ObjectStore
from risk.templates import TemplateStore

from .mapping import MappingError, apply_profile, read_source, sigma_from
from .validate import ValidationContext, ValidationReport, validate


@dataclass
class IngestResult:
    run_id: int | None = None
    status: RunStatus = RunStatus.failed
    batch_id: str = ""
    rows: int = 0
    report: ValidationReport | None = None
    error: str = ""
    alerts_sent: int = 0
    reports_sent: int = 0
    sigma_supplied: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.succeeded


def _previous_context(session: Session, firm_id: str) -> ValidationContext:
    """What the last good batch looked like, so drift is detectable."""
    record = session.scalars(
        select(BatchRecord)
        .where(BatchRecord.firm_id == firm_id, BatchRecord.status == BatchStatus.ready)
        .order_by(desc(BatchRecord.timestamp))
        .limit(1)
    ).one_or_none()
    if record is None:
        return ValidationContext()
    return ValidationContext(previous_rows=record.positions)


def quarantine(store: ObjectStore, firm_id: str, path: Path, reason: str) -> str:
    """Keep a rejected file so someone can look at what actually arrived."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    key = f"firms/{firm_id}/quarantine/{stamp}-{path.name}"
    store.put(key, path.read_bytes())
    store.put(f"{key}.reason.txt", reason.encode())
    return key


def ingest_file(
    session: Session,
    firm_id: str,
    path: Path,
    profile_row: IngestionProfile,
    store: ObjectStore,
    templates: TemplateStore,
    notifier: Notifier | None = None,
    batch_id: str | None = None,
) -> IngestResult:
    """Run one file all the way through, recording what happened at each step."""
    notifier = notifier or default_notifier()
    result = IngestResult()

    run = IngestionRun(
        firm_id=firm_id, profile_id=profile_row.id, file_name=path.name,
        status=RunStatus.running,
    )
    session.add(run)
    session.flush()
    result.run_id = run.id

    def finish(status: RunStatus, error: str = "") -> IngestResult:
        run.status = status
        run.error = error[:2000]
        run.finished_at = datetime.now(timezone.utc)
        result.status = status
        result.error = error
        return result

    # -- map ---------------------------------------------------------------
    try:
        raw = read_source(path, profile_row.file_format, profile_row.read_options)
        frame = apply_profile(raw, profile_row.to_profile())
    except MappingError as exc:
        key = quarantine(store, firm_id, path, "mapping failed:\n" + "\n".join(exc.problems))
        run.quarantine_key = key
        audit(session, "ingest.mapping_failed", firm_id=firm_id, target=path.name,
              problems=exc.problems)
        return finish(RunStatus.quarantined, str(exc))
    except Exception as exc:
        key = quarantine(store, firm_id, path, f"unreadable:\n{traceback.format_exc()}")
        run.quarantine_key = key
        return finish(RunStatus.failed, f"could not read {path.name}: {exc}")

    run.rows = frame.height
    result.rows = frame.height

    # -- validate ----------------------------------------------------------
    report = validate(frame, _previous_context(session, firm_id))
    run.findings = report.to_dict()
    result.report = report

    if not report.ok:
        key = quarantine(store, firm_id, path, report.summary())
        run.quarantine_key = key
        audit(session, "ingest.quarantined", firm_id=firm_id, target=path.name,
              findings=[f.code for f in report.errors])
        return finish(RunStatus.quarantined, report.summary())

    # -- reprice and write -------------------------------------------------
    try:
        positions, sigma, supplied = sigma_from(frame)
        result.sigma_supplied = supplied
        if not supplied:
            result.notes.append(
                "no historical sigma supplied; shock template calibrated from implied vol"
            )

        config = templates.get_config(profile_row.shock_config)
        batch = build_batch(
            positions, sigma, grid=config.to_grid(), label=profile_row.label,
            batch_id=batch_id, shock_config=config.name, firm_id=firm_id,
        )
        manifest = write_batch(batch, store)
    except Exception as exc:
        return finish(RunStatus.failed, f"build failed: {exc}")

    from dataclasses import asdict

    register_batch(session, asdict(manifest))
    run.batch_id = batch.id
    result.batch_id = batch.id
    session.flush()

    # -- alert and report --------------------------------------------------
    # Only after the batch is registered: an alert that fires on a batch nobody
    # can open sends people to a screen that is not there.
    try:
        served = load_batch(store, firm_id, batch.id)
        evaluations = evaluate_batch(session, served, firm_id)
        result.alerts_sent = len(deliver_alerts(session, served, firm_id, evaluations, notifier))
        sent, failed = run_reports(session, served, firm_id, templates, notifier)
        result.reports_sent = len(sent)
        result.notes.extend(f"report failed - {problem}" for problem in failed)
    except Exception as exc:
        # The batch is good and available; downstream delivery failing is worth
        # recording but must not mark the ingest a failure.
        result.notes.append(f"post-batch delivery failed: {exc}")

    return finish(RunStatus.succeeded)


def poll_firm(
    session: Session,
    firm_id: str,
    store: ObjectStore,
    templates: TemplateStore,
    notifier: Notifier | None = None,
    limit: int = 10,
) -> list[IngestResult]:
    """Process whatever is waiting for a firm, oldest first."""
    from .connectors import build_connector

    profiles = session.scalars(
        select(IngestionProfile)
        .where(IngestionProfile.firm_id == firm_id, IngestionProfile.active.is_(True))
        .order_by(desc(IngestionProfile.version))
    ).all()

    results: list[IngestResult] = []
    for profile in profiles:
        connector = build_connector(profile.connector_kind, profile.connector_settings or {})
        waiting = connector.list()[:limit]
        if not waiting:
            continue

        with tempfile.TemporaryDirectory() as tmp:
            for remote in waiting:
                local = connector.fetch(remote.name, Path(tmp) / Path(remote.name).name)
                result = ingest_file(
                    session, firm_id, local, profile, store, templates, notifier
                )
                results.append(result)
                if result.ok:
                    connector.complete(remote.name)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="risk_grid ingestion worker")
    parser.add_argument("--firm", required=True)
    parser.add_argument("--store", default="./data")
    parser.add_argument("--templates", default=str(Path.home() / ".risk_grid"))
    parser.add_argument("--database", default=None)
    parser.add_argument("--file", type=Path, help="ingest one file directly and exit")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args(argv)

    if args.database:
        set_database(Database(args.database))
    database = get_database()
    database.create_all()

    store = open_store(args.store)
    templates = TemplateStore(Path(args.templates) / args.firm)
    notifier = default_notifier()

    with database.transaction() as session:
        if args.file:
            profile = session.scalars(
                select(IngestionProfile)
                .where(IngestionProfile.firm_id == args.firm, IngestionProfile.active.is_(True))
                .order_by(desc(IngestionProfile.version))
                .limit(1)
            ).one_or_none()
            if profile is None:
                parser.error(f"no active ingestion profile for firm {args.firm}")
            results = [ingest_file(session, args.firm, args.file, profile, store, templates, notifier)]
        else:
            results = poll_firm(session, args.firm, store, templates, notifier)

        for result in results:
            detail = result.batch_id or result.error
            print(f"{result.status.value:12s} rows={result.rows:>8,}  {detail}")
            for note in result.notes:
                print(f"             note: {note}")
            if result.report:
                for finding in result.report.findings:
                    print(f"             {finding.severity}: {finding.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
