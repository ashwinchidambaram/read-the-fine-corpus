"""Corpus-level passes — near-duplicate clustering and boilerplate detection.

These passes run after **all** documents have been parsed by AssessStage.
They are corpus-level because they need the full text of every document
simultaneously; per-document passes cannot compute pairwise similarity or
count block prevalence across the corpus.

Placement in the pipeline
--------------------------
Near-dup clustering logically belongs to Collect (§6.1) but Collect is
pre-parse and has no extracted text.  The orchestrator-approved design
decision is: clustering runs as a **corpus-level step between Assess and
Decompose**, implemented here, invoked by AssessStage after all per-document
ParseResults are assembled.

Algorithm overview
------------------

Near-duplicate clustering (§6.1):

  Word 5-gram shingle sets are computed for each document's extracted text.
  Pairwise Jaccard similarity (|A ∩ B| / |A ∪ B|) is computed across all
  document pairs.  Two documents are near-duplicates when their Jaccard
  similarity exceeds ``near_duplicate_threshold`` (default 0.50).

  Connected components of the near-dup graph form "version families".
  Within each family the primary is the member with the latest
  ``source_modified_at`` timestamp (tie-break: lexicographically largest
  ``content_hash`` for determinism).  All other family members become
  ``superseded``.

  **Scaling ceiling (Phase 5 / D-07 note):** exact shingle-set Jaccard is
  O(N² × |shingles|).  For the corpora handled here (tens to low hundreds of
  documents) this is fast.  At corpus sizes above ~5,000 documents, pairwise
  exact Jaccard becomes expensive and should be replaced with MinHash-LSH
  approximate similarity (D-07); the threshold and family-formation logic
  remain unchanged.  Phase 5 should gate on corpus size and switch algorithm
  accordingly.

Boilerplate detection (§6.2):

  The extracted text of each document is split into normalised paragraph
  blocks (lower-cased, whitespace-collapsed).  A block is classified as
  corpus-wide boilerplate when EITHER of two branches fires (D-32):

  Branch (a) — dominant-prevalence (normal corpus):
  * Normal corpus  (≥ ``boilerplate_small_corpus_doc_count`` docs): fraction of
    unique docs containing the block must exceed ``boilerplate_corpus_proportion``
    (default 0.30).
  * Small corpus   (< ``boilerplate_small_corpus_doc_count`` docs): fraction
    must exceed ``boilerplate_small_corpus_proportion`` (default 0.50),
    raising the bar to avoid false positives.

  Branch (b) — absolute-floor (D-32, for larger corpora where fraction silently
  falls below branch-a threshold):
  * Total corpus occurrences of the block ≥ ``boilerplate_abs_floor_count``
    (default 3, counting multiple occurrences within a single document), AND
  * Unique-document fraction ≥ ``boilerplate_abs_floor_fraction`` (default 0.05,
    preventing single-document repetitions in very large corpora from misfiring).

  Rationale: the pure-fraction rule (branch a) was validated on 3-doc subsets where
  fraction is trivially 1.0; at full corpus scale (15+ eligible docs) shared footers
  appearing in 3 docs give fraction ≈ 0.20, below the 0.30 threshold.  The absolute
  floor keeps the 70%-line match threshold and R6 (structural retype only, never byte
  removal) unchanged; the 0.05 minimum prevalence prevents a 3-occurrence template
  in a 1000-doc corpus from blanket-classifying.

  The boilerplate block set is recorded in the ParseResultBatch so the
  Decompose stage can pass it to the BoilerplatePass.

  Reuses dedup machinery: boilerplate detection is not a separate algorithm —
  a block that appears in many documents *is* repeated corpus text, which is
  exactly the dedup criterion applied at block (not document) granularity.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Configuration defaults (mirroring docs/configuration/reference.md §2.5)
# ---------------------------------------------------------------------------

DEFAULT_NEAR_DUPLICATE_THRESHOLD: float = 0.50
"""Minimum Jaccard similarity for two documents to be near-duplicates.

0.50 was chosen from empirical testing on the golden corpus (policy_v1/v2/v3):
the three policy versions share a large common section but each adds substantial
new content, resulting in Jaccard similarities in the 0.52–0.65 range.  A
threshold of 0.70 would miss these real version families.

Trade-off: at 0.50, documents sharing roughly half their 5-gram shingle set are
grouped.  For most corpora this is a reasonable "same document, different version"
signal.  Operators can raise it if they see false positives (documents grouped
that are not actually version families).

Configurable via ``ingestion.dedup.near_duplicate_threshold`` in corpus.yaml
(docs/configuration/reference.md §2.5).
"""

DEFAULT_BOILERPLATE_CORPUS_PROPORTION: float = 0.30
"""Proportion of corpus docs a block must appear in to be boilerplate (normal corpus)."""

DEFAULT_BOILERPLATE_SMALL_CORPUS_PROPORTION: float = 0.50
"""Proportion threshold for small corpora."""

DEFAULT_BOILERPLATE_SMALL_CORPUS_DOC_COUNT: int = 10
"""Corpus size below which the small-corpus threshold applies."""

DEFAULT_BOILERPLATE_ABS_FLOOR_COUNT: int = 3
"""Absolute-floor branch (D-32): minimum total corpus occurrences for boilerplate.

A block that appears at least this many times across the corpus (counting multiple
occurrences within a single document, e.g. repeated nav chrome) triggers the
absolute-floor branch when fraction also meets ``DEFAULT_BOILERPLATE_ABS_FLOOR_FRACTION``.

Rationale (D-32): the pure-fraction rule (branch a) was validated only on 3-doc subsets
where fraction is trivially 1.0; at full corpus scale it silently stops firing (measured:
3/13 = 0.23 at 21-fixture scale).  The absolute floor keeps the 70%-line match threshold
and R6 (structural retype only, never byte removal) unchanged.

Configurable via ``assessment.boilerplate_abs_floor_count`` in corpus.yaml.
"""

DEFAULT_BOILERPLATE_ABS_FLOOR_FRACTION: float = 0.05
"""Absolute-floor branch (D-32): minimum unique-document fraction for the absolute floor.

A block must appear in at least this fraction of eligible documents (by unique-document
count, not total occurrences) to trigger the absolute-floor branch.  This prevents a
block appearing 3+ times within a single document in a large corpus from being
classified as corpus-wide boilerplate.

Example: 3 total occurrences in a 100-document corpus → unique_docs/n_docs = 0.01 < 0.05
→ does NOT fire.  3 total occurrences in a 15-document corpus → unique_docs/n_docs
≥ 0.05 → fires.

Configurable via ``assessment.boilerplate_abs_floor_fraction`` in corpus.yaml.
"""

_SHINGLE_N = 5
"""Word n-gram size for near-duplicate shingling."""

_MIN_TEXT_CHARS = 50
"""Minimum extracted text length to attempt shingling or boilerplate detection."""

_MIN_BLOCK_CHARS = 20
"""Minimum normalised block length to count as a boilerplate candidate."""


# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------


def _tokenise(text: str) -> list[str]:
    """Lowercase + split on non-alphanumeric runs."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _shingles(tokens: list[str], n: int = _SHINGLE_N) -> frozenset[tuple[str, ...]]:
    """Return the set of word n-grams as tuples."""
    if len(tokens) < n:
        return frozenset()
    return frozenset(tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1))


def _jaccard(a: frozenset, b: frozenset) -> float:
    """Compute Jaccard similarity of two sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _normalise_block(text: str) -> str:
    """Normalise a text block for boilerplate comparison.

    Lower-case, collapse whitespace.  Returns the normalised string.
    """
    return re.sub(r"\s+", " ", text.lower()).strip()


def _split_paragraphs(text: str) -> list[str]:
    """Split extracted text into paragraph blocks.

    Yields both paragraph-level blocks (split on blank lines) and individual
    lines within those blocks.  This is necessary because PDF text extraction
    wraps long lines arbitrarily, so the preamble text may appear as a sequence
    of short lines rather than a single long paragraph.  Including individual
    lines allows detection of boilerplate that spans multiple PDF line-wraps.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Paragraph-level blocks (split on blank lines)
    paragraphs = re.split(r"\n\s*\n+", text)
    blocks: list[str] = []
    for para in paragraphs:
        para = para.strip()
        if para:
            blocks.append(para)
            # Also yield individual lines within the paragraph for line-level matching.
            # This catches boilerplate text that pypdf wraps across lines.
            for line in para.split("\n"):
                line = line.strip()
                if line:
                    blocks.append(line)
    return blocks


# ---------------------------------------------------------------------------
# Near-duplicate clustering
# ---------------------------------------------------------------------------


def _extract_doc_text(parse_result: dict[str, Any]) -> str:
    """Concatenate all region texts from a ParseResult dict."""
    parts: list[str] = []
    for region in parse_result.get("regions", []):
        t = region.get("text") or ""
        if t.strip():
            parts.append(t.strip())
    return "\n\n".join(parts)


def _pick_primary(
    members: list[str],
    parse_results_by_id: dict[str, dict[str, Any]],
) -> str:
    """Pick the primary document from a version-family member list.

    Strategy: latest ``source_modified_at`` wins.  Tie-break: lexicographically
    largest ``content_hash`` for determinism (stable under re-runs).

    ``source_modified_at`` comes from the inventory item inside the parse result
    (stored as ``source_modified_at`` on the ParseResult dict when the
    AssessStage propagates it from the Inventory).  If absent, fall back to
    ``content_hash`` ordering only.
    """

    def _sort_key(doc_id: str) -> tuple:
        pr = parse_results_by_id.get(doc_id, {})
        ts_raw = pr.get("source_modified_at") or pr.get("discovered_at")
        if ts_raw:
            if isinstance(ts_raw, datetime):
                ts = ts_raw.timestamp()
            else:
                try:
                    ts = datetime.fromisoformat(str(ts_raw)).timestamp()
                except (ValueError, TypeError):
                    ts = 0.0
        else:
            ts = 0.0
        content_hash = pr.get("content_hash", "") or ""
        return (ts, content_hash)

    return max(members, key=_sort_key)


def _connected_components(
    doc_ids: list[str],
    edges: set[tuple[str, str]],
) -> list[list[str]]:
    """Union-find connected components from a set of edges."""
    parent: dict[str, str] = {d: d for d in doc_ids}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(x: str, y: str) -> None:
        rx, ry = _find(x), _find(y)
        if rx != ry:
            parent[rx] = ry

    for a, b in edges:
        if a in parent and b in parent:
            _union(a, b)

    groups: dict[str, list[str]] = {}
    for d in doc_ids:
        root = _find(d)
        groups.setdefault(root, []).append(d)

    return list(groups.values())


def compute_version_families(
    parse_results: list[dict[str, Any]],
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> list[dict[str, Any]]:
    """Cluster documents into near-duplicate version families.

    Returns a list of VersionFamily-shaped dicts (schema matches inventory.md).
    Only families with 2+ members are returned; singletons are not families.

    Scaling ceiling note: exact pairwise Jaccard is O(N²).  Fine for small
    corpora (<500 docs).  Phase 5 should replace with MinHash-LSH when
    corpus sizes approach D-07's ceiling.

    Args:
        parse_results: All ParseResult dicts from the batch.
        near_duplicate_threshold: Minimum Jaccard for near-dup membership.

    Returns:
        List of version-family dicts.
    """
    import hashlib

    # Restrict to parseable results with enough text.
    eligible: list[dict[str, Any]] = []
    shingles_by_id: dict[str, frozenset] = {}

    for pr in parse_results:
        status = pr.get("parse_status", "excluded_pre_parse")
        if status not in ("parsed", "partial"):
            continue
        text = _extract_doc_text(pr)
        if len(text) < _MIN_TEXT_CHARS:
            continue
        tokens = _tokenise(text)
        sh = _shingles(tokens)
        if not sh:
            continue
        eligible.append(pr)
        shingles_by_id[pr["document_id"]] = sh

    if len(eligible) < 2:
        return []

    parse_results_by_id = {pr["document_id"]: pr for pr in parse_results}
    eligible_ids = [pr["document_id"] for pr in eligible]

    # Pairwise Jaccard — O(N²) exact
    edges: set[tuple[str, str]] = set()
    similarity_scores: dict[tuple[str, str], float] = {}

    for i, id_a in enumerate(eligible_ids):
        for id_b in eligible_ids[i + 1 :]:
            j = _jaccard(shingles_by_id[id_a], shingles_by_id[id_b])
            if j >= near_duplicate_threshold:
                key = (min(id_a, id_b), max(id_a, id_b))
                edges.add(key)
                similarity_scores[key] = j

    if not edges:
        return []

    components = _connected_components(eligible_ids, edges)
    families: list[dict[str, Any]] = []

    for component in components:
        if len(component) < 2:
            continue

        primary_id = _pick_primary(component, parse_results_by_id)
        superseded_ids = [d for d in component if d != primary_id]

        # Build per-member similarity scores (score relative to primary)
        scores: dict[str, float] = {}
        for sid in superseded_ids:
            key = (min(primary_id, sid), max(primary_id, sid))
            scores[sid] = similarity_scores.get(key, 0.0)

        # Stable family_id from sorted member IDs
        raw = ":".join(sorted(component))
        family_id = "fam-" + hashlib.sha256(raw.encode()).hexdigest()[:24]

        pr_primary = parse_results_by_id.get(primary_id, {})
        if pr_primary.get("source_modified_at"):
            primacy_basis = "source_modified_at"
        elif pr_primary.get("discovered_at"):
            primacy_basis = "discovered_at"
        else:
            primacy_basis = "content_hash"

        families.append(
            {
                "family_id": family_id,
                "member_document_ids": sorted(component),
                "primary_document_id": primary_id,
                "superseded_document_ids": sorted(superseded_ids),
                "similarity_method": f"word_{_SHINGLE_N}gram_jaccard_exact",
                "similarity_scores": scores,
                "primacy_basis": primacy_basis,
            }
        )

    return families


# ---------------------------------------------------------------------------
# Boilerplate detection
# ---------------------------------------------------------------------------


def compute_boilerplate_blocks(
    parse_results: list[dict[str, Any]],
    corpus_proportion: float = DEFAULT_BOILERPLATE_CORPUS_PROPORTION,
    small_corpus_proportion: float = DEFAULT_BOILERPLATE_SMALL_CORPUS_PROPORTION,
    small_corpus_doc_count: int = DEFAULT_BOILERPLATE_SMALL_CORPUS_DOC_COUNT,
    abs_floor_count: int = DEFAULT_BOILERPLATE_ABS_FLOOR_COUNT,
    abs_floor_fraction: float = DEFAULT_BOILERPLATE_ABS_FLOOR_FRACTION,
) -> set[str]:
    """Detect corpus-wide boilerplate text blocks.

    A normalised paragraph block is boilerplate when EITHER of two branches fires (D-32):

    Branch (a) — dominant-prevalence:
      The fraction of unique documents containing the block exceeds the applicable
      threshold.  The threshold is raised for small corpora (§6.2, OQ-8):
      * Normal corpus (≥ ``small_corpus_doc_count`` docs): fraction > ``corpus_proportion``
        (default 0.30).
      * Small corpus  (< ``small_corpus_doc_count`` docs): fraction > ``small_corpus_proportion``
        (default 0.50, raising the bar to avoid false positives).

    Branch (b) — absolute-floor (D-32):
      Total corpus occurrences of the block ≥ ``abs_floor_count`` (default 3, counting
      multiple occurrences within a single document) AND the unique-document fraction ≥
      ``abs_floor_fraction`` (default 0.05).  This branch fires when the pure-fraction
      rule would silently miss blocks repeated in a small fraction of a large corpus
      — e.g. 3/15 = 0.20 falls below the 0.30 branch-a threshold but has absolute count 3.
      The 0.05 minimum prevalence prevents a block appearing 3+ times within one document
      in a 100+ doc corpus (fraction 0.01) from blanket-classifying.

    Block matching uses exact normalised string equality (lower-case,
    whitespace-collapsed).  This reuses the same dedup principle as
    near-duplicate clustering: text that is repeated across many documents is
    boilerplate by definition (§6.2 "boilerplate detection reuses dedup
    machinery").

    Args:
        parse_results: All ParseResult dicts from the batch.
        corpus_proportion: Fraction of docs for boilerplate (normal corpus, branch a).
        small_corpus_proportion: Fraction for small corpora (branch a).
        small_corpus_doc_count: Threshold for "small corpus" classification (branch a).
        abs_floor_count: Minimum total corpus occurrences to trigger branch (b).
        abs_floor_fraction: Minimum unique-doc fraction to trigger branch (b).

    Returns:
        Set of normalised block strings that are boilerplate.
    """
    eligible: list[dict[str, Any]] = []
    for pr in parse_results:
        status = pr.get("parse_status", "excluded_pre_parse")
        if status not in ("parsed", "partial"):
            continue
        text = _extract_doc_text(pr)
        if len(text) < _MIN_TEXT_CHARS:
            continue
        eligible.append(pr)

    n_docs = len(eligible)
    if n_docs < 2:
        return set()

    # Pick the applicable branch-a threshold
    threshold = small_corpus_proportion if n_docs < small_corpus_doc_count else corpus_proportion

    # Count block occurrences across documents.
    #
    # block_doc_count: unique-document count — at most 1 per document per block.
    #   Used for fraction calculation in both branches.
    #
    # block_total_count: total corpus occurrences of the block, counting each
    #   paragraph-level appearance once per document even when the block appears
    #   multiple times within the same document (e.g. nav chrome repeated across
    #   pages).  _split_paragraphs() produces both paragraph-level and line-level
    #   splits; we count only paragraph-level repetitions to avoid double-counting
    #   the same content unit.
    block_doc_count: dict[str, int] = {}
    block_total_count: dict[str, int] = {}

    for pr in eligible:
        text = _extract_doc_text(pr)
        paragraphs = _split_paragraphs(text)
        seen_in_doc: set[str] = set()
        # Track paragraph-level occurrences within this document (for total count).
        # A single-line paragraph appears in _split_paragraphs() twice (as a paragraph
        # and as a line); we must count it only once per paragraph-level occurrence.
        # The multi-pass design: first pass counts paragraphs, second pass picks up
        # line-level splits for detection.  Here we use seen_in_doc to deduplicate
        # so that identical paragraph and line representations are counted once.
        for para in paragraphs:
            norm = _normalise_block(para)
            if len(norm) < _MIN_BLOCK_CHARS:
                continue
            if norm not in seen_in_doc:
                seen_in_doc.add(norm)
                block_doc_count[norm] = block_doc_count.get(norm, 0) + 1
                # First time seeing this block in this document: start the per-doc count
                # We'll track how many DISTINCT positions in the document this block occupies.
                # Since seen_in_doc prevents double-counting, this is effectively 1 per doc.
                # The total cross-document count is simply the sum of per-document counts.
                block_total_count[norm] = block_total_count.get(norm, 0) + 1

    # NOTE: to capture within-document repetitions (branch b use case: nav chrome
    # appearing twice in a single HTML export), we need a second pass that counts
    # how many non-overlapping PARAGRAPH-level occurrences exist per document.
    # The approach: for each document, count how many times each block appears at
    # the paragraph level (the raw paragraph-split, not the line-level expansion).
    # We then add the excess occurrences (count - 1) to block_total_count.
    for pr in eligible:
        text = _extract_doc_text(pr)
        # Paragraph-level split only (not the line-level expansion in _split_paragraphs)
        raw_text = text.replace("\r\n", "\n").replace("\r", "\n")
        paragraphs_only = re.split(r"\n\s*\n+", raw_text)
        para_counter: dict[str, int] = {}
        for para in paragraphs_only:
            para = para.strip()
            if not para:
                continue
            norm = _normalise_block(para)
            if len(norm) < _MIN_BLOCK_CHARS:
                continue
            para_counter[norm] = para_counter.get(norm, 0) + 1
        # For each block appearing > 1 times in this document (within-doc repetition),
        # add the extra occurrences to block_total_count.
        for norm, count in para_counter.items():
            if count > 1 and norm in block_doc_count:
                block_total_count[norm] = block_total_count.get(norm, 0) + (count - 1)

    boilerplate: set[str] = set()
    for block, unique_count in block_doc_count.items():
        fraction = unique_count / n_docs
        total = block_total_count.get(block, unique_count)

        # Branch (a): dominant-prevalence
        if fraction > threshold:
            boilerplate.add(block)
            continue

        # Branch (b): absolute-floor (D-32)
        if total >= abs_floor_count and fraction >= abs_floor_fraction:
            boilerplate.add(block)

    return boilerplate


# ---------------------------------------------------------------------------
# Entry point: run all corpus-level passes, mutate the parse_results list
# ---------------------------------------------------------------------------


def run_corpus_passes(
    parse_results: list[dict[str, Any]],
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    boilerplate_corpus_proportion: float = DEFAULT_BOILERPLATE_CORPUS_PROPORTION,
    boilerplate_small_corpus_proportion: float = DEFAULT_BOILERPLATE_SMALL_CORPUS_PROPORTION,
    boilerplate_small_corpus_doc_count: int = DEFAULT_BOILERPLATE_SMALL_CORPUS_DOC_COUNT,
    boilerplate_abs_floor_count: int = DEFAULT_BOILERPLATE_ABS_FLOOR_COUNT,
    boilerplate_abs_floor_fraction: float = DEFAULT_BOILERPLATE_ABS_FLOOR_FRACTION,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Run near-dup clustering and boilerplate detection over the full corpus.

    Mutates ``parse_results`` in place to annotate each result with
    ``dedup_role`` (``"primary"``, ``"superseded"``, or ``"unique"``) and
    ``dedup_family_id`` (the family identifier for non-unique docs).

    Args:
        parse_results: All ParseResult dicts (mutable).
        near_duplicate_threshold: Jaccard threshold for near-dup membership.
        boilerplate_corpus_proportion: Boilerplate threshold (normal corpus, branch a).
        boilerplate_small_corpus_proportion: Boilerplate threshold (small corpus, branch a).
        boilerplate_small_corpus_doc_count: Small-corpus size cutoff (branch a).
        boilerplate_abs_floor_count: Minimum total occurrences for branch b (D-32).
        boilerplate_abs_floor_fraction: Minimum unique-doc fraction for branch b (D-32).

    Returns:
        Tuple of:
          - version_families: list of VersionFamily-shaped dicts
          - boilerplate_blocks: set of normalised boilerplate block strings
    """
    # --- Near-duplicate clustering ---
    version_families = compute_version_families(
        parse_results,
        near_duplicate_threshold=near_duplicate_threshold,
    )

    # Annotate parse results with dedup_role and dedup_family_id
    superseded_ids: set[str] = set()
    primary_ids: set[str] = set()
    family_by_doc: dict[str, str] = {}

    for fam in version_families:
        fam_id = fam["family_id"]
        primary_ids.add(fam["primary_document_id"])
        for sid in fam["superseded_document_ids"]:
            superseded_ids.add(sid)
            family_by_doc[sid] = fam_id
        family_by_doc[fam["primary_document_id"]] = fam_id

    for pr in parse_results:
        doc_id = pr["document_id"]
        if doc_id in superseded_ids:
            pr["dedup_role"] = "superseded"
            pr["dedup_family_id"] = family_by_doc[doc_id]
            primary_for_family = next(
                fam["primary_document_id"]
                for fam in version_families
                if doc_id in fam["superseded_document_ids"]
            )
            pr["dedup_primary_document_id"] = primary_for_family
        elif doc_id in primary_ids:
            pr["dedup_role"] = "primary"
            pr["dedup_family_id"] = family_by_doc[doc_id]
            pr["dedup_primary_document_id"] = None
        else:
            pr.setdefault("dedup_role", "unique")
            pr.setdefault("dedup_family_id", None)
            pr.setdefault("dedup_primary_document_id", None)

    # --- Boilerplate detection ---
    boilerplate_blocks = compute_boilerplate_blocks(
        parse_results,
        corpus_proportion=boilerplate_corpus_proportion,
        small_corpus_proportion=boilerplate_small_corpus_proportion,
        small_corpus_doc_count=boilerplate_small_corpus_doc_count,
        abs_floor_count=boilerplate_abs_floor_count,
        abs_floor_fraction=boilerplate_abs_floor_fraction,
    )

    return version_families, boilerplate_blocks


__all__ = [
    "DEFAULT_NEAR_DUPLICATE_THRESHOLD",
    "DEFAULT_BOILERPLATE_CORPUS_PROPORTION",
    "DEFAULT_BOILERPLATE_SMALL_CORPUS_PROPORTION",
    "DEFAULT_BOILERPLATE_SMALL_CORPUS_DOC_COUNT",
    "DEFAULT_BOILERPLATE_ABS_FLOOR_COUNT",
    "DEFAULT_BOILERPLATE_ABS_FLOOR_FRACTION",
    "compute_version_families",
    "compute_boilerplate_blocks",
    "run_corpus_passes",
]
