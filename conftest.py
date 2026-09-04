"""Root pytest configuration for Read The Fine Corpus.

Registers custom markers so pytest --co does not emit PytestUnknownMarkWarning.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "provider_integration: live embedding provider integration tests "
        "(require Ollama at localhost:11434 or OPENAI_API_KEY). "
        "Skipped by default. Run with: pytest -m provider_integration",
    )
