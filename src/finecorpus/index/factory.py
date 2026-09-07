"""Backend-selection seam for the vector index (§4.4, Phase 7 WU-B).

``build_adapter_from_config`` selects the concrete ``IndexAdapter`` from
``config.storage.index_backend`` — ``qdrant`` (default; unchanged behaviour for
existing deployments) or ``pgvector`` (index inside PostgreSQL, reusing
``config.storage.postgres``).

The config object is accepted as a parameter (duck-typed) rather than imported,
so this module keeps the ``finecorpus.index`` layer position (it does not import
upward into ``finecorpus.config``; callers pass the already-loaded config).
"""

from __future__ import annotations

from typing import Any

from finecorpus.index.adapter import IndexAdapter, IndexError


def build_adapter_from_config(config: Any) -> IndexAdapter:
    """Construct the configured ``IndexAdapter`` from a loaded config object.

    Args:
        config: A loaded ``Config`` (duck-typed): must expose
            ``storage.index_backend`` and the relevant ``storage.qdrant`` /
            ``storage.postgres`` settings.

    Returns:
        A ready ``IndexAdapter`` (``QdrantAdapter`` or ``PgVectorAdapter``).

    Raises:
        IndexError: If the selected backend is misconfigured (e.g. pgvector
            selected without a ``storage.postgres.url``) or unknown.
    """
    backend = str(getattr(config.storage, "index_backend", "qdrant"))

    if backend == "pgvector":
        dsn = config.storage.postgres.url
        if not dsn:
            raise IndexError(
                "storage.index_backend=pgvector requires storage.postgres.url to be set."
            )
        from finecorpus.index.pgvector import PgVectorAdapter

        return PgVectorAdapter(dsn=dsn)

    if backend == "qdrant":
        from finecorpus.index.qdrant import QdrantAdapter

        return QdrantAdapter(
            url=config.storage.qdrant.url,
            api_key=config.storage.qdrant.api_key,
        )

    raise IndexError(f"Unknown storage.index_backend: {backend!r}")


__all__ = ["build_adapter_from_config"]
