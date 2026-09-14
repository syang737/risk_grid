"""The grid endpoint, driven with AG Grid server-side row model payloads.

These are the request shapes AG Grid actually sends, so a passing test here
means the browser datasource needs no translation layer of its own.
"""

from __future__ import annotations

import os
import tempfile

import polars as pl
import pytest
from fastapi.testclient import TestClient

DIMS = ["sector", "underlying", "contract", "account"]


@pytest.fixture(scope="module")
def client():
    # Small batch and a scratch template directory: these tests are about the
    # request contract, not about scale.
    os.environ["RISK_GRID_POSITIONS"] = "20000"
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RISK_GRID_TEMPLATES"] = tmp
        import importlib

        from api import main

        importlib.reload(main)
        with TestClient(main.app) as c:
            yield c


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


def post(client, **overrides) -> dict:
    response = client.post("/api/grid/rows", json=rows_request(**overrides))
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def test_health_and_seeded_batch(client):
    assert client.get("/api/health").json()["ok"] is True
    batches = client.get("/api/batches").json()
    assert len(batches) == 1
    assert batches[0]["positions"] == 20_000
    assert len(batches[0]["scenarios"]) == 20


def test_dimensions_are_advertised(client):
    names = {d["name"] for d in client.get("/api/dimensions").json()}
    assert set(DIMS) <= names
    assert {"desk", "master_account", "sector"} <= names


def test_columns_resolve_scenarios_by_coordinate(client):
    payload = client.get("/api/grid/columns").json()
    labels = {c["label"]: c["field"] for c in payload["columns"]}
    assert labels["Current NLV"] == "market_value"
    assert labels["Max Risk"] == "worst"
    # The four Sdev columns must bind to four distinct flat-vol scenarios.
    sdev = [labels[k] for k in labels if "Sdev" in k]
    assert len(sdev) == 4 and len(set(sdev)) == 4
    assert not any(c["missing"] for c in payload["columns"])


def test_set_filter_values(client):
    payload = client.get("/api/grid/values/instrument_type").json()
    assert sorted(payload["values"]) == ["EQUITY", "OPTION"]
    assert client.get("/api/grid/values/nope").status_code == 404


# --------------------------------------------------------------------------
# Rows, drilling, paging
# --------------------------------------------------------------------------


def test_root_block(client):
    data = post(client)
    assert data["groupColumn"] == "sector"
    assert data["isLeaf"] is False
    assert data["lastRow"] == len(data["rows"]) == 25
    assert sum(r["positions"] for r in data["rows"]) == 20_000
    assert data["totals"]["positions"] == 20_000


def test_group_keys_drill_one_level_at_a_time(client):
    root = post(client)
    sector = root["rows"][0]["sector"]

    level2 = post(client, groupKeys=[sector])
    assert level2["groupColumn"] == "underlying"
    assert sum(r["positions"] for r in level2["rows"]) == root["rows"][0]["positions"]

    level3 = post(client, groupKeys=[sector, level2["rows"][0]["underlying"]])
    assert level3["groupColumn"] == "contract"


def test_leaf_returns_positions(client):
    keys: list = []
    for dim in DIMS:
        data = post(client, groupKeys=keys, endRow=1)
        keys.append(data["rows"][0][dim])

    leaf = post(client, groupKeys=keys, endRow=5)
    assert leaf["isLeaf"] is True
    assert leaf["groupColumn"] is None
    assert "position_id" in leaf["rows"][0]


def test_paging_reports_unpaged_last_row(client):
    full = post(client)
    page = post(client, startRow=5, endRow=10, sortModel=[{"colId": "worst", "sort": "asc"}])
    assert len(page["rows"]) == 5
    assert page["lastRow"] == full["lastRow"] == 25

    ranked = post(client, sortModel=[{"colId": "worst", "sort": "asc"}])
    assert [r["sector"] for r in page["rows"]] == [r["sector"] for r in ranked["rows"]][5:10]


def test_sorting_both_directions(client):
    asc = post(client, sortModel=[{"colId": "worst", "sort": "asc"}])["rows"]
    desc = post(client, sortModel=[{"colId": "worst", "sort": "desc"}])["rows"]
    assert [r["sector"] for r in asc] == [r["sector"] for r in desc][::-1]
    assert asc[0]["worst"] <= asc[-1]["worst"]


# --------------------------------------------------------------------------
# The distinct-value chip
# --------------------------------------------------------------------------


def test_detail_cells_carry_value_or_count(client):
    data = post(client)
    assert "account" in data["detailDimensions"]
    for row in data["rows"]:
        count = row["account__n"]
        assert (row["account"] is not None) == (count == 1)
        assert count >= 1


def test_chip_drill_reaches_the_accounts_it_counted(client):
    """The count on an instrument row must equal the rows you land on."""
    by_underlying = ["underlying", "account"]
    root = post(client, dimensions=by_underlying, detail_dimensions=["account"], endRow=1)
    row = root["rows"][0]

    drilled = post(client, dimensions=by_underlying, groupKeys=[row["underlying"]],
                   detail_dimensions=["account"])
    assert drilled["groupColumn"] == "account"
    assert drilled["lastRow"] == row["account__n"]


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def test_set_filter_narrows_the_book(client):
    data = post(client, filterModel={
        "instrument_type": {"filterType": "set", "values": ["OPTION"]}})
    assert data["totals"]["positions"] < 20_000
    assert sum(r["positions"] for r in data["rows"]) == data["totals"]["positions"]


def test_text_filter(client):
    data = post(client, filterModel={
        "sector": {"filterType": "text", "type": "contains", "filter": "Real"}})
    assert all("Real" in r["sector"] for r in data["rows"])


def test_number_filter_on_aggregate_column(client):
    """Filtering Max Risk must select groups, not positions."""
    unfiltered = post(client)
    threshold = sorted(r["worst"] for r in unfiltered["rows"])[12]

    data = post(client, filterModel={
        "worst": {"filterType": "number", "type": "lessThan", "filter": threshold}})
    assert all(r["worst"] < threshold for r in data["rows"])
    assert data["lastRow"] < unfiltered["lastRow"]
    # Totals must describe the filtered rows, not the whole book.
    assert data["totals"]["positions"] == sum(r["positions"] for r in data["rows"])


def test_range_filter(client):
    data = post(client, dimensions=["account"], filterModel={
        "strike": {"filterType": "number", "type": "inRange", "filter": 10, "filterTo": 50}})
    assert data["totals"]["positions"] < 20_000


def test_combined_and_filter(client):
    data = post(client, filterModel={"sector": {
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
def test_bad_requests_are_rejected(client, payload, fragment):
    response = client.post("/api/grid/rows", json=rows_request(**payload))
    assert response.status_code == 400
    assert fragment in response.json()["detail"]


def test_or_filters_are_refused_rather_than_silently_narrowing(client):
    response = client.post("/api/grid/rows", json=rows_request(filterModel={"sector": {
        "operator": "OR",
        "condition1": {"filterType": "text", "type": "equals", "filter": "Banking"},
        "condition2": {"filterType": "text", "type": "equals", "filter": "Drugs"},
    }}))
    assert response.status_code == 400
    assert "AND" in response.json()["detail"]


def test_unknown_batch_and_template(client):
    assert client.post("/api/grid/rows", json=rows_request(batch_id="nope")).status_code == 404
    assert client.post("/api/grid/rows", json=rows_request(template="nope")).status_code == 404


# --------------------------------------------------------------------------
# Templates and configs
# --------------------------------------------------------------------------


def test_templates_roundtrip(client):
    names = {t["name"] for t in client.get("/api/templates").json()}
    assert {"Exposure", "Greeks"} <= names

    saved = client.put("/api/templates", json={
        "name": "Minimal",
        "columns": [{"kind": "measure", "label": "NLV", "name": "market_value", "format": "money"}],
    })
    assert saved.status_code == 200
    assert "Minimal" in {t["name"] for t in client.get("/api/templates").json()}

    assert post(client, template="Minimal")["rows"]
    client.delete("/api/templates/Minimal")
    assert "Minimal" not in {t["name"] for t in client.get("/api/templates").json()}


def test_by_strike_config_is_refused(client):
    """An unimplemented shock must not be silently accepted and ignored."""
    response = client.put("/api/configs", json={
        "name": "ByStrike", "price_type": "sigma", "by_strike": True,
        "price": {"count": 2, "step": 1.0, "symmetric": True, "include_zero": False},
        "vol": {"count": 2, "step": 0.05, "symmetric": True, "include_zero": True},
        "horizon_days": 1.0,
    })
    assert response.status_code == 400
    assert "by-strike" in response.json()["detail"]


def test_template_reports_columns_its_config_cannot_supply(client):
    """A narrower shock config must surface missing columns, not hide them."""
    client.put("/api/configs", json={
        "name": "Narrow", "price_type": "sigma", "by_strike": False,
        "price": {"count": 1, "step": 1.0, "symmetric": True, "include_zero": False},
        "vol": {"count": 1, "step": 0.05, "symmetric": True, "include_zero": True},
        "horizon_days": 1.0,
    })
    created = client.post("/api/batches", json={"positions": 2000, "config": "Narrow"})
    assert created.status_code == 200

    payload = client.get("/api/grid/columns", params={"batch_id": created.json()["id"]}).json()
    missing = [c["label"] for c in payload["columns"] if c["missing"]]
    assert any("2 Sdev" in label for label in missing)
