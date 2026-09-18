"""Loading configuration from a `.env` file.

The precedence rule is the whole point: a real environment variable wins, so a
file baked into a container image cannot override what the orchestrator set.
"""

from __future__ import annotations

import os

import pytest

from config import FILENAME, OPT_OUT, find_env, load_env


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """A `.env` in a directory the loader will find, with the opt-out cleared."""
    monkeypatch.delenv(OPT_OUT, raising=False)
    path = tmp_path / FILENAME
    monkeypatch.chdir(tmp_path)
    return path


def test_a_variable_only_in_the_file_is_loaded(env_file, monkeypatch):
    monkeypatch.delenv("RISK_GRID_STORE", raising=False)
    env_file.write_text("RISK_GRID_STORE=/from/file\n")

    assert load_env(force=True) == env_file
    assert os.environ["RISK_GRID_STORE"] == "/from/file"


def test_a_real_environment_variable_beats_the_file(env_file, monkeypatch):
    """Deployed, the orchestrator is in charge and a stray file must not win."""
    monkeypatch.setenv("RISK_GRID_STORE", "/from/environment")
    env_file.write_text("RISK_GRID_STORE=/from/file\n")

    load_env(force=True)
    assert os.environ["RISK_GRID_STORE"] == "/from/environment"


def test_the_opt_out_disables_loading_entirely(env_file, monkeypatch):
    """What keeps a developer's own .env out of their test run."""
    monkeypatch.setenv(OPT_OUT, "1")
    monkeypatch.delenv("RISK_GRID_STORE", raising=False)
    env_file.write_text("RISK_GRID_STORE=/from/file\n")

    assert load_env() is None
    assert "RISK_GRID_STORE" not in os.environ


def test_force_overrides_the_opt_out(env_file, monkeypatch):
    monkeypatch.setenv(OPT_OUT, "1")
    monkeypatch.delenv("RISK_GRID_STORE", raising=False)
    env_file.write_text("RISK_GRID_STORE=/from/file\n")

    assert load_env(force=True) == env_file
    assert os.environ["RISK_GRID_STORE"] == "/from/file"


def test_a_missing_file_is_not_an_error(tmp_path, monkeypatch):
    """The normal case in a container."""
    monkeypatch.delenv(OPT_OUT, raising=False)
    monkeypatch.chdir(tmp_path)
    assert load_env(force=True) is None


def test_the_file_is_found_from_a_subdirectory(env_file, monkeypatch):
    """A command run from anywhere in the checkout still finds it."""
    env_file.write_text("RISK_GRID_STORE=/from/file\n")
    nested = env_file.parent / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert find_env() == env_file


def test_comments_and_quotes_are_handled(env_file, monkeypatch):
    monkeypatch.delenv("RISK_GRID_SMTP_SENDER", raising=False)
    env_file.write_text(
        "# a comment\n"
        'RISK_GRID_SMTP_SENDER="risk_grid <no-reply@example.com>"\n'
    )
    load_env(force=True)
    assert os.environ["RISK_GRID_SMTP_SENDER"] == "risk_grid <no-reply@example.com>"


def test_the_test_suite_itself_has_the_opt_out_set():
    """conftest.py sets it at import, before anything can read a .env."""
    assert os.environ.get(OPT_OUT) == "1"
