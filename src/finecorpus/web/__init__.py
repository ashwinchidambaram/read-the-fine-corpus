"""Server-rendered web application for Read The Fine Corpus (Phase 6).

FastAPI + Jinja2 + HTMX, with a pluggable authentication seam. This package is
a top-layer entry surface (sibling of ``finecorpus.cli``): it may import the
core library but must not be imported by it.

Public API::

    from finecorpus.web import create_app
"""

from finecorpus.web.app import create_app

__all__ = ["create_app"]
