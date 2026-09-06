"""Recorded-fixture test harness for connectors (no live credentials).

The concrete-connector work unit (SharePoint / Confluence / Google Drive) needs
deterministic tests that never touch a real provider. This package provides
:class:`RecordedHTTPClient`, an :class:`~finecorpus.connectors.http.HTTPClient`
implementation that replays request/response pairs from JSON fixtures.

A fixture is a list of interactions::

    [
      {
        "match": {"method": "POST", "url": "https://provider/oauth/token"},
        "response": {"status_code": 200, "json": {"access_token": "..."}}
      }
    ]

Matching is by (method, url), in recorded order, so a test asserts the exact
call sequence it expects. Any unmatched request raises ``UnmatchedRequestError``
— a test can therefore prove that NO unexpected network call was attempted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from finecorpus.connectors.http import HTTPResponse

# ---------------------------------------------------------------------------
# Recorded interaction model
# ---------------------------------------------------------------------------


class UnmatchedRequestError(AssertionError):
    """Raised when a request has no matching recorded interaction.

    Subclasses ``AssertionError`` so an accidental live-network attempt fails
    the test loudly rather than silently escaping to the internet.
    """


class RecordedInteraction:
    """One recorded (request-matcher, response) pair."""

    def __init__(self, match: dict[str, Any], response: dict[str, Any]) -> None:
        self.match = match
        self.response = response
        self.consumed = False

    def matches(self, method: str, url: str) -> bool:
        """Return True when this interaction matches the given method/url."""
        if self.consumed:
            return False
        want_method = self.match.get("method")
        want_url = self.match.get("url")
        if want_method is not None and want_method.upper() != method.upper():
            return False
        if want_url is not None and want_url != url:
            return False
        return True

    def build_response(self) -> HTTPResponse:
        """Build an :class:`HTTPResponse` from the recorded response spec."""
        status = int(self.response.get("status_code", 200))
        headers = {k.lower(): v for k, v in self.response.get("headers", {}).items()}
        if "json" in self.response:
            body = json.dumps(self.response["json"]).encode("utf-8")
            headers.setdefault("content-type", "application/json")
        else:
            body = self.response.get("body", "").encode("utf-8")
        return HTTPResponse(status_code=status, body=body, headers=headers)


# ---------------------------------------------------------------------------
# Recorded HTTP client
# ---------------------------------------------------------------------------


class RecordedHTTPClient:
    """Replays recorded HTTP interactions; makes NO network calls.

    Satisfies the :class:`~finecorpus.connectors.http.HTTPClient` protocol, so
    it is a drop-in for connector and OAuth tests.

    Parameters
    ----------
    interactions:
        Ordered list of ``{"match": ..., "response": ...}`` dicts. Each is
        matched at most once (in order), so a test asserts an exact sequence.
    """

    def __init__(self, interactions: list[dict[str, Any]]) -> None:
        self._interactions = [RecordedInteraction(i["match"], i["response"]) for i in interactions]
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: str | Path) -> RecordedHTTPClient:
        """Load recorded interactions from a JSON fixture file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: Any | None = None,
    ) -> HTTPResponse:
        """Return the recorded response for ``(method, url)`` or raise.

        Records each call (without auth headers) so tests can assert the call
        sequence. Raises :class:`UnmatchedRequestError` when no recorded
        interaction matches — proving no unexpected network call was made.
        """
        self.calls.append({"method": method.upper(), "url": url})
        for interaction in self._interactions:
            if interaction.matches(method, url):
                interaction.consumed = True
                return interaction.build_response()
        raise UnmatchedRequestError(
            f"No recorded interaction for {method.upper()} {url}. "
            f"RecordedHTTPClient makes no live calls; add a fixture for this request."
        )


__all__ = [
    "UnmatchedRequestError",
    "RecordedInteraction",
    "RecordedHTTPClient",
]
