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
| `findings_md` | `findings-<run-id>.md` |
| `findings_json` | `findings-<run-id>.json` |
| `exclusions_md` | `exclusions-<run-id>.md` |
| `exclusions_json` | `exclusions-<run-id>.json` |

---

## Findings Report

The findings report surfaces per-document parse quality, security flags, triage classification, OCR confidence, near-duplicate version families, and corpus-wide boilerplate blocks.

### JSON schema

```json
{
  "schema_version": "1.0.0",
  "contract": "findings_report",
  "run_id": "<run-id>",
  "summary": {
    "total_documents": 42,
    "parse_status_counts": {
      "parsed": 38,
      "partial": 2,
      "failed": 0,
      "excluded_pre_parse": 2
    },
    "version_family_count": 1,
    "boilerplate_block_count": 3,
    "total_invisible_content_detections": 5,
    "documents_with_injection_signals": 1,
    "corpus_language_distribution": [
      {"language": "en", "mean_fraction": 0.95},
      {"language": "fr", "mean_fraction": 0.05}
    ]
  },
  "version_families": [
    {
      "family_id": "fam-<hex>",
      "primary_document_id": "<doc-id>",
      "primary_source_path": "/path/to/newest.pdf",
      "primacy_basis": "source_modified_at",
      "superseded_document_ids": ["<older-doc-id>"],
      "superseded_source_paths": ["/path/to/older.pdf"],
      "similarity_scores": {
        "<older-doc-id>": 0.9127
      }
    }
  ],
  "boilerplate_blocks": ["...repeated text..."],
  "documents": [
    {
      "document_id": "<doc-id>",
      "source_path": "/path/to/file.pdf",
      "parse_status": "parsed",
      "document_kind": "text_pdf",
      "dedup_role": "primary",
      "quality": {
        "overall": 0.92,
        "is_near_empty": false,
        "text_extraction_ratio": null,
        "table_structure_retained": null
      },
      "security": {
        "invisible_content_count": 0,
        "invisible_content": [],
        "injection_max_suspicion": 0.0,
        "injection_flagged_segments": []
      },
      "ocr_summary": {
        "mean_ocr_confidence": 0.91,
        "min_page_confidence": 0.72,
        "page_count": 10,
        "scanned_page_count": 3
      },
      "mixed_pdf_scanned_pages": [2, 5, 9],
      "triage": {
        "code": "spreadsheet_triage",
        "severity": "warning",
        "message": "DATABASE | signal: single_sheet_uniform rows=501 >= 50"
      },
      "boilerplate_segment_count": 2,
      "segment_count": 47,
      "exclusion_count": 1,
      "language_distribution": [
        {"language": "en", "fraction": 0.95},
        {"language": "fr", "fraction": 0.05}
      ],
      "findings": [
        {"code": "link_record", "severity": "info", "message": "..."}
      ],
      "encoding_issues": []
    }
  ]
}
```

**Notes:**

- `summary.parse_status_counts` is a dict of `{status_string: count}` — not individual named fields.
- `summary.version_family_count` (not `version_families`) and `summary.boilerplate_block_count` (not `boilerplate_blocks`).
- `version_families[*].similarity_scores` is a per-member dict `{superseded_member_id: jaccard_score_vs_primary}`.
- `version_families[*].primacy_basis` is one of `"source_modified_at"`, `"discovered_at"`, or `"content_hash"` — explains why the primary was chosen.
- `ocr_summary` fields are `scanned_page_count`, `min_page_confidence`, `mean_ocr_confidence` (not `ocr_page_count` / `min_confidence` / `mean_confidence`).
- `document.boilerplate_segment_count` counts segments with `salience_tier == "boilerplate"` (not `boilerplate_candidate_count`).
- `language_distribution` is a list of `{language, fraction}` objects (not a flat dict).
- There is no `generated_at` field — the report is deterministic and wall-clock timestamps are omitted.

### Markdown sections

1. **Summary** — counts table
2. **Language Distribution** — aggregate language ratios across the corpus
3. **Near-Duplicate Version Families** — one entry per family listing primary (with primacy basis), superseded members, and per-member similarity scores
4. **Corpus-Wide Boilerplate Blocks** — blocks repeated across 2+ documents with sample text
5. **Per-Document Table** — all documents sorted by source path with parse status, kind, and security flags
6. **Document Detail** — per-document section with triage, OCR, mixed-PDF scanned pages, and language breakdown; `link_record` INFO findings are collapsed to a single count line

---

## Exclusion Report

The exclusion report lists every document or segment excluded from indexing with a reason code, reason detail, and scope. Zero silent gaps: every `ExclusionRecord` in the decompose artifact appears exactly once.

**If the decompose artifact is missing**, the report emits a prominent `WARNING` banner and sets `decompose_artifact_present: false` in the JSON. The "Nothing is silently dropped" guarantee does not apply in this case — re-run through the Decompose stage to obtain a complete report.

### JSON schema

```json
{
  "schema_version": "1.0.0",
  "contract": "exclusion_report",
  "run_id": "<run-id>",
  "decompose_artifact_present": true,
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
      "scope": "document",
      "reason": "spreadsheet_database",
      "reason_detail": "Spreadsheet classified as database/dump (rows >= threshold).",
      "reversible": false,
      "source_region_ids": [],
      "user_action": "Nothing — a row-oriented database spreadsheet ...",
      "primary_document_id": null,
      "primary_source_path": null
    }
  ]
}
```

**Note:** `contract` is `"exclusion_report"` (no `s`). There is no `generated_at` field.

### Reason codes

| Code | Scope | Reversible | User action |
|---|---|---|---|
| `unservable_content` | document | no | Nothing — see `reason_detail` for file-type specifics (audio, video, CAD, CSV, unrecognized extension) |
| `spreadsheet_database` | document | no | Convert the spreadsheet to a report-style format |
| `spreadsheet_model` | document | no | Convert the spreadsheet to a report-style format |
| `encrypted` | document | no | Decrypt the file and re-run the pipeline |
| `parse_failed` | document | yes | Inspect the document for corruption; re-run after repair |
| `superseded_version` | document | yes | Keep only the newest version, or set `index_superseded_versions=true` |
| `duplicate` | document | yes | Remove exact duplicates from the source directory |
| `empty_region` | segment | yes | Investigate source document; may be expected |
| `too_short` | segment | yes | Review minimum segment length in configuration |
| `other` | segment | yes | Inspect `reason_detail` for specifics |

**File-type exclusion detail:** All unservable file types (audio, video, CAD, CSV, unrecognized extensions) appear under the `unservable_content` reason code. The specific file-type reason is carried in `reason_detail`.

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

**Exclusion report has fewer entries than expected / shows WARNING**: The decompose artifact is missing. Check the decompose artifact directly for `exclusions` fields in each `segment_set`. Re-run the pipeline through the Decompose stage to obtain a complete exclusion report.
