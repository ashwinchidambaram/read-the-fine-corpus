"""Transport-agnostic HTTP seam for connectors and OAuth.

Connectors and the OAuth helper never talk to ``httpx`` directly. They depend
on the small :class:`HTTPClient` protocol here, so tests can inject a
:class:`~finecorpus.connectors.fixtures.RecordedHTTPClient` and run with NO live
network. Production code injects :class:`HttpxClient`, a thin adapter over the
``httpx`` client already vendored via FastAPI.

Keeping the seam minimal (one ``request`` method + a plain ``HTTPResponse``)
means recorded fixtures stay simple JSON and the replay client is trivial.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class HTTPResponse:
    """A minimal, transport-agnostic HTTP response.

    Attributes
    ----------
    status_code:
        HTTP status code.
    body:
        Raw response body bytes.
    headers:
        Response headers (lower-cased keys by convention).
    """

    status_code: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """Parse the body as JSON."""
        return json.loads(self.body.decode("utf-8"))


@runtime_checkable
class HTTPClient(Protocol):
    """The single HTTP method connectors and OAuth depend on.

    Production: :class:`HttpxClient`. Tests: ``RecordedHTTPClient``.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: Any | None = None,
    ) -> HTTPResponse:
        """Issue one HTTP request and return an :class:`HTTPResponse`."""
        ...


class HttpxClient:
    """Production ``HTTPClient`` backed by ``httpx`` (already a dependency).

    Constructed with an injected ``httpx.Client`` so callers control timeouts,
    transport, and lifecycle. This class never logs request bodies or auth
    headers (§14.2).
    """

    def __init__(self, client: Any) -> None:
        """Store the injected ``httpx.Client`` (typed ``Any`` to avoid a hard import)."""
        self._client = client

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: Any | None = None,
    ) -> HTTPResponse:
        """Delegate to the injected ``httpx.Client`` and normalise the response."""
        resp = self._client.request(
            method,
            url,
            headers=headers,
            data=data,
            json=json_body,
        )
        return HTTPResponse(
            status_code=resp.status_code,
            body=resp.content,
            headers={k.lower(): v for k, v in resp.headers.items()},
        )


__all__ = [
    "HTTPResponse",
    "HTTPClient",
    "HttpxClient",
]
