"""Alert rules, report rendering, and the pipeline end to end.

The interesting property under test is that an alert rule is a saved pivot plus
a threshold -- so most of what could go wrong is already covered by the pivot
tests, and what is left is the state that distinguishes a new breach from a
standing one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import polars as pl
import pytest

from alerting import AlertSpec, breaches_to_notify, evaluate_batch, evaluate_rule, evaluate_spec
from control.db import Database, set_database
from control.models import (
    AlertEvent,
    AlertMode,
    AlertRule,
    AlertState,
    IngestionProfile,
    Report,
    Role,
    RunStatus,
    SavedView,
)
from control.service import create_firm, create_user
from ingest.worker import ingest_file
from notify.base import ConsoleNotifier
from notify.deliver import build_alert_message, deliver_alerts, run_report, run_reports
from notify.render import ViewSpec, render_csv, render_html
from risk.aggregate import Filter, PivotRequest
from risk.batch import build_batch
from risk.build import open_store
from risk.scenarios import sigma_grid
from risk.synthetic import generate_book
from risk.templates import TemplateStore

FIRM = "acme"


@pytest.fixture(scope="module")
def batch():
    positions, sigma = generate_book(20_000, seed=31)
    return build_batch(positions, sigma, grid=sigma_grid(), batch_id="b1", firm_id=FIRM,
                       label="US WBL IntraDay", shock_config="Exposure")


@pytest.fixture
def env(batch):
    """A control plane with one firm, plus a store and templates."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        database = Database(f"sqlite+pysqlite:///{root/'c.db'}")
        set_database(database)
        database.create_all()
        with database.transaction() as session:
            create_firm(session, FIRM, "Acme")
            create_user(session, "ops@acme.test", Role.firm_admin, FIRM)
        yield database, open_store(str(root / "data")), TemplateStore(root / "tpl" / FIRM)


def loss_spec(threshold: float = -3_000_000) -> AlertSpec:
    return AlertSpec(
        dimensions=("sector",), conditions=(Filter("worst", "lessThan", threshold),)
    )


# --------------------------------------------------------------------------
# A rule is a pivot
# --------------------------------------------------------------------------


def test_a_rule_is_a_pivot_with_a_post_aggregation_filter(batch):
    spec = loss_spec()
    request = spec.to_pivot_request()
    assert isinstance(request, PivotRequest)

    breaches = evaluate_spec(batch, spec)
    # Exactly the rows the equivalent grid filter would show.
    manual = batch.aggregate(request).rows
    assert {b.group_key for b in breaches} == set(manual["sector"].to_list())


def test_breaches_are_the_rows_that_exceed_the_threshold(batch):
    everything = batch.aggregate(PivotRequest(dimensions=("sector",))).rows
    threshold = float(everything["worst"].median())

    breaches = evaluate_spec(batch, loss_spec(threshold))
    expected = everything.filter(pl.col("worst") < threshold)["sector"].to_list()
    assert sorted(b.group_key for b in breaches) == sorted(expected)


def test_a_rule_with_no_condition_is_refused():
    """Otherwise it would fire on every group of every batch, forever."""
    with pytest.raises(ValueError, match="at least one condition"):
        AlertSpec(dimensions=("sector",)).to_pivot_request()


def test_scope_narrows_before_aggregation_and_conditions_after(batch):
    scoped = AlertSpec(
        dimensions=("sector",),
        scope=(Filter("instrument_type", "equals", "OPTION"),),
        conditions=(Filter("worst", "lessThan", -1_000_000),),
    )
    request = scoped.to_pivot_request()
    result = batch.aggregate(request)
    options_only = batch.frame.filter(pl.col("is_option")).height
    assert result.rows["positions"].sum() <= options_only


def test_rules_serialise_and_describe_themselves():
    spec = loss_spec()
    assert AlertSpec.from_dict(spec.to_dict()).to_dict() == spec.to_dict()
    assert spec.describe() == "Any product where Max Risk is below -3,000,000"


def test_scenario_columns_are_stripped_from_breach_rows(batch):
    """Fifty scenario columns are noise in an email."""
    breach = evaluate_spec(batch, loss_spec())[0]
    assert not [k for k in breach.row if k.startswith("s") and k[1:].isdigit()]
    assert "worst" in breach.row


# --------------------------------------------------------------------------
# Transition vs every batch
# --------------------------------------------------------------------------


def _rule(session, mode=AlertMode.transition, threshold=-3_000_000) -> AlertRule:
    spec = loss_spec(threshold)
    rule = AlertRule(firm_id=FIRM, name="Sector loss limit", description=spec.describe(),
                     spec=spec.to_dict(), mode=mode, recipients=["risk@acme.test"])
    session.add(rule)
    session.flush()
    return rule


def test_a_standing_breach_alerts_once_in_transition_mode(env, batch):
    """Alerting every thirty minutes about the same breach trains people to ignore it."""
    database, _, _ = env
    with database.transaction() as session:
        rule = _rule(session)

        first = evaluate_rule(session, batch, rule)
        assert first.new_breaches and len(first.new_breaches) == len(first.breaches)
        assert breaches_to_notify(first, AlertMode.transition)

        session.flush()
        second = evaluate_rule(session, batch, rule)
        assert second.breaches, "still breaching"
        assert not second.new_breaches, "but not newly"
        assert not breaches_to_notify(second, AlertMode.transition)


def test_every_batch_mode_keeps_alerting(env, batch):
    database, _, _ = env
    with database.transaction() as session:
        rule = _rule(session, mode=AlertMode.every_batch)
        evaluate_rule(session, batch, rule)
        session.flush()
        again = evaluate_rule(session, batch, rule)
        assert breaches_to_notify(again, AlertMode.every_batch)


def test_a_breach_that_clears_is_recorded_as_resolved(env, batch):
    database, _, _ = env
    with database.transaction() as session:
        rule = _rule(session)
        first = evaluate_rule(session, batch, rule)
        assert first.new_breaches
        session.flush()

        # Move the threshold so nothing breaches any more.
        rule.spec = loss_spec(-10**12).to_dict()
        cleared = evaluate_rule(session, batch, rule)
        assert not cleared.breaches
        assert set(cleared.resolved) == {b.group_key for b in first.new_breaches}
        assert not any(s.breaching for s in session.query(AlertState).all())


def test_a_breach_can_fire_again_after_resolving(env, batch):
    database, _, _ = env
    with database.transaction() as session:
        rule = _rule(session)
        evaluate_rule(session, batch, rule)
        session.flush()

        rule.spec = loss_spec(-10**12).to_dict()
        evaluate_rule(session, batch, rule)
        session.flush()

        rule.spec = loss_spec().to_dict()
        again = evaluate_rule(session, batch, rule)
        assert again.new_breaches


def test_one_broken_rule_does_not_stop_the_others(env, batch):
    database, _, _ = env
    with database.transaction() as session:
        _rule(session)
        session.add(AlertRule(firm_id=FIRM, name="Broken", spec={"dimensions": ["nope"],
                              "conditions": [{"column": "worst", "op": "lessThan", "value": 0}]},
                              recipients=["risk@acme.test"]))
        session.flush()

        evaluations = evaluate_batch(session, batch, FIRM)
        assert len(evaluations) == 2
        assert any(e.breaches for e in evaluations)
        assert any("could not be evaluated" in e.description for e in evaluations)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def test_alert_email_names_what_breached(env, batch):
    database, _, _ = env
    with database.transaction() as session:
        rule = _rule(session)
        evaluation = evaluate_rule(session, batch, rule)
        message = build_alert_message(batch, rule, evaluation, evaluation.new_breaches)

        assert message.to == ["risk@acme.test"]
        assert "breach" in message.subject
        assert evaluation.new_breaches[0].group_key in message.body
        assert "once per breach" in message.body


def test_delivery_records_an_event_and_marks_it_notified(env, batch):
    database, _, _ = env
    notifier = ConsoleNotifier()
    with database.transaction() as session:
        rule = _rule(session)
        evaluations = evaluate_batch(session, batch, FIRM)
        sent = deliver_alerts(session, batch, FIRM, evaluations, notifier)
        session.flush()

        assert len(sent) == 1 and len(notifier.sent) == 1
        event = session.query(AlertEvent).one()
        assert event.notified is True and event.rows


def test_a_rule_with_no_recipients_records_why_it_was_not_sent(env, batch):
    database, _, _ = env
    with database.transaction() as session:
        rule = _rule(session)
        rule.recipients = []
        evaluations = evaluate_batch(session, batch, FIRM)
        assert deliver_alerts(session, batch, FIRM, evaluations, ConsoleNotifier()) == []
        session.flush()
        assert session.query(AlertEvent).one().notify_error == "no recipients configured"


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


@pytest.fixture
def view_spec():
    return ViewSpec(dimensions=("sector", "underlying"), detail_dimensions=("account",),
                    template="Exposure", depth=2, max_rows=30)


def test_rendered_html_contains_the_numbers(env, batch, view_spec):
    _, _, templates = env
    template = templates.get_template("Exposure")
    config = templates.get_config(batch.shock_config)
    totals = batch.totals(view_spec.to_pivot_request(template.measures()))

    document = render_html(batch, view_spec, template, config, "Daily exposure", totals=totals)
    assert "Daily exposure" in document
    assert batch.display_name in document
    assert "Max Risk" in document
    assert "Total" in document
    # Non-grouping dimensions render as chips, exactly as the grid does.
    assert "accounts</span>" in document


def test_rendered_html_is_not_virtualised(env, batch, view_spec):
    """The whole reason for a separate renderer: every row must be in the DOM."""
    _, _, templates = env
    template = templates.get_template("Exposure")
    document = render_html(batch, view_spec, template,
                           templates.get_config(batch.shock_config), "R")
    assert document.count("<tr") > 20
    assert "<script" not in document


def test_csv_has_a_row_per_rendered_row(env, batch, view_spec):
    _, _, templates = env
    template = templates.get_template("Exposure")
    csv_bytes = render_csv(batch, view_spec, template, templates.get_config(batch.shock_config))
    lines = csv_bytes.decode().strip().splitlines()
    assert lines[0].startswith("level,group")
    assert len(lines) - 1 <= view_spec.max_rows
    assert any(line.startswith("1,") for line in lines[1:]), "depth 2 means indented rows"


def test_a_report_pointing_at_a_deleted_view_says_so(env, batch):
    database, _, templates = env
    with database.transaction() as session:
        report = Report(firm_id=FIRM, view_id=999, name="Orphan",
                        recipients=["ops@acme.test"])
        session.add(report)
        session.flush()
        sent, failed = run_reports(session, batch, FIRM, templates, ConsoleNotifier())
        assert sent == [] and "no longer exists" in failed[0]


def test_report_failures_are_surfaced_not_swallowed(env, batch):
    """A report that silently stops arriving is unnoticed for a month."""
    database, _, templates = env
    with database.transaction() as session:
        view = SavedView(firm_id=FIRM, name="Broken",
                         spec={"dimensions": ["sector"], "template": "NoSuchTemplate"})
        session.add(view)
        session.flush()
        report = Report(firm_id=FIRM, view_id=view.id, name="Broken report",
                        recipients=["ops@acme.test"])
        session.add(report)
        session.flush()

        sent, failed = run_reports(session, batch, FIRM, templates, ConsoleNotifier())
        assert sent == [] and len(failed) == 1
        assert "Broken report" in failed[0]
        # And recorded on the row, so the admin screen can show it.
        assert report.last_error and report.last_run_at


def test_report_sends_image_and_csv(env, batch):
    database, _, templates = env
    notifier = ConsoleNotifier()
    with database.transaction() as session:
        view = SavedView(firm_id=FIRM, name="Exposure",
                         spec={"dimensions": ["sector"], "detail_dimensions": ["account"],
                               "template": "Exposure", "depth": 1, "max_rows": 25})
        session.add(view)
        session.flush()
        report = Report(firm_id=FIRM, view_id=view.id, name="Daily exposure",
                        recipients=["ops@acme.test"], formats=["image", "csv"])
        session.add(report)
        session.flush()

        message = run_report(session, batch, report, templates, notifier)

    names = [a.filename for a in message.attachments]
    assert any(n.endswith(".png") for n in names)
    assert any(n.endswith(".csv") for n in names)
    png = next(a for a in message.attachments if a.filename.endswith(".png"))
    assert png.content.startswith(b"\x89PNG") and len(png.content) > 10_000
    assert report.last_error == ""


# --------------------------------------------------------------------------
# The pipeline end to end
# --------------------------------------------------------------------------


def _export(n: int = 5_000, seed: int = 3) -> pl.DataFrame:
    """A firm's file in their own shape, not ours."""
    book, _ = generate_book(n, seed=seed)
    return book.select(
        pl.col("account").alias("Acct No"),
        pl.col("underlying").alias("Ticker"),
        pl.when(pl.col("qty") < 0).then(pl.lit("SHORT")).otherwise(pl.lit("LONG")).alias("Side"),
        pl.col("qty").abs().cast(pl.Int64).cast(pl.Utf8).alias("Quantity"),
        (pl.col("strike") * 1000).cast(pl.Int64).cast(pl.Utf8).alias("Strike Price"),
        pl.col("expiry").alias("Expiration"),
        pl.when(pl.col("is_option"))
        .then(pl.when(pl.col("is_call")).then(pl.lit("CALL")).otherwise(pl.lit("PUT")))
        .otherwise(pl.lit("")).alias("C/P"),
        pl.col("underlying_price").round(2).cast(pl.Utf8).alias("Und Last"),
        (pl.col("iv") * 100).round(2).cast(pl.Utf8).alias("Implied Vol"),
        pl.col("sector").alias("Product Group"),
    )


MAPPINGS = [
    {"field": "account", "source": "Acct No", "transform": "upper", "params": {}},
    {"field": "underlying", "source": "Ticker", "transform": "upper", "params": {}},
    {"field": "sector", "source": "Product Group", "transform": "trim", "params": {}},
    {"field": "qty", "source": "Quantity", "transform": "signed_by",
     "params": {"side_column": "Side", "short_values": ["SHORT"]}},
    {"field": "strike", "source": "Strike Price", "transform": "scale", "params": {"factor": 0.001}},
    {"field": "expiry", "source": "Expiration", "transform": "trim", "params": {}},
    {"field": "right", "source": "C/P", "transform": "call_put", "params": {}},
    {"field": "underlying_price", "source": "Und Last", "transform": "number", "params": {}},
    {"field": "iv", "source": "Implied Vol", "transform": "scale", "params": {"factor": 0.01}},
]


def _profile(session, **overrides) -> IngestionProfile:
    profile = IngestionProfile(
        firm_id=FIRM, name="clearing-export", connector_kind="local",
        connector_settings={}, file_format="csv", shock_config="Exposure",
        label="US WBL IntraDay", mappings=MAPPINGS, **overrides,
    )
    session.add(profile)
    session.flush()
    return profile


def test_a_firms_own_export_becomes_a_batch(env, tmp_path):
    database, store, templates = env
    path = tmp_path / "positions.csv"
    _export().write_csv(path)

    notifier = ConsoleNotifier()
    with database.transaction() as session:
        profile = _profile(session)
        result = ingest_file(session, FIRM, path, profile, store, templates, notifier)

    assert result.ok and result.rows == 5_000 and result.batch_id
    # No historical sigma in a position file, so the caller is told which
    # calibration it got rather than left to assume.
    assert result.sigma_supplied is False
    assert any("implied vol" in note for note in result.notes)


def test_a_broken_file_is_quarantined_not_half_loaded(env, tmp_path):
    database, store, templates = env
    export = _export().with_columns(pl.lit("").alias("Und Last"))
    path = tmp_path / "broken.csv"
    export.write_csv(path)

    with database.transaction() as session:
        profile = _profile(session)
        result = ingest_file(session, FIRM, path, profile, store, templates, ConsoleNotifier())

    assert result.status is RunStatus.quarantined
    assert not result.batch_id, "no batch may be built from a file that failed validation"
    keys = store.list(f"firms/{FIRM}/quarantine/")
    assert any(k.endswith("broken.csv") for k in keys)
    assert any(k.endswith(".reason.txt") for k in keys)


def test_a_file_the_profile_does_not_fit_is_quarantined(env, tmp_path):
    database, store, templates = env
    path = tmp_path / "wrong_shape.csv"
    pl.DataFrame({"something": ["else"]}).write_csv(path)

    with database.transaction() as session:
        profile = _profile(session)
        result = ingest_file(session, FIRM, path, profile, store, templates, ConsoleNotifier())

    assert result.status is RunStatus.quarantined
    assert "not in the file" in result.error


def test_a_truncated_second_file_is_caught(env, tmp_path):
    """Drift is measured against the previous batch, so this needs two runs."""
    database, store, templates = env
    full = tmp_path / "full.csv"
    _export(5_000).write_csv(full)
    short = tmp_path / "short.csv"
    _export(5_000).head(500).write_csv(short)

    with database.transaction() as session:
        profile = _profile(session)
        first = ingest_file(session, FIRM, full, profile, store, templates, ConsoleNotifier())
        assert first.ok
        session.flush()
        second = ingest_file(session, FIRM, short, profile, store, templates, ConsoleNotifier())

    assert second.status is RunStatus.quarantined
    assert any(f.code == "row_drift" for f in second.report.errors)


def test_ingest_fires_alerts_and_reports(env, tmp_path):
    database, store, templates = env
    path = tmp_path / "positions.csv"
    _export(8_000).write_csv(path)

    notifier = ConsoleNotifier()
    with database.transaction() as session:
        profile = _profile(session)
        spec = loss_spec(-100_000)
        session.add(AlertRule(firm_id=FIRM, name="Loss limit", description=spec.describe(),
                              spec=spec.to_dict(), recipients=["risk@acme.test"]))
        view = SavedView(firm_id=FIRM, name="V",
                         spec={"dimensions": ["sector"], "template": "Exposure",
                               "depth": 1, "max_rows": 25})
        session.add(view)
        session.flush()
        session.add(Report(firm_id=FIRM, view_id=view.id, name="Daily",
                           recipients=["ops@acme.test"], formats=["csv"]))

        result = ingest_file(session, FIRM, path, profile, store, templates, notifier)

    assert result.ok
    assert result.alerts_sent == 1
    assert result.reports_sent == 1
    assert not [n for n in result.notes if "failed" in n]
    recipients = {r for message in notifier.sent for r in message.to}
    assert recipients == {"risk@acme.test", "ops@acme.test"}
