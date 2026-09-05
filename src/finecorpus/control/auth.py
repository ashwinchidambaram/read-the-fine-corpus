"""Service-principal key management (§14.2).

Provides:
  - ServicePrincipalKeyRecord  ORM model
  - ApiKeyRepository           issue / validate / revoke / rotate
  - Principal                  frozen dataclass (authenticated identity)
  - Role / ScopeKind           StrEnums
  - AuthError                  base exception

Key format: ``rtfc_sk_<22 base62 chars>_<43 base62 chars>``

Security invariants enforced here:
  - Key material (the full secret) is NEVER stored, logged, or included in
    any exception message or repr.  Only the prefix (first 8 chars of the
    random portion) and the SHA-256 hex digest are persisted.
  - ``hmac.compare_digest`` is used for all timing-safe comparisons.
  - last_used_at writes are throttled to at most one write per key per 60 s
    to avoid a hot-path write storm.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import DateTime, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from finecorpus.control.metadata import Base

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE62_ALPHABET: Final[str] = string.ascii_letters + string.digits
_KEY_PREFIX_LEN: Final[int] = 22  # chars in the prefix segment
_KEY_SECRET_LEN: Final[int] = 43  # chars in the secret segment
_KEY_STORED_PREFIX_LEN: Final[int] = 8  # prefix stored in DB for lookup
_LAST_USED_THROTTLE_S: Final[int] = 60  # max one last_used write per key per 60 s

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Role(StrEnum):
    """RBAC roles for service principals."""

    admin = "admin"
    editor = "editor"
    viewer = "viewer"
    service = "service"


class ScopeKind(StrEnum):
    """Scope granularity for an API key."""

    global_ = "global"
    workspace = "workspace"
    kb = "kb"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Raised when API key validation fails.

    IMPORTANT: exception messages must NEVER include key material.
    """


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class ServicePrincipalKeyRecord(Base):
    """ORM model for the ``service_principal_keys`` table (§14.2)."""

    __tablename__ = "service_principal_keys"

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, index=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex
    principal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kb_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    def __repr__(self) -> str:
        # Key material is NEVER included in repr.
        return (
            f"ServicePrincipalKeyRecord("
            f"key_id={self.key_id!r}, "
            f"principal_name={self.principal_name!r}, "
            f"role={self.role!r}, "
            f"scope_kind={self.scope_kind!r})"
        )


# ---------------------------------------------------------------------------
# Principal (authenticated identity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Authenticated service principal returned after successful key validation."""

    principal_id: str  # = key_id
    name: str
    role: Role
    scope_kind: ScopeKind
    workspace_id: str | None
    kb_id: str | None
    kind: str = "api_key"

    def __repr__(self) -> str:  # noqa: D105
        return f"Principal(name={self.name!r}, role={self.role!r}, scope_kind={self.scope_kind!r})"


# ---------------------------------------------------------------------------
# Key generation helpers
# ---------------------------------------------------------------------------


def _b62_token(length: int) -> str:
    """Generate a cryptographically random base-62 string of given length."""
    return "".join(secrets.choice(_BASE62_ALPHABET) for _ in range(length))


def _generate_raw_key() -> tuple[str, str, str]:
    """Return ``(full_key, stored_prefix, sha256_hex)``.

    The full key is in the format ``rtfc_sk_<22b62>_<43b62>``.
    ``stored_prefix`` is the first ``_KEY_STORED_PREFIX_LEN`` characters of the
    prefix segment — used for fast DB lookup.
    ``sha256_hex`` is the SHA-256 digest of the full key — what we store.

    The full key is only returned here and must be given to the caller ONCE.
    It is never logged or stored.
    """
    prefix_seg = _b62_token(_KEY_PREFIX_LEN)
    secret_seg = _b62_token(_KEY_SECRET_LEN)
    full_key = f"rtfc_sk_{prefix_seg}_{secret_seg}"
    stored_prefix = prefix_seg[:_KEY_STORED_PREFIX_LEN]
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, stored_prefix, key_hash


def _sha256_hex(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ApiKeyRepository:
    """CRUD operations for ``service_principal_keys``.

    Args:
        session: SQLAlchemy Session bound to the control-plane engine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        # In-process cache for last_used throttle: key_id → epoch_s of last write
        self._last_used_written: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Issue
    # ------------------------------------------------------------------

    def issue(
        self,
        *,
        principal_name: str,
        role: Role,
        scope_kind: ScopeKind,
        created_by: str,
        workspace_id: str | None = None,
        kb_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[str, ServicePrincipalKeyRecord]:
        """Issue a new API key.

        Returns ``(plaintext_key, record)``.  The plaintext key is returned
        ONCE and never stored.  Callers must deliver it to the principal
        immediately (e.g. display once in the UI / API response).

        Args:
            principal_name: Human-readable name for the principal.
            role: Role for the principal.
            scope_kind: Scope granularity.
            created_by: Actor ID performing the issuance.
            workspace_id: Workspace scope (required when scope_kind=workspace or kb).
            kb_id: KB scope (required when scope_kind=kb).
            expires_at: Optional expiry timestamp.

        Returns:
            Tuple of (plaintext_key, ServicePrincipalKeyRecord).
        """
        full_key, stored_prefix, key_hash = _generate_raw_key()
        key_id = secrets.token_hex(16)
        now = datetime.now(tz=UTC)

        record = ServicePrincipalKeyRecord(
            key_id=key_id,
            key_prefix=stored_prefix,
            key_hash=key_hash,
            principal_name=principal_name,
            role=str(role),
            scope_kind=str(scope_kind),
            workspace_id=workspace_id,
            kb_id=kb_id,
            created_at=now,
            expires_at=expires_at,
            revoked_at=None,
            last_used_at=None,
            created_by=created_by,
        )
        self._session.add(record)
        return full_key, record

    # ------------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------------

    def validate(self, raw_key: str) -> Principal:
        """Validate a raw API key and return the authenticated Principal.

        Args:
            raw_key: Full API key string (``rtfc_sk_...``).

        Returns:
            Authenticated Principal.

        Raises:
            AuthError: If the key is invalid, expired, or revoked.
                       NEVER includes key material in the error message.
        """
        # Extract prefix for DB lookup — this avoids a full-table scan
        try:
            # Format: rtfc_sk_<22b62>_<43b62>
            parts = raw_key.split("_", 3)  # ['rtfc', 'sk', prefix_seg, secret_seg]
            if len(parts) != 4 or parts[0] != "rtfc" or parts[1] != "sk":
                raise ValueError("bad format")
            stored_prefix = parts[2][:_KEY_STORED_PREFIX_LEN]
        except (ValueError, IndexError):
            raise AuthError("invalid key format") from None

        stmt = select(ServicePrincipalKeyRecord).where(
            ServicePrincipalKeyRecord.key_prefix == stored_prefix
        )
        record = self._session.execute(stmt).scalar_one_or_none()

        # Always compute the digest so timing is uniform whether or not the
        # record exists.
        candidate_hash = _sha256_hex(raw_key)
        db_hash = record.key_hash if record is not None else ("0" * 64)

        if not hmac.compare_digest(candidate_hash, db_hash):
            raise AuthError("key not found or digest mismatch")

        if record is None:
            # Should not reach here after compare_digest, but be safe.
            raise AuthError("key not found")

        now = datetime.now(tz=UTC)

        if record.revoked_at is not None:
            raise AuthError("key has been revoked")

        if record.expires_at is not None and record.expires_at < now:
            raise AuthError("key has expired")

        # Throttled last_used update
        self._update_last_used(record, now)

        return Principal(
            principal_id=record.key_id,
            name=record.principal_name,
            role=Role(record.role),
            scope_kind=ScopeKind(record.scope_kind),
            workspace_id=record.workspace_id,
            kb_id=record.kb_id,
        )

    def _update_last_used(self, record: ServicePrincipalKeyRecord, now: datetime) -> None:
        """Write last_used_at at most once per ``_LAST_USED_THROTTLE_S`` seconds."""
        epoch = time.monotonic()
        last = self._last_used_written.get(record.key_id, 0.0)
        if epoch - last >= _LAST_USED_THROTTLE_S:
            record.last_used_at = now
            self._last_used_written[record.key_id] = epoch

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    def revoke(self, key_id: str, *, revoked_by: str) -> ServicePrincipalKeyRecord:
        """Revoke an API key.

        Args:
            key_id: Key identifier.
            revoked_by: Actor ID performing the revocation.

        Returns:
            Updated record.

        Raises:
            KeyError: If the key is not found.
            AuthError: If the key is already revoked.
        """
        stmt = select(ServicePrincipalKeyRecord).where(ServicePrincipalKeyRecord.key_id == key_id)
        record = self._session.execute(stmt).scalar_one_or_none()
        if record is None:
            raise KeyError(f"key_id {key_id!r} not found")
        if record.revoked_at is not None:
            raise AuthError("key is already revoked")
        record.revoked_at = datetime.now(tz=UTC)
        return record

    # ------------------------------------------------------------------
    # Rotate
    # ------------------------------------------------------------------

    def rotate(
        self,
        key_id: str,
        *,
        rotated_by: str,
        expires_at: datetime | None = None,
    ) -> tuple[str, ServicePrincipalKeyRecord]:
        """Rotate an API key: revoke the old one and issue a new one.

        Args:
            key_id: Key to rotate.
            rotated_by: Actor performing the rotation.
            expires_at: Optional new expiry for the replacement key.

        Returns:
            Tuple of (new_plaintext_key, new_record).
        """
        old = self.revoke(key_id, revoked_by=rotated_by)
        return self.issue(
            principal_name=old.principal_name,
            role=Role(old.role),
            scope_kind=ScopeKind(old.scope_kind),
            created_by=rotated_by,
            workspace_id=old.workspace_id,
            kb_id=old.kb_id,
            expires_at=expires_at,
        )


__all__ = [
    "ApiKeyRepository",
    "AuthError",
    "Principal",
    "Role",
    "ScopeKind",
    "ServicePrincipalKeyRecord",
]
