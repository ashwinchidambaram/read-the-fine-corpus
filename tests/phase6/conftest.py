"""Phase 6 test fixtures.

Re-exports the web KB-flow fixtures (``client``, ``engine_ctx``, ``source_dir``)
so the §19 acceptance layer in this directory can use them by parameter name
without importing them into its own module namespace (which would shadow the
same-named test parameters). Importing fixtures into a conftest is the idiomatic
pytest way to share them across directories.
"""

from __future__ import annotations

from tests.web.test_kb_flow import (  # noqa: F401  (re-exported fixtures)
    client,
    engine_ctx,
    source_dir,
)
