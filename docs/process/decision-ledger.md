# Decision Ledger

This ledger tracks every open decision identified during design and build (spec §20, build rule 11). Each decision must be closed or consciously deferred before the phase that depends on it. Release gate item 7 requires every row to show either a resolution or an explicit deferral with justification.

## Decisions Table

| ID | Decision | Status | Needed by | Resolution / Link |
|---|---|---|---|---|
| D-01 | OSS license model — which license, and is anything held back? | CLOSED 2026-09-03 | Phase 0 | Apache-2.0 on entire repository. See ADR-0001. |
| D-02 | Reference embedding provider pair — cloud and local providers for first-run prompt | CLOSED 2026-09-03 | Phase 1 | OpenAI (`text-embedding-3-*`) + Ollama (`nomic-embed-text` / `bge-m3`). See ADR-0002. |
| D-03 | Segment type taxonomy — executor proposes in design document | PROPOSED | Phase 2 | Proposal complete at `docs/architecture/segment-taxonomy.md` (13 types, 4 tiers, 11 open questions); pending design review. |
| D-04 | Break-glass grant window default — how long should admin content-access grants remain valid? | OPEN | Phase 4 | — |
| D-05 | Snapshot retention period, and interaction with erasure obligations — how long to retain N-1 and cold snapshots? | OPEN | Phase 4 | — |
| D-06 | Cross-KB retrieval within a workspace — is it supported, and what are the permission semantics? | OPEN | Phase 4 | — |
| D-07 | Corpus scale ceiling for v1, and sampling behavior above it — what is the maximum useful corpus size, and how to degrade gracefully? | OPEN | Phase 5 | — |
| D-08 | UI authentication model — local accounts, OIDC, or both? | OPEN | Phase 6 | — |
| D-09 | Name and domain registration — confirm `finecorpus` and `corpus` CLI binary are free at registries and secure domain | OPEN | Before public release | — |
| D-10 | **PROPOSED spec amendment (R1 / OQ-L-10):** amend or annotate spec constraint **C-4** ("the ingestion path MUST write to a shadow collection, never to the live one") to admit the opt-in direct-to-live incremental mode. The v1 default (clone-and-swap incremental, index-lifecycle §8.2) honors C-4 and §10.5; the opt-in direct-to-live mode (§8.2/§8.3) explicitly relaxes §10.5's no-partial-state window to the delete step and bypasses C-4. **The design records this as PROPOSED; the literal spec text of C-4 needs product-owner sign-off** (amend the wording, or record the opt-in as a documented exception). | PROPOSED | Phase 4 (incremental upsert) | Design at index-lifecycle.md §8 (marked PROPOSED), OQ-L-10. Owner sign-off pending. |
| D-11 | **PROPOSED spec amendment (R6 / C-R1, W-1, determinism attack 7):** §12's "chunk text is byte-identical to source" is proposed to be **defined against the Tier-1-normalized canonical source**, not the raw original. The literal reading is unsatisfiable because §7.2's Tier 1 (e.g. table-to-markdown) necessarily changes bytes. Design records per-op `changed_text` flags, retains the raw original addressably, and asserts Tier 2 never touches text. **Needs owner sign-off** on the amended definition. | PROPOSED | Phase 2 (Build) | Design at contracts/README.md TransformationRecord "Canonical source text" and chunk.md invariants (both marked PROPOSED-pending). |
| D-12 | `segment_path` canonical **format** must be pinned as a closed decision before Phase 1 (C-R4). R2's frozen-artifact rule guarantees its *reproducibility*, but the literal string format (`"/".join(structural_path) + "#" + within_heading_ordinal`, plus edge cases: heading-less docs, nested tables, cross-document refs) is still an open format decision that ADR-0006 depends on. | OPEN | Phase 1 | Referenced: segment-set.md OQ-1, chunk.md, ADR-0006. |
| D-13 | `figure_region` (and any null-`text`) segments vs the required `chunk.text` field (C-R6): decide whether such segments produce a chunk at all. Recommended reading — a segment with no textual representation produces no chunk; `chunk.text` becomes "required unless the segment type has no textual representation", with a closed list of such types; the taxonomy "everything is indexed" invariant is amended to "every segment with textual content is indexed." Needs a decision + coordinated taxonomy/segment-set/chunk update. | OPEN | Phase 2 | segment-taxonomy.md OQ-2; contracts/chunk.md, segment-set.md. |
| D-14 | Tier 3 "visible at citation time" (C-R7): §7.2 requires the original be visible at citation time, but the Chunk contract has no field to return both original and rewritten text; the original is only reachable via a `source_location` round-trip. Decide whether to add `original_text: str \| null` to the Chunk (populated when `tier=3, changed_text=true`) and surface both in the retrieval response. | OPEN | Phase 2 | contracts/chunk.md; contracts/retrieval-response.md. |
| D-15 | Reassembly of null-`text` segments (C-R9): `ReassemblyRecord.method=document_order_concat` is undefined when a segment's `text` is null (figures, some headings). Decide the rule (recommended: null-text segments contribute the empty string) and add it as a named invariant + property-test clause. | OPEN | Phase 2 | contracts/segment-set.md ReassemblyRecord. |
| D-16 | Connector permission-gap acknowledgement hardening (S-R2, T-2): make `SourceRun.acknowledged_permission_gap` **required with no default**; propagate the acknowledgement into every chunk's `TenancyBlock` (or a retrieval-visible field); and specify the Plan/Build check that blocks ingestion when `permission_fidelity=unavailable` and the gap is unacknowledged, recording the override as a governance audit event. | OPEN | Phase 4 | contracts/inventory.md SourceRun; contracts/README.md TenancyBlock; index-lifecycle Plan/Build gates. |
| D-17 | Source-mirrored permission staleness (S-R3): `permission_mode=source_mirrored` ACLs can go stale with no refresh mechanism and no retrieval-time freshness check. Decide a max-staleness policy (e.g. `permission_max_staleness_seconds`), a permissions-only refresh path (metadata-only update), and whether stale mirrored chunks fail closed (`PERMISSION_STALE`). | OPEN | Phase 4 | contracts/README.md TenancyBlock; index-lifecycle §7.2 metadata-only path. |
| D-18 | Provider log/trace URL-credential stripping (S-R4): the log-exclusion rule does not cover credentials embedded in provider URLs (userinfo `user:password@`, `api_key`/`token` query params). Decide the URL-sanitization rule and extend §18.3 test 10 to cover URL-embedded credential forms. | OPEN | Phase 1 | provider-abstraction.md §6.2. |
| D-19 | Trust-labelling of LLM free-text provenance in explain output (S-R5): `reasoning`/`diff_summary` are derived from untrusted document content but are surfaced in explain mode (§11.5). Decide that explain output labels them `untrusted_ingested`, the UI renders them as literal text (never HTML), and whether to cap their length. | OPEN | Phase 4 | provider-abstraction.md §4.4; contracts/retrieval-response.md ExplainBlock. |
| D-20 | Salience/classification anomaly detection (S-R6, T-1): LLM `salience_tier` classification is a high-value injection/poisoning target (a document steering high-value content to `boilerplate`/`excluded`, or adversarial content to `primary`). Decide a corpus-level integrity check (e.g. block promotion / halt ingestion when a class's tier distribution shifts beyond a threshold vs the prior build) and a pre-promotion salience-distribution gate. | OPEN | Phase 4 | provider-abstraction.md §4.3; segment-taxonomy.md §4; index-lifecycle §5 gates. |
| D-21 | Config-export runtime secret scan + token-count sanity ceiling (S-R7, S-R8): decide whether the config export additionally runs a runtime secret-pattern scan (defense-in-depth beyond the structural `secret_free_attestation`), and whether the cost tracker applies a sanity ceiling on provider-reported `input_tokens_used` against a compromised/misconfigured endpoint. | OPEN | Phase 3 | provider-abstraction.md §6.3, §2.3; ingestion-config.md. |
| D-22 | Eval `question_text` injection scanning (S-R13): generated eval `question_text` is free text later used as a prompt in automated sweeps (§9.3). Decide whether to add an `injection_suspicion` score to `EvalQuestion` and auto-flag high-suspicion questions to `unreviewed` before automated use. | OPEN | Phase 3 | contracts/eval-set.md; provider-abstraction.md §4.3. |
| D-23 | `confidence_floor` retrieval-policy warning + query-embedding cache flush scope (S-R14, S-R15): decide the UI warning when `confidence_floor` is lowered (previously-excluded low-confidence content becomes retrievable — a trust-gating change, audit-logged), and clarify that the per-KB cache flush scopes on the KB's current/former `model_id`. | OPEN | Phase 4 | ingestion-config.md; provider-abstraction.md §5.4. |
| D-24 | Injection-suspicion filter as content-suppression vector (T-3): decide advisory guidance (MCP tool description, retrieval API reference) that `injection_suspicion` is an advisory signal, not an access-control verdict, and optional observability for sessions repeatedly filtered to zero by suspicion filters. | OPEN | Phase 4 | provider-abstraction.md; contracts/retrieval-response.md. |

### Known limitation — branch protection

Server-side branch protection on `main` is unavailable on the current GitHub plan (free plan,
private repo). Until the repo is public or upgraded, the PR-only, CI-green-before-merge rule is
enforced by orchestrator discipline rather than by GitHub. Enable protection (required checks:
`test`, `docs-links`; PRs required) as the first act when the repository visibility changes.
Recorded 2026-09-03.

## Process Notes

- **CLOSED decisions** are resolved and have a link to the ADR, spec section, or documentation that captures the resolution.
- **PROPOSED decisions** are being designed and pending review. They will move to CLOSED once the design review approves them.
- **OPEN decisions** are recognized but not yet scheduled for resolution. They are not blockers for earlier phases but must be resolved before the listed phase begins.
- **Deferred decisions** (if any) are tracked with explicit reasoning for why they are being deferred and what phase they move to. A deferral is a deliberate choice, not an oversight.

## Design-Phase Open Questions

During design review, any ambiguities surfaced in individual architecture pages (e.g., bottom-of-page "open question" sections) are consolidated here at the end of review. Each one either becomes a decision (assigned an ID and added to the table) or is resolved by design changes.

---

**Last updated:** 2026-09-03 (initial ledger; updated as decisions are made and phases complete)
