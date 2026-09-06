"""M-1: the production construction PATH (§19 criterion 1 reachability).

These tests prove that a production ``create_app(config)`` actually builds a
working ``EngineContext`` from a real ``Config`` — i.e. that criterion 1 (a KB
flow reachable in production) is not stranded behind test-only injection — and
that the ``corpus web`` CLI subcommand exists and dispatches.

To avoid requiring a live Qdrant/Postgres/embedding backend, the three infra
builders that ``EngineContext.from_config`` calls are monkeypatched to fakes.
The point is that the CONSTRUCTION PATH runs in production code (from_config →
create_app), not that we can inject dependencies in a test.
"""

from __future__ import annotations

from typing import Any

import pytest

from finecorpus.config.models import Config
from finecorpus.web import create_app
from finecorpus.web.engine import EngineContext


@pytest.fixture
def patched_builders(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the three infra builders from_config uses with fakes.

    Patches at the definition site so the real ``from_config`` code path (the
    production wiring) executes end-to-end — only the leaf infra constructors
    are replaced.
    """
    calls: dict[str, Any] = {}

    class _FakeAdapter:
        def __init__(self, *, url: Any, api_key: Any) -> None:
            calls["adapter_url"] = url
            calls["adapter_api_key"] = api_key

    def _fake_build_provider(config: Any) -> Any:
        calls["provider_config"] = config
        return object()

    monkeypatch.setattr("finecorpus.index.qdrant.QdrantAdapter", _FakeAdapter)
    monkeypatch.setattr(
        "finecorpus.embedding.registry.build_provider_from_config", _fake_build_provider
    )
    return calls


def test_from_config_builds_the_three_deps(patched_builders: dict[str, Any]) -> None:
    config = Config()
    ctx = EngineContext.from_config(config)

    assert isinstance(ctx, EngineContext)
    # Session factory is a real sessionmaker bound to the control-plane engine.
    assert callable(ctx.session_factory)
    with ctx.session_factory() as session:  # context-manager compatible (engine.py uses this)
        assert session is not None
    # Adapter + provider came from the real from_config construction path.
    assert ctx.adapter is not None
    assert ctx.provider is not None
    assert ctx.config is config
    # The Qdrant adapter was constructed from config.storage.qdrant.*.
    assert patched_builders["adapter_url"] == config.storage.qdrant.url
    assert patched_builders["provider_config"] is config


def test_create_app_with_config_builds_engine(patched_builders: dict[str, Any]) -> None:
    """create_app(config) with engine=None must build a real engine (not 503)."""
    config = Config()
    app = create_app(config)
    # The engine was constructed via the production path and attached.
    assert app.state.engine is not None
    assert isinstance(app.state.engine, EngineContext)


def test_create_app_no_config_no_engine_stays_503_capable() -> None:
    """With neither config nor engine, the app still builds (login-only)."""
    app = create_app(config=None)
    assert app.state.engine is None


# ---------------------------------------------------------------------------
# corpus web CLI subcommand
# ---------------------------------------------------------------------------


def test_corpus_web_subparser_exists_and_parses() -> None:
    from finecorpus.cli.main import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["web"])
    assert args.command == "web"
    # --config is accepted.
    args2 = parser.parse_args(["web", "--config", "corpus.yaml"])
    assert args2.config == "corpus.yaml"


def test_corpus_web_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """`corpus web` must dispatch to _cmd_web, which loads config + serves."""
    import argparse

    from finecorpus.cli import main as cli_main

    served: dict[str, Any] = {}

    def _fake_create_app(config: Any) -> Any:
        served["config"] = config
        return object()

    def _fake_uvicorn_run(app: Any, *, host: str, port: int) -> None:
        served["host"] = host
        served["port"] = port

    # Real config load; fake the app build + serve so no socket is opened.
    monkeypatch.setattr("finecorpus.web.create_app", _fake_create_app)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _fake_uvicorn_run)

    rc = cli_main._cmd_web(argparse.Namespace(config=None))
    assert rc == 0
    assert "config" in served
    assert served["host"] == served["config"].web.host
    assert served["port"] == served["config"].web.port
