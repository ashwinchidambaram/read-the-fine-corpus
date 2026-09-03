"""Test that finecorpus can be imported and version is correct."""

import finecorpus


def test_import():
    """Test that finecorpus imports and has correct version."""
    assert finecorpus.__version__ == "0.0.1"
