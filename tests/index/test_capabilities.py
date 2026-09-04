"""Unit tests for the capability surface and UnsupportedCapabilityError (M-009/M-010)."""

from __future__ import annotations

import pytest

from finecorpus.index.adapter import BackendCapabilities, UnsupportedCapabilityError
from finecorpus.index.qdrant.backend import QdrantAdapter


class TestBackendCapabilities:
    def test_phase1_dense_supported(self) -> None:
        caps = BackendCapabilities()
        assert caps.dense_search is True

    def test_phase1_hybrid_unsupported(self) -> None:
        """Hybrid search is declared unsupported in Phase 1 (M-009/M-010)."""
        caps = BackendCapabilities()
        assert caps.hybrid_search is False

    def test_phase1_payload_filtering_supported(self) -> None:
        caps = BackendCapabilities()
        assert caps.payload_filtering is True

    def test_phase1_atomic_alias_swap_supported(self) -> None:
        caps = BackendCapabilities()
        assert caps.atomic_alias_swap is True

    def test_hybrid_unavailable_reason_is_non_empty(self) -> None:
        """The reason must be non-empty so it can be surfaced to the user."""
        caps = BackendCapabilities()
        assert caps.hybrid_search_unavailable_reason
        assert len(caps.hybrid_search_unavailable_reason) > 10


class TestUnsupportedCapabilityError:
    def test_error_carries_capability_and_backend(self) -> None:
        err = UnsupportedCapabilityError("hybrid_search", "qdrant")
        assert err.capability == "hybrid_search"
        assert err.backend == "qdrant"

    def test_error_message_names_capability(self) -> None:
        err = UnsupportedCapabilityError("hybrid_search", "qdrant", "Phase 1 only")
        assert "hybrid_search" in str(err)
        assert "qdrant" in str(err)

    def test_error_is_index_error_subclass(self) -> None:
        from finecorpus.index.adapter import IndexError as IdxError

        err = UnsupportedCapabilityError("hybrid_search", "qdrant")
        assert isinstance(err, IdxError)


class TestQdrantAdapterCapabilities:
    """Tests that do not require a live Qdrant (just the adapter class)."""

    def test_qdrant_capabilities_returned(self) -> None:
        # We can instantiate with a non-existent URL for capability tests
        adapter = QdrantAdapter(url="http://localhost:19999")
        caps = adapter.capabilities()
        assert isinstance(caps, BackendCapabilities)

    def test_qdrant_hybrid_search_raises_unsupported(self) -> None:
        """search_hybrid must raise UnsupportedCapabilityError, never silently degrade."""
        adapter = QdrantAdapter(url="http://localhost:19999")
        with pytest.raises(UnsupportedCapabilityError) as exc_info:
            adapter.search_hybrid(
                alias="rtfc_test",
                query_vector=[0.1, 0.2],
                top_k=5,
            )
        err = exc_info.value
        assert err.capability == "hybrid_search"
        # The reason must be in the error message (M-010: surface, not silent)
        assert "hybrid" in str(err).lower()

    def test_qdrant_hybrid_reason_in_error(self) -> None:
        """The unavailable reason from capabilities() must appear in the error."""
        adapter = QdrantAdapter(url="http://localhost:19999")
        caps = adapter.capabilities()
        with pytest.raises(UnsupportedCapabilityError) as exc_info:
            adapter.search_hybrid("alias", [0.1], top_k=1)
        # The detail from capabilities() is included in the error
        assert caps.hybrid_search_unavailable_reason[:20] in str(exc_info.value)
