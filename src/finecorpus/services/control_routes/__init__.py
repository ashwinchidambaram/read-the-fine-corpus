"""Control-plane route scaffold (Phase 4+).

This package is a router scaffold so later PRs adding control-plane API
routes (key management, audit, break-glass, etc.) can register their routers
here without colliding with ``services/control_api.py``.

Usage (for future PRs)::

    from finecorpus.services.control_routes import router
    # attach sub-routers:
    # router.include_router(keys_router, prefix="/v1/control/keys")

The empty router is importable now so callers can register it with the FastAPI
app even before any routes exist.
"""

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
