"""Pagination via list_documents cursor + incremental cursor round-trip."""

from __future__ import annotations

from finecorpus.connectors.models import DocumentRef
from tests.connectors.conftest import FakeConnector


def test_list_documents_paginates_via_cursor() -> None:
    conn = FakeConnector()
    all_docs: list[DocumentRef] = []
    incremental = None

    page = conn.list_documents()
    all_docs.extend(page.documents)
    while page.next_cursor is not None:
        page = conn.list_documents(cursor=page.next_cursor)
        all_docs.extend(page.documents)
    incremental = conn.incremental_cursor(page)

    assert [d.source_id for d in all_docs] == ["d1", "d2"]
    # The delta-sync watermark surfaces on the final page.
    assert incremental is not None
    assert incremental.token == "delta-100"


def test_incremental_cursor_threads_into_next_run() -> None:
    conn = FakeConnector()
    page1 = conn.list_documents()
    # Passing the previous next_cursor continues the listing (delta-sync shape).
    page2 = conn.list_documents(cursor=page1.next_cursor)
    assert page2.next_cursor is None
    assert page2.incremental_cursor is not None
