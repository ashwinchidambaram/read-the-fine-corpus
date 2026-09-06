"""Server-rendered web application for Read The Fine Corpus (Phase 6).

FastAPI + Jinja2 + HTMX, with a pluggable authentication seam. This package is
a top-layer entry surface: it may import the core library (config, services,
retrieval, pipeline, …). It sits just below ``finecorpus.cli`` in the import
layers (C-5) so the ``corpus web`` subcommand can launch it (``create_app``);
it must not import ``finecorpus.cli`` in return.

Public API::

    from finecorpus.web import create_app
"""

from finecorpus.web.app import create_app

__all__ = ["create_app"]
