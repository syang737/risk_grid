"""The admin API: onboarding, ingestion history, views, reports and alerts.

Two things get most of the attention: that the mapping screen can actually
onboard an awkward file, and that a firm admin cannot reach another firm.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

FIRM = "acme"
OTHER = "globex"

RAW_CSV = (
    "Acct No,Ticker,Side,Quantity,Strike Price,Days To Exp,C/P,Und Last,Implied Vol\n"
    "A-001,aapl,LONG,\"1,200\",150000,30,CALL,$182.50,28.5\n"
    "A-001,AAPL,SHORT,(300),160000,30,PUT,182.50,31.2\n"
    "A-002,SPY,LONG,\"2,000\",0,,,560.10,0\n"
)

MAPPINGS = [
    {"field": "account", "source": "Acct No", "transform": "upper", "params": {}},
    {"field": "underlying", "source": "Ticker", "transform": "upper", "params": {}},
    {"field": "qty", "source": "Quantity", "transform": "signed_by",
     "params": {"side_column": "Side", "short_values": ["SHORT"]}},
    {"field": "strike", "source": "Strike Price", "transform": "scale", "params": {"factor": 0.001}},
    {"field": "expiry", "source": "Days To Exp", "transform": "trim", "params": {}},
    {"field": "right", "source": "C/P", "transform": "call_put", "params": {}},
    {"field": "underlying_price", "source": "Und Last", "transform": "number", "params": {}},
    {"field": "iv", "source": "Implied Vol", "transform": "scale", "params": {"factor": 0.01}},
]


@pytest.fixture(scope="module")
def env():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["RISK_GRID_STORE"] = str(root / "data")
        os.environ["RISK_GRID_TEMPLATES"] = str(root / "templates")
        os.environ["RISK_GRID_DATABASE_URL"] = f"sqlite+pysqlite:///{root/'control.db'}"
        os.environ.pop("RISK_GRID_FIRM", None)

        from control.db import Database, set_database
        from control.models import Role
        from control.service import create_firm, create_user, issue_key

        database = Database(f"sqlite+pysqlite:///{root/'control.db'}")
        set_database(database)
        database.create_all()

        tokens = {}
        with database.transaction() as session:
            for firm in (FIRM, OTHER):
                create_firm(session, firm, firm.title())
                admin = create_user(session, f"admin@{firm}.test", Role.firm_admin, firm)
                viewer = create_user(session, f"user@{firm}.test", Role.firm_user, firm)
                session.flush()
                _, tokens[firm] = issue_key(session, admin, "test")
                _, tokens[f"{firm}-user"] = issue_key(session, viewer, "test")

        from risk.build import build, open_store
        from risk.synthetic import generate_book
        from risk.templates import TemplateStore

        store = open_store(str(root / "data"))
        for firm, n in ((FIRM, 8_000), (OTHER, 3_000)):
            config = TemplateStore(root / "templates" / firm).get_config("Exposure")
            positions, sigma = generate_book(n, seed=len(firm))
            build(firm, store, positions, sigma, config, "IntraDay", batch_id=f"{firm}-b1")

        # Reset cached handles: these modules read configuration lazily, but a
        # previous suite will have populated the caches from its own directories.
        import importlib

        from api import deps, main

        deps.reset_state()
        importlib.reload(main)
        with TestClient(main.app) as client:
            yield client, tokens


@pytest.fixture(scope="module")
def client(env):
    return env[0]


@pytest.fixture(scope="module")
def tokens(env):
    return env[1]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def sample_body(**overrides) -> dict:
    body = {
        "filename": "positions.csv",
        "content_base64": base64.b64encode(RAW_CSV.encode()).decode(),
        "file_format": "csv",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# Onboarding: the mapping screen
# --------------------------------------------------------------------------


def test_catalogue_describes_fields_transforms_and_connectors(client, tokens):
    payload = client.get("/api/admin/catalogue", headers=auth(tokens[FIRM])).json()
    assert {f["name"] for f in payload["fields"]} >= {"account", "underlying", "qty"}
    assert {t["name"] for t in payload["transforms"]} >= {"signed_by", "call_put", "scale"}
    assert {c["kind"] for c in payload["connectors"]} == {"local", "s3", "sftp"}
    assert any(f["required"] for f in payload["fields"])


def test_inspect_reads_columns_and_prefills_the_mapping(client, tokens):
    payload = client.post("/api/admin/mapping/inspect", headers=auth(tokens[FIRM]),
                          json=sample_body()).json()

    assert payload["rows"] == 3
    names = [c["name"] for c in payload["columns"]]
    assert names[0] == "Acct No" and "Implied Vol" in names
    assert payload["columns"][0]["samples"][0] == "A-001"

    suggested = {s["source"]: s["field"] for s in payload["suggested"]}
    assert suggested["Acct No"] == "account"
    assert suggested["C/P"] == "right"


def test_preview_shows_what_the_mapping_produces(client, tokens):
    payload = client.post("/api/admin/mapping/preview", headers=auth(tokens[FIRM]),
                          json=sample_body(mappings=MAPPINGS)).json()

    assert payload["ok"] and payload["validates"]
    rows = payload["rows"]
    assert [r["qty"] for r in rows] == [1200.0, -300.0, 2000.0]
    assert [r["strike"] for r in rows] == [150.0, 160.0, 0.0]
    assert [r["right"] for r in rows] == ["C", "P", "-"]
    assert [r["is_option"] for r in rows] == [True, True, False]


def test_preview_reports_an_incomplete_mapping_without_failing(client, tokens):
    """A half-filled form is the normal state of a form, not an error."""
    response = client.post("/api/admin/mapping/preview", headers=auth(tokens[FIRM]),
                           json=sample_body(mappings=MAPPINGS[:1]))
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert any("required fields not mapped" in p for p in payload["problems"])


def test_preview_surfaces_a_scaling_mistake_before_any_batch_is_built(client, tokens):
    """Catching this here is far cheaper than after a batch is built from it."""
    unscaled = [dict(m) for m in MAPPINGS]
    for mapping in unscaled:
        if mapping["field"] == "iv":
            mapping["transform"], mapping["params"] = "number", {}

    payload = client.post("/api/admin/mapping/preview", headers=auth(tokens[FIRM]),
                          json=sample_body(mappings=unscaled)).json()
    codes = {f["code"] for f in payload["findings"]}
    assert "implausible_iv" in codes
    assert any("scale transform" in f["message"] for f in payload["findings"])


def test_unreadable_upload_is_rejected(client, tokens):
    response = client.post("/api/admin/mapping/inspect", headers=auth(tokens[FIRM]),
                           json={"content_base64": "not base64!!", "filename": "x.csv"})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------


def test_saving_a_profile_creates_a_new_version_rather_than_editing(client, tokens):
    """Old batches were built under the old mapping and must stay reproducible."""
    body = {"name": "clearing-export", "connector_kind": "local",
            "connector_settings": {"directory": "/tmp/drop"}, "mappings": MAPPINGS}

    first = client.post("/api/admin/profiles", headers=auth(tokens[FIRM]), json=body).json()
    assert first["version"] == 1 and first["active"] is True

    second = client.post("/api/admin/profiles", headers=auth(tokens[FIRM]), json=body).json()
    assert second["version"] == 2

    listed = client.get("/api/admin/profiles", headers=auth(tokens[FIRM])).json()
    versions = {p["version"]: p["active"] for p in listed if p["name"] == "clearing-export"}
    assert versions == {1: False, 2: True}, "only the newest version stays active"


def test_a_firm_user_cannot_administer(client, tokens):
    """Reading the grid and changing how data arrives are different privileges."""
    assert client.get("/api/admin/profiles", headers=auth(tokens[f"{FIRM}-user"])).status_code == 403
    assert client.post("/api/admin/profiles", headers=auth(tokens[f"{FIRM}-user"]),
                       json={"name": "x", "mappings": MAPPINGS}).status_code == 403


def test_admin_endpoints_are_firm_scoped(client, tokens):
    for path in ("/api/admin/profiles", "/api/admin/runs", "/api/admin/alerts",
                 "/api/admin/reports", "/api/admin/views"):
        response = client.get(f"{path}?firm_id={OTHER}", headers=auth(tokens[FIRM]))
        assert response.status_code == 403, path


def test_admin_endpoints_need_credentials(client):
    assert client.get("/api/admin/profiles").status_code == 401
    assert client.post("/api/admin/alerts", json={"name": "x"}).status_code == 401


# --------------------------------------------------------------------------
# Views and reports
# --------------------------------------------------------------------------


def test_views_round_trip(client, tokens):
    spec = {"dimensions": ["sector"], "detail_dimensions": ["account"],
            "template": "Exposure", "depth": 1, "max_rows": 25}
    saved = client.post("/api/admin/views", headers=auth(tokens[FIRM]),
                        json={"name": "By sector", "spec": spec}).json()
    assert saved["spec"] == spec

    listed = client.get("/api/admin/views", headers=auth(tokens[FIRM])).json()
    assert any(v["name"] == "By sector" for v in listed)


def test_a_view_in_use_cannot_be_deleted_silently(client, tokens):
    spec = {"dimensions": ["sector"], "template": "Exposure", "depth": 1}
    view = client.post("/api/admin/views", headers=auth(tokens[FIRM]),
                       json={"name": "Attached", "spec": spec}).json()
    client.post("/api/admin/reports", headers=auth(tokens[FIRM]),
                json={"name": "Uses it", "view_id": view["id"],
                      "recipients": ["ops@acme.test"], "formats": ["csv"]})

    response = client.delete(f"/api/admin/views/{view['id']}", headers=auth(tokens[FIRM]))
    assert response.status_code == 409
    assert "Uses it" in response.json()["detail"]


def test_running_a_report_now_returns_its_attachments(client, tokens):
    spec = {"dimensions": ["sector"], "detail_dimensions": ["account"],
            "template": "Exposure", "depth": 1, "max_rows": 25}
    view = client.post("/api/admin/views", headers=auth(tokens[FIRM]),
                       json={"name": "Run now", "spec": spec}).json()
    report = client.post("/api/admin/reports", headers=auth(tokens[FIRM]),
                         json={"name": "Ad hoc", "view_id": view["id"],
                               "recipients": ["ops@acme.test"],
                               "formats": ["image", "csv"]}).json()

    payload = client.post(f"/api/admin/reports/{report['id']}/run",
                          headers=auth(tokens[FIRM])).json()
    assert payload["sent"] == ["ops@acme.test"]
    names = [a["filename"] for a in payload["attachments"]]
    assert any(n.endswith(".png") for n in names) and any(n.endswith(".csv") for n in names)
    assert all(a["bytes"] > 0 for a in payload["attachments"])


def test_a_report_cannot_borrow_another_firms_view(client, tokens):
    spec = {"dimensions": ["sector"], "template": "Exposure"}
    theirs = client.post("/api/admin/views", headers=auth(tokens[OTHER]),
                         json={"name": "Theirs", "spec": spec}).json()
    response = client.post("/api/admin/reports", headers=auth(tokens[FIRM]),
                           json={"name": "Sneaky", "view_id": theirs["id"],
                                 "recipients": ["x@acme.test"]})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def alert_body(threshold: float = -500_000, **overrides) -> dict:
    body = {
        "name": "Sector loss limit",
        "spec": {"dimensions": ["sector"],
                 "conditions": [{"column": "worst", "op": "lessThan", "value": threshold}]},
        "recipients": ["risk@acme.test"],
    }
    body.update(overrides)
    return body


def test_saving_an_alert_describes_it_back(client, tokens):
    saved = client.post("/api/admin/alerts", headers=auth(tokens[FIRM]),
                        json=alert_body()).json()
    assert saved["description"] == "Any product where Max Risk is below -500,000"
    assert saved["mode"] == "transition"


def test_an_alert_with_no_condition_is_refused(client, tokens):
    """It would fire on every group of every batch, forever."""
    response = client.post("/api/admin/alerts", headers=auth(tokens[FIRM]),
                           json=alert_body(name="Everything", spec={"dimensions": ["sector"]}))
    assert response.status_code == 400
    assert "at least one condition" in response.json()["detail"]


def test_testing_an_alert_shows_what_it_would_catch(client, tokens):
    """A threshold nobody checked either never fires or fires on everything."""
    loose = client.post("/api/admin/alerts/test", headers=auth(tokens[FIRM]),
                        json=alert_body(threshold=-1)).json()
    tight = client.post("/api/admin/alerts/test", headers=auth(tokens[FIRM]),
                        json=alert_body(threshold=-10**12)).json()

    assert loose["breaches"] > 0 and tight["breaches"] == 0
    assert loose["rows"] and "worst" in loose["rows"][0]
    assert "IntraDay" in loose["batch"]


def test_testing_an_alert_does_not_save_it(client, tokens):
    before = {a["name"] for a in client.get("/api/admin/alerts", headers=auth(tokens[FIRM])).json()}
    client.post("/api/admin/alerts/test", headers=auth(tokens[FIRM]),
                json=alert_body(name="Never saved", threshold=-1))
    after = {a["name"] for a in client.get("/api/admin/alerts", headers=auth(tokens[FIRM])).json()}
    assert "Never saved" not in after and before == after


def test_alerts_can_be_deleted(client, tokens):
    saved = client.post("/api/admin/alerts", headers=auth(tokens[FIRM]),
                        json=alert_body(name="Temporary")).json()
    assert client.delete(f"/api/admin/alerts/{saved['id']}",
                         headers=auth(tokens[FIRM])).status_code == 200
    names = {a["name"] for a in client.get("/api/admin/alerts", headers=auth(tokens[FIRM])).json()}
    assert "Temporary" not in names


def test_one_firm_cannot_delete_anothers_alert(client, tokens):
    theirs = client.post("/api/admin/alerts", headers=auth(tokens[OTHER]),
                         json=alert_body(name="Theirs", recipients=["x@globex.test"])).json()
    assert client.delete(f"/api/admin/alerts/{theirs['id']}",
                         headers=auth(tokens[FIRM])).status_code == 404


def test_ingestion_runs_are_listed_with_their_findings(client, tokens, env):
    """What an ops person actually asks: what happened to the 9:30 file."""
    from control.db import get_database
    from control.models import IngestionProfile
    from ingest.worker import ingest_file
    from notify.base import ConsoleNotifier
    from risk.build import open_store
    from risk.templates import TemplateStore

    from api.deps import store_uri, template_root

    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "broken.csv"
        broken.write_text("Acct No,Ticker,Side,Quantity,Strike Price,Days To Exp,C/P,Und Last,Implied Vol\n"
                          "A-001,AAPL,LONG,100,150000,30,CALL,0,28.5\n")
        with get_database().transaction() as session:
            profile = session.query(IngestionProfile).filter_by(firm_id=FIRM, active=True).first()
            ingest_file(session, FIRM, broken, profile, open_store(store_uri()),
                        TemplateStore(template_root() / FIRM), ConsoleNotifier())

    runs = client.get("/api/admin/runs", headers=auth(tokens[FIRM])).json()
    assert runs, "the run is recorded whether or not it worked"
    latest = runs[0]
    assert latest["status"] == "quarantined"
    assert latest["quarantineKey"], "the rejected file is kept to look at"
    assert any(f["code"] == "bad_price" for f in latest["findings"])
