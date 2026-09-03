"""Unit tests for Chunk invariants.

See docs/contracts/chunk.md and docs/contracts/README.md#provenance-block-provenance.

Tests:
- A Chunk cannot be constructed without provenance (ValidationError).
- A Chunk cannot be constructed without tenancy (ValidationError).
- A valid Chunk with full provenance can be constructed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from finecorpus.contracts.chunk import Augmentation, Chunk, EmbeddingRef
from finecorpus.contracts.shared.blocks import (
    LocatorKind,
    PermissionFidelity,
    PermissionMode,
    PermissionSource,
    Provenance,
    SalienceSignal,
    SalienceSignalKind,
    SalienceTier,
    SegmentType,
    SourceLocation,
    TenancyBlock,
    TrustLevel,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_tenancy() -> TenancyBlock:
    return TenancyBlock(
        workspace_id="01JWSPACE001",
        kb_id="01JKB000001",
        permission_mode=PermissionMode.public_to_kb,
        permission_principals=[],
        permission_source=PermissionSource.platform,
        permission_fidelity=PermissionFidelity.authoritative,
    )


def make_source_location() -> SourceLocation:
    return SourceLocation(
        locator_kind=LocatorKind.page,
        page_start=22,
        page_end=23,
        char_start=48200,
        char_end=51900,
    )


def make_provenance() -> Provenance:
    return Provenance(
        source_document_id="01JMANUAL0001",
        source_document_version=(
            "9f2c8b1e77a4d0c3e5b19a24d8f6c0b2e4a7913d5c8f0a2b4d6e8f1a3c5b7d9e0"
        ),
        source_location=make_source_location(),
        structural_path=["3. Installation", "3.2 Lubrication Procedure"],
        transformations=[],
        confidence=1.0,
        ocr_confidence=None,
        segment_type=SegmentType.prose,
        salience_tier=SalienceTier.primary,
        salience_basis=SalienceSignalKind.segment_type_prior,
        salience_signals=[
            SalienceSignal(
                kind=SalienceSignalKind.segment_type_prior,
                implied_tier=SalienceTier.primary,
                won=True,
            )
        ],
        language="en",
        injection_suspicion=0.0,
        invisible_content_flags=[],
        sensitivity_flags=[],
        trust_level=TrustLevel.untrusted_ingested,
    )


def make_augmentation() -> Augmentation:
    return Augmentation(
        parent_breadcrumb="3. Installation > 3.2 Lubrication Procedure",
        table_description=None,
        class_context=None,
        generated_by=["openai/gpt-4o-mini"],
    )


def make_embedding_ref() -> EmbeddingRef:
    return EmbeddingRef(
        provider="openai",
        model="text-embedding-3-large",
        dimensions=3072,
        config_version="c41d09f7b2e3a5" + "0" * 50,
    )


def make_valid_chunk(**overrides) -> dict:
    """Return a dict of valid Chunk constructor kwargs."""
    base = {
        "schema_version": "1.0.0",
        "chunk_id": "chk_k7m2p9x4rq8h3n6v0w1t5s2",
        "tenancy": make_tenancy(),
        "provenance": make_provenance(),
        "text": "Apply grease to all bearing surfaces before assembly.",
        "augmentation": make_augmentation(),
        "embedding_input": "3. Installation > 3.2 Lubrication Procedure\n\nApply grease...",
        "embedding_ref": make_embedding_ref(),
        "chunk_index": 0,
        "token_count": 10,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Invariant: provenance is required
# ---------------------------------------------------------------------------


class TestChunkProvenanceRequired:
    """A Chunk cannot be constructed without provenance (§8, §12)."""

    def test_missing_provenance_raises_validation_error(self) -> None:
        """Constructing a Chunk without provenance MUST raise ValidationError."""
        kwargs = make_valid_chunk()
        del kwargs["provenance"]
        with pytest.raises(ValidationError) as exc_info:
            Chunk(**kwargs)
        errors = exc_info.value.errors()
        fields = [e["loc"][0] for e in errors]
        assert "provenance" in fields, "ValidationError should flag 'provenance' as missing"

    def test_provenance_none_raises_validation_error(self) -> None:
        """Passing provenance=None to Chunk MUST raise ValidationError."""
        kwargs = make_valid_chunk()
        kwargs["provenance"] = None  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            Chunk(**kwargs)

    def test_valid_chunk_with_provenance_constructs(self) -> None:
        """A Chunk with all required fields (including provenance) constructs successfully."""
        chunk = Chunk(**make_valid_chunk())
        assert chunk.provenance is not None
        assert chunk.provenance.source_document_id == "01JMANUAL0001"


# ---------------------------------------------------------------------------
# Invariant: tenancy is required
# ---------------------------------------------------------------------------


class TestChunkTenancyRequired:
    """A Chunk cannot be constructed without tenancy (Phase 0 MUST, §19)."""

    def test_missing_tenancy_raises_validation_error(self) -> None:
        kwargs = make_valid_chunk()
        del kwargs["tenancy"]
        with pytest.raises(ValidationError) as exc_info:
            Chunk(**kwargs)
        errors = exc_info.value.errors()
        fields = [e["loc"][0] for e in errors]
        assert "tenancy" in fields

    def test_tenancy_none_raises_validation_error(self) -> None:
        kwargs = make_valid_chunk()
        kwargs["tenancy"] = None  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            Chunk(**kwargs)
