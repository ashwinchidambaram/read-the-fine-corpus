"""Unit tests for contract versioning (ContractVersionError, SpecRange, check_version).

See docs/contracts/README.md#contract-versioning.

Tests:
- Unsupported schema_version raises ContractVersionError (hard error, never warning).
- Supported versions (within range) do not raise.
- Wrong MAJOR raises.
- MINOR below min_minor raises.
- MINOR at or above min_minor succeeds.
- Malformed semver raises ContractVersionError.
"""

from __future__ import annotations

import pytest

from finecorpus.contracts.versions import (
    ContractVersionError,
    SpecRange,
    check_version,
)


class TestSpecRange:
    """SpecRange correctly accepts and rejects versions."""

    def test_exact_version_accepted(self) -> None:
        """1.0.0 is accepted by SpecRange(major=1, min_minor=0)."""
        sr = SpecRange(major=1, min_minor=0)
        sr.check("inventory", "1.0.0")  # must not raise

    def test_higher_minor_accepted(self) -> None:
        """1.5.3 is accepted by SpecRange(major=1, min_minor=2) (5 >= 2)."""
        sr = SpecRange(major=1, min_minor=2)
        sr.check("inventory", "1.5.3")

    def test_equal_min_minor_accepted(self) -> None:
        """1.2.0 is accepted by SpecRange(major=1, min_minor=2) (2 >= 2)."""
        sr = SpecRange(major=1, min_minor=2)
        sr.check("inventory", "1.2.0")

    def test_lower_minor_rejected(self) -> None:
        """1.1.0 is rejected by SpecRange(major=1, min_minor=2) (1 < 2)."""
        sr = SpecRange(major=1, min_minor=2)
        with pytest.raises(ContractVersionError) as exc_info:
            sr.check("inventory", "1.1.0")
        assert exc_info.value.contract == "inventory"
        assert exc_info.value.got == "1.1.0"

    def test_wrong_major_rejected(self) -> None:
        """2.0.0 is rejected by SpecRange(major=1, min_minor=0)."""
        sr = SpecRange(major=1, min_minor=0)
        with pytest.raises(ContractVersionError) as exc_info:
            sr.check("chunk", "2.0.0")
        assert exc_info.value.contract == "chunk"
        assert exc_info.value.got == "2.0.0"

    def test_zero_major_rejected_by_nonzero_consumer(self) -> None:
        """0.9.0 is rejected by SpecRange(major=1, min_minor=0)."""
        sr = SpecRange(major=1, min_minor=0)
        with pytest.raises(ContractVersionError):
            sr.check("segment_set", "0.9.0")

    def test_malformed_semver_raises(self) -> None:
        """A version string that is not valid semver raises ContractVersionError."""
        sr = SpecRange(major=1, min_minor=0)
        with pytest.raises(ContractVersionError):
            sr.check("eval_set", "not-a-version")

    def test_too_few_parts_raises(self) -> None:
        """'1.0' (missing patch) raises ContractVersionError."""
        sr = SpecRange(major=1, min_minor=0)
        with pytest.raises(ContractVersionError):
            sr.check("eval_set", "1.0")

    def test_too_many_parts_raises(self) -> None:
        """'1.0.0.0' (four parts) raises ContractVersionError."""
        sr = SpecRange(major=1, min_minor=0)
        with pytest.raises(ContractVersionError):
            sr.check("eval_set", "1.0.0.0")

    def test_error_message_contains_contract_name(self) -> None:
        """ContractVersionError message contains the contract name and received version."""
        sr = SpecRange(major=1, min_minor=0)
        with pytest.raises(ContractVersionError) as exc_info:
            sr.check("my_contract", "2.1.0")
        error_str = str(exc_info.value)
        assert "my_contract" in error_str
        assert "2.1.0" in error_str

    def test_describe_output(self) -> None:
        """SpecRange._describe() produces a human-readable string."""
        sr = SpecRange(major=1, min_minor=3)
        assert sr._describe() == "1.>=3"


class TestCheckVersion:
    """check_version() is the consumer-side helper that delegates to SpecRange.check()."""

    def test_supported_version_no_raise(self) -> None:
        sr = SpecRange(major=1, min_minor=0)
        check_version("inventory", "1.0.0", sr)  # must not raise

    def test_unsupported_version_raises(self) -> None:
        sr = SpecRange(major=1, min_minor=0)
        with pytest.raises(ContractVersionError):
            check_version("inventory", "2.0.0", sr)


class TestContractVersionError:
    """ContractVersionError has useful attributes."""

    def test_attributes(self) -> None:
        err = ContractVersionError(contract="chunk", got="2.0.0", supported="1.>=0")
        assert err.contract == "chunk"
        assert err.got == "2.0.0"
        assert err.supported == "1.>=0"

    def test_is_exception(self) -> None:
        err = ContractVersionError(contract="chunk", got="2.0.0", supported="1.>=0")
        assert isinstance(err, Exception)
