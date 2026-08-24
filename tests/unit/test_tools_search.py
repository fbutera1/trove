"""Unit tests for the retrieval tool.

Each test uses the `search_db` fixture (conftest.py) for a temp DB and
`insert_captured_nugget` (fixtures.py) + `nugget_enrich` for realistic data.
"""

import pytest

from trove.tools import nugget_enrich, nugget_search
from tests.fixtures import insert_captured_nugget


# ── T14: Keyword search over raw_content / summary / entities ─────────


def test_T14_keyword_search(search_db):
    """T14: nugget_search keyword query returns matching Nuggets by
    raw_content, summary, and entities.

    Inserts several captured Nuggets, enriches some with known summaries
    and entities, then verifies keyword matches across all FTS fields.
    """
    # Insert captured Nuggets with different content
    mid1 = insert_captured_nugget(
        search_db, text="need to reorder lobster clasps by Friday"
    )
    mid2 = insert_captured_nugget(
        search_db, text="Acme Beads has the best sterling chain"
    )
    mid3 = insert_captured_nugget(
        search_db, text="meeting with supplier at 3pm"
    )

    # Enrich mid1 and mid2 with summaries and entities
    nugget_enrich(
        mid1,
        classification="task",
        entities=["lobster clasps"],
        summary="Reorder lobster clasps by Friday",
        status="enriched",
        confidence=0.9,
    )
    nugget_enrich(
        mid2,
        classification="fact",
        entities=["Acme Beads", "sterling chain"],
        summary="Acme Beads carries sterling chain",
        status="enriched",
        confidence=0.85,
    )

    # Search by raw_content keyword
    results = nugget_search("lobster")
    assert len(results) >= 1
    assert results[0]["raw_content"] is not None
    assert "lobster" in results[0]["raw_content"].lower()

    # Search by summary keyword
    results = nugget_search("sterling")
    assert len(results) >= 1
    assert results[0]["message_id"] == mid2

    # Search by entities keyword (entity name in FTS index)
    results = nugget_search("Acme Beads")
    assert len(results) >= 1
    assert results[0]["message_id"] == mid2

    # Verify each result dict includes raw_content
    for row in results:
        assert "raw_content" in row
        assert row["raw_content"] is not None


# ── T15: Field filters ───────────────────────────────────────────────


def test_T15_field_filters(search_db):
    """T15: nugget_search field filters (classification, status, source)
    are applied correctly, including combined filters.
    """
    import sqlite3
    import time

    # Insert Nuggets spanning different classifications, statuses, sources
    mid1 = insert_captured_nugget(
        search_db, text="need to buy supplies"
    )
    mid2 = insert_captured_nugget(
        search_db, text="note about pricing strategy"
    )
    mid3 = insert_captured_nugget(
        search_db, text="quick note for next week's task"
    )

    # Insert a fourth row with a different source via raw SQL
    mid4 = "src-test-mid-4"
    now = time.time()
    conn = sqlite3.connect(str(search_db))
    conn.execute(
        """INSERT INTO nuggets
           (message_id, created_at, updated_at, author, source,
            raw_content, status, classification, entities, summary,
            confidence, links, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mid4, now, now, "00000000-0000-4000-8000-000000000099", "email",
            "supplies order from email",
            "captured", None, None, None,
            None, None, None,
        ),
    )
    conn.commit()
    conn.close()

    # Enrich mid1 as task, mid2 as fact, leave mid3 and mid4 as captured
    nugget_enrich(
        mid1,
        classification="task",
        entities=["supplies"],
        summary="Buy supplies",
        status="enriched",
    )
    nugget_enrich(
        mid2,
        classification="fact",
        entities=["pricing"],
        summary="Pricing strategy note",
        status="enriched",
    )

    # Filter by classification
    results = nugget_search("supplies", classification="task")
    assert len(results) >= 1
    assert all(r["classification"] == "task" for r in results)

    # Filter by status
    results = nugget_search("note", status="captured")
    assert len(results) >= 1
    assert all(r["status"] == "captured" for r in results)

    # Filter by source
    results = nugget_search("supplies", source="email")
    assert len(results) >= 1
    assert all(r["source"] == "email" for r in results)

    # Combined filter: classification + status
    results = nugget_search(
        "buy", classification="task", status="enriched"
    )
    assert len(results) >= 1
    assert all(
        r["classification"] == "task" and r["status"] == "enriched"
        for r in results
    )

    # Non-matching filter returns []
    results = nugget_search("buy", classification="idea")
    assert results == []


# ── T16: Empty / no-match / malformed — never raises ─────────────────


def test_T16_empty_no_match_never_raises(search_db):
    """T16: nugget_search returns [] for empty query, no match,
    malformed FTS input — never raises.
    """
    insert_captured_nugget(search_db, text="some content here")

    # No match
    assert nugget_search("zzznope") == []

    # Empty query
    assert nugget_search("") == []

    # Whitespace-only query
    assert nugget_search("   ") == []

    # Malformed FTS query (unbalanced quote) — should NOT raise
    assert nugget_search('"unbalanced quote') == []

    # limit=0 clamped to 1, but with no match still returns []
    assert nugget_search("zzznope", limit=0) == []

    # Negative limit clamped to 1
    assert nugget_search("zzznope", limit=-5) == []
