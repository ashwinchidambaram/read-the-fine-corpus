"""RecordedHTTPClient replay — deterministic, no live network."""

from __future__ import annotations

import pytest

from finecorpus.connectors.fixtures import RecordedHTTPClient, UnmatchedRequestError
from finecorpus.connectors.http import HTTPClient


def test_recorded_client_satisfies_protocol() -> None:
    client = RecordedHTTPClient([])
    assert isinstance(client, HTTPClient)


def test_replays_json_response() -> None:
    client = RecordedHTTPClient(
        [
            {
                "match": {"method": "GET", "url": "https://api.example/docs"},
                "response": {"status_code": 200, "json": {"items": [1, 2, 3]}},
            }
        ]
    )
    resp = client.request("GET", "https://api.example/docs")
    assert resp.status_code == 200
    assert resp.json() == {"items": [1, 2, 3]}
    assert resp.headers["content-type"] == "application/json"


def test_interactions_matched_in_order_once() -> None:
    client = RecordedHTTPClient(
        [
            {
                "match": {"method": "GET", "url": "https://api.example/p1"},
                "response": {"status_code": 200, "json": {"page": 1}},
            },
            {
                "match": {"method": "GET", "url": "https://api.example/p2"},
                "response": {"status_code": 200, "json": {"page": 2}},
            },
        ]
    )
    assert client.request("GET", "https://api.example/p1").json() == {"page": 1}
    assert client.request("GET", "https://api.example/p2").json() == {"page": 2}
    # First interaction consumed — replaying it again raises.
    with pytest.raises(UnmatchedRequestError):
        client.request("GET", "https://api.example/p1")


def test_unmatched_request_raises() -> None:
    client = RecordedHTTPClient([])
    with pytest.raises(UnmatchedRequestError, match="no live calls"):
        client.request("POST", "https://evil.example/exfil")


def test_calls_are_recorded_without_headers() -> None:
    client = RecordedHTTPClient(
        [
            {
                "match": {"method": "GET", "url": "https://api.example/x"},
                "response": {"status_code": 204, "body": ""},
            }
        ]
    )
    client.request("GET", "https://api.example/x", headers={"Authorization": "Bearer secret"})
    # Recorded call log must not carry the auth header (§14.2).
    assert client.calls == [{"method": "GET", "url": "https://api.example/x"}]
