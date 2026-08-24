"""Integration tests for the enrichment tool.

Each test uses the `enrich_db` fixture (conftest.py) for a temp DB and
`insert_captured_nugget` (fixtures.py) for a realistic captured row.
"""

import json

import pytest

from trove.tools import nugget_enrich, _handle_nugget_enrich
from tests.fixtures import _get_nuggets, _count_nuggets, insert_captured_nugget


# ── T7: Failed enrichment leaves row intact ─────────────────────────


def test_T7_failed_enrichment_leaves_row_intact(enrich_db):
    """T7: A failed enrichment leaves the row at status='captured' (or 'failed')
    with raw_content intact. Also: a raising nugget_enrich call leaves the row
    at status='captured', raw_content intact, no partial write.
    """
    original_text = "important thought that must survive"
    mid = insert_captured_nugget(enrich_db, text=original_text)

    # Variant 1: status='failed' (agent reports enrichment failed)
    result = nugget_enrich(
        mid,
        status="failed",
    )

    assert result["status"] == "failed"
    assert result["raw_content"] == original_text

    # Variant 2: invalid classification raises — row stays at 'failed'
    with pytest.raises(ValueError, match="Invalid classification"):
        nugget_enrich(mid, classification="bogus")

    # Row is still intact (no partial write from the raising call)
    rows = _get_nuggets(enrich_db)
    assert len(rows) == 1
    assert rows[0]["raw_content"] == original_text
    assert rows[0]["status"] == "failed"  # Still from variant 1


# ── T13: Malformed call does not corrupt or delete ──────────────────


def test_T13_malformed_call_does_not_corrupt(enrich_db):
    """T13: nugget_enrich with non-existent message_id raises KeyError.
    _handle_nugget_enrich with malformed args returns error JSON (no raise).
    Existing rows are untouched.
    """
    original_text = "this nugget must survive everything"
    mid = insert_captured_nugget(enrich_db, text=original_text)

    # Verify initial state
    initial_count = _count_nuggets(enrich_db)
    assert initial_count == 1

    # Variant 1: non-existent message_id raises KeyError
    with pytest.raises(KeyError, match="No Nugget found"):
        nugget_enrich("nonexistent-id-12345", classification="task")

    # Row count and content unchanged
    assert _count_nuggets(enrich_db) == initial_count
    rows = _get_nuggets(enrich_db)
    assert rows[0]["raw_content"] == original_text

    # Variant 2: _handle_nugget_enrich with missing message_id
    # Returns error JSON, does NOT raise
    result_str = _handle_nugget_enrich({})
    error_result = json.loads(result_str)
    assert "error" in error_result
    assert error_result["message_id"] is None

    # Variant 3: _handle_nugget_enrich with invalid classification
    result_str2 = _handle_nugget_enrich({
        "message_id": mid,
        "classification": "invalid_type",
    })
    error_result2 = json.loads(result_str2)
    assert "error" in error_result2
    assert error_result2["message_id"] == mid

    # Row count and content still unchanged
    assert _count_nuggets(enrich_db) == initial_count
    rows = _get_nuggets(enrich_db)
    assert rows[0]["raw_content"] == original_text
    assert rows[0]["status"] == "captured"  # Never touched
