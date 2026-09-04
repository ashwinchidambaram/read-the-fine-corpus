"""Shared test helpers for retrieval unit tests.

Provides:
- FakeAdapter: in-memory IndexAdapter (search returns seeded payload dicts).
- FakeSession / FakeAliasRecord: minimal SQLAlchemy session simulacrum.
- make_provenance_payload: build a fully-valid §8 provenance dict.
- make_chunk_payload: wrap provenance in a full Qdrant point payload.
- make_alias_record: build a FakeAliasRecord for a given model identity.
"""

from __future__ import annotations

from typing import Any

from finecorpus.index.adapter import (
    AliasNotFoundError,
    BackendCapabilities,
    CollectionInfo,
    IndexError,
    SearchResult,
    alias_name,
    collection_name,
)

# ---------------------------------------------------------------------------
# FakeAliasRecord (mirrors AliasRecord ORM shape without DB)
# ---------------------------------------------------------------------------


class FakeAliasRecord:
    """Minimal control-plane alias record for unit tests (no DB required)."""

    def __init__(
        self,
        alias: str,
        kb_id: str,
        workspace_id: str = "ws-test",
        collection: str | None = None,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
        config_version: str | None = None,
        promoted_at: Any = None,
    ) -> None:
        self.alias = alias
        self.kb_id = kb_id
        self.workspace_id = workspace_id
        self.collection_name = collection
        self.build_id = 1 if collection else None
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.config_version = config_version
        self.promoted_at = promoted_at
        self.previous_collection: str | None = None
        self.previous_embedding_provider: str | None = None
        self.previous_embedding_model: str | None = None
        self.previous_embedding_dimensions: int | None = None
        self.previous_config_version: str | None = None


# ---------------------------------------------------------------------------
# FakeSession
# ---------------------------------------------------------------------------


class FakeSession:
    """Minimal SQLAlchemy Session simulacrum for unit tests.

    Stores FakeAliasRecord objects by alias key.
    The context-manager protocol yields self so it can be used as both a
    plain session and as a session_factory context manager.
    """

    def __init__(self, records: dict[str, FakeAliasRecord] | None = None) -> None:
        self._records: dict[str, FakeAliasRecord] = dict(records or {})
        self.committed = 0

    def add(self, record: FakeAliasRecord) -> None:
        self._records[record.alias] = record

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    # AliasRepository.get() calls session.execute(select(...)).scalar_one_or_none()
    # We patch AliasRepository directly in tests, so this is for compatibility.
    def execute(self, stmt: Any) -> Any:
        raise NotImplementedError("Use FakeAliasRepository instead")


# ---------------------------------------------------------------------------
# FakeAliasRepository — patches AliasRepository for unit tests
# ---------------------------------------------------------------------------


class FakeAliasRepository:
    """Replaces AliasRepository in unit tests without a real DB session."""

    def __init__(self, records: dict[str, FakeAliasRecord]) -> None:
        self._records = records

    def get(self, alias: str) -> FakeAliasRecord | None:
        return self._records.get(alias)

    def get_by_kb(self, kb_id: str) -> FakeAliasRecord | None:
        for r in self._records.values():
            if r.kb_id == kb_id:
                return r
        return None


# ---------------------------------------------------------------------------
# FakeAdapter
# ---------------------------------------------------------------------------


class FakeAdapter:
    """In-memory IndexAdapter for retrieval unit tests.

    Supports seeding point payloads and optional failure injection.
    """

    def __init__(
        self,
        *,
        fail_on_search: bool = False,
        fail_alias_not_found: bool = False,
    ) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self._fail_on_search = fail_on_search
        self._fail_alias_not_found = fail_alias_not_found
        self.search_call_count = 0

    def seed_collection(
        self,
        alias: str,
        coll: str,
        points: list[dict[str, Any]],
    ) -> None:
        """Seed an alias → collection mapping with pre-built point payloads."""
        self.aliases[alias] = coll
        self.collections[coll] = {"points": points}

    # --- IndexAdapter interface ---

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()

    def create_collection(
        self, kb_id: str, build_id: int, dimensions: int, metadata: dict | None = None
    ) -> str:
        name = collection_name(kb_id, build_id)
        self.collections[name] = {"points": [], "metadata": metadata or {}}
        return name

    def drop_collection(self, collection: str) -> None:
        self.collections.pop(collection, None)

    def get_collection_info(self, collection: str) -> CollectionInfo:
        c = self.collections[collection]
        return CollectionInfo(
            name=collection,
            vector_size=64,
            point_count=len(c.get("points", [])),
            metadata=c.get("metadata", {}),
        )

    def collection_exists(self, collection: str) -> bool:
        return collection in self.collections

    def upsert_points(self, collection: str, points: list[dict[str, Any]]) -> None:
        self.collections.setdefault(collection, {"points": []})["points"].extend(points)

    def delete_by_document(self, collection: str, source_document_id: str) -> int:
        pts = self.collections.get(collection, {}).get("points", [])
        before = len(pts)
        self.collections[collection]["points"] = [
            p
            for p in pts
            if p.get("payload", {}).get("provenance", {}).get("source_document_id")
            != source_document_id
        ]
        return before - len(self.collections[collection]["points"])

    def set_collection_metadata(self, collection: str, metadata: dict[str, Any]) -> None:
        self.collections.setdefault(collection, {}).update({"metadata": metadata})

    def get_collection_metadata(self, collection: str) -> dict[str, Any]:
        return dict(self.collections.get(collection, {}).get("metadata", {}))

    def create_alias(self, alias: str, collection: str) -> None:
        self.aliases[alias] = collection

    def retarget_alias(self, alias: str, new_collection: str) -> None:
        if new_collection not in self.collections:
            raise CollectionInfo
        self.aliases[alias] = new_collection

    def resolve_alias(self, alias: str) -> str | None:
        return self.aliases.get(alias)

    def alias_exists(self, alias: str) -> bool:
        return alias in self.aliases

    def delete_alias(self, alias: str) -> None:
        self.aliases.pop(alias, None)

    @staticmethod
    def _matches_filter(payload: dict[str, Any], payload_filter: dict[str, Any]) -> bool:
        """Return True if the point payload satisfies all exact-match filter conditions.

        The filter dict uses dotted key paths (e.g. "tenancy.kb_id") mapped to
        expected string values.  A missing key or a value mismatch causes the
        point to be excluded from results — matching real Qdrant must-clause
        semantics used by the retrieval service.

        This enforces tenancy isolation in the FakeAdapter so tests that seed
        multi-tenant data cannot receive cross-tenant results (§18.3 test 2).
        """
        for dotted_key, expected in payload_filter.items():
            parts = dotted_key.split(".")
            node: Any = payload
            for part in parts:
                if not isinstance(node, dict):
                    return False
                node = node.get(part)
            if node != expected:
                return False
        return True

    def search(
        self,
        alias: str,
        query_vector: list[float],
        top_k: int = 10,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        self.search_call_count += 1

        if self._fail_on_search:
            raise IndexError(f"FakeAdapter: configured to fail on search (alias='{alias}').")

        if self._fail_alias_not_found:
            raise AliasNotFoundError(f"FakeAdapter: alias '{alias}' not found.")

        coll = self.aliases.get(alias)
        if coll is None:
            return []

        points = self.collections.get(coll, {}).get("points", [])
        results: list[SearchResult] = []
        for pt in points:
            pt_payload = pt.get("payload", {})
            if payload_filter and not self._matches_filter(pt_payload, payload_filter):
                continue
            results.append(
                SearchResult(
                    point_id=str(pt.get("id", "")),
                    chunk_id=pt_payload.get("chunk_id", "chk_fake"),
                    score=float(pt.get("score", 1.0)),
                    payload=pt_payload,
                )
            )
            if len(results) >= top_k:
                break
        return results

    def count_points(self, collection: str) -> int:
        return len(self.collections.get(collection, {}).get("points", []))

    def list_collections(self) -> list[str]:
        return list(self.collections.keys())

    def list_aliases(self) -> list[Any]:
        from finecorpus.index.adapter import AliasRecord as AdapterAliasRecord

        return [
            AdapterAliasRecord(alias_name=a, collection_name=c) for a, c in self.aliases.items()
        ]


# ---------------------------------------------------------------------------
# Payload / provenance builders
# ---------------------------------------------------------------------------


def make_provenance_payload(
    *,
    source_document_id: str = "doc-001",
    source_document_version: str = "v1",
    structural_path: list[str] | None = None,
    language: str = "en",
    confidence: float = 0.95,
    segment_type: str = "prose",
    salience_tier: str = "primary",
) -> dict[str, Any]:
    """Build a fully-valid §8 provenance dict (as stored in Qdrant payload)."""
    return {
        "source_document_id": source_document_id,
        "source_document_version": source_document_version,
        "source_location": {
            "locator_kind": "char_range",
            "char_start": 0,
            "char_end": 100,
        },
        "structural_path": structural_path or ["Section 1"],
        "transformations": [],
        "confidence": confidence,
        "ocr_confidence": None,
        "segment_type": segment_type,
        "salience_tier": salience_tier,
        "salience_basis": "default",
        "salience_signals": [
            {
                "kind": "default",
                "implied_tier": "supporting",
                "won": True,
                "detail": "no signal fired",
            }
        ],
        "language": language,
        "injection_suspicion": 0.0,
        "invisible_content_flags": [],
        "sensitivity_flags": [],
        "trust_level": "untrusted_ingested",
    }


def make_chunk_payload(
    *,
    chunk_id: str = "chk_001",
    text: str = "Test chunk text.",
    kb_id: str = "kb-test",
    score: float = 0.9,
    source_document_id: str = "doc-001",
) -> dict[str, Any]:
    """Build a Qdrant point dict with a full payload (ready for FakeAdapter.seed_collection)."""
    return {
        "id": chunk_id,
        "score": score,
        "payload": {
            "chunk_id": chunk_id,
            "text": text,
            "tenancy": {
                "kb_id": kb_id,
                "workspace_id": "ws-test",
            },
            "provenance": make_provenance_payload(
                source_document_id=source_document_id,
            ),
        },
    }


def make_alias_record(
    kb_id: str,
    *,
    model_id: str = "fake-embed-v1",
    dimensions: int = 64,
    provider_id: str = "fake",
    collection: str | None = None,
) -> FakeAliasRecord:
    """Build a FakeAliasRecord with a promoted collection."""
    alias = alias_name(kb_id)
    coll = collection or f"rtfc_{kb_id.replace('-', '').lower()}_00000001"
    return FakeAliasRecord(
        alias=alias,
        kb_id=kb_id,
        workspace_id="ws-test",
        collection=coll,
        embedding_provider=provider_id,
        embedding_model=model_id,
        embedding_dimensions=dimensions,
        config_version="cfgv1",
    )
