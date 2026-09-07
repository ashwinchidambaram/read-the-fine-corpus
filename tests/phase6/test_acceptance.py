"""Phase 6 acceptance layer — maps the two §19 Phase-6 criteria (and the five
Phase-6 MUSTs) to named tests.

§19 Phase 6 ("UI & connectors") acceptance criteria:
  1. A non-technical user completes create-KB → endpoint in Easy mode without
     documentation.
  2. Proficient mode exposes every value the recommender set.

The underlying behaviour is implemented and exercised end-to-end in
``tests/web/test_kb_flow.py`` against the REAL engine (real pipeline + retrieval
through ``EngineContext``, with only the three infra deps — control-plane DB,
index adapter, embedding provider — replaced by in-process fakes). This module
is the canonical §19 MAP: it reuses those fixtures and invokes the proven flows
under criterion-named classes so the acceptance criteria are individually
addressable and cannot silently regress.

The imported ``test_*`` callables are aliased with a leading underscore so pytest
does not double-collect them here; the imported fixtures (``client``,
``engine_ctx``, ``source_dir``) are re-exported for use by the wrappers below.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

# EngineContext is used only for type annotations here; the ``client``,
# ``engine_ctx`` and ``source_dir`` fixtures are provided by tests/phase6/conftest.py.
from finecorpus.web.engine import EngineContext

# Proven behaviours, aliased so they are not re-collected as tests in this module.
from tests.web.test_kb_flow import (
    test_easy_create_kb_to_endpoint_happy_path as _easy_create_to_endpoint,
)
from tests.web.test_kb_flow import (
    test_m023_triage_override_changes_config as _m023,
)
from tests.web.test_kb_flow import (
    test_m027_generated_draft_is_non_empty as _m027,
)
from tests.web.test_kb_flow import (
    test_m045_provisional_banner_shown as _m045,
)
from tests.web.test_kb_flow import (
    test_m089_delete_and_purge_labels_visible as _m089,
)
from tests.web.test_kb_flow import (
    test_m096_memory_cost_shown_on_retention_raise as _m096,
)
from tests.web.test_kb_flow import (
    test_proficient_edit_round_trips_into_config as _proficient_round_trip,
)
from tests.web.test_kb_flow import (
    test_proficient_renders_every_recommender_field_as_input as _proficient_exposes_all,
)


class TestCriterion1EasyModeCreateKbToEndpoint:
    """§19 crit 1: a non-technical user completes create-KB → endpoint in Easy
    mode, driven entirely through the web UI (no CLI, no docs), ending in a live
    query that returns matches."""

    def test_non_technical_create_kb_to_endpoint(
        self, client: TestClient, source_dir: Path
    ) -> None:
        _easy_create_to_endpoint(client, source_dir)

    def test_onboarding_guides_a_new_user_to_the_first_step(self, client: TestClient) -> None:
        """The guided onboarding path is reachable and links the first KB step,
        so a new user reaches the create-KB flow without documentation."""
        landing = client.get("/")
        assert landing.status_code == 200
        assert 'data-testid="landing-onboarding"' in landing.text
        assert "/onboarding" in landing.text

        page = client.get("/onboarding")
        assert page.status_code == 200
        assert 'data-testid="onboarding-steps"' in page.text
        # Step 1 links to the real create-KB handler (no duplicated logic).
        assert 'data-testid="onboarding-start"' in page.text
        assert "/kb/new?mode=easy" in page.text

    def test_onboarding_requires_auth(self, engine_ctx: EngineContext) -> None:
        """Fail-closed: onboarding is not reachable unauthenticated."""
        from finecorpus.web import create_app  # noqa: PLC0415
        from finecorpus.web.auth import LocalAccountsAuthProvider  # noqa: PLC0415

        app = create_app(config=None, auth_provider=LocalAccountsAuthProvider(), engine=engine_ctx)
        anon = TestClient(app, follow_redirects=False)
        assert anon.get("/onboarding").status_code == 401


class TestCriterion2ProficientExposesEveryRecommenderValue:
    """§19 crit 2: Proficient mode exposes AND edits every value the recommender
    set. Coverage is asserted against the recommender's own provenance targets
    (not against the form builder), so an omission fails."""

    def test_every_recommender_value_is_editable(
        self, client: TestClient, source_dir: Path
    ) -> None:
        _proficient_exposes_all(client, source_dir)

    def test_proficient_edit_round_trips(self, client: TestClient, source_dir: Path) -> None:
        _proficient_round_trip(client, source_dir)


class TestPhase6Musts:
    """The five Phase-6 UI MUSTs, surfaced in the web UI."""

    def test_m023_triage_visible_and_overridable(self, client: TestClient) -> None:
        _m023(client)

    def test_m027_class_description_generated_draft(
        self, client: TestClient, source_dir: Path
    ) -> None:
        _m027(client, source_dir)

    def test_m045_reviewed_vs_provisional_distinguished(
        self, client: TestClient, engine_ctx: EngineContext
    ) -> None:
        _m045(client, engine_ctx)

    def test_m089_delete_vs_purge_visible(self, client: TestClient) -> None:
        _m089(client)

    def test_m096_memory_cost_on_retention_raise(self, client: TestClient) -> None:
        _m096(client)
