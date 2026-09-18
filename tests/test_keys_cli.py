"""The keys CLI, and bootstrap's behaviour on a duplicate email.

The claim under test is that a lost token is recoverable in one command. The
strongest form of it is that a token the CLI mints actually authenticates
against the running API, so these go through the real service rather than
asserting on printed text alone.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIRM = "acme"
EMAIL = "ops@acme.test"


@pytest.fixture
def workspace():
    """A control plane with one firm and one bootstrapped user."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        url = f"sqlite+pysqlite:///{root/'control.db'}"
        os.environ["RISK_GRID_STORE"] = str(root / "data")
        os.environ["RISK_GRID_TEMPLATES"] = str(root / "templates")
        os.environ["RISK_GRID_DATABASE_URL"] = url
        os.environ.pop("RISK_GRID_FIRM", None)

        from control import bootstrap

        assert bootstrap.main(
            ["--database", url, "--firm", FIRM, "--name", "Acme", "--email", EMAIL]
        ) == 0
        yield root, url


def token_from(captured: str) -> str:
    line = next(l for l in captured.splitlines() if l.startswith("token :"))
    return line.split(":", 1)[1].strip()


def run_keys(url: str, *args: str) -> int:
    from control import keys

    return keys.main(["--database", url, *args])


# --------------------------------------------------------------------------
# Issuing
# --------------------------------------------------------------------------


def test_issue_mints_a_token_that_actually_works(workspace, capsys):
    """The real claim: a lost key is one command away from being replaced."""
    root, url = workspace

    # A batch, so there is something to authenticate against.
    from risk.build import build, open_store
    from risk.synthetic import generate_book
    from risk.templates import TemplateStore

    config = TemplateStore(root / "templates" / FIRM).get_config("Exposure")
    positions, sigma = generate_book(2_000, seed=4)
    build(FIRM, open_store(str(root / "data")), positions, sigma, config, "IntraDay",
          batch_id="b1")

    capsys.readouterr()
    assert run_keys(url, "issue", "--email", EMAIL, "--label", "laptop") == 0
    token = token_from(capsys.readouterr().out)

    import importlib

    from api import deps, main

    deps.reset_state()
    importlib.reload(main)
    with TestClient(main.app) as client:
        response = client.get("/api/batches", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert [b["id"] for b in response.json()] == ["b1"]


def test_each_issue_produces_a_different_token(workspace, capsys):
    _, url = workspace
    capsys.readouterr()
    run_keys(url, "issue", "--email", EMAIL)
    first = token_from(capsys.readouterr().out)
    run_keys(url, "issue", "--email", EMAIL)
    second = token_from(capsys.readouterr().out)
    assert first != second


def test_issuing_for_an_unknown_user_explains_the_fix(workspace, capsys):
    _, url = workspace
    assert run_keys(url, "issue", "--email", "nobody@acme.test") == 1
    out = capsys.readouterr().out
    assert "no such user" in out
    assert "control.bootstrap" in out


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def test_list_shows_keys_without_leaking_any_secret(workspace, capsys):
    _, url = workspace
    capsys.readouterr()
    run_keys(url, "issue", "--email", EMAIL, "--label", "laptop")
    token = token_from(capsys.readouterr().out)
    secret = token.split("_", 2)[2]

    assert run_keys(url, "list") == 0
    out = capsys.readouterr().out

    assert EMAIL in out and "laptop" in out and "active" in out
    # The prefix identifies a key; the secret must never appear.
    assert token.split("_")[1] in out
    assert secret not in out
    assert token not in out


def test_list_filters_by_firm_and_email(workspace, capsys):
    root, url = workspace
    from control.db import get_database
    from control.models import Role
    from control.service import create_firm, create_user, issue_key

    with get_database().transaction() as session:
        create_firm(session, "globex", "Globex")
        other = create_user(session, "ops@globex.test", Role.firm_admin, "globex")
        session.flush()
        issue_key(session, other, "theirs")

    capsys.readouterr()
    run_keys(url, "list", "--firm", FIRM)
    mine = capsys.readouterr().out
    assert EMAIL in mine and "ops@globex.test" not in mine

    run_keys(url, "list", "--email", "ops@globex.test")
    theirs = capsys.readouterr().out
    assert "ops@globex.test" in theirs and EMAIL not in theirs


def test_list_with_nothing_to_show_says_so(workspace, capsys):
    _, url = workspace
    capsys.readouterr()
    run_keys(url, "list", "--firm", "nosuchfirm")
    assert "no keys found" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Revoking
# --------------------------------------------------------------------------


def test_revoke_stops_a_token_working(workspace, capsys):
    root, url = workspace
    capsys.readouterr()
    run_keys(url, "issue", "--email", EMAIL)
    token = token_from(capsys.readouterr().out)
    prefix = token.split("_")[1]

    import importlib

    from api import deps, main

    deps.reset_state()
    importlib.reload(main)
    with TestClient(main.app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/dimensions", headers=headers).status_code == 200

        assert run_keys(url, "revoke", "--prefix", prefix) == 0
        assert client.get("/api/dimensions", headers=headers).status_code == 401


def test_revoking_twice_is_not_an_error(workspace, capsys):
    _, url = workspace
    capsys.readouterr()
    run_keys(url, "issue", "--email", EMAIL)
    prefix = token_from(capsys.readouterr().out).split("_")[1]

    assert run_keys(url, "revoke", "--prefix", prefix) == 0
    capsys.readouterr()
    assert run_keys(url, "revoke", "--prefix", prefix) == 0
    assert "already revoked" in capsys.readouterr().out


def test_revoking_an_unknown_prefix_explains_the_fix(workspace, capsys):
    _, url = workspace
    assert run_keys(url, "revoke", "--prefix", "deadbeef") == 1
    out = capsys.readouterr().out
    assert "no key with prefix" in out and "keys list" in out


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------


def test_bootstrap_on_a_duplicate_email_points_at_the_keys_command(workspace, capsys):
    """It used to exit with an unhandled ValueError."""
    _, url = workspace
    from control import bootstrap

    assert bootstrap.main(["--database", url, "--firm", FIRM, "--email", EMAIL]) == 1
    out = capsys.readouterr().out
    assert "user already exists" in out
    assert "control.keys issue" in out


def test_bootstrap_does_not_mint_a_second_key_for_a_duplicate(workspace, capsys):
    """Rotating a key is a different verb, and a mistyped email is not a request for one."""
    _, url = workspace
    from control.db import get_database
    from control.service import list_keys

    from control import bootstrap

    with get_database().transaction() as session:
        before = len(list_keys(session))

    bootstrap.main(["--database", url, "--firm", FIRM, "--email", EMAIL])

    with get_database().transaction() as session:
        assert len(list_keys(session)) == before
