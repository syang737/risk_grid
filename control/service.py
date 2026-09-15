"""Operations the admin API, CLI and build worker all need.

Thin on purpose: these wrap the writes that must also leave an audit trail, so
that recording who did what is not something each caller has to remember.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .models import ApiKey, AuditLog, BatchRecord, BatchStatus, Firm, Role, User


def audit(
    session: Session,
    action: str,
    *,
    actor: str = "system",
    firm_id: str | None = None,
    target: str = "",
    **detail,
) -> AuditLog:
    entry = AuditLog(
        action=action, actor=actor, firm_id=firm_id, target=target, detail=detail
    )
    session.add(entry)
    return entry


# --------------------------------------------------------------------------
# Firms and users
# --------------------------------------------------------------------------


def create_firm(session: Session, firm_id: str, name: str, *, actor: str = "system") -> Firm:
    if session.get(Firm, firm_id) is not None:
        raise ValueError(f"firm already exists: {firm_id}")
    firm = Firm(id=firm_id, name=name)
    session.add(firm)
    audit(session, "firm.create", actor=actor, firm_id=firm_id, target=firm_id, name=name)
    return firm


def get_firm(session: Session, firm_id: str) -> Firm:
    firm = session.get(Firm, firm_id)
    if firm is None:
        raise KeyError(firm_id)
    return firm


def list_firms(session: Session) -> list[Firm]:
    return list(session.scalars(select(Firm).order_by(Firm.id)))


def create_user(
    session: Session,
    email: str,
    role: Role,
    firm_id: str | None = None,
    name: str = "",
    *,
    actor: str = "system",
) -> User:
    if session.scalars(select(User).where(User.email == email)).one_or_none() is not None:
        raise ValueError(f"user already exists: {email}")
    user = User(email=email, role=role, firm_id=firm_id, name=name)
    user.validate()
    if firm_id is not None:
        get_firm(session, firm_id)
    session.add(user)
    audit(session, "user.create", actor=actor, firm_id=firm_id, target=email, role=role.value)
    return user


def issue_key(
    session: Session, user: User, label: str = "", *, actor: str = "system"
) -> tuple[ApiKey, str]:
    """Mint an API key. The returned token is the only time it is visible."""
    key, token = ApiKey.issue(user, label)
    session.add(key)
    audit(
        session, "apikey.issue", actor=actor, firm_id=user.firm_id,
        target=user.email, label=label,
    )
    return key, token


def revoke_key(session: Session, key: ApiKey, *, actor: str = "system") -> ApiKey:
    from .models import utcnow

    key.revoked_at = utcnow()
    audit(session, "apikey.revoke", actor=actor, target=str(key.id))
    return key


# --------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------


def register_batch(
    session: Session,
    manifest: dict,
    *,
    status: BatchStatus = BatchStatus.ready,
    actor: str = "build-worker",
) -> BatchRecord:
    """Index a batch that storage already holds.

    Upserts on (firm, batch) so a rebuild of the same batch id replaces its
    entry rather than creating a second one pointing at the same artifacts.
    """
    firm_id = manifest["firm_id"]
    batch_id = manifest["batch_id"]
    get_firm(session, firm_id)

    record = session.scalars(
        select(BatchRecord).where(
            BatchRecord.firm_id == firm_id, BatchRecord.batch_id == batch_id
        )
    ).one_or_none()
    if record is None:
        record = BatchRecord(firm_id=firm_id, batch_id=batch_id)
        session.add(record)

    record.label = manifest.get("label", "")
    record.timestamp = datetime.fromisoformat(manifest["timestamp"])
    record.positions = manifest.get("positions", 0)
    record.shock_config = manifest.get("shock_config", "")
    record.manifest = manifest
    record.status = status
    record.error = ""

    audit(
        session, "batch.register", actor=actor, firm_id=firm_id, target=batch_id,
        positions=record.positions,
    )
    return record


def fail_batch(
    session: Session, firm_id: str, batch_id: str, error: str, *, actor: str = "build-worker"
) -> BatchRecord:
    record = session.scalars(
        select(BatchRecord).where(
            BatchRecord.firm_id == firm_id, BatchRecord.batch_id == batch_id
        )
    ).one_or_none()
    if record is None:
        record = BatchRecord(firm_id=firm_id, batch_id=batch_id, timestamp=datetime.now())
        session.add(record)
    record.status = BatchStatus.failed
    record.error = error[:2000]
    audit(session, "batch.fail", actor=actor, firm_id=firm_id, target=batch_id, error=error[:500])
    return record


def list_batches(
    session: Session, firm_id: str, limit: int = 50, ready_only: bool = True
) -> list[BatchRecord]:
    query = select(BatchRecord).where(BatchRecord.firm_id == firm_id)
    if ready_only:
        query = query.where(BatchRecord.status == BatchStatus.ready)
    return list(session.scalars(query.order_by(desc(BatchRecord.timestamp)).limit(limit)))


def latest_batch(session: Session, firm_id: str) -> BatchRecord:
    batches = list_batches(session, firm_id, limit=1)
    if not batches:
        raise KeyError(f"no ready batches for firm {firm_id}")
    return batches[0]
