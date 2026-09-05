"""Unit tests for Tier 1 structure normalisation transforms.

Module: src/finecorpus/pipeline/build/transform.py

Covers:
- Each Tier 1 op: output correctness + record accuracy.
- No-op input produces a record with changed_text=False.
- Empty ops list → no records, text unchanged.
- Determinism: same (text, ops) always produces the same output.
- Changed-text flag accuracy (True only when bytes actually changed).
"""

from __future__ import annotations

from finecorpus.contracts.ingestion_config import Tier1Operation
from finecorpus.contracts.shared.blocks import AppliedBy, TransformationTier
from finecorpus.pipeline.build.transform import (
    _apply_header_inference,
    _apply_ocr_cleanup,
    _apply_whitespace_repair,
    apply_tier1,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_for_op(records, op: Tier1Operation):
    matches = [r for r in records if r.operation == op.value]
    assert matches, f"No record found for op={op.value!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# whitespace_repair
# ---------------------------------------------------------------------------


class TestWhitespaceRepair:
    def test_crlf_normalised(self):
        text = "line1\r\nline2\r\nline3"
        out, changed = _apply_whitespace_repair(text)
        assert "\r" not in out
        assert "line1\nline2\nline3" == out
        assert changed is True

    def test_cr_only_normalised(self):
        text = "a\rb"
        out, changed = _apply_whitespace_repair(text)
        assert out == "a\nb"
        assert changed is True

    def test_inline_spaces_collapsed(self):
        text = "hello   world"
        out, changed = _apply_whitespace_repair(text)
        assert out == "hello world"
        assert changed is True

    def test_trailing_whitespace_stripped(self):
        text = "hello   \nworld  "
        out, changed = _apply_whitespace_repair(text)
        assert not any(line != line.rstrip() for line in out.split("\n"))
        assert changed is True

    def test_excessive_blank_lines_collapsed(self):
        text = "para1\n\n\n\n\npara2"
        out, changed = _apply_whitespace_repair(text)
        # 3+ blank lines → 2
        assert "\n\n\n\n" not in out
        assert changed is True

    def test_already_clean_is_noop(self):
        text = "hello world\n\nfoo bar"
        out, changed = _apply_whitespace_repair(text)
        assert out == text
        assert changed is False

    def test_empty_string_noop(self):
        text = ""
        out, changed = _apply_whitespace_repair(text)
        assert out == ""
        assert changed is False


class TestTier1WhitespaceRecord:
    def test_record_tier_and_operation(self):
        text = "hello   world"
        _, records = apply_tier1(text, [Tier1Operation.whitespace_repair])
        assert len(records) == 1
        r = records[0]
        assert r.tier == TransformationTier.tier_1
        assert r.operation == "whitespace_repair"
        assert r.applied_by == AppliedBy.deterministic
        assert r.changed_text is True

    def test_noop_record_changed_text_false(self):
        text = "clean text already"
        _, records = apply_tier1(text, [Tier1Operation.whitespace_repair])
        assert records[0].changed_text is False

    def test_no_ops_no_records(self):
        text = "hello world"
        out, records = apply_tier1(text, [])
        assert out == text
        assert records == []


# ---------------------------------------------------------------------------
# ocr_cleanup
# ---------------------------------------------------------------------------


class TestOcrCleanup:
    def test_null_bytes_removed(self):
        text = "hel\x00lo\x00"
        out, changed = _apply_ocr_cleanup(text)
        assert "\x00" not in out
        assert changed is True

    def test_soft_hyphen_removed(self):
        text = "compli­cated"
        out, changed = _apply_ocr_cleanup(text)
        assert "­" not in out
        assert changed is True

    def test_control_chars_removed(self):
        text = "hello\x0bworld"
        out, changed = _apply_ocr_cleanup(text)
        assert "\x0b" not in out
        assert changed is True

    def test_repeated_punct_collapsed(self):
        # 4 dots → 1 dot
        text = "see figure 1...."
        out, changed = _apply_ocr_cleanup(text)
        assert "...." not in out
        assert changed is True

    def test_nfc_normalization(self):
        # Decomposed 'é' → composed 'é'
        decomposed = "é"  # e + combining acute
        out, changed = _apply_ocr_cleanup(decomposed)
        assert out == "\xe9"  # composed é
        assert changed is True

    def test_clean_text_noop(self):
        text = "This is clean text with normal punctuation."
        out, changed = _apply_ocr_cleanup(text)
        assert out == text
        assert changed is False

    def test_tabs_preserved(self):
        text = "col1\tcol2\tcol3"
        out, changed = _apply_ocr_cleanup(text)
        # tabs should NOT be removed (they are whitespace, not C0 control chars we remove)
        assert "\t" in out

    def test_newlines_preserved(self):
        text = "line1\nline2"
        out, changed = _apply_ocr_cleanup(text)
        assert "\n" in out


class TestTier1OcrRecord:
    def test_record_tier_and_operation(self):
        text = "hel\x00lo"
        _, records = apply_tier1(text, [Tier1Operation.ocr_cleanup])
        r = records[0]
        assert r.tier == TransformationTier.tier_1
        assert r.operation == "ocr_cleanup"
        assert r.changed_text is True

    def test_noop_changed_text_false(self):
        text = "Normal clean text."
        _, records = apply_tier1(text, [Tier1Operation.ocr_cleanup])
        assert records[0].changed_text is False


# ---------------------------------------------------------------------------
# table_to_markdown (always a no-op at Build layer)
# ---------------------------------------------------------------------------


class TestTableToMarkdown:
    def test_text_unchanged(self):
        text = "| Col A | Col B |\n|---|---|\n| 1 | 2 |"
        out, records = apply_tier1(text, [Tier1Operation.table_to_markdown])
        assert out == text

    def test_changed_text_false(self):
        text = "| Col A | Col B |"
        _, records = apply_tier1(text, [Tier1Operation.table_to_markdown])
        assert records[0].changed_text is False

    def test_record_emitted(self):
        text = "some table"
        _, records = apply_tier1(text, [Tier1Operation.table_to_markdown])
        assert len(records) == 1
        assert records[0].operation == "table_to_markdown"


# ---------------------------------------------------------------------------
# header_inference
# ---------------------------------------------------------------------------


class TestHeaderInference:
    def test_bare_short_heading_promoted(self):
        text = "Introduction\n\nThis is a longer paragraph of text that follows the heading."
        out, changed = _apply_header_inference(text)
        assert out.startswith("# Introduction")
        assert changed is True

    def test_already_markdown_heading_noop(self):
        text = "# Introduction\n\nSome body text here."
        out, changed = _apply_header_inference(text)
        assert out == text
        assert changed is False

    def test_sentence_ending_noop(self):
        text = "This is a sentence.\nFollowed by more text that is longer here."
        out, changed = _apply_header_inference(text)
        assert changed is False

    def test_long_first_line_noop(self):
        first_line = "A" * 80
        text = f"{first_line}\nFollowed by a longer sentence that goes on a lot."
        out, changed = _apply_header_inference(text)
        assert changed is False

    def test_no_second_line_noop(self):
        text = "Short title"
        out, changed = _apply_header_inference(text)
        assert changed is False


class TestTier1HeaderRecord:
    def test_record_operation(self):
        text = "Overview\n\nThis is a longer paragraph of body text that follows."
        _, records = apply_tier1(text, [Tier1Operation.header_inference])
        assert records[0].operation == "header_inference"

    def test_record_changed_reflects_actual_change(self):
        text = "Overview\n\nThis is a longer paragraph of body text that follows."
        _, records = apply_tier1(text, [Tier1Operation.header_inference])
        # Header should be promoted (changed_text=True) if heuristic fires
        out, _ = apply_tier1(text, [Tier1Operation.header_inference])
        if out.startswith("# "):
            assert records[0].changed_text is True
        else:
            assert records[0].changed_text is False


# ---------------------------------------------------------------------------
# Composed pipeline
# ---------------------------------------------------------------------------


class TestApplyTier1Composed:
    def test_ops_applied_in_order(self):
        # Both ops requested; records in order
        text = "hel\x00lo   world\r\n"
        _, records = apply_tier1(
            text,
            [Tier1Operation.ocr_cleanup, Tier1Operation.whitespace_repair],
        )
        assert len(records) == 2
        assert records[0].operation == "ocr_cleanup"
        assert records[1].operation == "whitespace_repair"

    def test_all_four_ops(self):
        text = "hel\x00lo   world\r\n"
        out, records = apply_tier1(
            text,
            [
                Tier1Operation.ocr_cleanup,
                Tier1Operation.whitespace_repair,
                Tier1Operation.table_to_markdown,
                Tier1Operation.header_inference,
            ],
        )
        assert len(records) == 4
        assert all(r.tier == TransformationTier.tier_1 for r in records)
        assert all(r.applied_by == AppliedBy.deterministic for r in records)

    def test_tier_1_changed_text_never_raises(self):
        """TransformationRecord with tier=1 and changed_text=True must NOT raise (§7.2)."""
        text = "hello   world"
        _, records = apply_tier1(text, [Tier1Operation.whitespace_repair])
        # The record has changed_text=True for whitespace_repair (text changed)
        r = records[0]
        assert r.changed_text is True  # validator does NOT block this for tier 1

    def test_determinism(self):
        text = "hel\x00lo   world\r\n"
        ops = [Tier1Operation.ocr_cleanup, Tier1Operation.whitespace_repair]
        out1, rec1 = apply_tier1(text, ops)
        out2, rec2 = apply_tier1(text, ops)
        assert out1 == out2
        assert [(r.operation, r.changed_text) for r in rec1] == [
            (r.operation, r.changed_text) for r in rec2
        ]

    def test_empty_ops_is_noop(self):
        text = "anything"
        out, records = apply_tier1(text, [])
        assert out == text
        assert records == []

    def test_noop_input_all_ops_changed_text_false(self):
        """Clean text → all changed_text=False."""
        text = "Clean text\n\nWith paragraph."
        ops = [
            Tier1Operation.whitespace_repair,
            Tier1Operation.ocr_cleanup,
            Tier1Operation.table_to_markdown,
        ]
        _, records = apply_tier1(text, ops)
        assert all(r.changed_text is False for r in records), [
            (r.operation, r.changed_text) for r in records
        ]
