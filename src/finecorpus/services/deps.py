"""FastAPI dependency providers for authentication and authorization (Phase 4).

Thin wiring layer — all logic lives in ``finecorpus.control.auth``.
These dependencies are injected into route handlers via FastAPI's DI system.

Design:
- ``require_principal``: Extracts the API key from ``Authorization: Bearer <key>``
  or ``X-API-Key`` header, calls ``ApiKeyRepository.validate()``, returns a
  ``Principal``.  Returns ``None`` when auth is disabled (``auth.enabled=False``)
  for Phase 1–3 backward compatibility.
- ``require_query_access``: Combines principal extraction with
  ``authorize(principal, Action.query_kb, kb_id=kb_id)``; raises HTTP 403 on deny.

Spec: §14.2 service principal API keys; §11.4 tenant filtering.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Path

from finecorpus.control.auth import Action, AuthError, Principal, authorize


def _extract_raw_key(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str | None:
    """Extract the raw API key string from request headers.

    Accepts ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``.
    Returns ``None`` if neither header is present.
    ``Authorization`` takes precedence over ``X-API-Key`` when both are present.
    """
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    if x_api_key:
        return x_api_key
    return None


def make_require_principal(
    repo_factory: Any,
    auth_enabled: bool,
) -> Any:
    """Build a ``require_principal`` FastAPI dependency.

    Args:
        repo_factory: Callable ``() -> ApiKeyRepository``.  The factory is called
            per-request within the dependency so each request gets its own session.
        auth_enabled: When ``False`` (Phase 1–3 compat mode) the dependency always
            returns ``None`` without touching the key store.

    Returns:
        A FastAPI dependency that returns ``Principal | None``.
    """

    def require_principal(
        raw_key: str | None = Depends(_extract_raw_key),
    ) -> Principal | None:
        if not auth_enabled:
            return None

        if raw_key is None:
            raise HTTPException(
                status_code=401,
                detail="Missing API key.  Provide 'Authorization: Bearer <key>' "
                "or 'X-API-Key: <key>'.",
            )

        repo = repo_factory()
        try:
            return repo.validate(raw_key)
        except AuthError:
            # Never include key material in the HTTP response.
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired API key.",
            ) from None

    return require_principal


def make_require_query_access(
    principal_dep: Any,
) -> Any:
    """Build a ``require_query_access`` FastAPI dependency for a query route.

    Args:
        principal_dep: The ``require_principal`` dependency produced by
            ``make_require_principal``.

    Returns:
        A FastAPI dependency that accepts ``kb_id`` as a path param and
        returns ``Principal | None``.  Raises HTTP 403 if the principal
        lacks permission to query the KB.
    """

    def require_query_access(
        kb_id: Annotated[str, Path(description="Knowledge-base UUID.")],
        principal: Annotated[Principal | None, Depends(principal_dep)],
    ) -> Principal | None:
        if principal is None:
            # Auth disabled — allow through (Phase 1–3 compat).
            return None

        try:
            authorize(principal, Action.query_kb, kb_id=kb_id)
        except AuthError as exc:
            raise HTTPException(
                status_code=403,
                detail=str(exc),
            ) from None

        return principal

    return require_query_access


__all__ = [
    "make_require_principal",
    "make_require_query_access",
]
