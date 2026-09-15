"""The query service, driven with AG Grid server-side row model payloads.

These are the request shapes AG Grid actually sends, so a passing test here
means the browser datasource needs no translation layer of its own. They also
cover the isolation model, which is the part that has to be right.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DIMS = ["sector", "underlying", "contract", "account"]
FIRM = "acme"
OTHER = "globex"


@pytest.fixture(scope="module")
def env():
    """A control plane, a store, two firms, and a built batch for each."""
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
                user = create_user(session, f"ops@{firm}.test", Role.firm_admin, firm)
                session.flush()
                _, tokens[firm] = issue_key(session, user, "test")
            admin = create_user(session, "root@risk.test", Role.platform_admin)
            session.flush()
            _, tokens["platform"] = issue_key(session, admin, "test")

        from risk.build import build, open_store
        from risk.synthetic import generate_book
        from risk.templates import TemplateStore

        store = open_store(str(root / "data"))
        batches = {}
        for firm, n in ((FIRM, 20_000), (OTHER, 5_000)):
            config = TemplateStore(root / "templates" / firm).get_config("Exposure")
            positions, sigma = generate_book(n, seed=hash(firm) % 1000)
            manifest = build(
                firm, store, positions, sigma, config, "IntraDay",
                batch_id=f"{firm}-batch-1",
            )
            batches[firm] = manifest["batch_id"]

        # Reset cached handles: these modules read configuration lazily, but a
        # previous suite will have populated the caches from its own directories.
        import importlib

        from api import deps, main

        deps.reset_state()
        importlib.reload(main)
        with TestClient(main.app) as client:
            yield client, tokens, batches


@pytest.fixture(scope="module")
def client(env):
    return env[0]


@pytest.fixture(scope="module")
def tokens(env):
    return env[1]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def rows_request(**overrides) -> dict:
    base = {
        "startRow": 0,
        "endRow": 100,
        "dimensions": DIMS,
        "groupKeys": [],
        "filterModel": {},
        "sortModel": [],
        "detail_dimensions": ["account", "underlying"],
    }
    base.update(overrides)
    return base


def post(client, token, **overrides) -> dict:
    response = client.post("/api/grid/rows", json=rows_request(**overrides), headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# Authentication and isolation
# --------------------------------------------------------------------------


def test_health_needs_no_credentials(client):
    assert client.get("/api/health").json()["ok"] is True


def test_unauthenticated_requests_are_refused(client):
    assert client.post("/api/grid/rows", json=rows_request()).status_code == 401
    assert client.get("/api/batches").status_code == 401


@pytest.mark.parametrize("header", [
    {"Authorization": "Bearer nonsense"},
    {"Authorization": "Bearer rg_dead_beef"},
    {"Authorization": "Basic abc"},
    {"Authorization": ""},
])
def test_bad_credentials_are_refused(client, header):
    assert client.get("/api/batches", headers=header).status_code == 401


def test_a_firm_cannot_read_another_firms_data(client, tokens):
    """The check that matters most in this product."""
    response = client.post(
        "/api/grid/rows", json=rows_request(firm_id=OTHER), headers=auth(tokens[FIRM])
    )
    assert response.status_code == 403
    assert client.get(f"/api/batches?firm_id={OTHER}", headers=auth(tokens[FIRM])).status_code == 403


def test_each_firm_sees_only_its_own_batches(client, tokens, env):
    _, _, batches = env
    for firm in (FIRM, OTHER):
        listed = client.get("/api/batches", headers=auth(tokens[firm])).json()
        assert [b["id"] for b in listed] == [batches[firm]]
        assert all(b["firmId"] == firm for b in listed)


def test_platform_admin_must_name_a_firm(client, tokens):
    """No sensible default exists, so guessing one would be worse than an error."""
    assert client.get("/api/batches", headers=auth(tokens["platform"])).status_code == 403
    ok = client.get(f"/api/batches?firm_id={FIRM}", headers=auth(tokens["platform"]))
    assert ok.status_code == 200 and ok.json()


def test_a_revoked_key_stops_working(client, tokens):
    from sqlalchemy import select

    from control.db import get_database
    from control.models import ApiKey, User
    from control.service import create_user, issue_key, revoke_key
    from control.models import Role

    database = get_database()
    with database.transaction() as session:
        user = create_user(session, "temp@acme.test", Role.firm_user, FIRM)
        session.flush()
        _, token = issue_key(session, user, "temp")

    assert client.get("/api/batches", headers=auth(token)).status_code == 200

    with database.transaction() as session:
        user = session.scalars(select(User).where(User.email == "temp@acme.test")).one()
        key = session.scalars(select(ApiKey).where(ApiKey.user_id == user.id)).one()
        revoke_key(session, key)

    assert client.get("/api/batches", headers=auth(token)).status_code == 401


def test_container_refuses_a_firm_it_was_not_started_for(monkeypatch, client, tokens):
    """Containment is independent of authorisation: the principal is valid here."""
    monkeypatch.setenv("RISK_GRID_FIRM", OTHER)
    response = client.post(
        "/api/grid/rows", json=rows_request(firm_id=FIRM), headers=auth(tokens[FIRM])
    )
    assert response.status_code == 403
    assert "serves firm" in response.json()["detail"]


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def test_batches_come_from_the_control_plane(client, tokens, env):
    _, _, batches = env
    listed = client.get("/api/batches", headers=auth(tokens[FIRM])).json()
    assert listed[0]["id"] == batches[FIRM]
    assert listed[0]["positions"] == 20_000
    assert len(listed[0]["scenarios"]) == 20
    # Which dimensions roll up depends on their cardinality, not on their name
    # (see test_batch.py); at any size the low-cardinality ones qualify.
    assert {"sector", "desk", "instrument_type"} <= set(listed[0]["rollups"])


def test_dimensions_are_advertised(client, tokens):
    names = {d["name"] for d in client.get("/api/dimensions", headers=auth(tokens[FIRM])).json()}
    assert set(DIMS) <= names
    assert {"desk", "master_account", "sector"} <= names


def test_columns_resolve_scenarios_by_coordinate(client, tokens):
    payload = client.get("/api/grid/columns", headers=auth(tokens[FIRM])).json()
    labels = {c["label"]: c["field"] for c in payload["columns"]}
    assert labels["Current NLV"] == "market_value"
    assert labels["Max Risk"] == "worst"
    sdev = [labels[k] for k in labels if "Sdev" in k]
    assert len(sdev) == 4 and len(set(sdev)) == 4
    assert not any(c["missing"] for c in payload["columns"])


def test_set_filter_values(client, tokens):
    payload = client.get("/api/grid/values/instrument_type", headers=auth(tokens[FIRM])).json()
    assert sorted(payload["values"]) == ["EQUITY", "OPTION"]
    assert client.get("/api/grid/values/nope", headers=auth(tokens[FIRM])).status_code == 404


def test_templates_are_namespaced_per_firm(client, tokens):
    """Saved templates are user content and must not cross firms either."""
    saved = client.put("/api/templates", headers=auth(tokens[FIRM]), json={
        "name": "AcmeOnly",
        "columns": [{"kind": "measure", "label": "NLV", "name": "market_value", "format": "money"}],
    })
    assert saved.status_code == 200

    mine = {t["name"] for t in client.get("/api/templates", headers=auth(tokens[FIRM])).json()}
    theirs = {t["name"] for t in client.get("/api/templates", headers=auth(tokens[OTHER])).json()}
    assert "AcmeOnly" in mine and "AcmeOnly" not in theirs

    client.delete("/api/templates/AcmeOnly", headers=auth(tokens[FIRM]))


# --------------------------------------------------------------------------
# Rows, drilling, paging
# --------------------------------------------------------------------------


def test_root_block(client, tokens):
    data = post(client, tokens[FIRM])
    assert data["groupColumn"] == "sector"
    assert data["isLeaf"] is False
    assert data["lastRow"] == len(data["rows"]) == 25
    assert sum(r["positions"] for r in data["rows"]) == 20_000
    assert data["totals"]["positions"] == 20_000


def test_group_keys_drill_one_level_at_a_time(client, tokens):
    root = post(client, tokens[FIRM])
    sector = root["rows"][0]["sector"]

    level2 = post(client, tokens[FIRM], groupKeys=[sector])
    assert level2["groupColumn"] == "underlying"
    assert sum(r["positions"] for r in level2["rows"]) == root["rows"][0]["positions"]

    level3 = post(client, tokens[FIRM], groupKeys=[sector, level2["rows"][0]["underlying"]])
    assert level3["groupColumn"] == "contract"


def test_leaf_returns_positions(client, tokens):
    keys: list = []
    for dim in DIMS:
        data = post(client, tokens[FIRM], groupKeys=keys, endRow=1)
        keys.append(data["rows"][0][dim])

    leaf = post(client, tokens[FIRM], groupKeys=keys, endRow=5)
    assert leaf["isLeaf"] is True
    assert leaf["groupColumn"] is None
    assert "position_id" in leaf["rows"][0]


def test_paging_reports_unpaged_last_row(client, tokens):
    full = post(client, tokens[FIRM])
    ranked = post(client, tokens[FIRM], sortModel=[{"colId": "worst", "sort": "asc"}])
    page = post(client, tokens[FIRM], startRow=5, endRow=10,
                sortModel=[{"colId": "worst", "sort": "asc"}])

    assert len(page["rows"]) == 5
    assert page["lastRow"] == full["lastRow"] == 25
    assert [r["sector"] for r in page["rows"]] == [r["sector"] for r in ranked["rows"]][5:10]


def test_sorting_both_directions(client, tokens):
    asc = post(client, tokens[FIRM], sortModel=[{"colId": "worst", "sort": "asc"}])["rows"]
    desc = post(client, tokens[FIRM], sortModel=[{"colId": "worst", "sort": "desc"}])["rows"]
    assert [r["sector"] for r in asc] == [r["sector"] for r in desc][::-1]
    assert asc[0]["worst"] <= asc[-1]["worst"]


# --------------------------------------------------------------------------
# The distinct-value chip
# --------------------------------------------------------------------------


def test_detail_cells_carry_value_or_count(client, tokens):
    data = post(client, tokens[FIRM])
    assert "account" in data["detailDimensions"]
    for row in data["rows"]:
        count = row["account__n"]
        assert (row["account"] is not None) == (count == 1)
        assert count >= 1


def test_chip_drill_reaches_the_accounts_it_counted(client, tokens):
    by_underlying = ["underlying", "account"]
    root = post(client, tokens[FIRM], dimensions=by_underlying,
                detail_dimensions=["account"], endRow=1)
    row = root["rows"][0]

    drilled = post(client, tokens[FIRM], dimensions=by_underlying,
                   groupKeys=[row["underlying"]], detail_dimensions=["account"])
    assert drilled["groupColumn"] == "account"
    assert drilled["lastRow"] == row["account__n"]


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def test_set_filter_narrows_the_book(client, tokens):
    data = post(client, tokens[FIRM], filterModel={
        "instrument_type": {"filterType": "set", "values": ["OPTION"]}})
    assert data["totals"]["positions"] < 20_000
    assert sum(r["positions"] for r in data["rows"]) == data["totals"]["positions"]


def test_text_filter(client, tokens):
    data = post(client, tokens[FIRM], filterModel={
        "sector": {"filterType": "text", "type": "contains", "filter": "Real"}})
    assert all("Real" in r["sector"] for r in data["rows"])


def test_number_filter_on_aggregate_column(client, tokens):
    """Filtering Max Risk must select groups, not positions."""
    unfiltered = post(client, tokens[FIRM])
    threshold = sorted(r["worst"] for r in unfiltered["rows"])[12]

    data = post(client, tokens[FIRM], filterModel={
        "worst": {"filterType": "number", "type": "lessThan", "filter": threshold}})
    assert all(r["worst"] < threshold for r in data["rows"])
    assert data["lastRow"] < unfiltered["lastRow"]
    assert data["totals"]["positions"] == sum(r["positions"] for r in data["rows"])


def test_range_filter(client, tokens):
    data = post(client, tokens[FIRM], dimensions=["account"], filterModel={
        "strike": {"filterType": "number", "type": "inRange", "filter": 10, "filterTo": 50}})
    assert data["totals"]["positions"] < 20_000


def test_combined_and_filter(client, tokens):
    data = post(client, tokens[FIRM], filterModel={"sector": {
        "operator": "AND",
        "condition1": {"filterType": "text", "type": "contains", "filter": "a"},
        "condition2": {"filterType": "text", "type": "contains", "filter": "e"},
    }})
    assert all("a" in r["sector"] and "e" in r["sector"] for r in data["rows"])


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload,fragment", [
    ({"dimensions": ["nope"]}, "unknown dimensions"),
    ({"dimensions": []}, "at least one dimension"),
    ({"groupKeys": ["a", "b", "c", "d", "e"]}, "more group keys"),
])
def test_bad_requests_are_rejected(client, tokens, payload, fragment):
    response = client.post(
        "/api/grid/rows", json=rows_request(**payload), headers=auth(tokens[FIRM])
    )
    assert response.status_code == 400
    assert fragment in response.json()["detail"]


def test_or_filters_are_refused_rather_than_silently_narrowing(client, tokens):
    response = client.post("/api/grid/rows", headers=auth(tokens[FIRM]), json=rows_request(
        filterModel={"sector": {
            "operator": "OR",
            "condition1": {"filterType": "text", "type": "equals", "filter": "Banking"},
            "condition2": {"filterType": "text", "type": "equals", "filter": "Drugs"},
        }}))
    assert response.status_code == 400
    assert "AND" in response.json()["detail"]


def test_unknown_batch_and_template(client, tokens):
    assert client.post("/api/grid/rows", headers=auth(tokens[FIRM]),
                       json=rows_request(batch_id="nope")).status_code == 404
    assert client.post("/api/grid/rows", headers=auth(tokens[FIRM]),
                       json=rows_request(template="nope")).status_code == 404


def test_by_strike_config_is_refused(client, tokens):
    """An unimplemented shock must not be silently accepted and ignored."""
    response = client.put("/api/configs", headers=auth(tokens[FIRM]), json={
        "name": "ByStrike", "price_type": "sigma", "by_strike": True,
        "price": {"count": 2, "step": 1.0, "symmetric": True, "include_zero": False},
        "vol": {"count": 2, "step": 0.05, "symmetric": True, "include_zero": True},
        "horizon_days": 1.0,
    })
    assert response.status_code == 400
    assert "by-strike" in response.json()["detail"]
