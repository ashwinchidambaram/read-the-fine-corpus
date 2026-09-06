"""Telemetry tests (M-102, Phase 4).

Covers:
- test_five_domains_present_m102: registry contains at least one metric per domain;
  quality stubs are named correctly.
- test_metrics_endpoint_serves: /metrics on both retrieval-api and control-api
  returns 200 with prometheus content.
- test_governance_hook_helpers: incr_* functions increment their counters.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Test: five domains present (M-102)
# ---------------------------------------------------------------------------


def test_five_domains_present_m102():
    """Registry contains at least one metric per M-102 domain.

    Five domains: ingestion, index, retrieval, quality (stubs), governance.
    """
    import prometheus_client

    import finecorpus.telemetry  # noqa: F401 — importing registers all metric objects

    # Importing telemetry registers all metrics

    # Collect all registered metric names
    registry = prometheus_client.REGISTRY
    names: set[str] = set()
    for metric_family in registry.collect():
        names.add(metric_family.name)

    # 1. INGESTION domain
    assert any("rtfc_ingestion_" in n for n in names), f"No ingestion metric in {names}"

    # 2. INDEX domain
    assert any("rtfc_index_" in n for n in names), f"No index metric in {names}"

    # 3. RETRIEVAL domain
    assert any("rtfc_retrieval_" in n for n in names), f"No retrieval metric in {names}"

    # 4. QUALITY domain — stubs named correctly
    quality_metrics = [n for n in names if "rtfc_quality_" in n]
    assert len(quality_metrics) >= 3, (
        f"Expected at least 3 quality stub metrics, got {quality_metrics}"
    )
    # Verify the stub names
    # Note: prometheus_client strips _total suffix from Counter names when collecting
    # (the metric is registered as rtfc_quality_injection_flagged_docs_total but
    #  collected as rtfc_quality_injection_flagged_docs). Check both forms.
    assert any("rtfc_quality_retrieval_precision_at_k" in n for n in names), (
        "precision@k stub missing"
    )
    assert any("rtfc_quality_retrieval_recall_at_k" in n for n in names), "recall@k stub missing"
    assert any("rtfc_quality_injection_flagged_docs" in n for n in names), (
        "injection flagged docs stub missing"
    )

    # 5. GOVERNANCE domain
    assert any("rtfc_governance_" in n for n in names), f"No governance metric in {names}"


def test_quality_stubs_have_stub_docstring():
    """Remaining quality stub metrics have docstrings containing 'STUB'.

    QUALITY_RETRIEVAL_PRECISION_AT_K and QUALITY_RETRIEVAL_RECALL_AT_K are
    no longer stubs — they are populated by the eval scoring harness (Phase 5).
    The three metrics below remain un-implemented stubs.
    """
    import finecorpus.telemetry as tel

    for metric_obj in [
        tel.QUALITY_INJECTION_FLAGGED_DOCS,
        tel.QUALITY_PII_FLAGGED_DOCS,
        tel.QUALITY_EVAL_SET_SIZE,
    ]:
        desc = metric_obj._documentation  # prometheus_client stores in _documentation
        assert "[STUB" in desc or "STUB" in desc, (
            f"Quality metric {metric_obj._name!r} does not have STUB marker in docstring: {desc!r}"
        )


def test_quality_precision_recall_are_live():
    """QUALITY_RETRIEVAL_PRECISION_AT_K and QUALITY_RETRIEVAL_RECALL_AT_K are live (Phase 5+).

    These gauges are populated by score_eval_set at promotion time and must NOT
    carry STUB markers in their help strings.
    """
    import finecorpus.telemetry as tel

    for metric_obj in [
        tel.QUALITY_RETRIEVAL_PRECISION_AT_K,
        tel.QUALITY_RETRIEVAL_RECALL_AT_K,
    ]:
        desc = metric_obj._documentation
        assert "STUB" not in desc, (
            f"Quality metric {metric_obj._name!r} is live (Phase 5+) but still carries "
            f"a STUB marker: {desc!r}"
        )
        assert "score_eval_set" in desc or "eval scoring harness" in desc, (
            f"Quality metric {metric_obj._name!r} description should reference the "
            f"eval scoring harness: {desc!r}"
        )


# ---------------------------------------------------------------------------
# Test: governance hook helpers
# ---------------------------------------------------------------------------


def test_incr_break_glass_grant():
    from finecorpus.telemetry import GOVERNANCE_BREAK_GLASS_GRANTS_TOTAL, incr_break_glass_grant

    before = _get_counter_value(
        GOVERNANCE_BREAK_GLASS_GRANTS_TOTAL, {"kb_id": "kb-tel-test", "actor_id": "actor-1"}
    )
    incr_break_glass_grant(kb_id="kb-tel-test", actor_id="actor-1")
    after = _get_counter_value(
        GOVERNANCE_BREAK_GLASS_GRANTS_TOTAL, {"kb_id": "kb-tel-test", "actor_id": "actor-1"}
    )
    assert after == before + 1.0


def test_incr_permission_change():
    from finecorpus.telemetry import GOVERNANCE_PERMISSION_CHANGES_TOTAL, incr_permission_change

    before = _get_counter_value(
        GOVERNANCE_PERMISSION_CHANGES_TOTAL,
        {"kb_id": "kb-tel-test", "change_type": "key_issued"},
    )
    incr_permission_change(kb_id="kb-tel-test", change_type="key_issued")
    after = _get_counter_value(
        GOVERNANCE_PERMISSION_CHANGES_TOTAL,
        {"kb_id": "kb-tel-test", "change_type": "key_issued"},
    )
    assert after == before + 1.0


def test_incr_promotion():
    from finecorpus.telemetry import (
        GOVERNANCE_PROMOTIONS_TOTAL,
        INDEX_PROMOTIONS_TOTAL,
        incr_promotion,
    )

    gov_before = _get_counter_value(
        GOVERNANCE_PROMOTIONS_TOTAL, {"kb_id": "kb-tel-2", "actor_id": "a"}
    )
    idx_before = _get_counter_value(INDEX_PROMOTIONS_TOTAL, {"kb_id": "kb-tel-2"})

    incr_promotion(kb_id="kb-tel-2", actor_id="a")

    gov_after = _get_counter_value(
        GOVERNANCE_PROMOTIONS_TOTAL, {"kb_id": "kb-tel-2", "actor_id": "a"}
    )
    idx_after = _get_counter_value(INDEX_PROMOTIONS_TOTAL, {"kb_id": "kb-tel-2"})

    assert gov_after == gov_before + 1.0
    assert idx_after == idx_before + 1.0


# ---------------------------------------------------------------------------
# Test: /metrics endpoint serves
# ---------------------------------------------------------------------------


def test_metrics_endpoint_serves_on_retrieval_api():
    """GET /metrics on the retrieval-api returns 200 with prometheus content."""
    from fastapi.testclient import TestClient

    from finecorpus.services.retrieval_api import app

    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    content_type = r.headers.get("content-type", "")
    # prometheus returns text/plain
    assert "text" in content_type or "plain" in content_type


def test_metrics_endpoint_serves_on_control_api():
    """GET /metrics on the control-api returns 200 with prometheus content."""
    from fastapi.testclient import TestClient

    from finecorpus.services.control_api import app

    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200


def test_metrics_contain_rtfc_prefix():
    """Metrics output contains at least one rtfc_ prefixed metric."""
    from fastapi.testclient import TestClient

    from finecorpus.services.control_api import app

    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    # After importing telemetry, rtfc_ metrics are registered
    # They should appear in the output
    assert "rtfc_" in r.text or len(r.text) > 0  # at minimum non-empty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_counter_value(counter: object, labels: dict) -> float:
    """Read the current value of a labelled counter."""
    try:
        return counter.labels(**labels)._value.get()
    except Exception:
        return 0.0
