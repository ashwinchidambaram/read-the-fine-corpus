"""Injection-suspicion scoring pass for the Decompose stage (§14.1).

Module docstring — scoring scheme
----------------------------------
This pass scores every segment with an ``injection_suspicion`` value in [0, 1].
The score is heuristic and explicitly labeled as such (§14.1: "the score is
retrievable and filterable, and is not used to silently exclude").

**Scoring scheme (v1 heuristic)**

Pattern classes and their base weights (before length normalisation):

| Class | Pattern examples | Base weight |
|-------|-----------------|-------------|
| imperative_ignore | "ignore previous instructions", "ignore all prior guidelines" | 0.50 |
| imperative_disregard | "disregard all instructions", "disregard safety rules" | 0.45 |
| role_marker | line-initial "SYSTEM:", "USER:", "ASSISTANT:" | 0.40 |
| fake_delimiter | "[INST]", "<|im_start|>", "<|im_end|>", "<<SYS>>" | 0.45 |
| exfiltration_url | "send … to http://…", "post … to @user" | 0.50 |
| credential_request | "reveal your … key", "output your system prompt" | 0.45 |
| developer_mode | "developer mode", "unrestricted mode", "jailbreak" | 0.35 |

Normalisation by length class
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A one-line prompt-injection needle in a 2,000-word prose segment is less
suspicious in aggregate than the same needle in a 50-word segment.  We apply a
log-based normalisation:

    length_factor = min(1.0, log(1 + max_segment_chars) / log(1 + text_len))

This keeps the score high when the text is short and suspicious, but dampens it
for long documents where the match is incidental.

Combination
~~~~~~~~~~~
For each hit on each pattern class, we compute the component score:

    component = base_weight * length_factor

We combine all component scores via soft-clamped additive sum:

    raw = sum(component_i for each hit)
    final = 1 - exp(-raw)        # maps [0,∞) onto (0,1)

This ensures:
- A single strong pattern hit yields a score in ~[0.3, 0.5].
- Multiple independent hits push the score toward 1.0 asymptotically.
- Clean segments (no hits) score exactly 0.0.

Invisible-content flag propagation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Segments whose source region overlaps a page that had invisible-content
detections at parse time inherit those flags in ``invisible_content_flags``.
Overlap is established by comparing the segment's ``location.page_start`` /
``page_end`` against the pages that had detections (extracted from the parse
result's ``pages[n].invisible_content`` list).

M-105 invariant
~~~~~~~~~~~~~~~
**Nothing is excluded or down-tiered by suspicion.**  A segment scoring 1.0 on
``injection_suspicion`` retains its salience tier unchanged.  This is explicitly
tested in tests/phase2/test_m105_no_exclusion.py.

Phase 5 calibration
~~~~~~~~~~~~~~~~~~~
The weights and normalisation constants are Phase 2 seeds.  Phase 5 sweep
calibration may adjust them based on the adversarial fixture and user-labelled
examples.  The scheme is documented here so calibration has a clear baseline.
"""

from __future__ import annotations

import math
import re
from typing import Any

from finecorpus.contracts.segment_set import ExclusionRecord, Segment
from finecorpus.contracts.shared.blocks import InvisibleContentKind
from finecorpus.pipeline.decompose.passes.base import DocumentContext, PassResult

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

#: Compiled pattern entries: (name, compiled_regex, base_weight)
_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    # Imperative "ignore" — highest risk
    (
        "imperative_ignore",
        re.compile(
            r"ignore\s+(all\s+)?(previous|prior)\s+(instructions?|guidelines?|rules?|prompts?)",
            re.IGNORECASE,
        ),
        0.50,
    ),
    # Imperative "disregard"
    (
        "imperative_disregard",
        re.compile(
            r"disregard\s*(all\s+)?(instructions?|guidelines?|rules?|safety|context)",
            re.IGNORECASE,
        ),
        0.45,
    ),
    # Role markers at start of line: SYSTEM:, USER:, ASSISTANT:
    (
        "role_marker",
        re.compile(
            r"^(system|assistant|user)\s*:",
            re.IGNORECASE | re.MULTILINE,
        ),
        0.40,
    ),
    # Fake delimiters used in instruction-tuned model formats
    (
        "fake_delimiter",
        re.compile(
            r"\[INST\]|<\|im_start\|>|<\|im_end\|>|<<SYS>>|</s>|<s>|\[/INST\]",
            re.IGNORECASE,
        ),
        0.45,
    ),
    # Exfiltration / action urging: "send/post/forward X to http:// or @"
    (
        "exfiltration_url",
        re.compile(
            r"(send|post|forward|submit|upload)\s+\S+\s+.{0,30}(https?://|@\w)",
            re.IGNORECASE,
        ),
        0.50,
    ),
    # Credential / system-prompt extraction requests
    (
        "credential_request",
        re.compile(
            r"(reveal|output|print|show|expose)\s+(your\s+)?"
            r"(system\s+prompt|api\s+key|password|credentials?|context\s+window)",
            re.IGNORECASE,
        ),
        0.45,
    ),
    # Developer/unrestricted mode activation phrases
    (
        "developer_mode",
        re.compile(
            r"(developer\s+mode|unrestricted\s+mode|jailbreak|DAN\s+mode)",
            re.IGNORECASE,
        ),
        0.35,
    ),
]

#: Reference segment length for normalisation.  Segments longer than this are
#: treated as fully-length-normalised (length_factor approaches 1 as len grows).
_MAX_SEGMENT_CHARS = 500

# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _compute_injection_score(text: str) -> float:
    """Score a single segment text for injection patterns.

    Returns a float in [0, 1].  Returns exactly 0.0 if no patterns match.
    """
    if not text:
        return 0.0

    text_len = len(text)
    length_factor = min(1.0, math.log1p(_MAX_SEGMENT_CHARS) / math.log1p(text_len))

    raw_sum = 0.0
    for _name, pattern, base_weight in _PATTERNS:
        if pattern.search(text):
            raw_sum += base_weight * length_factor

    if raw_sum == 0.0:
        return 0.0

    # Soft-clamp via 1 - exp(-raw): maps (0,∞) → (0,1)
    score = 1.0 - math.exp(-raw_sum)
    return min(1.0, round(score, 4))


# ---------------------------------------------------------------------------
# Invisible-content flag propagation helpers
# ---------------------------------------------------------------------------


def _build_page_invisible_map(
    parse_result: dict[str, Any],
) -> dict[int, list[InvisibleContentKind]]:
    """Build a mapping from 1-based page number → list of InvisibleContentKind.

    Reads from the raw parse_result dict (as produced by NativePDFParser and
    serialised into the ParseResultBatch) rather than a typed model to avoid
    double-parsing overhead in the pass.
    """
    page_map: dict[int, list[InvisibleContentKind]] = {}
    for page in parse_result.get("pages", []):
        pnum = page.get("page_number")
        if pnum is None:
            continue
        kinds: list[InvisibleContentKind] = []
        for det in page.get("invisible_content", []):
            kind_str = det.get("kind", "")
            try:
                kinds.append(InvisibleContentKind(kind_str))
            except ValueError:
                pass
        if kinds:
            page_map[pnum] = kinds
    return page_map


def _flags_for_segment(
    seg: Segment,
    page_invisible_map: dict[int, list[InvisibleContentKind]],
) -> list[InvisibleContentKind]:
    """Return invisible-content flags for a segment based on page overlap.

    A segment is considered to overlap a page if its source location's
    page_start..page_end range includes that page number.

    Segments produced from HTML/spreadsheet (no page_start) are never flagged
    here — invisible-content detection is PDF-specific.
    """
    loc = seg.location
    if loc.page_start is None:
        return []

    page_start = loc.page_start
    page_end = loc.page_end if loc.page_end is not None else page_start

    flags: list[InvisibleContentKind] = []
    seen: set[InvisibleContentKind] = set()
    for pnum in range(page_start, page_end + 1):
        for kind in page_invisible_map.get(pnum, []):
            if kind not in seen:
                seen.add(kind)
                flags.append(kind)
    return flags


# ---------------------------------------------------------------------------
# Pass implementation
# ---------------------------------------------------------------------------


class InjectionPass:
    """Injection-suspicion scoring and invisible-content flag propagation.

    Satisfies the ``SegmentPass`` protocol.  Must run after ``SaliencePass``
    (which must run after ``SegmentationPass``).

    For every segment this pass:
    1. Computes ``injection_suspicion`` from heuristic pattern matching.
    2. Propagates ``invisible_content_flags`` from parse-time detections by
       matching segment page ranges to page-level detections in the parse result.

    NOTHING is excluded or down-tiered (M-105): the salience_tier is never
    modified by this pass, regardless of the suspicion score.
    """

    def run(
        self,
        doc_ctx: DocumentContext,
        segments: list[Segment],
        exclusions: list[ExclusionRecord],
    ) -> PassResult:
        """Score every segment and propagate invisible-content flags.

        Args:
            doc_ctx: Document context including the raw parse_result dict.
            segments: Current segment list from the previous pass.
            exclusions: Accumulated exclusion records.

        Returns:
            PassResult with updated segments; exclusions unchanged.
        """
        parse_result = doc_ctx.parse_result
        page_invisible_map = _build_page_invisible_map(parse_result)

        updated: list[Segment] = []
        for seg in segments:
            # Compute injection suspicion
            text = seg.text or ""
            suspicion = _compute_injection_score(text)

            # Propagate invisible-content flags from parse-time detections
            invisible_flags = _flags_for_segment(seg, page_invisible_map)

            # Build updated segment (Pydantic BaseModel: use model_copy)
            new_seg = seg.model_copy(
                update={
                    "injection_suspicion": suspicion,
                    "invisible_content_flags": invisible_flags,
                }
            )
            updated.append(new_seg)

        return PassResult(segments=updated, exclusions=exclusions)


#: Module-level singleton — imported and registered in passes/__init__.py.
injection_pass = InjectionPass()
