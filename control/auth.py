"""Resolving a request to a principal, and what that principal may reach.

The isolation model has two independent layers, and this file is only the first:

  1. Authorisation -- a principal belongs to exactly one firm (or is a platform
     admin), and every firm-scoped call checks it.
  2. Containment -- a query container is started with one firm's id and refuses
     anything else, so layer 1 failing is not sufficient to leak data.

Two layers because a missing `WHERE` clause is the most ordinary bug there is,
and in this market one cross-firm leak ends the company.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ApiKey, Firm, Role, User

FIRM_ENV = "RISK_GRID_FIRM"


class AuthError(Exception):
    """Authentication failed. Never carries a reason to the caller."""


class Forbidden(Exception):
    """Authenticated, but not for this firm."""


@dataclass(frozen=True)
class Principal:
    user_id: int
    email: str
    role: Role
    firm_id: str | None

    @property
    def is_platform_admin(self) -> bool:
        return self.role.is_platform

    def may_read(self, firm_id: str) -> bool:
        return self.is_platform_admin or self.firm_id == firm_id

    def may_administer(self, firm_id: str) -> bool:
        return self.is_platform_admin or (
            self.firm_id == firm_id and self.role.can_administer_firm
        )

    def require_read(self, firm_id: str) -> None:
        if not self.may_read(firm_id):
            raise Forbidden(f"not authorised for firm {firm_id}")

    def require_administer(self, firm_id: str) -> None:
        if not self.may_administer(firm_id):
            raise Forbidden(f"not authorised to administer firm {firm_id}")


def authenticate(session: Session, token: str) -> Principal:
    """Resolve a bearer token to a principal.

    Every failure raises the same bare `AuthError`: distinguishing "no such key"
    from "revoked" from "wrong secret" tells an attacker which half of a guess
    was right.
    """
    parts = ApiKey.split(token or "")
    if parts is None:
        raise AuthError("invalid token")
    prefix, secret = parts

    key = session.scalars(select(ApiKey).where(ApiKey.prefix == prefix)).one_or_none()
    if key is None or not key.active:
        raise AuthError("invalid token")

    import hmac

    if not hmac.compare_digest(key.hashed, ApiKey.hash_secret(secret)):
        raise AuthError("invalid token")

    user = session.get(User, key.user_id)
    if user is None or not user.active:
        raise AuthError("invalid token")
    if user.firm_id is not None:
        firm = session.get(Firm, user.firm_id)
        if firm is None or not firm.active:
            raise AuthError("invalid token")

    key.last_used_at = datetime.now(timezone.utc)
    session.flush()

    return Principal(user_id=user.id, email=user.email, role=user.role, firm_id=user.firm_id)


def bearer_token(header: str | None) -> str:
    """Pull the token out of an Authorization header."""
    if not header:
        raise AuthError("missing credentials")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("missing credentials")
    return token.strip()


# --------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------


def served_firm() -> str | None:
    """The single firm this process may serve, if it is a query container."""
    value = os.environ.get(FIRM_ENV, "").strip()
    return value or None


def require_served_firm(firm_id: str) -> None:
    """Refuse work for any firm other than the one this container was started for.

    Deliberately independent of the principal: this holds even if the caller
    authenticated correctly and the router sent them to the wrong container.
    """
    served = served_firm()
    if served is not None and served != firm_id:
        raise Forbidden(
            f"this service instance serves firm {served!r} and cannot serve {firm_id!r}"
        )


def resolve_firm(principal: Principal, requested: str | None) -> str:
    """Decide which firm a request is for, and check it is allowed.

    A firm user need not name their firm; a platform admin must, because there
    is no sensible default and guessing one would be worse than an error.
    """
    firm_id = requested or served_firm() or principal.firm_id
    if firm_id is None:
        raise Forbidden("no firm specified")
    principal.require_read(firm_id)
    require_served_firm(firm_id)
    return firm_id
