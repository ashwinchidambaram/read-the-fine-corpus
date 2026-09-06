"""Acceptance tests for the Easy/Proficient KB flow (Phase 6, WU-C).

Exercises the two §19 Phase-6 criteria against the REAL engine, in-process:

  (1) A non-technical user completes create-KB → endpoint in Easy mode. The whole
      flow runs through FastAPI TestClient with no documentation and no CLI.
  (2) Proficient mode renders every recommender-set field as an editable input
      AND a Proficient edit round-trips into the config that drives Build.

Plus the Phase-6 MUSTs: M-023 (triage override), M-027 (generated draft),
M-045 (provisional banner), M-089 (delete/purge labels), M-096 (memory figure).

The engine's three dependencies (control-plane session, index adapter, embedding
provider) are injected as in-process fakes so the flow runs the real pipeline +
retrieval code end-to-end without Qdrant or Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from finecorpus.control.metadata import create_tables
from finecorpus.embedding.fake import FakeProvider
from finecorpus.web import create_app
from finecorpus.web.auth import LocalAccountsAuthProvider
from finecorpus.web.engine import EngineContext

# The tests/retrieval helpers provide the in-memory IndexAdapter used across the
# retrieval test-suite; reuse it here rather than defining a parallel fake.
from tests.retrieval.helpers import FakeAdapter

DIMENSIONS = 64
MODEL_ID = "fake-embed-v1"


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """A tiny HTML corpus (HTML is a supported parser; fast + hermetic)."""
    d = tmp_path / "docs"
    d.mkdir()
    (d / "handbook.html").write_text(
        "<html><body>"
        "<h1>Customer Handbook</h1>"
        "<h2>Refund policy</h2>"
        "<p>Customers may request a refund within 30 days of purchase. Returns "
        "must include the original packaging. Refunds are issued to the original "
        "payment method within five business days.</p>"
        "<h2>Support</h2>"
        "<p>Support hours are Monday to Friday, nine to five. Contact support by "
        "email for help with your account or billing questions.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    (d / "faq.html").write_text(
        "<html><body>"
        "<h1>Frequently asked questions</h1>"
        "<p>How do I reset my password? Use the reset link on the sign-in page "
        "and follow the emailed instructions.</p>"
        "<p>Who do I contact for billing questions? Email the billing team.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def engine_ctx(tmp_path: Path) -> EngineContext:
    """EngineContext wired to in-process fakes (SQLite + FakeAdapter + FakeProvider)."""
    # StaticPool + a single shared connection so the in-memory DB persists
    # across the sessions opened by TestClient's worker thread.
    sa_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_tables(sa_engine)

    @contextmanager
    def session_factory() -> Any:
        with Session(sa_engine) as s:
            yield s

    return EngineContext(
        session_factory=session_factory,
        adapter=FakeAdapter(),
        provider=FakeProvider(dimensions=DIMENSIONS, model_id=MODEL_ID),
        artifacts_root=str(tmp_path / "artifacts"),
    )


@pytest.fixture
def client(engine_ctx: EngineContext) -> TestClient:
    provider = LocalAccountsAuthProvider()
    provider.add_account("alice", "s3cret")
    app = create_app(config=None, auth_provider=provider, engine=engine_ctx)
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "alice", "password": "s3cret"})
    return c


# ---------------------------------------------------------------------------
# §19 criterion 1 — Easy mode create-KB → endpoint, no documentation
# ---------------------------------------------------------------------------


def test_easy_create_kb_to_endpoint_happy_path(client: TestClient, source_dir: Path) -> None:
    kb_id = "handbook"

    # Step 1: create KB + run recommender.
    r = client.post(
        "/kb/new",
        data={"kb_id": kb_id, "source_dir": str(source_dir), "mode": "easy"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/kb/{kb_id}/plan?mode=easy"

    # Step 2: review the plan (plain-language summary + why).
    r = client.get(f"/kb/{kb_id}/plan?mode=easy")
    assert r.status_code == 200
    assert "Easy mode" in r.text
    assert "why" in r.text  # the "why" affordance (§3.1)

    # Step 3: approve → preview.
    r = client.post(f"/kb/{kb_id}/plan?mode=easy", data={"mode": "easy"})
    assert r.status_code == 303
    assert "/preview" in r.headers["location"]

    r = client.get(f"/kb/{kb_id}/preview?mode=easy")
    assert r.status_code == 200
    assert "preview-chunks" in r.text

    # Step 4: ingest → endpoint.
    r = client.post(f"/kb/{kb_id}/ingest", data={"mode": "easy"})
    assert r.status_code == 303
    assert "/endpoint" in r.headers["location"]

    r = client.get(f"/kb/{kb_id}/endpoint?mode=easy")
    assert r.status_code == 200
    assert "endpoint-ready" in r.text
    assert f"/v1/kb/{kb_id}/query" in r.text

    # The endpoint answers a test query against the real retrieval service.
    r = client.post(
        f"/kb/{kb_id}/test-query",
        data={"query": "What is the refund policy?", "mode": "easy"},
    )
    assert r.status_code == 200
    assert "query-results" in r.text
    # A dense query over a non-empty index returns matches.
    assert "matches" in r.text


# ---------------------------------------------------------------------------
# §19 criterion 2 — Proficient exposes AND edits every recommender-set value
# ---------------------------------------------------------------------------


def _covered_by_input(html: str, target: str) -> bool:
    """True if ``target`` (a provenance pointer) has a matching editable input.

    A recommender provenance ``target`` is either an exact field pointer or an
    ancestor subtree pointer (e.g. ``/default_rule``). It is "covered" when the
    render contains an input whose ``data-testid`` id is that pointer OR any
    descendant of it — the rendered-input-id normalisation of the target.
    """
    import re

    exact = f'data-testid="input-{target}"'
    if exact in html:
        return True
    prefix = f'data-testid="input-{target}/'
    return re.search(re.escape(prefix), html) is not None


def test_proficient_renders_every_recommender_field_as_input(
    client: TestClient, source_dir: Path
) -> None:
    kb_id = "prof-render"
    client.post(
        "/kb/new",
        data={"kb_id": kb_id, "source_dir": str(source_dir), "mode": "proficient"},
    )
    r = client.get(f"/kb/{kb_id}/plan?mode=proficient")
    assert r.status_code == 200
    assert "Proficient mode" in r.text

    # Oracle = the recommender's OWN provenance list (NOT build_fields). Every
    # value the recommender set carries a provenance entry (M-025); §19
    # criterion 2 requires every one of those to be an editable input in
    # Proficient mode. Deriving expectations from provenance (not build_fields)
    # makes this a real check, not a circular one.
    flow = client.app.state.kb_flows[kb_id]
    targets = [p["target"] for p in flow.config.get("provenance", [])]
    assert targets, "recommender produced no provenance targets"

    missing = [t for t in targets if not _covered_by_input(r.text, t)]
    assert not missing, (
        f"Proficient mode is missing editable inputs for {len(missing)}/{len(targets)} "
        f"recommender provenance targets: {missing}"
    )

    # A representative value that the hardcoded-subset implementation dropped —
    # a per-class table rule's atomic_rows — must be surfaced AND round-trip
    # through apply_edits. Seed a table class rule with recommender provenance
    # for it (mirrors what the recommender emits for §6.4 table content) and
    # re-render + edit through the SAME code path.
    from finecorpus.web import config_forms

    cfg = flow.config
    cfg.setdefault("class_rules", []).append(
        {
            "segment_class": "table",
            "transformation": {
                "tier1_enabled": True,
                "tier1_operations": ["whitespace_repair"],
                "tier2_enabled": True,
                "tier2_operations": ["table_description"],
                "tier3_enabled": False,
                "tier3_settings": None,
                "retain_original_ref": False,
                "diff_preview_required": False,
                "mark_rewritten_chunks": False,
            },
            "chunking": {
                "strategy": "recursive_char",
                "max_tokens": 512,
                "overlap_tokens": 0,
                "tokenizer": "whitespace_word",
                "respect_headings": False,
                "atomic_rows": True,
                "repeat_headers_on_split": True,
                "split_boundaries": None,
            },
            "embedding_override": None,
            "metadata_schema": [],
            "retrieval_treatment": {
                "default_salience_filter": ["primary", "supporting"],
                "salience_weights": None,
                "rerank_eligible": True,
                "strategy": "dense",
                "confidence_floor": None,
            },
        }
    )
    cfg.setdefault("provenance", []).append(
        {
            "target": "/class_rules/table/chunking/atomic_rows",
            "basis": "heuristic",
            "sweep_run_id": None,
            "rationale": "Heuristic (§6.4): atomic_rows keeps rows with their headers.",
        }
    )

    r2 = client.get(f"/kb/{kb_id}/plan?mode=proficient")
    atomic_ptr = "/class_rules/table/chunking/atomic_rows"
    assert _covered_by_input(r2.text, atomic_ptr), (
        "table atomic_rows (a previously-dropped recommender target) is not editable"
    )

    # Round-trip the previously-missing value through apply_edits.
    key = config_forms.field_key(atomic_ptr)
    updated = config_forms.apply_edits(cfg, {key: "false"})
    assert config_forms._resolve(updated, atomic_ptr) is False


def test_proficient_edit_round_trips_into_config(client: TestClient, source_dir: Path) -> None:
    kb_id = "prof-edit"
    client.post(
        "/kb/new",
        data={"kb_id": kb_id, "source_dir": str(source_dir), "mode": "proficient"},
    )
    flow = client.app.state.kb_flows[kb_id]
    from finecorpus.web import config_forms

    # Pick a numeric field (max_tokens) and change it.
    target = next(
        f
        for f in config_forms.build_fields(flow.config)
        if f.pointer.endswith("/chunking/max_tokens")
    )
    original = int(target.value)
    new_value = original + 128
    key = config_forms.field_key(target.pointer)

    r = client.post(
        f"/kb/{kb_id}/plan?mode=proficient",
        data={"mode": "proficient", key: str(new_value)},
    )
    assert r.status_code == 303

    # The edit is reflected in the working config that drives Build.
    updated = client.app.state.kb_flows[kb_id].config
    resolved = config_forms._resolve(updated, target.pointer)
    assert int(resolved) == new_value


def test_easy_omitting_advanced_fields_keeps_recommender_values(
    client: TestClient, source_dir: Path
) -> None:
    """§3.1: Easy mode is Proficient with the recommender's answers filled in.

    An Easy-mode approval posts no advanced fields; their recommender values must
    survive untouched (one config object, not a separate code path).
    """
    kb_id = "easy-keep"
    client.post(
        "/kb/new",
        data={"kb_id": kb_id, "source_dir": str(source_dir), "mode": "easy"},
    )
    from finecorpus.web import config_forms

    before = client.app.state.kb_flows[kb_id].config
    advanced = next(f for f in config_forms.build_fields(before) if f.advanced)
    before_value = config_forms._resolve(before, advanced.pointer)

    # Easy-mode approval: no advanced field in the form.
    client.post(f"/kb/{kb_id}/plan?mode=easy", data={"mode": "easy"})

    after = client.app.state.kb_flows[kb_id].config
    assert config_forms._resolve(after, advanced.pointer) == before_value


# ---------------------------------------------------------------------------
# M-027 — generated draft class descriptions (not a blank box)
# ---------------------------------------------------------------------------


def test_m027_generated_draft_is_non_empty(client: TestClient, source_dir: Path) -> None:
    kb_id = "m027"
    client.post(
        "/kb/new",
        data={"kb_id": kb_id, "source_dir": str(source_dir), "mode": "easy"},
    )
    flow = client.app.state.kb_flows[kb_id]
    assert flow.class_drafts, "no class drafts generated"
    for cls, draft in flow.class_drafts.items():
        assert draft.strip(), f"draft for {cls} is blank (M-027 requires a generated draft)"

    r = client.get(f"/kb/{kb_id}/plan?mode=easy")
    assert "class-descriptions" in r.text
    # A draft appears pre-filled in a textarea, not a blank box.
    a_class = next(iter(flow.class_drafts))
    assert f'data-testid="draft-{a_class}"' in r.text


def test_m027_edited_description_folds_into_config(client: TestClient, source_dir: Path) -> None:
    kb_id = "m027-edit"
    client.post(
        "/kb/new",
        data={"kb_id": kb_id, "source_dir": str(source_dir), "mode": "easy"},
    )
    flow = client.app.state.kb_flows[kb_id]
    a_class = next(iter(flow.class_drafts))
    client.post(
        f"/kb/{kb_id}/plan?mode=easy",
        data={"mode": "easy", f"desc.{a_class}": "Custom description for retrieval."},
    )
    cfg = client.app.state.kb_flows[kb_id].config
    descs = cfg.get("class_descriptions", [])
    assert any(d["description"] == "Custom description for retrieval." for d in descs)


# ---------------------------------------------------------------------------
# M-023 — spreadsheet triage visible + overridable in Proficient mode
# ---------------------------------------------------------------------------


def test_m023_triage_override_changes_config(client: TestClient) -> None:
    kb_id = "m023"
    # Seed a flow directly with a triage entry (no spreadsheet in the tiny corpus).
    from finecorpus.web.kb_routes import KBFlow

    flow = KBFlow(
        kb_id=kb_id,
        workspace_id="default",
        source_dir="/tmp",
        run_id="run-m023",
        config={
            "provenance": [],
            "class_rules": [],
            "spreadsheet_triage": [
                {
                    "document_id": "sheet.xlsx",
                    "kind": "database",
                    "disposition": "exclude_unservable",
                    "source": "detected",
                    "reason": "Detected as a database-style spreadsheet.",
                }
            ],
        },
    )
    client.app.state.kb_flows[kb_id] = flow

    # Proficient view shows the triage classification and its source.
    r = client.get(f"/kb/{kb_id}/plan?mode=proficient")
    assert "spreadsheet-triage" in r.text
    assert 'data-testid="triage-select-sheet.xlsx"' in r.text
    assert "detected" in r.text

    # Override database → report.
    client.post(
        f"/kb/{kb_id}/plan?mode=proficient",
        data={"mode": "proficient", "triage.sheet.xlsx": "report"},
    )
    entry = client.app.state.kb_flows[kb_id].config["spreadsheet_triage"][0]
    assert entry["kind"] == "report"
    assert entry["source"] == "user_override"  # M-023: source flips to user_override
    assert entry["disposition"] == "ingest"


# ---------------------------------------------------------------------------
# M-045 — provisional eval baseline visually distinguished
# ---------------------------------------------------------------------------


def test_m045_provisional_banner_shown(client: TestClient, engine_ctx: EngineContext) -> None:
    kb_id = "m045"
    # Seed a provisional eval set with unreviewed questions in the control plane.
    from datetime import UTC, datetime

    from finecorpus.control.eval_store import EvalSetRepository

    with engine_ctx.session_factory() as session:
        repo = EvalSetRepository(session)
        repo.create(
            eval_set_id="es-m045",
            kb_id=kb_id,
            workspace_id="default",
            schema_version="1.0.0",
            origin="generated_eval_set",
            confidence_level="provisional",
            baseline_ref=None,
            created_at=datetime.now(tz=UTC),
        )
        repo.add_question(
            question_id="q1",
            eval_set_id="es-m045",
            kb_id=kb_id,
            text="What is the refund window?",
            question_type="factual_lookup",
            generation_method="llm_generated",
            review_status="unreviewed",
            source_segment_ids=[],
            source_unknown=True,
            expected_segment_ids=[],
        )
        session.commit()

    r = client.get(f"/kb/{kb_id}/eval")
    assert r.status_code == 200
    assert "confidence-banner" in r.text
    assert "PROVISIONAL" in r.text  # the render_confidence_banner provisional text
    assert 'data-provisional="true"' in r.text


# ---------------------------------------------------------------------------
# M-089 — deleted-from-service vs purged-from-all-copies distinction
# ---------------------------------------------------------------------------


def test_m089_delete_and_purge_labels_visible(client: TestClient) -> None:
    r = client.get("/kb/some-kb/delete")
    assert r.status_code == 200
    assert 'data-testid="label-delete"' in r.text
    assert 'data-testid="label-purge"' in r.text
    assert "Delete from service" in r.text
    assert "Purge from all copies" in r.text


# ---------------------------------------------------------------------------
# M-096 — memory cost shown when hot-copy retention count is raised
# ---------------------------------------------------------------------------


def test_m096_memory_cost_shown_on_retention_raise(client: TestClient) -> None:
    r = client.post(
        "/kb/mem/retention/preview",
        data={
            "new_count": "3",
            "current_count": "1",
            "vector_count": "100000",
            "dimensions": "768",
        },
    )
    assert r.status_code == 200
    assert "memory-cost" in r.text
    assert "MB" in r.text  # a concrete memory figure is shown


# ---------------------------------------------------------------------------
# Auth: KB routes are fail-closed
# ---------------------------------------------------------------------------


def test_kb_routes_require_session(engine_ctx: EngineContext) -> None:
    provider = LocalAccountsAuthProvider()
    provider.add_account("alice", "s3cret")
    app = create_app(config=None, auth_provider=provider, engine=engine_ctx)
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/kb/new").status_code == 401
    assert anon.get("/kb/x/plan").status_code == 401
    assert anon.get("/kb/x/endpoint").status_code == 401
