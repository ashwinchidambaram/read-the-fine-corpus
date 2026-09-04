# Security Detection — §14.1 Content-Security Detection (Phase 2)

## Overview

Phase 2 adds two complementary security signals to the pipeline:

1. **Parse-time invisible-content detection** (`pipeline/assess/security.py`) — inspects raw PDF content streams for text that is hidden via white-on-white coloring, sub-threshold font size, or off-page positioning.
2. **Segment-time injection scoring** (`pipeline/decompose/passes/injection.py`) — heuristically scores every segment for prompt-injection patterns (imperative language, fake delimiters, role markers, exfiltration language, credential requests).

Both signals are **additive metadata only**. In accordance with §14.1 ("labelled, not sanitised") and M-105, content is never excluded, stripped, or down-tiered based on these signals.

---

## Invisible-Content Detection

### How It Works

The detector (`detect_invisible_content(reader, page_num)`) operates directly on decompressed PDF content streams, independent of pypdf's text-extraction layer.

**Content-stream tokenisation.** Each page's `/Contents` stream is decompressed (via pypdf's `get_data()`, with a zlib fallback) and tokenised using a simple regex that matches numbers, name tokens (`/FontName`), and PDF operator keywords.

**Graphics-state tracking.** The scanner maintains a minimal graphics state:

| State variable | Updated by |
|---|---|
| `fill_is_white` | `rg` (RGB non-stroking) and `g` (gray non-stroking) operators |
| `font_size` | `Tf` operator |
| `cur_x`, `cur_y` | `BT` (reset to 0,0), `Tm` (absolute set), `Td`/`TD` (relative move) |
| `in_text` | `BT` / `ET` markers |

**Detection triggers.** On every text-show operator (`Tj`, `TJ`, `'`, `"`):

- **white_on_white**: `fill_is_white` is True at the moment of the text-show. White is defined as all RGB components ≥ 0.9 (or gray ≥ 0.9). This threshold deliberately excludes near-white (e.g., 0.95/0.95/0.95 on a white background would still trigger, while 0.85/0.85/0.85 would not).
- **zero_size_font**: `font_size < 2.0` pt. The PDF spec calls these "zero-size fonts"; we extend the category to any sub-threshold size because 1pt text is practically invisible at normal viewing distances.
- **off_page**: `cur_x` or `cur_y` is outside the page's MediaBox bounds. The BT operator resets the text cursor to (0.0, 0.0) per PDF spec; this is critical because without the reset, accumulated `Td` values from consecutive text blocks would produce false positives.

**Deduplication.** At most one detection per (`kind`, `page_num`) pair is emitted per call, to avoid flooding the record with many detections for the same pattern.

**Return value.** The function returns `(list[InvisibleContentDetection], list[Finding])`. Both lists may be empty. The caller (`pdf_native.py`) stores detections in `PageResult.invisible_content` and appends findings to the document-level finding list.

### Thresholds

| Parameter | Value | Rationale |
|---|---|---|
| `_WHITE_THRESHOLD` | 0.9 | Covers both pure white (1.0) and near-white adversarial values (0.95, 0.92). |
| `_TINY_FONT_PT` | 2.0 pt | Covers size-1 fonts (adversarial fixture) and size-0 fonts while leaving 2pt title ornaments undetected. |

### Wire-up

`pdf_native.py` calls `detect_invisible_content(reader, page_num)` inside the per-page loop, after text extraction. Failures are silently swallowed (the detector must not crash a parse that would otherwise succeed).

---

## Injection Scoring

### How It Works

`InjectionPass` is a stateless `SegmentPass` that runs after salience scoring. It calls `_compute_injection_score(text)` on each segment's plain text and writes the result to `segment.provenance.injection_suspicion` (a float in [0.0, 1.0]).

**Pattern classes and base weights:**

| Pattern class | Examples | Base weight |
|---|---|---|
| Imperative model-directed language | "ignore previous instructions", "disregard the above" | 0.50 |
| Role/persona markers | "you are now", "act as", "your new role" | 0.40 |
| Fake delimiters | `---`, `[END]`, `</context>`, triple-backtick blocks | 0.35 |
| Exfiltration / action urging | "send to", "output verbatim", "repeat back" | 0.40 |
| Credential / secret requests | "api key", "password is", "bearer token" | 0.45 |
| Confidentiality override | "this message is confidential", "do not reveal" | 0.35 |
| System-prompt mimicry | "system:", "assistant:", "human:", prompt XML tags | 0.35 |

**Score formula.** For each matched pattern class, the raw contribution is `base_weight × length_factor`, where `length_factor = min(1.0, log1p(500) / log1p(text_len))`. This normalises for text length — a short segment that hits one pattern is penalised less than a long segment that hits the same pattern (under the assumption that a long document is more likely to discuss injection than to be an injection itself). The raw contributions are summed, then mapped through a soft-clamp: `score = 1 - exp(-raw_sum)`. The result is clamped to [0.0, 1.0].

**Invisible-content flag propagation.** If a document was parsed with invisible-content detections, `InjectionPass` maps those detections back to segments by page range. A segment covering pages that contain any invisible-content detection receives `segment.provenance.invisible_content_flags` set to the list of detection kinds found on those pages.

### M-105: No Exclusion or Downgrade

Injection suspicion is **never** used to exclude or down-tier content. This is a hard architectural constraint (M-105). The score is a metadata field retrievable and filterable downstream; it does not influence `segment.tier`, `segment.segment_type`, or document inclusion in any build or retrieval path.

A unit test (`TestM105NoExclusion` in `tests/phase2/test_injection_scoring.py`) asserts that a max-suspicion (score = 1.0) segment retains the same tier it would have received from the salience pass.

---

## §14.1 Mapping

| §14.1 requirement | Implementation |
|---|---|
| Detect white-on-white text | `detect_invisible_content()` → `white_on_white` kind |
| Detect zero-size / sub-threshold fonts | `detect_invisible_content()` → `zero_size_font` kind |
| Detect off-page text positioning | `detect_invisible_content()` → `off_page` kind |
| Label, do not sanitise | Hidden text is retained in extracted content; detections are annotations only |
| Score injection risk | `InjectionPass._compute_injection_score()` → `injection_suspicion` |
| Do not exclude on suspicion | M-105; score is additive metadata only |

---

## Honest Limitations (Phase 2 Scope)

**Invisible-content detector:**

- The scanner does not maintain a full PDF graphics-state stack (`gsave` / `grestore`). A document that uses graphics-state saves to temporarily switch to white and then restore will defeat the detector if the text operators fall inside a saved state that the scanner does not unwind.
- Background color is assumed white. True contrast detection (e.g., white text on a colored background vs. white text on a white background) is not implemented.
- Off-page detection uses the MediaBox; CropBox clipping is not considered.
- Form XObjects and Pattern color spaces are not walked. A document that places hidden text inside an XObject will not be detected.
- Only `rg` (RGB) and `g` (gray) non-stroking color operators are tracked. The `cs`/`scn` path-based operators, ICC profiles, and device-N colors are not modeled.

**Injection scorer:**

- Pattern matching is regex-based and English-language-centric. Multilingual injections or injections encoded in unusual Unicode normalisation forms may evade detection.
- The length normalization heuristic can be gamed by padding a short injection with filler text.
- A score of 0.0 does not mean a segment is injection-free; it means no pattern matched. The scorer is deliberately conservative to avoid noise on legitimate corpus content.
- No model-based scoring is used in Phase 2. A calibration pass (Phase 5) may add embedding-based or LLM-based signal.

---

## Files

| File | Role |
|---|---|
| `src/finecorpus/pipeline/assess/security.py` | Parse-time invisible-content detector |
| `src/finecorpus/pipeline/assess/parsers/pdf_native.py` | Wire-up: calls `detect_invisible_content` per page |
| `src/finecorpus/pipeline/decompose/passes/injection.py` | `InjectionPass` segment-level scoring |
| `src/finecorpus/pipeline/decompose/passes/__init__.py` | Registers `injection_pass` in `PASSES` after `salience_pass` |
| `tests/phase2/test_invisible_content_detector.py` | Unit tests for the detector against `adversarial.pdf` |
| `tests/phase2/test_injection_scoring.py` | Unit tests for injection scoring + M-105 + propagation |
| `tests/phase2/test_t09_injection_flagging.py` | T-09 integration test (unit + live Qdrant variant) |
