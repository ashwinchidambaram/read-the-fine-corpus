"""Per-KB dashboard metrics — reuse ``finecorpus.telemetry`` (§8.5/§9.3).

Dashboards are secondary to the create-KB→endpoint flow.  This reads the live
prometheus collectors already defined in ``finecorpus.telemetry`` (ingestion,
index, retrieval, quality, governance) and projects the per-KB samples the web
dashboard renders — including spend attribution (ingestion cost in USD).

No new metric is defined here; the web layer only *reads* the existing registry
so the CLI, retrieval service, and dashboard cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KBMetrics:
    """Projected per-KB metric samples for the dashboard."""

    kb_id: str
    retrieval_requests: float = 0.0
    retrieval_errors: float = 0.0
    ingestion_docs: float = 0.0
    ingestion_cost_usd: float = 0.0
    promotions: float = 0.0
    rollbacks: float = 0.0
    spend_by_operation: dict[str, float] = field(default_factory=dict)


def _sum_for_kb(collector: Any, kb_id: str, *, group_label: str | None = None) -> Any:
    """Sum a prometheus Counter/Gauge's samples for a given kb_id.

    When ``group_label`` is set, returns ``{label_value: sum}`` grouped by that
    label instead of a single total (used for spend-by-operation attribution).
    """
    total = 0.0
    grouped: dict[str, float] = {}
    try:
        for metric in collector.collect():
            for sample in metric.samples:
                labels = sample.labels
                if labels.get("kb_id") != kb_id:
                    continue
                # Counters expose a `_total` sample; gauges expose the bare name.
                if not (sample.name.endswith("_total") or sample.name == metric.name):
                    continue
                if group_label is not None:
                    key = labels.get(group_label, "unknown")
                    grouped[key] = grouped.get(key, 0.0) + sample.value
                else:
                    total += sample.value
    except Exception:  # noqa: BLE001 — dashboard is best-effort, never fails a request
        pass
    return grouped if group_label is not None else total


def kb_metrics(kb_id: str) -> KBMetrics:
    """Project the per-KB dashboard metrics from the live telemetry registry."""
    from finecorpus import telemetry

    spend = _sum_for_kb(telemetry.INGESTION_COST_USD, kb_id, group_label="operation_type")
    return KBMetrics(
        kb_id=kb_id,
        retrieval_requests=_sum_for_kb(telemetry.RETRIEVAL_REQUESTS_TOTAL, kb_id),
        retrieval_errors=_sum_for_kb(telemetry.RETRIEVAL_ERRORS_TOTAL, kb_id),
        ingestion_docs=_sum_for_kb(telemetry.INGESTION_DOCS_PROCESSED, kb_id),
        ingestion_cost_usd=sum(spend.values()) if isinstance(spend, dict) else 0.0,
        promotions=_sum_for_kb(telemetry.INDEX_PROMOTIONS_TOTAL, kb_id),
        rollbacks=_sum_for_kb(telemetry.INDEX_ROLLBACKS_TOTAL, kb_id),
        spend_by_operation=spend if isinstance(spend, dict) else {},
    )


__all__ = ["KBMetrics", "kb_metrics"]
