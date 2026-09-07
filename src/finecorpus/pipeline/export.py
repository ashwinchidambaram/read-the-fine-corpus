"""KB export (M-090, §17.3).

``export_kb`` bundles all durable artifacts for a knowledge base into a
portable directory:

  <out_dir>/
    config.json              — ingestion config artifact (from artifact store)
    class_descriptions.json  — class definitions artifact (if present)
    findings_report.json     — findings report artifact (if present)
    exclusion_report.json    — exclusion report artifact (if present)
    chunks/
      chunks_<offset>.jsonl  — chunk payloads scrolled from live collection
    eval_sets/
      README.txt             — stub note (populated Phase 5)
    manifest.json            — export manifest with counts and SHA-256 hashes

The export is a point-in-time snapshot of the live collection.  It is NOT
a backup; it does not include Qdrant binary snapshots.

M-090 acceptance: the export contains config + chunks + reports + manifest
with accurate counts.

Spec references: §17.3 (export), §8 (provenance blocks), §6.4 (config export).
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ExportManifest
# ---------------------------------------------------------------------------


@dataclass
class ExportManifest:
    """Describes the contents of a KB export bundle.

    Attributes:
        kb_id: The exported knowledge base.
        exported_at: UTC timestamp of the export.
        chunk_count: Total number of chunk records written to chunks/.
        file_hashes: Dict mapping relative file path → SHA-256 hex digest.
        eval_sets_note: Advisory note about eval-set availability.
        eval_set_count: Number of eval sets exported (0 when none configured).
        eval_question_count: Total questions across all exported eval sets.
        out_dir: Absolute path to the export directory.
    """

    kb_id: str
    exported_at: datetime
    chunk_count: int
    file_hashes: dict[str, str] = field(default_factory=dict)
    eval_sets_note: str = ""
    eval_set_count: int = 0
    eval_question_count: int = 0
    out_dir: pathlib.Path = field(default_factory=pathlib.Path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "kb_id": self.kb_id,
            "exported_at": self.exported_at.isoformat(),
            "chunk_count": self.chunk_count,
            "file_hashes": self.file_hashes,
            "eval_sets_note": self.eval_sets_note,
            "eval_set_count": self.eval_set_count,
            "eval_question_count": self.eval_question_count,
            "out_dir": str(self.out_dir),
        }


# ---------------------------------------------------------------------------
# Helper: SHA-256 of a file
# ---------------------------------------------------------------------------


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Helper: try to load an artifact file, return None on missing
# ---------------------------------------------------------------------------


def _try_load_artifact(artifacts_root: pathlib.Path | None, run_id: str, stage: str) -> Any:
    """Attempt to load an artifact JSON file; return None if missing."""
    if artifacts_root is None:
        return None
    path = artifacts_root / run_id / f"{stage}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load artifact '%s' from %s: %s", stage, path, exc)
        return None


# ---------------------------------------------------------------------------
# Core export function
# ---------------------------------------------------------------------------


def export_kb(
    kb_id: str,
    out_dir: str | pathlib.Path,
    *,
    session: Any,
    adapter: Any,
    artifacts_root: str | pathlib.Path | None = None,
    run_id: str | None = None,
) -> ExportManifest:
    """Export a knowledge base to a portable bundle directory.

    Args:
        kb_id: The knowledge base to export.
        out_dir: Output directory (created if it does not exist).
        session: SQLAlchemy Session bound to the control-plane engine.
        adapter: IndexAdapter instance for scrolling the live collection.
        artifacts_root: Root of the artifact store (for config/report files).
            If None, artifact files are skipped.
        run_id: Pipeline run ID for artifact lookup.  Required when
            artifacts_root is provided.

    Returns:
        ExportManifest describing the bundle contents.

    Raises:
        ValueError: If the KB is not registered or has no live collection.
        OSError: On file-system errors.
    """
    out_path = pathlib.Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)
    art_root = pathlib.Path(artifacts_root).resolve() if artifacts_root else None

    # ------------------------------------------------------------------
    # Resolve live collection from alias record
    # ------------------------------------------------------------------
    from finecorpus.control.metadata import AliasRepository
    from finecorpus.index.adapter import alias_name

    alias_repo = AliasRepository(session)
    alias = alias_repo.get(alias_name(kb_id))
    if alias is None:
        raise ValueError(f"Knowledge base '{kb_id}' is not registered.")
    if not alias.collection_name:
        raise ValueError(f"Knowledge base '{kb_id}' has no live collection (never promoted).")
    collection = alias.collection_name

    file_hashes: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 1. Ingestion config artifact
    # ------------------------------------------------------------------
    if art_root and run_id:
        config_data = _try_load_artifact(art_root, run_id, "config")
        if config_data is not None:
            config_path = out_path / "config.json"
            config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")
            file_hashes["config.json"] = _sha256_file(config_path)
            logger.info("Exported config artifact → %s", config_path)

    # ------------------------------------------------------------------
    # 2. Class descriptions artifact
    # ------------------------------------------------------------------
    if art_root and run_id:
        cls_data = _try_load_artifact(art_root, run_id, "classes")
        if cls_data is not None:
            cls_path = out_path / "class_descriptions.json"
            cls_path.write_text(json.dumps(cls_data, indent=2), encoding="utf-8")
            file_hashes["class_descriptions.json"] = _sha256_file(cls_path)

    # ------------------------------------------------------------------
    # 3. Findings / exclusion reports
    # ------------------------------------------------------------------
    if art_root and run_id:
        for stage, fname in [
            ("report", "findings_report.json"),
            ("exclusion", "exclusion_report.json"),
        ]:
            data = _try_load_artifact(art_root, run_id, stage)
            if data is not None:
                p = out_path / fname
                p.write_text(json.dumps(data, indent=2), encoding="utf-8")
                file_hashes[fname] = _sha256_file(p)

    # ------------------------------------------------------------------
    # 4. Chunks — scroll from live collection
    # ------------------------------------------------------------------
    chunks_dir = out_path / "chunks"
    chunks_dir.mkdir(exist_ok=True)

    chunk_count = 0
    batch_size = 500
    batch_num = 0

    try:
        # Iterate the live collection through the first-class adapter API
        # (D-41).  Previously this reached into the adapter's private
        # ``_client`` and called ``qdrant_client.scroll`` directly, coupling
        # export to the Qdrant backend.  ``scroll_all`` is backend-agnostic
        # (works on Qdrant, pgvector, and the test FakeAdapter) and applies the
        # metadata-sentinel exclusion internally.
        batch_records: list[dict[str, Any]] = []

        def _flush_batch(records: list[dict[str, Any]], num: int) -> None:
            batch_path = chunks_dir / f"chunks_{num:06d}.jsonl"
            with open(batch_path, "w", encoding="utf-8") as fh:
                for payload in records:
                    fh.write(json.dumps(payload, default=str) + "\n")
            rel = f"chunks/chunks_{num:06d}.jsonl"
            file_hashes[rel] = _sha256_file(batch_path)

        for result in adapter.scroll_all(collection):
            batch_records.append(result.payload or {})
            chunk_count += 1
            if len(batch_records) >= batch_size:
                _flush_batch(batch_records, batch_num)
                batch_num += 1
                batch_records = []

        if batch_records:
            _flush_batch(batch_records, batch_num)
            batch_num += 1

    except Exception as exc:  # noqa: BLE001
        logger.error("Chunk export failed: %s", exc)

    logger.info("Exported %d chunks in %d batch file(s)", chunk_count, batch_num)

    # ------------------------------------------------------------------
    # 5. Eval sets (M-090) — serialize each eval set to eval_sets/<id>.json
    # ------------------------------------------------------------------
    eval_dir = out_path / "eval_sets"
    eval_dir.mkdir(exist_ok=True)

    eval_set_count = 0
    eval_question_count = 0
    eval_sets_note = ""

    try:
        from finecorpus.control.eval_store import EvalSetRepository  # noqa: PLC0415

        eval_repo = EvalSetRepository(session)
        eval_set_records = eval_repo.list_for_kb(kb_id)

        if eval_set_records:
            for es_record in eval_set_records:
                questions = eval_repo.get_questions(es_record.eval_set_id)
                eval_set_export = {
                    "eval_set_id": es_record.eval_set_id,
                    "kb_id": es_record.kb_id,
                    "workspace_id": es_record.workspace_id,
                    "schema_version": es_record.schema_version,
                    "origin": es_record.origin,
                    "confidence_level": es_record.confidence_level,
                    "baseline_ref": es_record.baseline_ref,
                    "created_at": es_record.created_at.isoformat()
                    if es_record.created_at
                    else None,
                    "question_count": len(questions),
                    "questions": [
                        {
                            "question_id": q.question_id,
                            "text": q.text,
                            "question_type": q.question_type,
                            "generation_method": q.generation_method,
                            "review_status": q.review_status,
                            "source_segment_ids": q.source_segment_ids,
                            "source_unknown": q.source_unknown,
                            "expected_segment_ids": q.expected_segment_ids,
                            "injection_suspicion": q.injection_suspicion,
                            "reviewed_by": q.reviewed_by,
                            "reviewed_at": q.reviewed_at.isoformat() if q.reviewed_at else None,
                            "class_description_ref": q.class_description_ref,
                        }
                        for q in questions
                    ],
                }

                es_path = eval_dir / f"{es_record.eval_set_id}.json"
                es_path.write_text(
                    json.dumps(eval_set_export, indent=2, default=str), encoding="utf-8"
                )
                rel_path = f"eval_sets/{es_record.eval_set_id}.json"
                file_hashes[rel_path] = _sha256_file(es_path)

                eval_set_count += 1
                eval_question_count += len(questions)
                logger.info(
                    "Exported eval set %s with %d questions → %s",
                    es_record.eval_set_id,
                    len(questions),
                    es_path,
                )

            eval_sets_note = (
                f"Exported {eval_set_count} eval set(s) with "
                f"{eval_question_count} total question(s)."
            )
        else:
            eval_sets_note = "No eval sets configured for this knowledge base."
            # Write an informative README for empty eval_sets/ directory
            readme = eval_dir / "README.txt"
            readme.write_text(
                "No eval sets are configured for this knowledge base.\n"
                "Use 'corpus eval import' to add curated (query, chunk) pairs.\n",
                encoding="utf-8",
            )
            file_hashes["eval_sets/README.txt"] = _sha256_file(readme)

    except Exception as exc:  # noqa: BLE001
        logger.error("Eval set export failed: %s", exc)
        eval_sets_note = f"Eval set export failed: {exc}"
        # Ensure the directory exists with a fallback README
        readme = eval_dir / "README.txt"
        if not readme.exists():
            readme.write_text(
                f"Eval set export encountered an error: {exc}\n",
                encoding="utf-8",
            )
            file_hashes["eval_sets/README.txt"] = _sha256_file(readme)

    logger.info(
        "Exported %d eval set(s) with %d total question(s)", eval_set_count, eval_question_count
    )

    # ------------------------------------------------------------------
    # 6. Manifest
    # ------------------------------------------------------------------
    exported_at = datetime.now(tz=UTC)
    manifest = ExportManifest(
        kb_id=kb_id,
        exported_at=exported_at,
        chunk_count=chunk_count,
        file_hashes=file_hashes,
        eval_sets_note=eval_sets_note,
        eval_set_count=eval_set_count,
        eval_question_count=eval_question_count,
        out_dir=out_path,
    )
    manifest_path = out_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    file_hashes["manifest.json"] = _sha256_file(manifest_path)

    logger.info(
        "KB export complete: kb_id=%s, chunks=%d, files=%d, out=%s",
        kb_id,
        chunk_count,
        len(file_hashes),
        out_path,
    )
    return manifest


__all__ = ["ExportManifest", "export_kb"]
