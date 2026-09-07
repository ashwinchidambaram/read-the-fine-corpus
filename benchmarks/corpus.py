"""Synthetic + golden corpus helpers for the benchmark harness.

Only native-text PDFs currently produce chunks through the real Assess→Build
path (the Phase-1 throughput baseline uses exactly this subset).  ``.txt`` /
``.md`` files route to the honest-exclusion parser and are skipped, so a
synthetic text corpus would measure "0 docs".  We therefore reuse the golden
native-PDF subset for ingestion/throughput, and synthesize a larger corpus for
cold-start by tiling that subset (symlinks) up to the requested document count.

This mirrors ``tests/phase1/test_throughput_baseline.py`` so the FAKE-mode
throughput number is directly comparable to the recorded Phase-1 baseline
methodology (same corpus, same pipeline, provider swapped for FakeProvider).
"""

from __future__ import annotations

import pathlib
import platform
import shutil
import tempfile

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_FIXTURE_ROOT = _REPO_ROOT / "tests" / "fixtures" / "golden"
_MANIFEST_PATH = _FIXTURE_ROOT / "manifest.yaml"
_CORPUS_ROOT = _FIXTURE_ROOT / "corpus"


def native_pdf_files() -> list[pathlib.Path]:
    """Return the golden native-PDF corpus files (same subset as the P1 baseline)."""
    with _MANIFEST_PATH.open() as f:
        manifest = yaml.safe_load(f)
    return [
        _CORPUS_ROOT / pathlib.Path(entry["file"]).name
        for entry in manifest["fixtures"]
        if entry.get("expected_triage_class") == "native_pdf"
        and (_CORPUS_ROOT / pathlib.Path(entry["file"]).name).exists()
    ]


def make_native_pdf_subset_dir() -> pathlib.Path:
    """Create a temp dir of symlinks to the native-PDF corpus files.

    Caller owns cleanup (``shutil.rmtree``).
    """
    subset_dir = pathlib.Path(tempfile.mkdtemp(prefix="rtfc-bench-src-"))
    for src in native_pdf_files():
        (subset_dir / src.name).symlink_to(src.resolve())
    return subset_dir


def _synthetic_pdf_bytes(doc_index: int, *, lines: int = 40) -> bytes:
    """Deterministically render a native-text PDF with unique content.

    Uses fpdf2 (a dev/bench dependency).  Each document gets distinct text so
    the pipeline's near-duplicate detection does NOT collapse the corpus — a
    real "N distinct docs" cold-start needs genuinely distinct content, not
    N byte-variants of the same source (which the assess/corpus passes dedup
    down to a handful of no_text_segments skips).
    """
    try:
        from fpdf import FPDF
    except ModuleNotFoundError as exc:  # pragma: no cover - env guard
        raise RuntimeError(
            "make_synthetic_corpus_dir requires fpdf2 (dev/bench dependency). "
            "Install it with `uv sync` (it is in the dev dependency group)."
        ) from exc

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    body = f"Synthetic benchmark document {doc_index}. " + " ".join(
        f"topic{doc_index}-term{j} unique content about subject {doc_index} item {j}."
        for j in range(lines)
    )
    pdf.multi_cell(0, 8, body)
    return bytes(pdf.output())


def make_synthetic_corpus_dir(target_docs: int) -> pathlib.Path:
    """Materialize ``target_docs`` DISTINCT native-text PDFs for scale testing.

    Used by the cold-start harness to synthesize a ~1k-doc corpus without
    shipping 1k real PDFs.  Every file has unique text content (rendered by
    fpdf2), so all ``target_docs`` pass through the full assess→build path as
    distinct documents — the pipeline does not dedup or skip them.

    Deterministic: the same ``target_docs`` always produces the same bytes.

    Args:
        target_docs: Number of distinct document files to materialize (>= 1).

    Returns:
        Path to a fresh temp dir; caller owns cleanup.
    """
    out = pathlib.Path(tempfile.mkdtemp(prefix="rtfc-bench-1k-"))
    for i in range(max(1, target_docs)):
        (out / f"doc_{i:05d}.pdf").write_bytes(_synthetic_pdf_bytes(i))
    return out


def cleanup_dir(path: pathlib.Path) -> None:
    """Best-effort recursive removal of a temp corpus dir."""
    shutil.rmtree(path, ignore_errors=True)


def env_description() -> str:
    """One-line description of the machine the benchmark ran on (no secrets)."""
    return (
        f"{platform.system()} {platform.machine()} | "
        f"Python {platform.python_version()} | host={platform.node() or 'unknown'}"
    )


__all__ = [
    "native_pdf_files",
    "make_native_pdf_subset_dir",
    "make_synthetic_corpus_dir",
    "cleanup_dir",
    "env_description",
]
