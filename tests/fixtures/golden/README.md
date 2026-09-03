# Golden Corpus Fixture Set

Phase 0 build artifact for Read The Fine Corpus (RTFC). Spec reference: §18.1, build rule 7.

This is the substrate for nearly every meaningful integration test. Every fixture is synthetic
and licence-clear by construction (§18.1: no real client documents).

## What is here

```
tests/fixtures/golden/
  corpus/          committed fixture files (19 files, ~1.4 MB total)
  generators/      Python generator scripts — one per fixture or fixture group
  manifest.yaml    machine-readable index: file, role, sha256, expected findings
  README.md        this file
```

## How to regenerate

```bash
uv run python tests/fixtures/golden/generators/run_all.py
```

This regenerates all fixtures deterministically (fixed seeds, no wall-clock content).
After regenerating, update sha256 values in manifest.yaml if any generator changed:

```bash
# Check which hashes changed
uv run python -c "
import hashlib, yaml
from pathlib import Path
root = Path('tests/fixtures/golden')
manifest = yaml.safe_load((root / 'manifest.yaml').read_text())
for entry in manifest['fixtures']:
    f = root / entry['file']
    actual = hashlib.sha256(f.read_bytes()).hexdigest()
    expected = entry['sha256']
    status = 'OK' if actual == expected else 'CHANGED'
    print(f'{status}: {f.name}')
"
```

## Rule: fixtures change only via generators

Never hand-edit files in `corpus/`. If a fixture needs to change:
1. Edit the relevant generator in `generators/`.
2. Run `uv run python tests/fixtures/golden/generators/run_all.py`.
3. Update `manifest.yaml` sha256 values.
4. Commit generator + corpus file + manifest together.

This rule ensures reproducibility: the generator is the source of truth.

## Fixture inventory

| File | §18.1 Role | Key properties |
|---|---|---|
| `clean_native.pdf` | Clean native-text PDF | Headings, prose, table, list — happy path baseline |
| `scanned_poor.pdf` | Poorly scanned PDF | 12 rasterized pages; page 3 OCR ~0.42 (Case 2) |
| `bloated_manual.pdf` | Bloated manual | Boilerplate, cross-refs, mixed content (Case 1) |
| `boilerplate_a.pdf` | Boilerplate auxiliary | Shares legal preamble — corpus detection trigger |
| `boilerplate_b.pdf` | Boilerplate auxiliary | Shares legal preamble — corpus detection trigger |
| `nested_tables.html` | Complex/nested tables | Inner table in cell, merged col/row headers |
| `report_spreadsheet.xlsx` | Spreadsheet: report kind | Narrative sheets, tables — ingest path (Case 3) |
| `database_spreadsheet.xlsx` | Spreadsheet: database kind | 501 rows, exclude path (Case 3) |
| `model_spreadsheet.xlsx` | Spreadsheet: model kind | Formula-dense amortization, exclude path (Case 3) |
| `confluence_export.html` | Confluence-style HTML export | Nav boilerplate, wiki links, code blocks |
| `policy_v1.pdf` | Near-duplicate: version 1 (superseded) | Oldest, superseded by v3 (Case 5) |
| `policy_v2.pdf` | Near-duplicate: version 2 (superseded) | Intermediate, superseded by v3 (Case 5) |
| `policy_v3.pdf` | Near-duplicate: version 3 (primary) | Newest — primary version at full tier (Case 5) |
| `password_protected.pdf` | Unservable: encrypted PDF | /Encrypt marker, no text without password |
| `audio_stub.wav` | Unservable: audio file | Valid RIFF/WAVE, 1 sec silence |
| `video_stub.mp4` | Unservable: video file | Valid ftyp/moov/mdat stub, 152 bytes |
| `image_only.pdf` | Unservable: image-only PDF | JPEG page, no text layer |
| `cad_binary.dwg` | Unservable: CAD binary | AC1015 magic bytes, deterministic filler |
| `adversarial.pdf` | Adversarial: injection + invisible content | 6 §14.1 vectors (Case 4, non-negotiable test 9) |
| `form_filled.pdf` | Filled form (Finding F-1) | label/value pairs, form_field coverage |
| `malformed_structure.pdf` | Malformed structure (Finding F-2) | Corrupted stream, unknown segment type test |

## §18.1 mandatory category coverage

| §18.1 item | Covered by |
|---|---|
| Clean native-text PDF | `clean_native.pdf` |
| Poorly scanned PDF with known-degraded regions | `scanned_poor.pdf` |
| Bloated manual with cross-references, boilerplate, mixed content | `bloated_manual.pdf` + `boilerplate_a.pdf` + `boilerplate_b.pdf` |
| Document with complex/nested tables | `nested_tables.html` |
| Spreadsheet — report kind | `report_spreadsheet.xlsx` |
| Spreadsheet — database kind | `database_spreadsheet.xlsx` |
| Spreadsheet — model kind | `model_spreadsheet.xlsx` |
| Confluence-style HTML export | `confluence_export.html` |
| Document with heavy near-duplicate siblings | `policy_v1.pdf` + `policy_v2.pdf` + `policy_v3.pdf` |
| Several unservable files | `password_protected.pdf`, `audio_stub.wav`, `video_stub.mp4`, `image_only.pdf`, `cad_binary.dwg` |
| Adversarial document (injection + invisible content) | `adversarial.pdf` |
| Form document (Finding F-1) | `form_filled.pdf` |
| Malformed-structure file (Finding F-2) | `malformed_structure.pdf` |
