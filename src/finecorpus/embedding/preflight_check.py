"""Embedding provider preflight check — higher-layer injection target (F-04).

Lives in ``finecorpus.embedding`` rather than ``finecorpus.config``.
``finecorpus.embedding`` sits below ``finecorpus.config`` in the import-linter
layers contract (C-5: cli/services > pipeline > config/index > embedding >
control/contracts in pyproject.toml).

F-04 resolution chosen: accept an injected checker from a higher layer.
``finecorpus.config.preflight.run_preflight`` accepts an optional
``extra_checks`` list; callers at the ``cli``/``services`` tier inject this
check using ``make_embedding_check()`` defined here.  This keeps
``finecorpus.config`` free from any ``finecorpus.embedding`` import (which
would be a forbidden upward edge), while still running the check.

Usage::

    from finecorpus.embedding.preflight_check import make_embedding_check
    from finecorpus.config.preflight import run_preflight

    report = run_preflight(config, extra_checks=[make_embedding_check()])
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _check_embedding_provider(config: Any) -> Any:
    """Embedding provider availability check (§4.6, reference.md §3 check 6).

    Runs ``health_check()`` against each configured embedding provider:
    - Sends a fixed probe string and confirms the endpoint is reachable.
    - Confirms the declared model ID is available.
    - Confirms probe embedding dimensions match the declared ``*.dimensions``.
    - Reports latency.
    - In airgap mode (``platform.airgap=True``): skips cloud providers and
      confirms all providers have ``is_local=True``.
    - Checks pricing staleness (OQ-P-5).

    SKIPPED (not FAIL) when no provider is configured in the default slot.

    The ``config`` parameter is ``Any`` rather than ``Config`` to avoid a
    module-level import of ``finecorpus.config.models`` here
    (F-04: embedding must not import config at the module level).
    """
    import datetime
    import os

    import finecorpus.embedding.registry as _registry

    # Import result types from finecorpus._check_types (not finecorpus.config.preflight)
    # so that this module does not create a finecorpus.embedding → finecorpus.config
    # import edge. finecorpus._check_types is not listed in the C-5 layers contract
    # (exhaustive=false) and has no intra-package imports, so both config and embedding
    # can safely import from it. F-04 conscious resolution.
    from finecorpus._check_types import CheckResult, CheckStatus

    emb = config.providers.embedding
    airgap = config.platform.airgap or os.environ.get("RTFC_AIRGAP", "").lower() in {
        "1",
        "true",
        "yes",
    }
    pricing_stale_days = config.index_lifecycle.pricing_stale_warn_days

    # If no default provider is configured, SKIP (not an error — first-run prompt
    # wires this before ingestion begins).
    if not emb.default:
        return CheckResult(
            name="embedding_provider",
            status=CheckStatus.SKIPPED,
            message=(
                "No embedding provider is configured "
                "(providers.embedding.default is not set). "
                "Run the first-run prompt or set providers.embedding.default "
                "to 'openai' or 'ollama' in corpus.yaml."
            ),
        )

    results: list[str] = []
    has_fail = False
    has_warn = False

    # Check EVERY fully-configured provider, not just the default.
    providers_to_check: list[tuple[str, str]] = []  # (label, which)
    if (
        emb.default in {"openai", "cloud"}
        or (emb.cloud.provider_id and emb.cloud.provider_id != "openai")
        or emb.default in {"openai", "cloud"}
    ):
        if emb.default in {"openai", "cloud"}:
            providers_to_check.append(("cloud (openai) [default]", "cloud"))
        elif emb.cloud.provider_id:
            providers_to_check.append(("cloud (openai)", "cloud"))
    if emb.default in {"ollama", "local"} or emb.local.provider_id:
        if emb.default in {"ollama", "local"}:
            providers_to_check.append(("local (ollama) [default]", "local"))
        else:
            providers_to_check.append(("local (ollama)", "local"))

    # De-duplicate while preserving order (edge case: default matches provider_id)
    seen: set[str] = set()
    unique_providers: list[tuple[str, str]] = []
    for label, which in providers_to_check:
        if which not in seen:
            seen.add(which)
            unique_providers.append((label, which))
    providers_to_check = unique_providers

    for label, which in providers_to_check:
        try:
            provider = _registry.build_provider_from_config(config, which=which)
        except Exception as exc:
            results.append(f"FAIL  {label}: Could not construct provider — {exc}.")
            has_fail = True
            continue

        caps = provider.capabilities

        # Air-gap enforcement: cloud providers are blocked
        if airgap and not caps.is_local:
            results.append(
                f"FAIL  {label}: Air-gap mode is enabled (RTFC_AIRGAP) but "
                f"'{caps.provider_id}' is a cloud provider (is_local=False). "
                f"Use a local provider in air-gapped deployments."
            )
            has_fail = True
            continue

        # Run health_check()
        try:
            hc = provider.health_check()
        except Exception as exc:
            results.append(f"FAIL  {label}: health_check() raised {type(exc).__name__}: {exc}.")
            has_fail = True
            continue

        if not hc.reachable:
            results.append(
                f"FAIL  {label} ({caps.provider_id}/{caps.model_id}): "
                f"endpoint not reachable. "
                f"Error: {hc.error or 'none'}. "
                f"Check that the provider service is running and that the "
                f"endpoint/API key is configured correctly."
            )
            has_fail = True
            continue

        if not hc.model_available:
            results.append(
                f"FAIL  {label} ({caps.provider_id}/{caps.model_id}): "
                f"model not available at provider. "
                f"Error: {hc.error or 'none'}."
            )
            has_fail = True
            continue

        if not hc.declared_dimensions_confirmed:
            results.append(
                f"FAIL  {label} ({caps.provider_id}/{caps.model_id}): "
                f"declared dimensions ({caps.vector_dimensions}) do not match "
                f"probe response. "
                f"Error: {hc.error or 'none'}. "
                f"This is a fatal configuration error — the index would be corrupt."
            )
            has_fail = True
            continue

        # Pricing staleness check (cloud only, OQ-P-5)
        if not caps.is_local and caps.pricing_as_of:
            try:
                as_of = datetime.date.fromisoformat(caps.pricing_as_of)
                age_days = (datetime.date.today() - as_of).days
                if age_days > pricing_stale_days:
                    results.append(
                        f"WARN  {label}: pricing_as_of is {caps.pricing_as_of} "
                        f"({age_days} days ago). "
                        f"Run `corpus provider update-pricing {caps.provider_id}` "
                        f"to refresh cost estimates."
                    )
                    has_warn = True
            except ValueError:
                pass  # malformed date; not a hard error

        results.append(
            f"OK    {label} ({caps.provider_id}/{caps.model_id}): "
            f"reachable; dimensions={caps.vector_dimensions} confirmed; "
            f"latency={hc.latency_ms:.0f}ms."
        )

    message = " | ".join(results) if results else "No providers checked."

    if has_fail:
        return CheckResult(name="embedding_provider", status=CheckStatus.FAIL, message=message)
    if has_warn:
        return CheckResult(name="embedding_provider", status=CheckStatus.WARN, message=message)
    return CheckResult(name="embedding_provider", status=CheckStatus.OK, message=message)


def make_embedding_check() -> Callable[[Any], Any]:
    """Return the embedding_provider check callable for injection into run_preflight.

    Usage::

        from finecorpus.embedding.preflight_check import make_embedding_check
        from finecorpus.config.preflight import run_preflight

        report = run_preflight(config, extra_checks=[make_embedding_check()])
    """
    return _check_embedding_provider


__all__ = ["make_embedding_check"]
