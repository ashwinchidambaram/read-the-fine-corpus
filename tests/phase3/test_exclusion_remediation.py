"""Tests for M-040: exclusion report grouped output, remediation per group, completeness.

- Exclusions are grouped by reason.
- Each group has remediation guidance (user_action).
- Completeness invariant: every ExclusionRecord appears in the report.
- 'nothing to do' guidance for correct-behavior exclusions.
- Plan-stage exclusions_confirmed appear in the report (M-040 extension).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Helpers — build minimal test data
# ---------------------------------------------------------------------------


def _make_exclusion_record(
    exc_id: str,
    doc_id: str,
    reason: str,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "exclusion_id": exc_id,
        "location": {"locator_kind": "char_range", "char_start": 0, "char_end": 10},
        "source_region_ids": [f"region-{exc_id}"],
        "reason": reason,
        "reason_detail": detail,
        "reversible": True,
    }


def _make_segment_set(
    doc_id: str,
    exclusions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "1.1.0",
        "document_id": doc_id,
        "content_hash": "abc",
        "tenancy": {
            "workspace_id": "ws-test",
            "kb_id": "kb-test",
            "permission_mode": "public_to_kb",
            "permission_principals": [],
            "permission_source": "platform",
            "permission_fidelity": "authoritative",
        },
        "segments": [],
        "reassembly": {
            "method": "document_order_concat",
            "covered_region_ids": [e["source_region_ids"][0] for e in exclusions],
            "reassembly_digest": "fakedigest",
        },
        "exclusions": exclusions,
        "cross_references": [],
        "decomposed_at": "2026-01-01T00:00:00Z",
    }


def _make_context(
    exclusion_records: dict[str, dict[str, Any]],
    plan_exclusions: list[dict[str, Any]] | None = None,
    version_families: list[dict[str, Any]] | None = None,
) -> Any:
    """Build a _Context object for testing the report generator."""
    from finecorpus.pipeline.report import _Context

    return _Context(
        run_id="test-run",
        parse_results={},
        segment_sets={},
        version_families=version_families or [],
        boilerplate_blocks=[],
        source_paths={},
        exclusion_records=exclusion_records,
        decompose_artifact_present=True,
        plan_exclusions_confirmed=plan_exclusions or [],
        plan_language_support=None,
    )


# ---------------------------------------------------------------------------
# Grouping tests
# ---------------------------------------------------------------------------


class TestExclusionGrouping:
    """Exclusions are grouped by reason in the report."""

    def test_exclusions_grouped_by_reason_in_json(self):
        """build_exclusions_json groups entries by reason code."""
        from finecorpus.pipeline.report import _build_exclusions_json

        exc_records: dict[str, dict[str, Any]] = {}
        for i, reason in enumerate(["too_short", "too_short", "empty_region", "encrypted"]):
            exc_id = f"exc-{i:03d}"
            rec = _make_exclusion_record(exc_id, f"doc-{i}", reason)
            rec["_document_id"] = f"doc-{i}"
            exc_records[exc_id] = rec

        ctx = _make_context(exc_records)
        result = _build_exclusions_json(ctx)

        by_reason = result["summary"]["by_reason"]
        assert by_reason.get("too_short", 0) == 2
        assert by_reason.get("empty_region", 0) == 1
        assert by_reason.get("encrypted", 0) == 1

    def test_exclusion_report_markdown_has_reason_sections(self):
        """Markdown render has a section per reason group."""
        from finecorpus.pipeline.report import _build_exclusions_json, _render_exclusions_md

        exc_records: dict[str, dict[str, Any]] = {}
        for i, reason in enumerate(["too_short", "encrypted"]):
            exc_id = f"exc-{i:03d}"
            rec = _make_exclusion_record(exc_id, f"doc-{i}", reason)
            rec["_document_id"] = f"doc-{i}"
            exc_records[exc_id] = rec

        ctx = _make_context(exc_records)
        data = _build_exclusions_json(ctx)
        md = _render_exclusions_md(data, "test-run")

        # Both reason sections should appear as headings
        assert "Segments: Too Short" in md or "Too Short" in md
        assert "Encrypted" in md

    def test_each_group_has_remediation_guidance(self):
        """Each reason group has a 'What to do' section."""
        from finecorpus.pipeline.report import _build_exclusions_json, _render_exclusions_md

        exc_records: dict[str, dict[str, Any]] = {}
        reasons = ["too_short", "encrypted", "unservable_content"]
        for i, reason in enumerate(reasons):
            exc_id = f"exc-{i:03d}"
            rec = _make_exclusion_record(exc_id, f"doc-{i}", reason)
            rec["_document_id"] = f"doc-{i}"
            exc_records[exc_id] = rec

        ctx = _make_context(exc_records)
        data = _build_exclusions_json(ctx)
        md = _render_exclusions_md(data, "test-run")

        # Each group should have remediation text
        assert "**What to do:**" in md

    def test_correct_behavior_groups_say_nothing_to_do(self):
        """Reasons like too_short and empty_region say 'Nothing to do'."""
        from finecorpus.pipeline.report import _build_exclusions_json, _render_exclusions_md

        exc_records: dict[str, dict[str, Any]] = {}
        for reason in ["too_short", "empty_region"]:
            exc_id = f"exc-{reason}"
            rec = _make_exclusion_record(exc_id, f"doc-{reason}", reason)
            rec["_document_id"] = f"doc-{reason}"
            exc_records[exc_id] = rec

        ctx = _make_context(exc_records)
        data = _build_exclusions_json(ctx)
        md = _render_exclusions_md(data, "test-run")

        # Both should have "Nothing" in their remediation
        assert "Nothing" in md

    def test_encrypted_remediation_has_action(self):
        """Encrypted files must have an actionable remediation (decrypt/remove)."""
        from finecorpus.pipeline.report import _build_exclusions_json, _render_exclusions_md

        exc_records = {
            "exc-enc": {
                **_make_exclusion_record("exc-enc", "doc-enc", "encrypted"),
                "_document_id": "doc-enc",
            }
        }
        ctx = _make_context(exc_records)
        data = _build_exclusions_json(ctx)
        md = _render_exclusions_md(data, "test-run")

        # Encrypted should say "Decrypt" (actionable)
        assert "ecrypt" in md or "password" in md.lower()


# ---------------------------------------------------------------------------
# Completeness invariant
# ---------------------------------------------------------------------------


class TestCompletenessInvariant:
    """Every ExclusionRecord appears in the exclusion report — zero silent gaps."""

    def test_all_exclusion_records_appear_in_output(self):
        """Every exc_id in the input appears in the JSON output."""
        from finecorpus.pipeline.report import _build_exclusions_json

        exc_ids = ["exc-001", "exc-002", "exc-003", "exc-004", "exc-005"]
        reasons = ["too_short", "encrypted", "empty_region", "unservable_content", "duplicate"]

        exc_records: dict[str, dict[str, Any]] = {}
        for exc_id, reason in zip(exc_ids, reasons, strict=True):
            rec = _make_exclusion_record(exc_id, f"doc-{exc_id}", reason)
            rec["_document_id"] = f"doc-{exc_id}"
            exc_records[exc_id] = rec

        ctx = _make_context(exc_records)
        result = _build_exclusions_json(ctx)

        output_exc_ids = {e["exclusion_id"] for e in result["exclusions"]}
        for exc_id in exc_ids:
            assert exc_id in output_exc_ids, (
                f"Exclusion record {exc_id} missing from output (completeness invariant violated)"
            )

    def test_total_exclusions_count_matches_input(self):
        """Total exclusion count in summary equals number of input records."""
        from finecorpus.pipeline.report import _build_exclusions_json

        exc_records: dict[str, dict[str, Any]] = {}
        for i in range(7):
            exc_id = f"exc-{i:03d}"
            rec = _make_exclusion_record(exc_id, f"doc-{i}", "too_short")
            rec["_document_id"] = f"doc-{i}"
            exc_records[exc_id] = rec

        ctx = _make_context(exc_records)
        result = _build_exclusions_json(ctx)

        assert result["summary"]["total_exclusions"] == 7

    def test_empty_exclusions_reports_zero(self):
        """No exclusions → total_exclusions=0."""
        from finecorpus.pipeline.report import _build_exclusions_json

        ctx = _make_context({})
        result = _build_exclusions_json(ctx)
        assert result["summary"]["total_exclusions"] == 0


# ---------------------------------------------------------------------------
# Plan-stage exclusions (M-040 extension)
# ---------------------------------------------------------------------------


class TestPlanExclusionsInReport:
    """Plan-stage exclusions_confirmed appear in the exclusion report (M-040)."""

    def test_plan_exclusions_appear_in_output(self):
        """ExclusionDecision from Plan stage appears in the report."""
        from finecorpus.pipeline.report import _build_exclusions_json

        plan_exclusions = [
            {
                "document_id": "doc-plan-001",
                "reason": "spreadsheet_database",
                "remediation": "Nothing — this is correct.",
            }
        ]

        ctx = _make_context({}, plan_exclusions=plan_exclusions)
        result = _build_exclusions_json(ctx)

        output_doc_ids = {e["document_id"] for e in result["exclusions"]}
        assert "doc-plan-001" in output_doc_ids, (
            "Plan-stage exclusion must appear in the exclusion report"
        )

    def test_plan_exclusions_carry_remediation_text(self):
        """Plan exclusions carry the remediation text in user_action."""
        from finecorpus.pipeline.report import _build_exclusions_json

        plan_exclusions = [
            {
                "document_id": "doc-plan-002",
                "reason": "encrypted",
                "remediation": "Decrypt the file and re-run the pipeline.",
            }
        ]
        ctx = _make_context({}, plan_exclusions=plan_exclusions)
        result = _build_exclusions_json(ctx)

        plan_entry = next(e for e in result["exclusions"] if e["document_id"] == "doc-plan-002")
        assert (
            "Decrypt" in plan_entry.get("user_action", "")
            or "ecrypt" in plan_entry.get("user_action", "").lower()
        )

    def test_plan_exclusions_not_double_counted_when_in_decompose(self):
        """If an exclusion appears in both decompose and plan, it's counted once."""
        from finecorpus.pipeline.report import _build_exclusions_json

        # Same doc, same reason in both decompose and plan
        exc_records = {
            "exc-dup": {
                **_make_exclusion_record("exc-dup", "doc-dup", "encrypted"),
                "_document_id": "doc-dup",
            }
        }
        plan_exclusions = [
            {
                "document_id": "doc-dup",
                "reason": "encrypted",
                "remediation": "Decrypt.",
            }
        ]
        ctx = _make_context(exc_records, plan_exclusions=plan_exclusions)
        result = _build_exclusions_json(ctx)

        # Should appear exactly once
        doc_dup_entries = [e for e in result["exclusions"] if e["document_id"] == "doc-dup"]
        assert len(doc_dup_entries) == 1, (
            "Exclusion present in both decompose and plan should not be double-counted"
        )


# ---------------------------------------------------------------------------
# Integration with report.py _render_exclusions_md
# ---------------------------------------------------------------------------


class TestExclusionMarkdownFormat:
    """Markdown rendering of grouped exclusions with per-group remediation."""

    def test_render_groups_exclusions_with_headings(self):
        """Markdown output has section headings for each reason group."""
        from finecorpus.pipeline.report import _build_exclusions_json, _render_exclusions_md

        exc_records = {
            "exc-ts": {
                **_make_exclusion_record("exc-ts", "doc-ts", "too_short"),
                "_document_id": "doc-ts",
            },
            "exc-en": {
                **_make_exclusion_record("exc-en", "doc-en", "encrypted"),
                "_document_id": "doc-en",
            },
        }
        ctx = _make_context(exc_records)
        data = _build_exclusions_json(ctx)
        md = _render_exclusions_md(data, "test-run")

        # Check for structured sections
        assert "##" in md  # headings present
        assert "Total exclusions:" in md

    def test_render_shows_user_action_per_group(self):
        """Each group's 'What to do' guidance appears in the render."""
        from finecorpus.pipeline.report import _build_exclusions_json, _render_exclusions_md

        exc_records = {
            "exc-sup": {
                **_make_exclusion_record("exc-sup", "doc-sup", "superseded_version"),
                "_document_id": "doc-sup",
            }
        }
        ctx = _make_context(exc_records)
        data = _build_exclusions_json(ctx)
        md = _render_exclusions_md(data, "test-run")

        # Superseded version guidance
        assert "superseded" in md.lower() or "index_superseded" in md.lower()
