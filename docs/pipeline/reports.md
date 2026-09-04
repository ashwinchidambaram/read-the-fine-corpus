# Findings and Exclusion Reports

Phase 2 introduces two client-facing reports generated from pipeline artifacts: a **findings report** and an **exclusion report**. Both are produced by `finecorpus.pipeline.report` and exposed through the `corpus report` CLI command.

---

## Command

```
corpus report --artifacts <dir> --run-id <id> [--format md|json|both] [--out <path>]
```

| Flag | Required | Default | Description |
|---|---|---|---|
| `--artifacts` | yes | — | Artifact root used with `corpus pipeline run` |
| `--run-id` | yes | — | Pipeline run ID to report on |
| `--format` | no | `both` | Output format: `md`, `json`, or `both` |
| `--out` | no | stdout | Directory to write report files into |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Success |
| 3 | Report generation error (malformed or missing assess artifact) |
| 4 | Artifact store error (run directory not found) |

**File names written to `--out` directory**

| Key | File |
|---|---|
| `findings_md` | `<run-id>-findings.md` |
| `findings_json` | `<run-id>-findings.json` |
| `exclusions_md` | `<run-id>-exclusions.md` |
| `exclusions_json` | `<run-id>-exclusions.json` |

---

## Findings Report

The findings report surfaces per-document parse quality, security flags, triage classification, OCR confidence, near-duplicate version families, and corpus-wide boilerplate blocks.

### JSON schema

```json
{
  "schema_version": "1.0.0",
  "contract": "findings_report",
  "run_id": "<run-id>",
  "generated_at": "<ISO-8601>",
  "summary": {
    "total_documents": 42,
    "parsed_ok": 38,
    "partial_parse": 2,
    "failed_parse": 0,
    "excluded_pre_parse": 2,
    "version_families": 1,
    "boilerplate_blocks": 3,
    "total_invisible_content_detections": 5,
    "documents_with_injection_signals": 1
  },
  "version_families": [...],
  "boilerplate_blocks": [...],
  "documents": [
    {
      "document_id": "<doc-id>",
      "source_path": "/path/to/file.pdf",
      "parse_status": "parsed",
      "document_kind": "text_pdf",
      "security": {
        "invisible_content_count": 0,
        "injection_max_suspicion": 0.0
      },
      "ocr_summary": {
        "page_count": 10,
        "ocr_page_count": 3,
        "min_confidence": 0.72,
        "mean_confidence": 0.91
      },
      "triage": {
        "severity": "warning",
        "message": "DATABASE | signal: single_sheet_uniform rows=501 >= 50"
      },
      "mixed_pdf_scanned_pages": [2, 5, 9],
      "dedup_role": "primary",
      "boilerplate_candidate_count": 2,
      "language_distribution": {"en": 0.95, "fr": 0.05}
    }
  ]
}
```

### Markdown sections

1. **Summary** — counts table
2. **Language Distribution** — aggregate language ratios across the corpus
3. **Near-Duplicate Version Families** — one table per family listing members, primacy, and source path
4. **Corpus-Wide Boilerplate Blocks** — blocks repeated across 2+ documents with sample text
5. **Per-Document Table** — all documents sorted by source path with parse status, kind, and security flags
6. **Document Detail** — per-document section with triage, OCR, mixed-PDF scanned pages, and language breakdown

---

## Exclusion Report

The exclusion report lists every document or segment excluded from indexing with a reason code, reason detail, and scope. Zero silent gaps: every `ExclusionRecord` in the decompose artifact appears exactly once.

### JSON schema

```json
{
  "schema_version": "1.0.0",
  "contract": "exclusions_report",
  "run_id": "<run-id>",
  "generated_at": "<ISO-8601>",
  "summary": {
    "total_exclusions": 7,
    "by_reason": {
      "spreadsheet_database": 1,
      "superseded_version": 2,
      "too_short": 4
    }
  },
  "exclusions": [
    {
      "exclusion_id": "<exc-id>",
      "document_id": "<doc-id>",
      "source_path": "/path/to/file.xlsx",
      "reason": "spreadsheet_database",
      "reason_detail": "Spreadsheet classified as database/dump (rows >= threshold).",
      "scope": "document",
      "reversible": false,
      "primary_document_id": null,
      "primary_source_path": null
    }
  ]
}
```

### Reason codes

| Code | Scope | Reversible | User action |
|---|---|---|---|
| `unservable_content` | document | no | Remove or replace the document |
| `spreadsheet_database` | document | no | Convert the spreadsheet to a report-style format |
| `spreadsheet_model` | document | no | Convert the spreadsheet to a report-style format |
| `encrypted` | document | no | Decrypt the file and re-run the pipeline |
| `parse_failed` | document | yes | Inspect the document for corruption; re-run after repair |
| `superseded_version` | document | yes | Keep only the newest version, or set `index_superseded_versions=true` |
| `duplicate` | document | yes | Remove exact duplicates from the source directory |
| `empty_region` | segment | yes | Investigate source document; may be expected |
| `too_short` | segment | yes | Review minimum segment length in configuration |
| `other` | segment | yes | Inspect `reason_detail` for specifics |

The `superseded_version` exclusions carry two extra fields:

- `primary_document_id` — document ID of the newer (kept) version
- `primary_source_path` — source path of the newer (kept) version

### Markdown sections

1. **Summary** — total count and breakdown by reason
2. **Grouped by reason** — each reason gets a table of affected documents/segments with source path, scope, and guidance

---

## Architecture

Report logic lives in `finecorpus.pipeline.report` (pipeline layer). The CLI in `finecorpus.cli.main` is a thin consumer that calls `generate_report()` and writes or prints the result. This satisfies constraint C-5 (cli/services above pipeline in the layer hierarchy).

The `generate_report(artifacts_root, run_id)` function:

1. Loads the collect artifact (optional, for source path enrichment).
2. Loads the assess artifact (required, for parse results).
3. Loads the decompose artifact (optional, for segment-level exclusions).
4. Returns a `ReportResult` dataclass with `findings_md`, `exclusions_md`, `findings_json`, `exclusions_json`, and a `write()` method.

**Exclusion completeness invariant**: `_build_exclusions_json` uses segment-set `ExclusionRecord` objects as the sole source of truth. The `DecomposeStage` creates `ExclusionRecord`s for all excluded content (including pre-parse exclusions such as spreadsheets and encrypted files), so no additional derivation is needed and there are no duplicates.

---

## Troubleshooting

**`ERROR: Artifact store`** (exit 4): The `--artifacts` directory or run subdirectory does not exist. Verify the path and run ID match what was used with `corpus pipeline run`.

**`ERROR: Report generation — No assess artifact found`** (exit 3): The assess stage artifact is missing. Re-run the pipeline to produce it.

**All documents show `source_path = <document-id>`**: The collect artifact is missing. The report falls back to document IDs as source paths. Re-run the pipeline from the collect stage.

**Exclusion report has fewer entries than expected**: Check the decompose artifact directly for `exclusions` fields in each `segment_set`. If the decompose artifact is missing, the report will show zero exclusions.
