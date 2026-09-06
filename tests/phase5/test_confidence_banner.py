"""Phase 5 tests for render_confidence_banner (§9.2, §19 criterion 3).

The confidence banner is the shared, dependency-free surface that makes
provisional eval-set status inescapable.  These tests prove:

- PROVISIONAL (uppercase) appears when the set is provisional OR any question is
  unreviewed, and the banner states the unreviewed count and that scores are not
  authoritative.
- A fully-reviewed set yields a calm banner that does NOT contain PROVISIONAL.
"""

from __future__ import annotations

from finecorpus.contracts.eval_set import ConfidenceLevel
from finecorpus.pipeline.report import render_confidence_banner


class TestProvisionalBanner:
    """A generated + unreviewed set must surface PROVISIONAL prominently."""

    def test_provisional_confidence_level_shows_provisional(self) -> None:
        banner = render_confidence_banner(
            confidence_level=ConfidenceLevel.provisional,
            n_total=15,
            n_unreviewed=15,
        )
        assert "PROVISIONAL" in banner
        # unreviewed count is stated
        assert "15" in banner
        # scores are declared not authoritative
        assert "NOT AUTHORITATIVE" in banner.upper()

    def test_unreviewed_forces_provisional_even_if_level_reviewed(self) -> None:
        """n_unreviewed > 0 overrides a (mislabelled) reviewed level."""
        banner = render_confidence_banner(
            confidence_level=ConfidenceLevel.reviewed,
            n_total=10,
            n_unreviewed=3,
        )
        assert "PROVISIONAL" in banner
        assert "3" in banner

    def test_banner_is_multiline_and_plain_string(self) -> None:
        banner = render_confidence_banner(
            confidence_level=ConfidenceLevel.provisional,
            n_total=5,
            n_unreviewed=5,
        )
        assert isinstance(banner, str)
        assert "\n" in banner


class TestReviewedBanner:
    """A fully-reviewed set must NOT surface PROVISIONAL."""

    def test_reviewed_no_provisional(self) -> None:
        banner = render_confidence_banner(
            confidence_level=ConfidenceLevel.reviewed,
            n_total=12,
            n_unreviewed=0,
        )
        assert "PROVISIONAL" not in banner
        assert "REVIEWED" in banner.upper()

    def test_production_derived_no_provisional(self) -> None:
        banner = render_confidence_banner(
            confidence_level=ConfidenceLevel.production_derived,
            n_total=20,
            n_unreviewed=0,
        )
        assert "PROVISIONAL" not in banner
