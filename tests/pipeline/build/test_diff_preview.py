"""Tests for the Tier 3 diff preview (§7.2, §7.4).

The diff preview is a stdlib-``difflib`` unified diff between the canonical
Tier-1 text and the Tier-3 rewritten text, surfaced when
``diff_preview_required`` is set.
"""

from __future__ import annotations

from finecorpus.pipeline.build.diff_preview import render_tier3_diff


class TestRenderTier3Diff:
    def test_renders_unified_diff_for_changed_text(self) -> None:
        original = "The cat sat on the mat.\nIt was a sunny day."
        rewritten = "A cat rested on the mat.\nThe day was sunny."
        diff = render_tier3_diff(original, rewritten)
        # Unified-diff structure present.
        assert "original (canonical Tier-1)" in diff
        assert "rewritten (Tier-3)" in diff
        assert "@@" in diff
        # Removed and added lines present.
        assert any(line.startswith("-The cat sat on the mat.") for line in diff.splitlines())
        assert any(line.startswith("+A cat rested on the mat.") for line in diff.splitlines())

    def test_identical_text_reports_no_changes(self) -> None:
        text = "Unchanged content."
        diff = render_tier3_diff(text, text)
        assert "no changes" in diff.lower()

    def test_no_trailing_newline(self) -> None:
        diff = render_tier3_diff("a", "b")
        assert not diff.endswith("\n")

    def test_deterministic(self) -> None:
        original = "line one\nline two\nline three"
        rewritten = "line one\nline TWO\nline three"
        assert render_tier3_diff(original, rewritten) == render_tier3_diff(original, rewritten)
