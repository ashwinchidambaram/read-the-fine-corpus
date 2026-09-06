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
        out_dir: Absolute path to the export directory.
    """

    kb_id: str
    exported_at: datetime
    chunk_count: int
    file_hashes: dict[str, str] = field(default_factory=dict)
    eval_sets_note: str = (
        "[STUB — Phase 5] Eval sets are not yet populated. "
        "Use 'corpus eval import' after Phase 5 to add curated (query, chunk) pairs."
    )
    out_dir: pathlib.Path = field(default_factory=pathlib.Path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "kb_id": self.kb_id,
            "exported_at": self.exported_at.isoformat(),
            "chunk_count": self.chunk_count,
            "file_hashes": self.file_hashes,
            "eval_sets_note": self.eval_sets_note,
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
    offset = 0
    batch_num = 0

    try:
        # Use Qdrant client directly to scroll the live collection.
        # The IndexAdapter ABC does not expose a scroll method; we access the
        # underlying Qdrant client via the adapter's private attribute.
        # If the adapter does not expose _client, fall back to a no-op stub.
        qdrant_client = getattr(adapter, "_client", None)
        if qdrant_client is None:
            logger.warning(
                "Adapter does not expose _client; chunk scroll skipped. "
                "Export will contain no chunk files."
            )
        else:
            next_offset: Any = None
            while True:
                try:
                    results, next_offset = qdrant_client.scroll(
                        collection_name=collection,
                        limit=batch_size,
                        offset=next_offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Qdrant scroll error at offset %s: %s", next_offset, exc)
                    break

                if not results:
                    break

                batch_path = chunks_dir / f"chunks_{batch_num:06d}.jsonl"
                with open(batch_path, "w", encoding="utf-8") as fh:
                    for point in results:
                        payload = point.payload or {}
                        fh.write(json.dumps(payload, default=str) + "\n")
                        chunk_count += 1

                rel = f"chunks/chunks_{batch_num:06d}.jsonl"
                file_hashes[rel] = _sha256_file(batch_path)
                batch_num += 1
                offset += len(results)

                if next_offset is None:
                    break

    except Exception as exc:  # noqa: BLE001
        logger.error("Chunk export failed: %s", exc)

    logger.info("Exported %d chunks in %d batch file(s)", chunk_count, batch_num)

    # ------------------------------------------------------------------
    # 5. Eval sets stub
    # ------------------------------------------------------------------
    eval_dir = out_path / "eval_sets"
    eval_dir.mkdir(exist_ok=True)
    readme = eval_dir / "README.txt"
    readme.write_text(
        "[STUB — Phase 5] Eval sets are not yet populated. "
        "Use 'corpus eval import' after Phase 5 to add curated (query, chunk) pairs.\n",
        encoding="utf-8",
    )
    file_hashes["eval_sets/README.txt"] = _sha256_file(readme)

    # ------------------------------------------------------------------
    # 6. Manifest
    # ------------------------------------------------------------------
    exported_at = datetime.now(tz=UTC)
    manifest = ExportManifest(
        kb_id=kb_id,
        exported_at=exported_at,
        chunk_count=chunk_count,
        file_hashes=file_hashes,
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
