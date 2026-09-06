# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 4 closeout: §19 acceptance test layer (`tests/phase4/test_acceptance.py`) with 17 tests across 5 classes (TestIsolationAndBreakGlass, TestFourReindexTriggers, TestT06Rollback, TestT08DeletionCompleteness, TestMCPTrustLabel) mapping all five §19 Phase 4 criteria; T-08 container-gated integration tier (`tests/phase4/integration/test_t08_integration.py`); seven new/updated runbooks (rollback.md, restore-cold.md, purge.md, tombstone-replay.md, budget-cap-hit.md, break-glass.md, backup.md); traceability truth pass for all Phase 4 MUSTs (M-001..M-004, M-052..M-054, M-060..M-063, M-072..M-073, M-085..M-090, M-096, M-101, M-102, T-02, T-05, T-06, T-08); decision ledger D-04, D-05, D-06, D-16, D-19, D-20, D-23, D-24 CLOSED, D-17 deferred to Phase 5, new entries D-36..D-39 CLOSED.
- Phase 4 MCP server with trust labels — `finecorpus.mcp` module (`query_knowledge_base` + `explain_query` tools), `_TRUST_STATEMENT` in every tool description, `_D24_ADVISORY` on `explain_query`, API-key auth with `_validate_key()`, per-KB scope enforcement `_authorize_kb()`, `ExplainBlock` with `untrusted_ingested` label for LLM free-text (M-062, M-063, D-24) (#36).
- Phase 4 tenancy enforcement and rate limiting — server-side `_build_tenancy_filter()`, `TenancyScope` per KB, `TokenBucketLimiter` (ADR-0008), 429+Retry-After, per-tenant quotas at retrieval service (M-060, M-061, M-101, D-37) (#33).
- Phase 4 reindex triggers and shadow validation — manual, scheduled (anchor-then-fire cron), change-detected (content hash comparison), config-change (fires `reindex_full` + `acknowledged_permission_gap` D-16 gate); `validate_shadow()` gates (1-4); `consecutive_cap_hits` budget alert (M-052, M-053, M-054, M-073, M-085) (#34).
- Phase 4 deletion lifecycle — `delete_document()` removing all derived artifacts (chunks in live+N-1, augmentation, LLM cache), tombstone log, `snapshot_cold()` / `restore_from_snapshot()` with `_RESTORED_UNREPLAYED_MARKER_KEY` blocking promotion until tombstone replay completes (`RestoredUnreplayedError`), `scan_orphans()`, `purge` destroying cold snapshots (D-05); delete vs purge CLI distinction (M-086, M-087, M-088, M-089) (#31).
- Phase 4 break-glass access control — `issue_grant()` with `_validate_reason()` (reason required, M-001) and `_validate_window()` (finite 4h default, D-04, M-002), per-read audit rows, fail-closed on audit write failure (M-003, D-38), `notified_principals` capture (M-004, partial), control API routes (`POST /admin/break-glass/grant`, `GET /admin/break-glass/active`, `DELETE /admin/break-glass/{grant_id}`) (#35).
- Phase 4 export and telemetry — `corpus kb export` command with optional `--include-chunks`, eval set export deferred Phase 5 (M-090); `finecorpus.observability` module with OTel traces and Prometheus metrics across all five domains: ingestion, index, retrieval, quality, governance (M-102) (#36).
- Phase 4 job queue and budget controls — `JobState.paused_budget` on cap hit, `pause_budget()` / `resume()`, `corpus jobs resume <job_id>` resumes from last checkpoint; job control routes (M-085) (#32).
- Phase 4 admin interfaces — per-KB reindex trigger CRUD, break-glass grant management, job queue management, startup reconciliation for alias consistency (#29, #30).
- Phase 3 closeout: §19 acceptance test layer (`tests/phase3/test_acceptance.py`) mapping all four §19 Phase 3 criteria (TestRoutingPlan, TestT04, TestConfigRoundtrip, TestCostGate); manifest 1.1.0 with additive `phase3` block on all 21 fixtures (measured-then-pinned expected_chunking_strategy and expected_augmentation values); traceability truth pass for Phase 3 MUSTs (M-005, M-015, M-025, M-026, M-031–M-041, M-067, M-071, M-097, M-099, T-04); decision ledger D-21 CLOSED, D-14/D-22 deferral notes updated, D-33–D-35 CLOSED (Phase 3 rulings); new architecture doc `docs/architecture/llm-provider.md`; new pipeline docs `docs/pipeline/plan.md` and `docs/pipeline/transformations-preview-costing.md`; `docs/configuration/reference.md §2.4` updated with `endpoint` and `max_output_tokens` keys; troubleshooting entries for pytest norecursedirs and absolute-path subprocess cwd.
- Phase 3 build wiring, preview, and costing — `corpus pipeline preview` subcommand (dry_run Build), cost estimation gate with CostEstimateUnavailable honesty path (D-35), `resolve_costing_providers()` returning unavailable sentinel instead of fake $0.00 for declared paid providers without credentials (#27).
- Phase 3 real Plan stage — heuristic recommender over corpus statistics (`CorpusStatsPass`), §6.4 matrix encoding (prose/table/code/scanned/heading/boilerplate class rules), LanguageSupportDecision (M-041), config_version derivation excluding retrieval-treatment-only changes (M-015), export/import with tamper detection and secret-free attestation (M-026, M-071), `corpus pipeline build` now guarded by cost-gate confirmation (#26).
- Phase 3 IngestionConfig 1.2.0 and config IO — `IngestionConfig.schema_version=1.2.0`, `ClassDescription`, `RecommendationProvenance.basis` enum (heuristic/sweep_backed/class_description), `Tier3Settings`, `ExclusionDecision.remediation`, `LanguageSupportDecision`, `SpreadsheetTriageDecision`; `export_config()` / `import_config()` with denylist secret scan and config_version tamper detection; `IngestionConfig.secret_free_attestation: Literal[True]` (#25).
- Phase 3 Build transformations — Tier 1 (whitespace_repair, table_to_markdown, ocr_cleanup), Tier 2 (breadcrumb_augment, table_description, class_context), T-04 byte-identity invariant enforced (chunk.text = canonical[char_start:char_end] position-exact), per-chunk TransformationRecord with changed_text, augmentation fields on chunk payload (#24).
- Phase 3 LLM provider layer — `finecorpus.llm` module (LLMProvider ABC, ResolvedOpConfig, FakeLLMProvider, OpenAILLMProvider, OllamaLLMProvider); M-067 content-as-data framing (`<document_content>` delimiter); schema-validated structured output; max_output_tokens ceiling (D-21); exponential backoff with jitter (#23).
- Phase 2 closeout: acceptance verification over all 21 fixtures (manifest-vs-pipeline parity, HTML reassembly, spreadsheet triage at corpus scale, adversarial flagging); traceability truth pass for all Phase 2 MUSTs (M-017–M-022, M-024, M-039, M-066, M-074, M-097–M-099, M-105, T-09); decision ledger D-27–D-31 and open questions OQ-3, OQ-5, OQ-6.
- Phase 2 findings and exclusion reports — `finecorpus.pipeline.report.generate_report()` consuming collect/assess/decompose artifacts, Markdown and JSON output, exclusion-completeness invariant (every ExclusionRecord surfaces), corpus report CLI subcommand, zero silent gaps (#21).
- Phase 2 full taxonomy typing, cross-references, and language detection — 14-type segment taxonomy fully assigned, intra-document cross-reference resolution with explicit unresolved records, per-segment BCP-47 language codes via langdetect, TaxonomyPass + XrefResolvePass + LanguagePass in the decompose pipeline (§6.3, §6.4, §7.6) (#20).
- Phase 2 corpus-wide boilerplate detection and near-duplicate version families — word-5-gram Jaccard clustering into version families (newest primary, threshold 0.50), automatic boilerplate-block detection (0.30 proportion / 0.50 small-corpus), D-25 enforced: superseded docs yield empty segment sets with honest exclusion records (#19).
- Phase 2 OCR assessment with per-page confidence — scanned-PDF parsing via tesseract with per-page confidence retained raw (§6.2 MUST), sub-decomposition above 0.85, tier bands for low-confidence pages, honest missing-binary failure path; `image_only.pdf` graduates from unservable to OCR-parsed (#18).
- Phase 2 spreadsheet and HTML parsers with fixed triage — §6.4 spreadsheet triage (report/database/model), report kind parsed (sheets → sections, tables verbatim), database/model honestly excluded with reasons, HTML parser (stdlib) for Confluence-style exports (headings/prose/code/lists/tables typed with explicit segment taxonomy) (#17).
- Phase 2 §14.1 security detection — invisible-content detection via PDF content-stream analysis (white-on-white, sub-2pt fonts, off-MediaBox positioning), heuristic injection-suspicion scoring as a decompose pass (7 pattern classes, length-normalized), M-105 asserted: max suspicion never changes tier or excludes (#16).
- Phase 1 closeout (this PR): `corpus init` first-run provider prompt (M-103, §4.6) with scripted and interactive modes, preflight integration, secret-never-written guarantee, and 12 tests in `tests/config/test_init_flow.py`; real troubleshooting docs with per-error-code operator actions; `docs/runbooks/ingest.md` runbook; traceability status updates for C-1, C-2, M-008, M-011, M-064, M-097 (partial), M-103, T-01, T-07.
- Phase 1 acceptance test layer — T-01 alias swap under load, T-07 embedding model mismatch end-to-end, provider parity (Ollama + OpenAI chunk-identity invariant), and throughput baseline (#13).
- Phase 1 REST retrieval service — dense query path, fail-closed semantics (EMBEDDING_MODEL_MISMATCH, PROVIDER_UNAVAILABLE, KB_NOT_READY, CONTROL_PLANE_UNAVAILABLE, PAYLOAD_CORRUPT), full §8 provenance in every result, and complete API reference (#12).
- Phase 1 reference chunker and real Build stage — recursive-character splitting at 512 tokens / 50-token overlap, provenance-complete chunks, shadow-collection write (C-4), per-document resumability checkpoints (#11).
- Phase 1 real Assess and Decompose for native-text PDF prose; D-26 CLOSED: ParseResultBatch and SegmentSetBatch promoted to official §12 contracts with SpecRange declarations and version-check enforcement at all five stage boundaries (#10).
- Phase 1 embedding providers — OpenAI (`text-embedding-3-*`) and Ollama (`nomic-embed-text` / `bge-m3`) with deterministic FakeProvider, query-embedding cache, and real preflight health check with dimension verification (#9).
- Phase 1 vector-index adapter, QdrantAdapter backend, alias lifecycle (create_shadow / promote / rollback), AliasRepository control-plane records, and model-identity mismatch check (#8).
- Design package (pre-implementation, spec build rule 2): architecture pages (overview,
  segment taxonomy, provider abstraction, index lifecycle), seven data-contract schemas with
  deterministic chunk-identity derivation, configuration reference (90 consolidated options),
  runbook index, ADRs 0001–0006, subagent delegation model, decision ledger, MUST traceability
  matrix (~120 requirements), golden-corpus paper walkthroughs, and four adversarial design
  reviews (correctness, security, operability, determinism) with arbitration rulings applied.
- Phase 0 contracts package (#4): seven Pydantic v2 contract models (Inventory, ParseResult,
  SegmentSet, IngestionConfig, Chunk, EvalSet, RetrievalResponse) with shared TenancyBlock +
  Provenance blocks, deterministic chunk-identity derivation (chk_ prefix, SHA-256/base32),
  ContractVersionError + SpecRange for consumer-side version gating, and model-level invariants
  (Tier 2 changed_text=False, source_mirrored requires connector, Literal[True] opt-in acks).
- Phase 0 golden corpus fixture (#5): 21 synthetic fixtures generated via fpdf2/openpyxl/PIL
  covering all §18.1 mandatory categories (clean PDF, scanned PDF, bloated manual, nested
  tables, three spreadsheet kinds, Confluence HTML, near-duplicate family, unservable set,
  adversarial document with injection + invisible-content flags, form fields, malformed PDF);
  manifest.yaml with sha256 integrity; determinism-tested generator scripts.
- Phase 0 config module + preflight (#3): single-file Config model (90 options, Pydantic v2),
  three-layer load_config (YAML → env-var override → built-in defaults), FINECORPUS_ env
  prefix with __ path nesting, plaintext-secret rejection, and preflight check framework with
  PASS/FAIL/SKIPPED status per check.
- Phase 0 compose bring-up (#2): docker-compose.yml with all five service stubs
  (retrieval-api, ingest-worker, embedding-service, control-api, web-ui); healthz endpoint
  per HTTP service; Dockerfile + entrypoint; service health-check wiring.
- Phase 0 pipeline skeleton + corpus CLI seed (#6): five-stage orchestrator
  (Collect → Assess → Decompose → Plan → Build) with ArtifactStore (JSON artifacts,
  schema_version gating), Stage ABC with contract-version validation and output validation,
  empty end-to-end run over the golden corpus, and corpus CLI (pipeline run + preflight).
- Phase 0 CI hardening (this PR): mypy strict-ish type checking on src/ (pydantic plugin,
  disallow_untyped_defs, ignore_missing_imports for fpdf/openpyxl/PIL); import-linter with
  two contracts enforcing C-5 (layers contract + forbidden-imports contract); both added to
  the CI test job alongside existing ruff and pytest steps.
