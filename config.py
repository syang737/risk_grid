"""Loading configuration from a `.env` file.

Every setting is an environment variable, which is right for a deployed
container and awkward everywhere else: `RISK_GRID_FIRM=acme python ...` is bash
syntax, a parse error in PowerShell and a no-op in cmd. A `.env` file makes one
set of instructions work on every operating system.

Two rules, both load-bearing:

  A real environment variable always wins. Deployed, the orchestrator sets the
  environment and a file that happened to be baked into an image must not
  override it. `override=False` gives exactly that precedence.

  Tests opt out. The API suites reload `api.main`, which would run the loader,
  so a developer's own `.env` could inject a stray `RISK_GRID_FIRM` and fail
  their tests for a reason invisible in the diff. `tests/conftest.py` sets
  `RISK_GRID_NO_DOTENV`.
"""

from __future__ import annotations

import os
from pathlib import Path

OPT_OUT = "RISK_GRID_NO_DOTENV"
FILENAME = ".env"

_loaded: Path | None = None


def find_env(start: Path | None = None) -> Path | None:
    """Nearest `.env`, searching upwards from `start` (default: cwd).

    Walking up means a command run from a subdirectory of the checkout still
    finds the repository's file.
    """
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_env(path: Path | str | None = None, force: bool = False) -> Path | None:
    """Load `.env` into the environment and return the file used, if any.

    Returns None when there is nothing to load, which is the normal case in a
    container and is not an error. Pass `force=True` to ignore the opt-out.
    """
    global _loaded

    if os.environ.get(OPT_OUT) and not force:
        return None

    target = Path(path) if path else find_env()
    if target is None or not target.is_file():
        return None

    from dotenv import load_dotenv

    load_dotenv(target, override=False)
    _loaded = target
    return target


def loaded_from() -> Path | None:
    """Which file the current process loaded, for diagnostics."""
    return _loaded
