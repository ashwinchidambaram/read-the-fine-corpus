# Contract 6 — Eval set

Boundary: **⟷ Plan, Serve** (bidirectional cross-cutting; §5, §9). Governing spec: §9.1, §9.2,
§12 (Eval set invariants), §18.1.

The Eval set carries evaluation questions used by Plan (to rank configs, §9.3) and Serve (to
detect drift, §9.4). Every question carries its **generation method, review status, and source
segments** (§12). **Provisional status is inseparable from the data** (§9.2, §12): `review_status`
is a required enum with **no default**, so a question cannot exist without declaring whether it was
reviewed. Users can **import** an existing eval set or a production query log, which **supersedes
generation** (§9.2).

Root model: `EvalSet`.

---

## `EvalSet` (root)

| Field | Type | Required | Semantics |
|---|---|---|---|
| `schema_version` | `str` (semver) | yes | Contract version. |
| `tenancy` | `TenancyBlock` | yes | Owning workspace/KB. |
| `eval_set_id` | `str` (ULID) | yes | Identity. |
| `origin` | `enum{generated, imported_eval_set, imported_query_log}` | yes | Where this set came from (§9.2). Imported origins **supersede** generation. |
| `provenance_note` | `str` | no | For imported sets: what was imported and when (audit). |
| `created_at` | `datetime` (UTC) | yes | Creation/import time. |
| `confidence_level` | `enum{provisional, reviewed, production_derived}` | yes | Set-level confidence, stated explicitly and prominently (§9.2). `provisional` when any question is unreviewed and origin is `generated`; `production_derived` for query-log imports. |
| `questions` | `list[EvalQuestion]` | yes | The questions. |
| `baseline_ref` | `NaiveBaselineRef` | no | The §9.3 naive baseline these scores are measured against, when scored. |

## `EvalQuestion`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `question_id` | `str` (ULID) | yes | Identity. |
| `text` | `str` | yes | The question. |
| `generation_method` | `enum{generated_factual, generated_interpretive, generated_synthesis, imported, query_log}` | yes | **How this question was produced** (§12). Generated questions are stratified across types (§9.1). |
| `review_status` | `enum{unreviewed, reviewed_kept, reviewed_edited, reviewed_rejected}` | **yes, NO DEFAULT** | **Provisional status inseparable from data** (§9.2). A question cannot be constructed without an explicit value; there is no default (pydantic field with no default → construction error if omitted). |
| `source_segment_ids` | `list[str]` | yes | **Source segments** this question was derived from (§12). Empty only for `query_log`/`imported` where provenance is unknown; then `source_unknown=true`. |
| `source_unknown` | `bool` | yes | True when source segments are not knowable (imported/query-log). Makes "no sources" explicit rather than an empty-list ambiguity. |
| `question_type` | `enum{factual_lookup, interpretive, multi_document_synthesis}` | yes | Stratification class (§9.1). |
| `expected_segment_ids` | `list[str]` | no | Gold segments expected in retrieval, when known (used for context recall/precision, §9.3). |
| `reviewed_by` | `str` | no | Reviewer identity when `review_status` is a `reviewed_*` value. |
| `reviewed_at` | `datetime` | no | When reviewed. |
| `class_description_ref` | `str` | no | The class description (§6.5) that seeded generation, for auditability. |

## `NaiveBaselineRef`

Same shape as in [ingestion-config.md](ingestion-config.md): `{reference_id, description}`. Pins
the fixed §9.3 reference configuration so scores are comparable across KBs and releases; if the
reference changes, prior scores are marked against the old one (§9.3).

---

## Invariants

- Every question carries `generation_method`, `review_status`, and `source_segment_ids` (or an
  explicit `source_unknown`) — the §12 triple (§12).
- **`review_status` has no default.** Omitting it is a construction/validation error. Provisional
  status is therefore inseparable from the data (§9.2, §12) — you cannot serialize a question that
  is silent about its review state.
- **Set-level `confidence_level`:** a `generated` set with any `unreviewed` question is
  `provisional`; this label appears everywhere the set is shown, prominently, not in a footnote
  (§9.2). Reviewed and unreviewed baselines are visually distinguishable downstream.
- **Import supersedes generation:** an `imported_eval_set` or `imported_query_log` origin takes
  precedence over generated questions for scoring (§9.2); the platform does not silently blend a
  provisional generated set into an imported one.
- **Deletion linkage (removal, §17.1 MUST):** eval questions derived from a document are
  **REMOVED** when that document is deleted — not retained with a flag. A question is derived from
  the deleted document if **any** of its `source_segment_ids` belong to that document.
  `source_segment_ids` makes this traceable. On removal:
  - The **eval set version increments** (a MINOR-or-higher bump, since the question list changed).
  - The removal is written to the **audit log**: the **count** of removed questions and their
    **question IDs** — never the question content (which is gone).
  - There is **no `source_deleted` enum value and no retained-with-flag state.** The earlier
    `review_status` enum (`unreviewed, reviewed_kept, reviewed_edited, reviewed_rejected`) is
    unchanged and gains no `source_deleted` member; removal is deletion of the row, not a status
    (R7, resolving W-6/W-7 and the §17.1-vs-index-lifecycle-§12.1 contradiction). The deletion flow
    lives in [index-lifecycle.md](../architecture/index-lifecycle.md) §12.1.
- `tenancy` present (Phase 0 MUST).

## Golden-corpus expressibility

- **Known-good question set (§18.1 retrieval-quality layer):** an `EvalSet` with
  `review_status=reviewed_kept` questions and `expected_segment_ids` for regression testing
  (§18.2 retrieval-quality layer fails the build on regression).
- **Provisional generated set:** `origin=generated`, mixed `question_type`s stratified per §9.1,
  `confidence_level=provisional`, each question `review_status=unreviewed` and carrying
  `source_segment_ids`.
- **Imported production query log:** `origin=imported_query_log`, questions with
  `generation_method=query_log`, `source_unknown=true`, `confidence_level=production_derived` —
  supersedes generation (§9.2).

## Open questions

1. **Small-corpus behaviour.** §9.3 says decline to sweep below a document threshold. **Proposed
   default:** an `EvalSet` may still be generated for inspection but is flagged
   `confidence_level=provisional` and marked non-sweepable; the sweep contract (not this one)
   enforces the decline. **Flagged** — needs the threshold pinned (Open Decision §20.7).
2. **Answer text vs answerless questions.** Whether questions carry a gold answer or only gold
   segments. **Proposed default:** gold **segments** only (`expected_segment_ids`) — the platform
   serves retrieval, not answers (§1.3 boundary); scoring is context recall/precision (§9.3), not
   answer correctness. **Flagged.**
3. **Query-log privacy.** Imported query logs may contain PII. **Proposed default:** treat imported
   query text as sensitive; apply §14.4 detection and never export it in a secret-free config.
   **Flagged.**
4. **Cross-KB eval sharing.** Whether an eval set can span KBs in a workspace. **Proposed
   default:** no in v1 — an eval set belongs to one KB (ties to Open Decision §20.6). **Flagged.**
