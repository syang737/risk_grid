"""Shared request dependencies.

Separated from `main` so the admin router can use the same authentication,
firm resolution and batch loading without importing it back.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from control.auth import (
    AuthError,
    Forbidden,
    Principal,
    authenticate,
    bearer_token,
    resolve_firm,
)
from control.db import get_database
from control.service import list_batches as list_batch_records
from risk.batch import Batch, BatchStore, load_batch
from risk.build import open_store
from risk.templates import TemplateStore

# Configuration is read when it is first needed rather than at import, so the
# process can be repointed -- which is what makes the service testable, and
# stops an import order from deciding which storage a test suite talks to.


def store_uri() -> str:
    return os.environ.get("RISK_GRID_STORE", "./data")


def template_root() -> Path:
    return Path(os.environ.get("RISK_GRID_TEMPLATES", Path.home() / ".risk_grid"))


def batch_cache_size() -> int:
    return int(os.environ.get("RISK_GRID_BATCH_CACHE", 3))


_store = None
# One loaded-batch cache per firm. Normally a container serves a single firm and
# this holds one entry; the dict exists so local development can run without
# spinning up a container per firm.
_batches: dict[str, BatchStore] = {}
# Templates and shock configs are user content, so they are namespaced by firm
# for the same reason position data is.
_templates: dict[str, TemplateStore] = {}


def get_store():
    global _store
    if _store is None:
        _store = open_store(store_uri())
    return _store


def reset_state() -> None:
    """Drop every cached handle. For tests and for a config reload."""
    global _store
    _store = None
    _batches.clear()
    _templates.clear()


def get_session() -> Session:
    session = get_database().session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def principal(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Principal:
    try:
        return authenticate(session, bearer_token(authorization))
    except AuthError:
        raise HTTPException(401, "invalid credentials")


def resolve(requested: str | None, who: Principal) -> str:
    """Which firm this request is for, checked against both layers."""
    try:
        return resolve_firm(who, requested)
    except Forbidden as exc:
        raise HTTPException(403, str(exc))


def administer(who: Principal, firm_id: str | None) -> str:
    """Resolve the firm and require admin rights over it."""
    firm = resolve(firm_id, who)
    try:
        who.require_administer(firm)
    except Forbidden as exc:
        raise HTTPException(403, str(exc))
    return firm


def templates_for(firm_id: str) -> TemplateStore:
    if firm_id not in _templates:
        _templates[firm_id] = TemplateStore(template_root() / firm_id)
    return _templates[firm_id]


def batch_for(firm_id: str, batch_id: str | None, session: Session) -> Batch:
    """Load a batch, from the process cache or from storage.

    `batch_id` of None means the firm's most recent ready batch, which is what
    a grid opening cold wants.
    """
    cache = _batches.setdefault(firm_id, BatchStore(batch_cache_size()))

    if batch_id is None:
        records = list_batch_records(session, firm_id, limit=1)
        if not records:
            raise HTTPException(404, f"no batches available for firm {firm_id}")
        batch_id = records[0].batch_id

    try:
        return cache.get(batch_id)
    except KeyError:
        pass

    try:
        return cache.put(load_batch(get_store(), firm_id, batch_id))
    except (FileNotFoundError, KeyError, OSError):
        raise HTTPException(404, f"no such batch: {batch_id}")
