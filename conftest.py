"""Root pytest configuration for Read The Fine Corpus.

Registers custom markers so pytest --co does not emit PytestUnknownMarkWarning.

Provides :func:`make_qdrant_integration_mark` — a helper that returns a composite
decorator combining two marks:

1. ``pytest.mark.qdrant_integration`` — the **named** mark, so that
   ``pytest -m qdrant_integration`` selects these tests.
2. ``pytest.mark.skipif(...)`` — the **auto-skip** mark, so that tests are
   silently skipped when Qdrant / Postgres are unreachable without live
   services being a prerequisite for a plain ``pytest`` run.

Both marks are necessary:

- Without the named mark, ``-m qdrant_integration`` finds *no* tests (the
  ``skipif`` variable in each test module is not a marker, it is just a
  decorator that happens to look like one).
- Without the ``skipif``, a plain ``pytest`` run fails on the test collection
  step whenever services are down.

Usage in test modules::

    from conftest import qdrant_integration_mark

    @qdrant_integration_mark
    class TestFoo: ...

The mark is also available as a module-level constant exported from conftest
so that all integration test modules can import from the same place instead
of redeclaring the skip logic locally.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "provider_integration: live embedding provider integration tests "
        "(require Ollama at localhost:11434 or OPENAI_API_KEY). "
        "Skipped by default. Run with: pytest -m provider_integration",
    )
    config.addinivalue_line(
        "markers",
        "qdrant_integration: retrieval integration tests against live Qdrant + Postgres. "
        "Both a named mark (selectable via -m qdrant_integration) and a skipif "
        "(auto-skipped when services are unreachable). "
        "Run with: pytest -m qdrant_integration",
    )
    config.addinivalue_line(
        "markers",
        "pgvector_integration: index-adapter integration tests against a live "
        "PostgreSQL with the pgvector extension. Both a named mark (selectable via "
        "-m pgvector_integration) and a skipif (auto-skipped when Postgres+pgvector "
        "is unreachable). Run with: pytest -m pgvector_integration",
    )


# ---------------------------------------------------------------------------
# Shared qdrant_integration composite mark
# ---------------------------------------------------------------------------


def _qdrant_and_postgres_reachable() -> bool:
    """Return True only when BOTH Qdrant and Postgres are reachable locally.

    Called once at import time (module-level) so the skipif condition is
    evaluated eagerly rather than deferred.  Failures are caught and treated
    as unreachable so that a missing service never causes collection to fail.
    """
    try:
        from qdrant_client import QdrantClient

        c = QdrantClient(url="http://localhost:6333", timeout=2)
        c.get_collections()
    except Exception:
        return False

    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(
            "postgresql+psycopg://finecorpus:finecorpus@localhost:5432/finecorpus",
            connect_args={"connect_timeout": 2},
        )
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return False

    return True


_SERVICES_UP = _qdrant_and_postgres_reachable()

_SKIP_REASON = (
    "Qdrant or Postgres not reachable at localhost — skipping integration tests. "
    "Start services with: "
    "docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d"
)


# Composite mark: named + skipif.
#
# Applying BOTH marks to a class/function is necessary:
#   - pytest.mark.qdrant_integration   → named mark; ``-m qdrant_integration`` selects it.
#   - pytest.mark.skipif(...)          → auto-skip when services are down.
#
# We build the composite as a plain decorator so that applying it once stacks
# both marks in a single expression.  Using ``pytest.mark.X(pytest.mark.Y)``
# does NOT work — mark calls don't forward other marks.  The decorator function
# pattern is the correct idiom.
def qdrant_integration_mark(obj):  # type: ignore[no-untyped-def]
    """Composite decorator: qdrant_integration named mark + services-up skipif.

    Apply to test classes or functions that require live Qdrant + Postgres::

        from conftest import qdrant_integration_mark

        @qdrant_integration_mark
        class TestFoo: ...
    """
    obj = pytest.mark.qdrant_integration(obj)
    obj = pytest.mark.skipif(not _SERVICES_UP, reason=_SKIP_REASON)(obj)
    return obj


# ---------------------------------------------------------------------------
# Shared pgvector_integration composite mark (Phase 7 WU-B)
# ---------------------------------------------------------------------------

# The default compose Postgres image may not ship the pgvector extension; these
# tests skip cleanly when Postgres is unreachable OR ``CREATE EXTENSION vector``
# is unavailable.  The orchestrator runs the overlay with a pgvector-enabled
# image at phase close.
PGVECTOR_DSN = "postgresql://finecorpus:finecorpus@localhost:5432/finecorpus"


def _pgvector_reachable() -> bool:
    """Return True only when Postgres is reachable AND pgvector can be enabled."""
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(PGVECTOR_DSN, connect_timeout=2, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return True
    except Exception:
        return False


_PGVECTOR_UP = _pgvector_reachable()

_PGVECTOR_SKIP_REASON = (
    "PostgreSQL with the pgvector extension not reachable at localhost — skipping "
    "pgvector integration tests. Start a pgvector-enabled Postgres (e.g. the "
    "pgvector/pgvector image) and ensure CREATE EXTENSION vector succeeds."
)


def pgvector_integration_mark(obj):  # type: ignore[no-untyped-def]
    """Composite decorator: pgvector_integration named mark + pgvector-up skipif.

    Apply to test classes/functions that require a live Postgres with pgvector::

        from conftest import pgvector_integration_mark

        @pgvector_integration_mark
        class TestPgVector: ...
    """
    obj = pytest.mark.pgvector_integration(obj)
    obj = pytest.mark.skipif(not _PGVECTOR_UP, reason=_PGVECTOR_SKIP_REASON)(obj)
    return obj
