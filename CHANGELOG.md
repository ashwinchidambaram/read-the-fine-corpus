# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
