"""Composing and sending reports and alerts."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from alerting import Breach, Evaluation, breaches_to_notify, record_event
from control.models import AlertMode, AlertRule, Report
from risk.templates import TemplateStore

from .base import Attachment, Message, Notifier
from .render import ViewSpec, capture_png, render_csv, render_html


def _stamp(batch) -> str:
    return f"{batch.timestamp:%Y%m%d-%H%M}"


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


def build_report_message(
    batch,
    view: ViewSpec,
    templates: TemplateStore,
    name: str,
    recipients: list[str],
    formats: list[str],
) -> Message:
    """Render a saved view into an email with a picture and the data."""
    template = templates.get_template(view.template)
    config = templates.get_config(batch.shock_config)

    request = view.to_pivot_request(template.measures())
    totals = batch.totals(request)
    document = render_html(batch, view, template, config, name, totals=totals)

    attachments: list[Attachment] = []
    if "image" in formats:
        attachments.append(Attachment(
            f"{name.replace(' ', '-').lower()}-{_stamp(batch)}.png",
            capture_png(document),
            "image/png",
        ))
    if "csv" in formats:
        attachments.append(Attachment(
            f"{name.replace(' ', '-').lower()}-{_stamp(batch)}.csv",
            render_csv(batch, view, template, config),
            "text/csv",
        ))

    worst = totals.get("worst")
    body = (
        f"{name}\n{batch.display_name}\n\n"
        f"{totals.get('positions', 0):,} positions"
        + (f", Max Risk {worst:,.0f}" if isinstance(worst, (int, float)) else "")
        + "\n\nThe attached image is the view as it appears on screen; the CSV is the "
        "same rows at full precision.\n"
    )

    return Message(
        to=recipients,
        subject=f"{name} - {batch.display_name}",
        body=body,
        # The image is attached rather than inlined so it survives clients that
        # block remote content, which in this industry is most of them.
        html=f"<p>{html.escape(name)}<br><b>{html.escape(batch.display_name)}</b></p>"
             f"<p>{totals.get('positions', 0):,} positions</p>",
        attachments=attachments,
    )


def run_report(
    session: Session,
    batch,
    report: Report,
    templates: TemplateStore,
    notifier: Notifier,
) -> Message:
    """Render and send one report, recording the outcome on the row.

    Rendering is inside the try on purpose: it is the likeliest thing to fail
    (a missing browser, a template a config cannot supply) and leaving it
    outside meant the most common failure was the one never recorded.
    """
    try:
        if report.view is None:
            raise ValueError(f"report {report.name!r} points at a view that no longer exists")
        view = ViewSpec.from_dict(report.view.spec or {})
        message = build_report_message(
            batch, view, templates, report.name,
            list(report.recipients or []), list(report.formats or ["image", "csv"]),
        )
        if message.to:
            notifier.send(message)
        report.last_error = ""
        return message
    except Exception as exc:
        report.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        report.last_run_at = datetime.now(timezone.utc)


def run_reports(session: Session, batch, firm_id: str, templates: TemplateStore,
                notifier: Notifier) -> tuple[list[Message], list[str]]:
    """Run every active report for a firm. Returns (sent, failures).

    Failures are returned rather than swallowed: a report that silently stops
    arriving is the failure mode nobody notices for a month.
    """
    reports = session.scalars(
        select(Report).where(Report.firm_id == firm_id, Report.active.is_(True))
    ).all()

    sent, failed = [], []
    for report in reports:
        try:
            sent.append(run_report(session, batch, report, templates, notifier))
        except Exception as exc:
            # One broken report must not stop the rest, but it must be visible.
            failed.append(f"{report.name}: {type(exc).__name__}: {exc}")
    return sent, failed


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def _breach_table(breaches: list[Breach], columns: list[str]) -> str:
    header = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body = []
    for breach in breaches:
        cells = []
        for column in columns:
            value = breach.row.get(column)
            text = f"{value:,.0f}" if isinstance(value, (int, float)) else str(value or "")
            cells.append(f"<td>{html.escape(text)}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        "<table border='1' cellpadding='5' cellspacing='0' "
        f"style='border-collapse:collapse'><tr>{header}</tr>{''.join(body)}</table>"
    )


def build_alert_message(
    batch, rule: AlertRule, evaluation: Evaluation, breaches: list[Breach]
) -> Message:
    group = evaluation.breaches[0].row if evaluation.breaches else {}
    columns = [k for k in ("worst", "market_value", "delta", "positions") if k in group]
    dimension = next((k for k in group if k not in columns), None)
    if dimension:
        columns = [dimension] + columns

    noun = "breach" if len(breaches) == 1 else "breaches"
    lines = "\n".join(
        f"  - {b.group_key}: "
        + ", ".join(
            f"{c}={b.row[c]:,.0f}" if isinstance(b.row.get(c), (int, float)) else f"{c}={b.row.get(c)}"
            for c in columns if c != dimension
        )
        for b in breaches
    )

    body = (
        f"{rule.name}\n{evaluation.description}\n\n"
        f"{len(breaches)} {noun} in {batch.display_name}:\n\n{lines}\n"
    )
    if rule.mode is AlertMode.transition:
        body += "\nYou are told once per breach, not once per batch, so this is new.\n"

    return Message(
        to=list(rule.recipients or []),
        subject=f"[risk_grid] {rule.name} - {len(breaches)} {noun}",
        body=body,
        html=(
            f"<p><b>{html.escape(rule.name)}</b><br>{html.escape(evaluation.description)}</p>"
            f"<p>{len(breaches)} {noun} in {html.escape(batch.display_name)}</p>"
            + _breach_table(breaches, columns)
        ),
    )


def deliver_alerts(
    session: Session,
    batch,
    firm_id: str,
    evaluations: list[Evaluation],
    notifier: Notifier,
) -> list[Message]:
    """Send whatever the evaluations say should be sent, and record it."""
    rules = {
        rule.id: rule
        for rule in session.scalars(select(AlertRule).where(AlertRule.firm_id == firm_id))
    }

    sent = []
    for evaluation in evaluations:
        rule = rules.get(evaluation.rule_id)
        if rule is None:
            continue

        breaches = breaches_to_notify(evaluation, rule.mode)
        if not breaches:
            continue

        event = record_event(session, firm_id, evaluation, breaches)
        message = build_alert_message(batch, rule, evaluation, breaches)
        if not message.to:
            event.notify_error = "no recipients configured"
            continue
        try:
            notifier.send(message)
            event.notified = True
            sent.append(message)
        except Exception as exc:
            event.notify_error = str(exc)[:1000]
    return sent
