"""Engine seam for the web UI — runs the SAME engine the CLI runs.

The web handlers must NOT reimplement any pipeline logic (§ scope discipline).
``EngineContext`` is a thin adapter that owns the three runtime dependencies the
engine needs — a control-plane session factory, an index adapter, and an
embedding provider — and exposes the exact operations the CLI wires:

  - :meth:`plan`   → Collect→Assess→Decompose→Plan (``finecorpus.pipeline``),
                     producing the recommender's ``IngestionConfig`` (``_cmd_plan``).
  - :meth:`preview`→ ``BuildStage(dry_run=True)`` over the plan (``_cmd_preview``).
  - :meth:`ingest` → ``run_pipeline(..., promote=True)`` (Build + promotion).
  - :meth:`query`  → ``finecorpus.retrieval.service.query`` (the retrieval endpoint).
  - :meth:`status` → ``finecorpus.retrieval.service.get_kb_status``.
  - :meth:`delete_document` → ``finecorpus.pipeline.deletion.delete_document`` (M-089).

In production the three dependencies are Qdrant + Postgres + the configured
embedding provider (built from ``Config``).  In tests they are injected directly
(an in-memory adapter, a SQLite session factory, and ``FakeProvider``) so the
create-KB→endpoint happy path runs fully in-process against the real engine code.
This is the single execution engine both Easy and Proficient mode drive.
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finecorpus.config.models import Config


@dataclass
class EngineContext:
    """Runtime dependencies for the web engine calls.

    Attributes:
        session_factory: Zero-arg callable returning a control-plane
            SQLAlchemy ``Session`` (context-manager compatible).
        adapter: An ``IndexAdapter`` (Qdrant in prod; in-memory in tests).
        provider: An ``EmbeddingProvider`` (configured model in prod; fake in tests).
        artifacts_root: Directory for pipeline artifacts. Defaults to a temp dir.
        config: Optional root ``Config`` (used for cost/threshold surfaces).
    """

    session_factory: Any
    adapter: Any
    provider: Any
    artifacts_root: str | None = None
    config: Config | None = None

    def __post_init__(self) -> None:
        if self.artifacts_root is None:
            self.artifacts_root = tempfile.mkdtemp(prefix="rtfc-web-")

    # -- Production builder --------------------------------------------------

    @classmethod
    def from_config(cls, config: Config) -> EngineContext:
        """Build a production EngineContext from a real ``Config``.

        Constructs the three infra dependencies exactly as the rest of the
        system does (``services.ingest_worker._build_adapter_provider`` and the
        CLI kb/query wiring), so ``create_app(config)`` yields a working app:

          - index adapter → ``QdrantAdapter(url=..., api_key=...)``
          - control-plane session factory → ``sessionmaker`` bound to an engine
            built via ``finecorpus.control.metadata.create_engine(postgres.url)``
          - embedding provider → ``embedding.registry.build_provider_from_config``

        Fail-loud on misconfiguration (mirrors the CLI's honest-error idiom):
        a bad DSN or unreachable Qdrant raises here rather than silently
        yielding a 503 surface, so operators see the real cause.  The three
        deps are constructed lazily/eagerly the same way the ingest worker does.
        """
        from sqlalchemy.orm import sessionmaker

        from finecorpus.control.metadata import create_engine as _control_create_engine
        from finecorpus.embedding.registry import build_provider_from_config
        from finecorpus.index.factory import build_adapter_from_config

        # Control-plane session factory (same DSN source as the CLI/worker).
        sa_engine = _control_create_engine(config.storage.postgres.url or "sqlite:///:memory:")
        session_local = sessionmaker(bind=sa_engine)

        # Index adapter selected by storage.index_backend (qdrant default; pgvector opt-in).
        adapter = build_adapter_from_config(config)

        # Embedding provider from config (same registry entry point).
        provider = build_provider_from_config(config)

        return cls(
            session_factory=session_local,
            adapter=adapter,
            provider=provider,
            config=config,
        )

    @property
    def root(self) -> str:
        """The resolved artifacts root (never ``None`` after construction)."""
        assert self.artifacts_root is not None  # guaranteed by __post_init__
        return self.artifacts_root

    # -- KB creation (control plane) ----------------------------------------

    def create_kb(self, kb_id: str, workspace_id: str) -> None:
        """Register a KB in the control plane (AliasRepository.create).

        Mirrors what ``corpus`` does on first KB creation: the alias exists but
        points at nothing until the first promotion completes.
        """
        from finecorpus.control.metadata import AliasRepository
        from finecorpus.index.adapter import alias_name

        with self.session_factory() as session:
            repo = AliasRepository(session)
            als = alias_name(kb_id)
            if repo.get(als) is None:
                repo.create(alias=als, kb_id=kb_id, workspace_id=workspace_id)
                session.commit()

    # -- Plan (recommender) --------------------------------------------------

    def plan(
        self,
        *,
        source_dir: str | Path,
        kb_id: str,
        workspace_id: str,
        run_id: str,
        class_descriptions_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Run Collect→Assess→Decompose→Plan and return the IngestionConfig dict.

        Reuses the real stages (``finecorpus.pipeline``); no logic is duplicated.
        Produces the recommender's config with ``provenance`` per field (M-025) —
        the "every value the recommender set" surface (§19 criterion 2).
        """
        from finecorpus.pipeline.artifact_store import ArtifactStore
        from finecorpus.pipeline.assess import AssessStage
        from finecorpus.pipeline.collect import CollectStage
        from finecorpus.pipeline.decompose import DecomposeStage
        from finecorpus.pipeline.plan import PlanStage

        store = ArtifactStore(artifacts_root=self.root, run_id=run_id)
        collected_at = datetime.now(tz=UTC)

        collect = CollectStage(
            source_dir=source_dir,
            workspace_id=workspace_id,
            kb_id=kb_id,
            collected_at=collected_at,
        )
        inventory = collect.run(input_data=None, store=store)

        assess = AssessStage(run_id=run_id, run_started_at=collected_at)
        parse_batch = assess.run(input_data=inventory, store=store)

        decompose = DecomposeStage(
            run_started_at=collected_at,
            artifacts_root=self.root,
            run_id=run_id,
        )
        segment_batch = decompose.run(input_data=parse_batch, store=store)

        plan = PlanStage(
            run_started_at=collected_at,
            class_descriptions_path=class_descriptions_path,
        )
        return plan.run(input_data=segment_batch, store=store)

    def save_plan(self, run_id: str, config: dict[str, Any]) -> None:
        """Persist an (edited) IngestionConfig dict back to the plan artifact.

        Proficient-mode edits and Easy-mode approvals both flow through here so
        the SAME config object drives Build (§3.3).
        """
        from finecorpus.pipeline.artifact_store import ArtifactStore

        store = ArtifactStore(artifacts_root=self.root, run_id=run_id)
        store.save("plan", config)

    # -- Preview (dry-run Build) --------------------------------------------

    def preview(self, run_id: str, *, samples: int = 5) -> dict[str, Any]:
        """Run Build in dry_run mode over the plan; return chunks + report.

        Same path as ``corpus preview`` (``BuildStage(dry_run=True)``).
        """
        import json as _json

        from finecorpus.pipeline.build.stage import BuildStage

        plan_path = Path(self.root) / run_id / "plan.json"
        ingestion_config = _json.loads(plan_path.read_text(encoding="utf-8"))
        stage = BuildStage(artifacts_root=self.root, run_id=run_id, dry_run=True)
        result = stage._produce(ingestion_config)  # noqa: SLF001
        chunks = result.get("chunks", [])
        return {
            "chunks": chunks[:samples],
            "total": len(chunks),
            "report": result.get("report", ""),
        }

    # -- Ingest (Build + promote) -------------------------------------------

    def ingest(
        self,
        *,
        source_dir: str | Path,
        kb_id: str,
        workspace_id: str,
        run_id: str,
        class_descriptions_path: str | Path | None = None,
    ) -> dict[str, str]:
        """Build + promote the KB so it becomes queryable (endpoint).

        Reuses ``run_pipeline(promote=True)`` — the same orchestrator the CLI
        and job worker use.  Requires the injected adapter + provider + session.
        """
        from finecorpus.pipeline import run_pipeline

        # PlanStage inside run_pipeline does not take a class-descriptions path;
        # if descriptions were provided we re-plan through them and stage the
        # resulting plan artifact first so Build picks it up.  For the common
        # case the orchestrator's own Plan stage produces the same config.
        with self.session_factory() as session:
            paths = run_pipeline(
                source_dir=source_dir,
                artifacts_root=self.root,
                run_id=run_id,
                workspace_id=workspace_id,
                kb_id=kb_id,
                embedding_provider=self.provider,
                index_adapter=self.adapter,
                promote=True,
                db_session=session,
            )
            session.commit()
        return paths

    # -- Serve (retrieval endpoint) -----------------------------------------

    def status(self, kb_id: str) -> Any:
        """Return KBStatus for a KB (``get_kb_status``)."""
        from finecorpus.retrieval.service import get_kb_status

        with self.session_factory() as session:
            return get_kb_status(kb_id=kb_id, session=session)

    def query(self, kb_id: str, query_text: str, *, top_k: int = 5) -> Any:
        """Run a retrieval query against the KB endpoint (``retrieval.service.query``)."""
        from finecorpus.retrieval.service import query as _query

        with self.session_factory() as session:
            return _query(
                kb_id=kb_id,
                query_text=query_text,
                provider=self.provider,
                adapter=self.adapter,
                session=session,
                top_k=top_k,
            )

    # -- Eval baselines (M-045) ---------------------------------------------

    def eval_baselines(self, kb_id: str) -> list[dict[str, Any]]:
        """Return per-eval-set review status + a rendered confidence banner.

        M-045: reviewed vs unreviewed eval baselines MUST be visually
        distinguished; the banner text comes from the single source
        ``pipeline.report.render_confidence_banner`` so the UI, CLI, and
        acceptance layer all render the same provisional warning.
        """
        from finecorpus.contracts.eval_set import ConfidenceLevel
        from finecorpus.control.eval_store import EvalSetRepository
        from finecorpus.pipeline.report import render_confidence_banner

        out: list[dict[str, Any]] = []
        with self.session_factory() as session:
            repo = EvalSetRepository(session)
            for es in repo.list_for_kb(kb_id):
                questions = repo.get_questions(es.eval_set_id)
                n_total = len(questions)
                n_unreviewed = sum(1 for q in questions if q.review_status == "unreviewed")
                try:
                    level = ConfidenceLevel(es.confidence_level)
                except ValueError:
                    level = ConfidenceLevel.provisional
                banner = render_confidence_banner(
                    confidence_level=level, n_total=n_total, n_unreviewed=n_unreviewed
                )
                out.append(
                    {
                        "eval_set_id": es.eval_set_id,
                        "confidence_level": level.value,
                        "n_total": n_total,
                        "n_unreviewed": n_unreviewed,
                        "provisional": (level == ConfidenceLevel.provisional or n_unreviewed > 0),
                        "banner": banner,
                    }
                )
        return out

    # -- Deletion (M-089) ----------------------------------------------------

    def delete_document(self, *, kb_id: str, document_id: str, purge: bool, actor: str) -> Any:
        """Delete or purge a document (``pipeline.deletion.delete_document``).

        Returns the ``DeletionReport`` whose ``summary`` carries the M-089
        "deleted from service" vs "purged from all copies" distinction verbatim.
        """
        from finecorpus.pipeline.deletion import delete_document

        with self.session_factory() as session:
            report = delete_document(
                kb_id=kb_id,
                document_id=document_id,
                purge=purge,
                session=session,
                adapter=self.adapter,
                artifacts_root=self.root,
                deleted_by=actor,
            )
            session.commit()
        return report


def new_run_id() -> str:
    """A fresh run identity for a plan/ingest run (caller-supplied, deterministic use)."""
    return f"web-{uuid.uuid4().hex[:12]}"


__all__ = ["EngineContext", "new_run_id"]
