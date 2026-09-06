"""Sweep candidate enumeration and deterministic corpus sampling (§9.3, M-046, D-07).

This module is **pure**: no I/O, no retrieval, no index or service imports.
It may only import from ``finecorpus.contracts`` and the standard library.

Overview
--------
Configuration sweeps (§9.3) evaluate a bounded set of ``IngestionConfig``
variants (candidates) against a sampled subset of the corpus to find the
best-scoring configuration without evaluating the full Cartesian product.

Two public functions are provided:

``sample_corpus``
    Seeded, deterministic stratified sample of ``InventoryItem`` documents
    across document kinds (media-type buckets, M-046). Returns the full
    corpus when it is already small; otherwise returns a strict subset of
    size ``min(min_docs * sample_factor, corpus_size)``.

``enumerate_candidates``
    Deterministic, budget-capped enumeration of ``IngestionConfig`` variants
    (D-07). Performs a grid-lite/evolutionary search over a bounded parameter
    space. The base/reference config is always candidate index 0 (D-07). All
    returned configs are valid ``IngestionConfig`` objects.

Stratification in ``sample_corpus``
-------------------------------------
Documents are grouped into strata by *media-type family* derived from the
``InventoryItem.media_type`` MIME string:

* ``pdf``        — application/pdf
* ``html``       — text/html, application/xhtml+xml
* ``office``     — Word, Excel, PowerPoint (application/vnd.*, .docx, .xlsx …)
* ``spreadsheet`` — text/csv, application/vnd.*sheet*, application/vnd.*calc*
* ``text``       — text/plain, text/markdown, text/x-*
* ``other``      — everything else (images, audio, CAD, …)

Within each stratum the items are sorted deterministically by ``document_id``
before shuffling with the provided seed, so the sample is fully reproducible
given the same (documents, seed) inputs.

Candidate parameter space in ``enumerate_candidates``
-------------------------------------------------------
The following axes are explored (grid-lite):

* ``chunking.strategy`` ∈ {recursive_char, structure_aware}
* ``chunking.max_tokens`` ∈ {256, 512, 1024}   (3 values)
* ``chunking.overlap_tokens`` as a fraction of max_tokens ∈ {1/16, 1/8}  (2 values)
* ``retrieval_defaults.strategy`` ∈ {dense, hybrid}
* ``embedding`` — held fixed from the base config (not varied; changing the
  embedding model invalidates the index and is a separate sweep concern)

Cartesian product: 2 × 3 × 2 × 2 = 24 combinations, well inside the
default budget of 40.  The base config is prepended as candidate 0, giving
up to 25 candidates before de-duplication and budget capping.

Determinism guarantee
----------------------
Given the same ``seed`` and ``base_config``, ``enumerate_candidates`` always
returns the same list in the same order (the grid is sorted before any
random selection, and ``random.Random(seed)`` is used for any shuffle step
needed when the grid exceeds the budget).
"""

from __future__ import annotations

import copy
import itertools
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass

from finecorpus.contracts.ingestion_config import (
    ChunkingConfig,
    ChunkingStrategy,
    IngestionConfig,
    RetrievalStrategy,
)
from finecorpus.contracts.inventory import InventoryItem

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Media-type → stratum mapping
# ---------------------------------------------------------------------------

_STRATUM_PDF = "pdf"
_STRATUM_HTML = "html"
_STRATUM_OFFICE = "office"
_STRATUM_SPREADSHEET = "spreadsheet"
_STRATUM_TEXT = "text"
_STRATUM_OTHER = "other"


def _media_type_stratum(media_type: str) -> str:
    """Map a MIME media-type string to a stratum label for stratified sampling.

    The mapping is intentionally coarse-grained: the sweep does not need
    fine-grained document distinctions, only enough differentiation to ensure
    that every major document *kind* is represented in the sample.

    Args:
        media_type: MIME type string (e.g. ``"application/pdf"``).

    Returns:
        One of the six stratum labels: pdf, html, office, spreadsheet,
        text, other.
    """
    mt = media_type.lower().strip()

    if mt == "application/pdf":
        return _STRATUM_PDF

    if mt in {"text/html", "application/xhtml+xml"}:
        return _STRATUM_HTML

    # Spreadsheets: check before generic office so csv/calc variants match first
    if (
        mt in {"text/csv", "application/csv"}
        or "sheet" in mt
        or "calc" in mt
        or mt
        in {
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
    ):
        return _STRATUM_SPREADSHEET

    # Generic Office documents (Word, PowerPoint, LibreOffice …)
    if mt.startswith("application/vnd.") or mt in {
        "application/msword",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }:
        return _STRATUM_OFFICE

    # Plain text variants
    if mt.startswith("text/"):
        return _STRATUM_TEXT

    return _STRATUM_OTHER


# ---------------------------------------------------------------------------
# sample_corpus  (M-046)
# ---------------------------------------------------------------------------


def sample_corpus(
    documents: Sequence[InventoryItem],
    *,
    min_docs: int,
    sample_factor: int,
    seed: int,
) -> list[InventoryItem]:
    """Return a seeded deterministic stratified sample of corpus documents.

    Stratification ensures every media-type family present in the corpus is
    represented in the sample proportionally.  The sample is fully reproducible
    given the same ``(documents, seed)`` pair.

    Sampling algorithm
    ------------------
    1. Compute target sample size ``n = min(min_docs * sample_factor, N)``
       where ``N = len(documents)``.  When ``N <= n`` the full corpus is
       returned (no sampling needed; order is still deterministic).
    2. Partition documents into strata by media-type family.
    3. Sort each stratum by ``document_id`` for a deterministic base order.
    4. Allocate stratum quotas proportionally to stratum size, with at least
       one document per non-empty stratum (up to the total quota).
    5. Shuffle each stratum with ``random.Random(seed)`` and take the quota.
    6. Return the concatenated sample sorted by ``document_id`` for a stable
       caller-side order.

    Args:
        documents:     Full corpus as a sequence of ``InventoryItem`` objects.
        min_docs:      Minimum document threshold (``assessment.sweep_min_corpus_docs``).
        sample_factor: Multiplier applied to ``min_docs`` to derive sample size
                       (``assessment.sweep_sample_factor``).
        seed:          Integer seed for the PRNG — same seed ⟹ same sample.

    Returns:
        A list of ``InventoryItem`` objects.  When ``len(documents)`` exceeds
        the derived sample size the returned list is a strict subset; otherwise
        it is a copy of the full corpus in deterministic order.

    Reproducibility:
        ``sample_corpus(docs, min_docs=m, sample_factor=f, seed=s)`` is
        idempotent: repeated calls with the same arguments return the same list
        (same items, same order).
    """
    docs = list(documents)
    n_total = len(docs)
    target = min(min_docs * sample_factor, n_total)

    # Small corpus or corpus already within target — return full set sorted by id.
    if n_total <= target:
        return sorted(docs, key=lambda d: d.document_id)

    # --- Stratify ---
    strata: dict[str, list[InventoryItem]] = {}
    for doc in docs:
        stratum = _media_type_stratum(doc.media_type)
        strata.setdefault(stratum, []).append(doc)

    # Sort each stratum deterministically before shuffling
    for key in strata:
        strata[key].sort(key=lambda d: d.document_id)

    rng = random.Random(seed)

    # --- Allocate quota per stratum (proportional, floor 1) ---
    stratum_names = sorted(strata.keys())  # stable iteration order
    stratum_sizes = {k: len(strata[k]) for k in stratum_names}
    total_sized = sum(stratum_sizes.values())
    n_active_strata = len(stratum_names)

    # --- Shuffle each stratum with the seeded RNG before quota selection ---
    shuffled_strata: dict[str, list[InventoryItem]] = {}
    for k in stratum_names:
        items = list(strata[k])
        rng.shuffle(items)
        shuffled_strata[k] = items

    if n_active_strata > target:
        # Special case: more active strata than the target allows.
        # Applying floor-1 per stratum would exceed the target, violating the
        # strict-subset invariant.  Instead, do a round-robin one-per-stratum
        # pass (deterministic by stratum_names sort order) until we reach the
        # target.  The stratum order is stable (sorted), and within each
        # stratum we take the first item from the already-shuffled list.
        floor_quotas: dict[str, int] = {k: 0 for k in stratum_names}
        slots_remaining = target
        for k in stratum_names:
            if slots_remaining <= 0:
                break
            floor_quotas[k] = 1
            slots_remaining -= 1
    else:
        # Normal path: proportional allocation with floor 1 per stratum.
        # Compute raw float quotas, then round to integers preserving total=target.
        raw_quotas: dict[str, float] = {
            k: (stratum_sizes[k] / total_sized) * target for k in stratum_names
        }
        # Integer floor quotas (at least 1 per stratum)
        floor_quotas = {k: max(1, int(raw_quotas[k])) for k in stratum_names}
        # Cap each stratum quota at its actual size
        floor_quotas = {k: min(floor_quotas[k], stratum_sizes[k]) for k in stratum_names}

        allocated = sum(floor_quotas.values())
        remainder = target - allocated

        # Distribute remaining slots to strata with the largest fractional parts
        if remainder > 0:
            fractional_parts = sorted(
                stratum_names,
                key=lambda k: -(raw_quotas[k] - int(raw_quotas[k])),
            )
            for k in fractional_parts:
                if remainder <= 0:
                    break
                slack = stratum_sizes[k] - floor_quotas[k]
                if slack > 0:
                    floor_quotas[k] += 1
                    remainder -= 1

    # --- Sample from each stratum using the pre-shuffled lists ---
    sampled: list[InventoryItem] = []
    for k in stratum_names:
        sampled.extend(shuffled_strata[k][: floor_quotas[k]])

    # Return in deterministic document_id order
    sampled.sort(key=lambda d: d.document_id)
    return sampled


# ---------------------------------------------------------------------------
# SweepCandidate  (optional dataclass — see module docstring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SweepCandidate:
    """A sweep candidate: a stable label paired with its ``IngestionConfig``.

    Attributes:
        candidate_id: Zero-based index within the candidate list.  Candidate 0
            is always the reference/base config (D-07).
        label:        Human-readable description of the axes that differ from
            the base config (e.g. ``"base"`` for the reference;
            ``"recursive_char/512t/64ov/hybrid"`` for a variant).
        config:       The full ``IngestionConfig`` for this candidate.
    """

    candidate_id: int
    label: str
    config: IngestionConfig


# ---------------------------------------------------------------------------
# Parameter-space grid
# ---------------------------------------------------------------------------

# Chunking strategy axis — only splitter types appropriate for general prose;
# table_atomic and code_syntax are class-specific and not swept globally.
_SWEEP_STRATEGIES: list[ChunkingStrategy] = [
    ChunkingStrategy.recursive_char,
    ChunkingStrategy.structure_aware,
]

# Max-tokens axis (token counts; whitespace-word proxy §6.4)
_SWEEP_MAX_TOKENS: list[int] = [256, 512, 1024]

# Overlap fraction of max_tokens
_SWEEP_OVERLAP_FRACTIONS: list[float] = [1 / 16, 1 / 8]

# Retrieval strategy axis
_SWEEP_RETRIEVAL_STRATEGIES: list[RetrievalStrategy] = [
    RetrievalStrategy.dense,
    RetrievalStrategy.hybrid,
]


def _derive_overlap(max_tokens: int, fraction: float) -> int:
    """Compute overlap_tokens = max(16, int(max_tokens * fraction))."""
    return max(16, int(max_tokens * fraction))


def _make_label(
    strategy: ChunkingStrategy,
    max_tokens: int,
    overlap: int,
    retrieval: RetrievalStrategy,
) -> str:
    """Build a human-readable candidate label for diagnostics."""
    return f"{strategy.value}/{max_tokens}t/{overlap}ov/{retrieval.value}"


def _apply_chunking_variant(
    base_config: IngestionConfig,
    strategy: ChunkingStrategy,
    max_tokens: int,
    overlap_tokens: int,
    retrieval: RetrievalStrategy,
) -> IngestionConfig:
    """Return a deep-copied ``IngestionConfig`` with chunking/retrieval axes varied.

    Only ``default_rule.chunking``, ``class_rules[*].chunking``, and
    ``retrieval_defaults.strategy`` are modified.  The embedding config and
    all other fields are held fixed from the base config so that a single
    candidate does not simultaneously vary multiple dependent axes.

    The returned config is re-validated by Pydantic (``model_validate`` round-
    trip via ``model_dump`` + constructor) to ensure structural invariants hold.
    """
    raw = base_config.model_dump(mode="python")

    def _patch_chunking(rule_dict: dict) -> None:  # type: ignore[type-arg]
        c = rule_dict["chunking"]
        c["strategy"] = strategy.value
        c["max_tokens"] = max_tokens
        c["overlap_tokens"] = overlap_tokens
        # Preserve respect_headings, atomic_rows, repeat_headers_on_split,
        # split_boundaries from the base — only the sweep axes change.

    _patch_chunking(raw["default_rule"])
    for rule in raw["class_rules"]:
        _patch_chunking(rule)

    raw["retrieval_defaults"]["strategy"] = retrieval.value
    for rule in raw["class_rules"]:
        rule["retrieval_treatment"]["strategy"] = retrieval.value
    raw["default_rule"]["retrieval_treatment"]["strategy"] = retrieval.value

    return IngestionConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# enumerate_candidates  (D-07)
# ---------------------------------------------------------------------------


def enumerate_candidates(
    base_config: IngestionConfig,
    *,
    budget: int,
    seed: int,
) -> list[SweepCandidate]:
    """Enumerate a deterministic, budget-capped list of sweep candidates (D-07).

    The reference/base config is always candidate 0.  Remaining candidates
    are drawn from a grid-lite parameter space (see module docstring for the
    full axis definitions).  The total number of candidates never exceeds
    ``budget``.

    Parameter space
    ---------------
    * chunking.strategy ∈ {recursive_char, structure_aware}
    * chunking.max_tokens ∈ {256, 512, 1024}
    * chunking.overlap_tokens derived as max(16, int(max_tokens × fraction))
      for fraction ∈ {1/16, 1/8}
    * retrieval_defaults.strategy ∈ {dense, hybrid}
    * embedding — held fixed (not varied)

    Grid size: 2 × 3 × 2 × 2 = 24 combinations.  Adding the base config
    gives up to 25 candidates before de-duplication and budget capping.

    De-duplication: a grid point whose parameter values are identical to the
    base config is skipped (it is already candidate 0).

    Budget enforcement: if the grid + base exceed ``budget``, the excess grid
    points are removed by a seeded shuffle of the non-base points before
    truncation, ensuring deterministic reduction given the same seed.

    Determinism: given the same ``(base_config, budget, seed)`` triple the
    returned list is always identical.  Pure — no I/O, no side effects.

    Args:
        base_config: The reference ``IngestionConfig``.  Always candidate 0.
        budget:      Maximum number of candidates to return (≥ 1).
        seed:        Integer seed for reproducible ordering/truncation.

    Returns:
        A list of ``SweepCandidate`` objects of length ≤ ``budget``.
        ``result[0].config`` is structurally equal to ``base_config``.
        ``result[0].label == "base"``.
        All configs are valid ``IngestionConfig`` instances.

    Raises:
        ValueError: If ``budget < 1``.
    """
    if budget < 1:
        raise ValueError(f"budget must be >= 1, got {budget}")

    # Candidate 0: the base/reference config (always present)
    base_candidate = SweepCandidate(
        candidate_id=0,
        label="base",
        config=copy.deepcopy(base_config),
    )

    if budget == 1:
        return [base_candidate]

    # Extract base config axes for de-duplication
    base_default_chunking: ChunkingConfig = base_config.default_rule.chunking
    base_strategy = base_default_chunking.strategy
    base_max_tokens = base_default_chunking.max_tokens
    base_overlap = base_default_chunking.overlap_tokens
    base_retrieval = base_config.retrieval_defaults.strategy

    # Build the full grid (sorted for determinism)
    grid_points = list(
        itertools.product(
            _SWEEP_STRATEGIES,
            _SWEEP_MAX_TOKENS,
            _SWEEP_OVERLAP_FRACTIONS,
            _SWEEP_RETRIEVAL_STRATEGIES,
        )
    )
    # Sort by tuple value for a fully deterministic canonical order
    grid_points.sort(key=lambda p: (p[0].value, p[1], p[2], p[3].value))

    # Build variant candidates (skip duplicates of the base)
    variants: list[SweepCandidate] = []
    for strategy, max_tokens, frac, retrieval in grid_points:
        overlap = _derive_overlap(max_tokens, frac)
        # Skip if this point is parameter-identical to the base
        if (
            strategy == base_strategy
            and max_tokens == base_max_tokens
            and overlap == base_overlap
            and retrieval == base_retrieval
        ):
            continue
        label = _make_label(strategy, max_tokens, overlap, retrieval)
        try:
            config = _apply_chunking_variant(base_config, strategy, max_tokens, overlap, retrieval)
        except Exception as exc:  # noqa: BLE001 — one bad variant must not abort enumeration
            _log.warning(
                "enumerate_candidates: skipping grid point %r — failed to construct config: %s",
                label,
                exc,
            )
            continue
        variants.append(
            SweepCandidate(
                candidate_id=len(variants) + 1,  # temporary; reassigned below
                label=label,
                config=config,
            )
        )

    # Budget enforcement: budget - 1 slots remain after the base
    remaining_budget = budget - 1
    if len(variants) > remaining_budget:
        rng = random.Random(seed)
        rng.shuffle(variants)
        variants = variants[:remaining_budget]
        # Re-sort for stable output order (by label) after shuffle-truncation
        variants.sort(key=lambda c: c.label)

    # Re-assign candidate_id sequentially
    final: list[SweepCandidate] = [base_candidate]
    for idx, v in enumerate(variants, start=1):
        final.append(SweepCandidate(candidate_id=idx, label=v.label, config=v.config))

    return final


__all__ = [
    "sample_corpus",
    "SweepCandidate",
    "enumerate_candidates",
]
