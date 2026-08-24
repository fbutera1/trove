"""Integration tests for the dashboard backend API.

Each test uses the `dashboard_db` fixture (conftest.py) for a temp DB and
FastAPI TestClient wrapping the real app.
"""

import json
import socket
import sqlite3
import subprocess
import time

import pytest

from trove.tools import nugget_enrich
from tests.fixtures import insert_captured_nugget


# ── T18: GET /api/nuggets returns recent Nuggets, paginated ────────────


def test_T18_list_nuggets_paginated(dashboard_db):
    """T18: GET /api/nuggets returns recent Nuggets, paginated.

    Inserts ~6 Nuggets with staggered created_at, verifies count ≥ 6,
    items length matches limit, newest first by default, and pagination
    with offset returns different/older items. Asserts each item has
    raw_content.
    """
    client, db_path = dashboard_db

    # Insert 6 Nuggets with staggered created_at
    mids = []
    for i in range(6):
        mid = insert_captured_nugget(
            db_path,
            text=f"Nugget number {i+1}",
        )
        mids.append(mid)
        # Stagger created_at via raw SQL (each 100s apart, newest first)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE nuggets SET created_at = ? WHERE message_id = ?",
            (time.time() - (5 - i) * 100, mid),
        )
        conn.commit()
        conn.close()

    # GET with limit=2, offset=0
    resp = client.get("/api/nuggets?limit=2&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 6
    assert len(data["items"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0

    # Newest first by default
    assert data["items"][0]["created_at"] >= data["items"][1]["created_at"]

    # Each item has raw_content
    for item in data["items"]:
        assert "raw_content" in item
        assert item["raw_content"] is not None

    # Paginate with offset=2 → different items (older)
    resp2 = client.get("/api/nuggets?limit=2&offset=2")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["items"]) == 2
    # Items should be different (older)
    assert data2["items"][0]["message_id"] not in [
        i["message_id"] for i in data["items"]
    ]


# ── T19: Filters and sort work server-side ────────────────────────────


def test_T19_filters_and_sort(dashboard_db):
    """T19: GET /api/nuggets filters (classification/status/date/source)
    and sort (newest/oldest/relevant) work server-side.
    """
    client, db_path = dashboard_db

    # Insert Nuggets spanning classifications/statuses/sources
    mid1 = insert_captured_nugget(db_path, text="task about supplies")
    mid2 = insert_captured_nugget(db_path, text="fact about pricing")
    mid3 = insert_captured_nugget(db_path, text="note about next week's task")

    # Enrich mid1 as task, mid2 as fact
    nugget_enrich(mid1, classification="task", entities=["supplies"], summary="Order supplies", status="enriched", db_path=db_path)
    nugget_enrich(mid2, classification="fact", entities=["pricing"], summary="Pricing fact", status="enriched", db_path=db_path)

    # Insert mid4 with source=email via raw SQL
    mid4 = "src-test-mid-4"
    now = time.time()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO nuggets
           (message_id, created_at, updated_at, author, source,
            raw_content, status, classification, entities, summary,
            confidence, links, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mid4, now, now, "00000000-0000-4000-8000-000000000099", "email",
            "email about supplies",
            "captured", None, None, None,
            None, None, None,
        ),
    )
    conn.commit()
    conn.close()

    # Set an old created_at on mid3 (2 weeks ago)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE nuggets SET created_at = ? WHERE message_id = ?",
        (time.time() - 14 * 86400, mid3),
    )
    conn.commit()
    conn.close()

    # Filter by classification=task
    resp = client.get("/api/nuggets?classification=task")
    assert resp.status_code == 200
    data = resp.json()
    assert all(item["classification"] == "task" for item in data["items"])

    # Filter by status=captured
    resp = client.get("/api/nuggets?status=captured")
    assert resp.status_code == 200
    data = resp.json()
    assert all(item["status"] == "captured" for item in data["items"])

    # Filter by source=email
    resp = client.get("/api/nuggets?source=email")
    assert resp.status_code == 200
    data = resp.json()
    assert all(item["source"] == "email" for item in data["items"])

    # Filter by date=today (mid3 is 2 weeks old, should be excluded)
    resp = client.get("/api/nuggets?date=today")
    assert resp.status_code == 200
    data = resp.json()
    mid_ids_today = [item["message_id"] for item in data["items"]]
    assert mid3 not in mid_ids_today

    # Filter by date=this-week (mid3 is 2 weeks old, should be excluded)
    resp = client.get("/api/nuggets?date=this-week")
    assert resp.status_code == 200
    data = resp.json()
    mid_ids_week = [item["message_id"] for item in data["items"]]
    assert mid3 not in mid_ids_week

    # Sort oldest
    resp = client.get("/api/nuggets?sort=oldest")
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]
    if len(items) >= 2:
        assert items[0]["created_at"] <= items[1]["created_at"]

    # Sort relevant → falls back to newest
    resp_relevant = client.get("/api/nuggets?sort=relevant")
    resp_newest = client.get("/api/nuggets?sort=newest")
    assert resp_relevant.json()["items"] == resp_newest.json()["items"]

    # Combined filter: classification=task AND status=enriched
    resp = client.get("/api/nuggets?classification=task&status=enriched")
    assert resp.status_code == 200
    data = resp.json()
    assert all(
        item["classification"] == "task" and item["status"] == "enriched"
        for item in data["items"]
    )


# ── T20: GET /api/nuggets/{id} returns full detail + related ──────────


def test_T20_detail_and_related(dashboard_db):
    """T20: GET /api/nuggets/{id} returns full detail incl. raw_content,
    metadata, related Nuggets; returns 404 for unknown id.
    """
    client, db_path = dashboard_db

    # Insert + enrich a Nugget with entities
    mid1 = insert_captured_nugget(
        db_path, text="need to reorder lobster clasps"
    )
    nugget_enrich(
        mid1,
        classification="task",
        entities=["lobster clasps", "Acme Beads"],
        summary="Reorder lobster clasps from Acme Beads",
        status="enriched",
        confidence=0.9,
        db_path=db_path,
    )

    # Insert + enrich another Nugget sharing an entity
    mid2 = insert_captured_nugget(
        db_path, text="Acme Beads has sterling chain"
    )
    nugget_enrich(
        mid2,
        classification="fact",
        entities=["Acme Beads", "sterling chain"],
        summary="Acme Beads carries sterling chain",
        status="enriched",
        confidence=0.85,
        db_path=db_path,
    )

    # Insert an unrelated Nugget
    mid3 = insert_captured_nugget(
        db_path, text="unrelated thought about lunch"
    )
    nugget_enrich(
        mid3,
        classification="note",
        entities=["lunch", "sushi"],
        summary="Thinking about lunch",
        status="enriched",
        db_path=db_path,
    )

    # GET detail for mid1
    resp = client.get(f"/api/nuggets/{mid1}")
    assert resp.status_code == 200
    data = resp.json()

    # Full row fields present
    assert data["message_id"] == mid1
    assert data["raw_content"] == "need to reorder lobster clasps"
    assert data["classification"] == "task"
    assert data["confidence"] == 0.9

    # metadata parseable JSON
    metadata = json.loads(data["metadata"])
    assert isinstance(metadata, dict)
    assert "attempts" in metadata

    # related non-empty (shares "Acme Beads" with mid2)
    related = data["related"]
    assert len(related) > 0

    # related excludes self
    related_ids = [r["message_id"] for r in related]
    assert mid1 not in related_ids
    assert mid2 in related_ids

    # GET a bogus id → 404
    resp = client.get("/api/nuggets/nonexistent-id")
    assert resp.status_code == 404


# ── T21: GET /api/nuggets/search?q= returns FTS5 results ─────────────


def test_T21_search(dashboard_db):
    """T21: GET /api/nuggets/search?q= returns FTS5-ranked results.

    Reuses the T14 seed pattern: enrich Nuggets with known summaries/entities.
    Verifies keyword search, entity search, empty query, and malformed query.
    """
    client, db_path = dashboard_db

    # Insert + enrich Nuggets (same pattern as test_T14)
    mid1 = insert_captured_nugget(
        db_path, text="need to reorder lobster clasps by Friday"
    )
    mid2 = insert_captured_nugget(
        db_path, text="Acme Beads has the best sterling chain"
    )
    mid3 = insert_captured_nugget(
        db_path, text="meeting with supplier at 3pm"
    )

    nugget_enrich(
        mid1,
        classification="task",
        entities=["lobster clasps"],
        summary="Reorder lobster clasps by Friday",
        status="enriched",
        confidence=0.9,
        db_path=db_path,
    )
    nugget_enrich(
        mid2,
        classification="fact",
        entities=["Acme Beads", "sterling chain"],
        summary="Acme Beads carries sterling chain",
        status="enriched",
        confidence=0.85,
        db_path=db_path,
    )

    # Search by keyword "sterling"
    resp = client.get("/api/nuggets/search?q=sterling")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert any("sterling" in r["raw_content"].lower() or
               "sterling" in (r.get("summary") or "").lower()
               for r in data["results"])

    # Search by entity "Acme Beads"
    resp = client.get("/api/nuggets/search?q=Acme%20Beads")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1

    # Empty query → count: 0
    resp = client.get("/api/nuggets/search?q=")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["results"] == []

    # Malformed FTS query → count: 0, never 500
    resp = client.get("/api/nuggets/search?q=%22unbalanced")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0

    # Verify result shape: has query, count, results
    resp = client.get("/api/nuggets/search?q=lobster")
    data = resp.json()
    assert "query" in data
    assert "count" in data
    assert "results" in data
    assert isinstance(data["results"], list)


# ── T22: Server binds loopback only ──────────────────────────────────


def test_T22_loopback_bind(dashboard_db):
    """T22: Server binds 127.0.0.1 only (no external listen).

    Starts Uvicorn on an ephemeral port in a subprocess via
    trove.dashboard.server.run(host="127.0.0.1", ...), then asserts a
    connection to 127.0.0.1 succeeds.
    """
    client, db_path = dashboard_db

    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    # Start server in subprocess
    proc = subprocess.Popen(
        [
            "uv", "run", "python", "-c",
            f"from trove.dashboard.server import run; run(host='127.0.0.1', port={free_port}, db_path='{db_path}', log_level='warning')",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait for server to start
        time.sleep(2)

        # Verify connection to 127.0.0.1 succeeds
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            result = s.connect_ex(("127.0.0.1", free_port))
            assert result == 0, f"Could not connect to 127.0.0.1:{free_port}"

        # Verify it's actually serving our API
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{free_port}/api/nuggets",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
            assert "items" in data

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ── T45: author_label resolution on the API ──────────────────────────


def _insert_author_row(db_path, message_id, author):
    """Insert a captured row with an explicit raw-ID author."""
    conn = sqlite3.connect(str(db_path))
    now = time.time()
    conn.execute(
        """INSERT INTO nuggets
           (message_id, created_at, updated_at, author, source, raw_content, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (message_id, now, now, author, "signal", "dashboard author row", "captured"),
    )
    conn.commit()
    conn.close()


class TestT45_AuthorLabel:
    """T45 — API endpoints attach author_label resolved via TROVE_PEOPLE
    (the post-v4 author column stores raw IDs; the dashboard must not
    show bare UUIDs/phones)."""

    DASH_UUID = "00000000-0000-4000-8000-000000000077"

    def test_list_resolves_author_label(self, dashboard_db, monkeypatch):
        """GET /api/nuggets items carry author_label from a UUID key."""
        client, db_path = dashboard_db
        monkeypatch.setenv(
            "TROVE_PEOPLE",
            "+13100000000:Jami," + self.DASH_UUID + ":Jami",
        )
        _insert_author_row(db_path, "dash-a1", self.DASH_UUID)

        resp = client.get("/api/nuggets")
        assert resp.status_code == 200
        items = {i["message_id"]: i for i in resp.json()["items"]}
        item = items["dash-a1"]
        # Raw ID still present (identity), label resolved (display).
        assert item["author"] == self.DASH_UUID
        assert item["author_label"] == "Jami"

    def test_list_unresolved_author_label_none(self, dashboard_db, monkeypatch):
        """Unknown author / unset TROVE_PEOPLE → author_label is None
        (never a crash, never a fabricated name)."""
        client, db_path = dashboard_db
        monkeypatch.delenv("TROVE_PEOPLE", raising=False)
        _insert_author_row(db_path, "dash-a2", self.DASH_UUID)

        resp = client.get("/api/nuggets")
        assert resp.status_code == 200
        item = {i["message_id"]: i for i in resp.json()["items"]}["dash-a2"]
        assert item["author_label"] is None

    def test_detail_includes_author_label(self, dashboard_db, monkeypatch):
        """GET /api/nuggets/{id} includes author_label on the row and
        on related Nuggets."""
        client, db_path = dashboard_db
        monkeypatch.setenv("TROVE_PEOPLE", self.DASH_UUID + ":Jami")
        _insert_author_row(db_path, "dash-d1", self.DASH_UUID)
        _insert_author_row(db_path, "dash-d2", self.DASH_UUID)

        from trove.tools import nugget_enrich

        nugget_enrich(
            "dash-d1",
            classification="task",
            entities=["lobster clasps"],
            summary="clasp task",
            status="enriched",
            db_path=db_path,
        )
        nugget_enrich(
            "dash-d2",
            classification="fact",
            entities=["lobster clasps"],
            summary="clasp fact",
            status="enriched",
            db_path=db_path,
        )

        resp = client.get("/api/nuggets/dash-d1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["author_label"] == "Jami"
        related_ids = [r["message_id"] for r in data["related"]]
        assert "dash-d2" in related_ids
        for related in data["related"]:
            assert related["author_label"] == "Jami"

    def test_search_results_include_author_label(self, dashboard_db, monkeypatch):
        """GET /api/nuggets/search results carry author_label."""
        client, db_path = dashboard_db
        monkeypatch.setenv("TROVE_PEOPLE", self.DASH_UUID + ":Jami")
        _insert_author_row(db_path, "dash-s1", self.DASH_UUID)

        resp = client.get("/api/nuggets/search?q=dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        for r in data["results"]:
            assert r["author"] == self.DASH_UUID
            assert r["author_label"] == "Jami"
