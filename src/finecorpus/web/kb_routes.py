"""KB flow routes — Easy/Proficient create-KB → endpoint, over ONE engine.

This module registers every knowledge-base route on the FastAPI app.  There is
exactly ONE set of handlers; ``mode`` (``easy`` | ``proficient``) is a query-string
presentation flag threaded into the templates (§3.3).  Both modes:

  - call the SAME :class:`~finecorpus.web.engine.EngineContext` operations, and
  - render from the SAME :func:`~finecorpus.web.config_forms.build_fields` output.

Easy mode renders only non-advanced fields as a read-only plain-language summary
with a "why" affordance; Proficient mode renders every recommender-set field as
an editable input.  A Proficient edit posts back through the same
``/kb/{kb_id}/plan`` handler that Easy-mode approval uses; omitted advanced fields
keep their recommender values (§3.1).

Every route requires a valid session (``require_session`` dependency, WU-A).

Spec MUSTs surfaced here (see the route/template that renders each):
  M-023 spreadsheet triage override, M-027 generated class-description drafts,
  M-045 provisional eval banner, M-089 delete-vs-purge labels, M-096 memory cost.
"""

# NOTE: this module deliberately does NOT use ``from __future__ import
# annotations``.  The route handlers are nested functions whose ``session``
# parameter depends on the ``require_session`` closure passed into
# ``register_kb_routes``.  FastAPI must be able to evaluate the annotation
# eagerly (with the closure variable in scope); a stringified (PEP 563)
# annotation would fail to resolve ``require_session`` from module globals and
# FastAPI would misread ``session`` as a query parameter (→ 422).

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from finecorpus.web import config_forms
from finecorpus.web.auth import Session
from finecorpus.web.class_draft import draft_map
from finecorpus.web.engine import EngineContext, new_run_id
from finecorpus.web.memory_cost import retention_delta_summary

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates


# ---------------------------------------------------------------------------
# In-memory flow state (per KB) — the working IngestionConfig between steps
# ---------------------------------------------------------------------------


@dataclass
class KBFlow:
    """The working state of one KB's create→endpoint flow."""

    kb_id: str
    workspace_id: str
    source_dir: str
    run_id: str
    config: dict[str, Any] = field(default_factory=dict)
    class_drafts: dict[str, str] = field(default_factory=dict)
    ingested: bool = False


def _normalise_mode(mode: str | None) -> str:
    return "proficient" if mode == "proficient" else "easy"


def register_kb_routes(
    app: FastAPI,
    templates: "Jinja2Templates",
    get_engine: Callable[[], EngineContext],
    require_session: Callable[..., "Session"],
) -> None:
    """Register all KB routes on ``app``.

    Args:
        app: The FastAPI application.
        templates: Jinja2 templates instance.
        get_engine: Zero-arg callable returning the active EngineContext.
        require_session: The fail-closed session dependency (WU-A). Every KB
            route depends on it, so no KB surface is reachable unauthenticated.
    """
    if not hasattr(app.state, "kb_flows"):
        app.state.kb_flows = {}

    flows: dict[str, KBFlow] = app.state.kb_flows

    # -- Create KB (step 1) -------------------------------------------------

    @app.get("/kb/new", response_class=HTMLResponse)
    def kb_new_form(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        mode: str | None = None,
    ) -> Response:
        """Render the create-KB form (Easy mode is the default)."""
        return templates.TemplateResponse(
            request,
            "kb_new.html",
            {"mode": _normalise_mode(mode), "username": session.username},
        )

    @app.post("/kb/new")
    def kb_new_submit(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: Annotated[str, Form()],
        source_dir: Annotated[str, Form()],
        workspace_id: Annotated[str, Form()] = "default",
        mode: Annotated[str, Form()] = "easy",
    ) -> Response:
        """Create the KB (control plane) and run the recommender (Plan)."""
        engine = get_engine()
        engine.create_kb(kb_id, workspace_id)
        run_id = new_run_id()
        config = engine.plan(
            source_dir=source_dir,
            kb_id=kb_id,
            workspace_id=workspace_id,
            run_id=run_id,
        )
        # M-027: generate an editable draft description per observed class.
        class_names = [r.get("segment_class", "") for r in config.get("class_rules", [])]
        drafts = draft_map(class_names)
        flows[kb_id] = KBFlow(
            kb_id=kb_id,
            workspace_id=workspace_id,
            source_dir=source_dir,
            run_id=run_id,
            config=config,
            class_drafts=drafts,
        )
        return RedirectResponse(
            url=f"/kb/{kb_id}/plan?mode={_normalise_mode(mode)}", status_code=303
        )

    # -- Review / edit the plan (step 2) — the ONE shared surface -----------

    @app.get("/kb/{kb_id}/plan", response_class=HTMLResponse)
    def kb_plan_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
        mode: str | None = None,
    ) -> Response:
        """Render the recommender's plan.

        Easy mode: non-advanced fields as a plain-language summary + "why".
        Proficient mode: EVERY recommender-set field as an editable input
        (§19 criterion 2). Both render from ``config_forms.build_fields`` — the
        single enumeration — so the modes cannot diverge.
        """
        flow = flows.get(kb_id)
        if flow is None:
            return RedirectResponse(url="/kb/new", status_code=303)
        norm = _normalise_mode(mode)
        all_fields = config_forms.build_fields(flow.config)
        easy = [f for f in all_fields if not f.advanced]
        triage = flow.config.get("spreadsheet_triage", [])
        return templates.TemplateResponse(
            request,
            "kb_plan.html",
            {
                "mode": norm,
                "kb_id": kb_id,
                "fields": all_fields if norm == "proficient" else easy,
                "all_fields": all_fields,
                "field_key": config_forms.field_key,
                "class_drafts": flow.class_drafts,
                "spreadsheet_triage": triage,
                "username": session.username,
            },
        )

    @app.post("/kb/{kb_id}/plan")
    async def kb_plan_submit(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
        mode: str | None = None,
    ) -> Response:
        """Apply edits (Proficient) or accept the recommender's values (Easy).

        Both paths call ``config_forms.apply_edits`` on the SAME config; Easy
        mode simply omits advanced inputs, so their values survive untouched.
        Class-description edits (M-027) are folded into the config as real
        ClassDescription entries. The result is persisted as the plan artifact.
        """
        flow = flows.get(kb_id)
        if flow is None:
            return RedirectResponse(url="/kb/new", status_code=303)
        form = await request.form()
        form_dict = {k: str(v) for k, v in form.items()}

        # Apply field edits (Proficient); Easy posts no advanced fields.
        flow.config = config_forms.apply_edits(flow.config, form_dict)

        # M-027: fold edited class-description drafts into the config.
        _apply_class_descriptions(flow, form_dict)

        # M-023: apply spreadsheet-triage overrides (Proficient mode).
        _apply_triage_overrides(flow, form_dict)

        # Persist the (possibly edited) config as the plan artifact so Build uses it.
        get_engine().save_plan(flow.run_id, flow.config)
        norm = _normalise_mode(mode or form_dict.get("mode"))
        return RedirectResponse(url=f"/kb/{kb_id}/preview?mode={norm}", status_code=303)

    # -- Preview (step 3) ----------------------------------------------------

    @app.get("/kb/{kb_id}/preview", response_class=HTMLResponse)
    def kb_preview_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
        mode: str | None = None,
    ) -> Response:
        """Dry-run preview of the chunks Build will produce (``_cmd_preview``)."""
        flow = flows.get(kb_id)
        if flow is None:
            return RedirectResponse(url="/kb/new", status_code=303)
        preview = get_engine().preview(flow.run_id, samples=5)
        return templates.TemplateResponse(
            request,
            "kb_preview.html",
            {
                "mode": _normalise_mode(mode),
                "kb_id": kb_id,
                "preview": preview,
                "username": session.username,
            },
        )

    # -- Ingest → endpoint (step 4) -----------------------------------------

    @app.post("/kb/{kb_id}/ingest")
    def kb_ingest_submit(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
        mode: Annotated[str, Form()] = "easy",
    ) -> Response:
        """Build + promote the KB, making the query endpoint live."""
        flow = flows.get(kb_id)
        if flow is None:
            return RedirectResponse(url="/kb/new", status_code=303)
        get_engine().ingest(
            source_dir=flow.source_dir,
            kb_id=kb_id,
            workspace_id=flow.workspace_id,
            run_id=new_run_id(),
        )
        flow.ingested = True
        return RedirectResponse(
            url=f"/kb/{kb_id}/endpoint?mode={_normalise_mode(mode)}", status_code=303
        )

    @app.get("/kb/{kb_id}/endpoint", response_class=HTMLResponse)
    def kb_endpoint_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
        mode: str | None = None,
    ) -> Response:
        """Show the query endpoint + a test-query box (the Easy-mode finish line)."""
        engine = get_engine()
        status = engine.status(kb_id)
        return templates.TemplateResponse(
            request,
            "kb_endpoint.html",
            {
                "mode": _normalise_mode(mode),
                "kb_id": kb_id,
                "status": status,
                "endpoint_path": f"/v1/kb/{kb_id}/query",
                "username": session.username,
            },
        )

    @app.post("/kb/{kb_id}/test-query", response_class=HTMLResponse)
    def kb_test_query(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
        query: Annotated[str, Form()],
        mode: Annotated[str, Form()] = "easy",
    ) -> Response:
        """Run a test query against the live endpoint and render the results."""
        response = get_engine().query(kb_id, query, top_k=5)
        return templates.TemplateResponse(
            request,
            "kb_query_results.html",
            {"kb_id": kb_id, "response": response, "query": query},
        )

    # -- M-089: deletion (deleted-from-service vs purged-from-all-copies) ----

    @app.get("/kb/{kb_id}/delete", response_class=HTMLResponse)
    def kb_delete_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
    ) -> Response:
        """Render the delete surface with BOTH M-089 labels visible."""
        return templates.TemplateResponse(
            request,
            "kb_delete.html",
            {"kb_id": kb_id, "username": session.username},
        )

    @app.post("/kb/{kb_id}/delete", response_class=HTMLResponse)
    def kb_delete_submit(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
        document_id: Annotated[str, Form()],
        action: Annotated[str, Form()] = "delete",
    ) -> Response:
        """Delete-from-service (purge=False) or purge-from-all-copies (purge=True).

        The returned ``DeletionReport.summary`` carries the M-089 distinction
        verbatim; the template renders it prominently.
        """
        purge = action == "purge"
        report = get_engine().delete_document(
            kb_id=kb_id,
            document_id=document_id,
            purge=purge,
            actor=session.username,
        )
        return templates.TemplateResponse(
            request,
            "kb_delete_result.html",
            {"kb_id": kb_id, "report": report},
        )

    # -- M-096: hot-copy retention memory cost ------------------------------

    @app.post("/kb/{kb_id}/retention/preview", response_class=HTMLResponse)
    def kb_retention_preview(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
        new_count: Annotated[int, Form()],
        vector_count: Annotated[int, Form()] = 0,
        dimensions: Annotated[int, Form()] = 0,
        current_count: Annotated[int, Form()] = 1,
    ) -> Response:
        """Show the memory cost of raising hot_retention_count (M-096, §10.2)."""
        summary = retention_delta_summary(
            vector_count=vector_count,
            dimensions=dimensions,
            current_count=current_count,
            new_count=new_count,
        )
        return HTMLResponse(f'<p class="memory-cost" data-testid="memory-cost">{summary}</p>')

    # -- M-045: eval baselines (reviewed vs unreviewed, visually distinguished)

    @app.get("/kb/{kb_id}/eval", response_class=HTMLResponse)
    def kb_eval_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
    ) -> Response:
        """Render eval baselines with the provisional banner (M-045)."""
        baselines = get_engine().eval_baselines(kb_id)
        return templates.TemplateResponse(
            request,
            "kb_eval.html",
            {"kb_id": kb_id, "baselines": baselines, "username": session.username},
        )

    # -- Dashboard (secondary) ----------------------------------------------

    @app.get("/kb/{kb_id}/dashboard", response_class=HTMLResponse)
    def kb_dashboard(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kb_id: str,
    ) -> Response:
        """Per-KB metrics + spend attribution (reuse ``finecorpus.telemetry``)."""
        from finecorpus.web.dashboard import kb_metrics

        metrics = kb_metrics(kb_id)
        return templates.TemplateResponse(
            request,
            "kb_dashboard.html",
            {"kb_id": kb_id, "metrics": metrics, "username": session.username},
        )


def _apply_class_descriptions(flow: KBFlow, form: dict[str, str]) -> None:
    """Fold submitted class-description drafts into the config (M-027).

    Form keys are ``desc.<segment_class>``.  A non-empty value becomes a real
    ClassDescription entry (folded into config_version, S-R14).  Duplicate
    class ids are de-duplicated (last write wins).
    """
    descs: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, value in form.items():
        if not key.startswith("desc."):
            continue
        seg_class = key[len("desc.") :]
        text = value.strip()
        if not text or seg_class in seen:
            continue
        seen.add(seg_class)
        descs.append({"segment_class": seg_class, "class_id": seg_class, "description": text})
    if descs:
        flow.config["class_descriptions"] = descs


# Disposition per §6.4: only `report` spreadsheets are ingested; database/model
# are excluded as unservable.
_TRIAGE_DISPOSITION = {
    "report": "ingest",
    "database": "exclude_unservable",
    "model": "exclude_unservable",
}


def _apply_triage_overrides(flow: KBFlow, form: dict[str, str]) -> None:
    """Apply spreadsheet-triage overrides into the config (M-023).

    Form keys are ``triage.<document_id>``.  A changed classification flips the
    entry's ``source`` to ``user_override`` and updates ``disposition`` per §6.4,
    so the override is both visible and effective on the config object.
    """
    triage = flow.config.get("spreadsheet_triage", [])
    if not triage:
        return
    by_doc = {t.get("document_id"): t for t in triage}
    for key, value in form.items():
        if not key.startswith("triage."):
            continue
        doc_id = key[len("triage.") :]
        entry = by_doc.get(doc_id)
        if entry is None:
            continue
        new_kind = value.strip()
        if new_kind and new_kind != entry.get("kind"):
            entry["kind"] = new_kind
            entry["source"] = "user_override"
            entry["disposition"] = _TRIAGE_DISPOSITION.get(new_kind, entry.get("disposition"))
            entry["reason"] = f"User override: classified as {new_kind}."


__all__ = ["KBFlow", "register_kb_routes"]
