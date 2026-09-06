"""Session store abstraction for the web application (Phase 6, WU-A).

Defines the ``SessionStore`` protocol and an ``InMemorySessionStore`` default.
The store maps opaque session tokens to ``Session`` objects. It never logs
tokens.

Persistent-store seam
---------------------
The in-memory store is process-local and lost on restart — acceptable for a
single-replica foundation. A persistent implementation (Postgres via the
existing control-plane, or Redis via the configured cache backend) would
implement the same ``SessionStore`` protocol and be injected into
``LocalAccountsAuthProvider(session_store=...)``. No auth or route code
changes: see the ``# Persistent-store seam:`` note on ``InMemorySessionStore``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from finecorpus.web.auth import Session


@runtime_checkable
class SessionStore(Protocol):
    """Server-side store mapping opaque tokens to sessions."""

    def save(self, session: Session) -> None: ...

    def get(self, token: str) -> Session | None: ...

    def delete(self, token: str) -> None: ...


class InMemorySessionStore:
    """Thread-safe in-process session store (default).

    Persistent-store seam: replace this with a Postgres- or Redis-backed
    implementation of ``SessionStore`` to survive restarts and share sessions
    across replicas. The rest of the web layer is unaffected.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def save(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.token] = session

    def get(self, token: str) -> Session | None:
        with self._lock:
            return self._sessions.get(token)

    def delete(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)


__all__ = ["InMemorySessionStore", "SessionStore"]
