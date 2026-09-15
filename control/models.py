"""Control-plane schema.

Metadata only. Position data lives in object storage under a firm-namespaced
prefix and is served by a container that holds exactly one firm's id, so a
cross-firm read needs both the routing and that container's own guard to fail.
Nothing in this database is a risk number.

`BatchRecord` is an *index* over what storage holds, not the source of truth --
storage is. That matters when they disagree: a batch present on disk but absent
here is recoverable; the reverse is a broken pointer.
"""

from __future__ import annotations

import enum
import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

KEY_PREFIX = "rg"
KEY_PREFIX_LEN = 8


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    """Who can do what.

    `platform_admin` is us and has no firm. The other two belong to exactly one
    firm -- a firm admin can manage their own ingestion and users without
    needing us, which is the difference between onboarding scaling and not.
    """

    platform_admin = "platform_admin"
    firm_admin = "firm_admin"
    firm_user = "firm_user"

    @property
    def is_platform(self) -> bool:
        return self is Role.platform_admin

    @property
    def can_administer_firm(self) -> bool:
        return self in (Role.platform_admin, Role.firm_admin)


class Firm(Base):
    __tablename__ = "firms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="firm")
    batches: Mapped[list["BatchRecord"]] = relationship(back_populates="firm")

    def __repr__(self) -> str:
        return f"<Firm {self.id}>"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.firm_user)
    # Null firm means a platform admin; enforced in `validate`.
    firm_id: Mapped[str | None] = mapped_column(ForeignKey("firms.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    firm: Mapped[Firm | None] = relationship(back_populates="users")
    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user")

    def validate(self) -> None:
        if self.role.is_platform and self.firm_id is not None:
            raise ValueError("a platform admin must not belong to a firm")
        if not self.role.is_platform and self.firm_id is None:
            raise ValueError(f"{self.role.value} must belong to a firm")

    def __repr__(self) -> str:
        return f"<User {self.email} {self.role.value}>"


class ApiKey(Base):
    """A bearer token.

    Only a SHA-256 of the secret is stored. These are 256-bit random tokens, not
    passwords, so there is nothing to brute-force offline and a slow KDF would
    only tax every request -- the standard reasoning for tokens over passwords.
    """

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_prefix", "prefix"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(String(200), default="")
    prefix: Mapped[str] = mapped_column(String(32))
    hashed: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @staticmethod
    def hash_secret(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    @classmethod
    def issue(cls, user: User, label: str = "") -> tuple["ApiKey", str]:
        """Mint a key. The plaintext is returned once and never stored."""
        prefix = secrets.token_hex(KEY_PREFIX_LEN // 2)
        secret = secrets.token_urlsafe(32)
        token = f"{KEY_PREFIX}_{prefix}_{secret}"
        key = cls(user=user, label=label, prefix=prefix, hashed=cls.hash_secret(secret))
        return key, token

    @staticmethod
    def split(token: str) -> tuple[str, str] | None:
        """Split a presented token into (prefix, secret), or None if malformed."""
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != KEY_PREFIX:
            return None
        return parts[1], parts[2]


class BatchStatus(str, enum.Enum):
    building = "building"
    ready = "ready"
    failed = "failed"


class BatchRecord(Base):
    """An index entry for a batch that exists in object storage."""

    __tablename__ = "batches"
    __table_args__ = (
        UniqueConstraint("firm_id", "batch_id", name="uq_batches_firm_batch"),
        Index("ix_batches_firm_time", "firm_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firm_id: Mapped[str] = mapped_column(ForeignKey("firms.id"))
    batch_id: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(200), default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    positions: Mapped[int] = mapped_column(Integer, default=0)
    shock_config: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[BatchStatus] = mapped_column(Enum(BatchStatus), default=BatchStatus.building)
    error: Mapped[str] = mapped_column(String(2000), default="")
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    firm: Mapped[Firm] = relationship(back_populates="batches")

    def __repr__(self) -> str:
        return f"<BatchRecord {self.firm_id}/{self.batch_id} {self.status.value}>"


class AuditLog(Base):
    """Who did what, when.

    Kept because Exchange Act Rule 17a-3(a)(23) requires firms over certain
    thresholds to maintain records of their risk management controls; if this
    product becomes that system of record, this table is part of it. See
    docs/strategy.md -- the rule still needs confirming against its text.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_firm_time", "firm_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    firm_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(120))
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
