"""Unit tests for the nugget_tasks tool.
"""

import json
import os
import time
from datetime import datetime, timedelta

import pytest

from trove.schema import init_db
from trove.tools import nugget_enrich, nugget_tasks, _parse_people_env
from tests.fixtures import insert_captured_nugget


# ── Helper: insert a task nugget with due_at and assignee ────────────

# Test-world identities (clearly-fake UUIDs): the partner-world shape,
# where capture stores the raw UUID sender ID in `author` and
# TROVE_PEOPLE resolves it to a display name.
ALEX_UUID = "00000000-0000-4000-8000-0000000000aa"
SARAH_UUID = "00000000-0000-4000-8000-0000000000bb"


def _insert_task(db_path, author=ALEX_UUID, due_at=None, assignee=None,
                 status="enriched", classification="task", text="task item"):
    """Insert a task Nugget, enriched with optional due_at and assignee."""
    mid = insert_captured_nugget(db_path, text=text)
    # Override the author
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE nuggets SET author = ? WHERE message_id = ?",
                 (author, mid))
    conn.commit()
    conn.close()
    # Enrich it
    enrich_kwargs = {
        "classification": classification,
        "status": status,
        "summary": text,
        "confidence": 0.9,
    }
    if due_at is not None:
        enrich_kwargs["due_at"] = due_at
    if assignee is not None:
        enrich_kwargs["assignee"] = assignee
    nugget_enrich(mid, **enrich_kwargs, db_path=db_path)
    return mid


# ── T40: enumerate open tasks ────────────────────────────────────────


class TestT40_EnumerateOpenTasks:
    """T40 — nugget_tasks enumerates open tasks ordered by due_at ASC NULLS LAST;
    resolved/non-task excluded; [] when none; never raises on a bad horizon."""

    def test_empty_db_returns_list(self, search_db):
        """Empty DB returns []."""
        results = nugget_tasks(db_path=search_db)
        assert results == []

    def test_only_open_tasks_returned(self, search_db):
        """Only open tasks (classification='task', status != 'resolved') are returned."""
        now = datetime.now()
        future = (now + timedelta(days=5)).timestamp()

        # Open task with due date
        open_mid = _insert_task(search_db, due_at=future, text="open task")
        # Resolved task (should be excluded)
        resolved_mid = _insert_task(search_db, due_at=future, status="resolved",
                                    text="resolved task")
        # Non-task classification (should be excluded)
        fact_mid = _insert_task(search_db, classification="fact", text="a fact")
        # Open task without due date
        open_no_date = _insert_task(search_db, due_at=None, text="open no date")

        results = nugget_tasks(db_path=search_db)

        ids = [r["message_id"] for r in results]
        assert open_mid in ids
        assert open_no_date in ids
        assert resolved_mid not in ids
        assert fact_mid not in ids

    def test_ordered_by_due_at_asc_nulls_last(self, search_db):
        """Tasks ordered by due_at ASC NULLS LAST."""
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).timestamp()
        next_week = (now + timedelta(days=7)).timestamp()

        mid_far = _insert_task(search_db, due_at=next_week, text="far")
        mid_near = _insert_task(search_db, due_at=tomorrow, text="near")
        mid_none = _insert_task(search_db, due_at=None, text="no date")

        results = nugget_tasks(db_path=search_db)

        # Should be: near (tomorrow), far (next week), no date (NULL last)
        assert len(results) == 3
        assert results[0]["message_id"] == mid_near
        assert results[1]["message_id"] == mid_far
        assert results[2]["message_id"] == mid_none

    def test_bad_horizon_no_raise(self, search_db):
        """Unknown horizon falls back to 'all' — never raises."""
        mid = _insert_task(search_db, text="test")
        results = nugget_tasks(horizon="bogus", db_path=search_db)
        assert len(results) == 1
        assert results[0]["message_id"] == mid

    def test_has_author_label_and_assignee_display(self, search_db):
        """Each result has author_label and assignee_display keys."""
        mid = _insert_task(search_db, text="test")
        results = nugget_tasks(db_path=search_db)
        assert len(results) == 1
        assert "author_label" in results[0]
        assert "assignee_display" in results[0]


# ── T41: horizon and assignee filters ────────────────────────────────


class TestT41_HorizonAndAssigneeFilters:
    """T41 — horizon (overdue/today/week/month/unscheduled/all) and
    assignee filters applied correctly at the boundaries."""

    def _setup_tasks(self, db_path):
        """Insert tasks at known time offsets. Returns dict of mid -> label."""
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Overdue: yesterday
        overdue_ts = (today_start - timedelta(days=1)).timestamp()
        # Today: noon today
        today_ts = today_start.timestamp() + 43200  # +12h
        # Week: +3 days
        week_ts = (today_start + timedelta(days=3)).timestamp()
        # Month: +20 days
        month_ts = (today_start + timedelta(days=20)).timestamp()
        # Unscheduled: no due date
        # All: covered by above

        mids = {}
        mids["overdue"] = _insert_task(db_path, due_at=overdue_ts, text="overdue")
        mids["today"] = _insert_task(db_path, due_at=today_ts, text="today")
        mids["week"] = _insert_task(db_path, due_at=week_ts, text="week")
        mids["month"] = _insert_task(db_path, due_at=month_ts, text="month")
        mids["unscheduled"] = _insert_task(db_path, due_at=None, text="unscheduled")

        return mids

    def test_overdue_horizon(self, search_db):
        """overdue returns only past-due tasks."""
        mids = self._setup_tasks(search_db)
        results = nugget_tasks(horizon="overdue", db_path=search_db)
        ids = [r["message_id"] for r in results]
        assert mids["overdue"] in ids
        assert mids["today"] not in ids
        assert mids["week"] not in ids
        assert mids["month"] not in ids
        assert mids["unscheduled"] not in ids

    def test_today_horizon(self, search_db):
        """today returns only tasks due today (excludes overdue)."""
        mids = self._setup_tasks(search_db)
        results = nugget_tasks(horizon="today", db_path=search_db)
        ids = [r["message_id"] for r in results]
        assert mids["today"] in ids
        assert mids["overdue"] not in ids
        assert mids["week"] not in ids

    def test_week_horizon(self, search_db):
        """week returns tasks due today + next 7 days (excludes overdue + far month)."""
        mids = self._setup_tasks(search_db)
        results = nugget_tasks(horizon="week", db_path=search_db)
        ids = [r["message_id"] for r in results]
        assert mids["today"] in ids
        assert mids["week"] in ids
        assert mids["overdue"] not in ids
        assert mids["month"] not in ids  # +20 days is beyond 7-day window
        assert mids["unscheduled"] not in ids

    def test_month_horizon(self, search_db):
        """month returns tasks due today + next 30 days."""
        mids = self._setup_tasks(search_db)
        results = nugget_tasks(horizon="month", db_path=search_db)
        ids = [r["message_id"] for r in results]
        assert mids["today"] in ids
        assert mids["week"] in ids
        assert mids["month"] in ids
        assert mids["overdue"] not in ids
        assert mids["unscheduled"] not in ids

    def test_unscheduled_horizon(self, search_db):
        """unscheduled returns only tasks with NULL due_at."""
        mids = self._setup_tasks(search_db)
        results = nugget_tasks(horizon="unscheduled", db_path=search_db)
        ids = [r["message_id"] for r in results]
        assert mids["unscheduled"] in ids
        assert mids["today"] not in ids
        assert mids["overdue"] not in ids

    def test_all_horizon(self, search_db):
        """all returns all open tasks regardless of due_at."""
        mids = self._setup_tasks(search_db)
        results = nugget_tasks(horizon="all", db_path=search_db)
        ids = [r["message_id"] for r in results]
        assert len(ids) == 5  # all 5 tasks
        for mid in mids.values():
            assert mid in ids

    def test_assignee_filter(self, search_db):
        """assignee= filters to exact match."""
        mid_alex = _insert_task(search_db, due_at="2026-09-01", assignee="Alex",
                                  text="alex's task")
        mid_sarah = _insert_task(search_db, due_at="2026-09-01", assignee="Sarah",
                                  text="sarah's task")
        mid_none = _insert_task(search_db, due_at="2026-09-01", assignee=None,
                                 text="unassigned")

        alex_results = nugget_tasks(assignee="Alex", db_path=search_db)
        assert len(alex_results) == 1
        assert alex_results[0]["message_id"] == mid_alex

        sarah_results = nugget_tasks(assignee="Sarah", db_path=search_db)
        assert len(sarah_results) == 1
        assert sarah_results[0]["message_id"] == mid_sarah

        # No filter returns all
        all_results = nugget_tasks(db_path=search_db)
        assert len(all_results) == 3


# ── T42: TROVE_PEOPLE resolution ─────────────────────────────────────


class TestT42_PeopleResolution:
    """T42 — nugget_tasks resolves author->name via TROVE_PEOPLE and
    computes assignee_display (assignee if set, else author_label, else None);
    unset -> solo flat (no labels)."""

    def test_people_resolution(self, search_db, monkeypatch):
        """With TROVE_PEOPLE set, author_label and assignee_display resolve correctly."""
        # Dual-format keys (the post-1b TROVE_PEOPLE shape): each person
        # under their phone AND their UUID. Authors are raw UUIDs.
        monkeypatch.setenv(
            "TROVE_PEOPLE",
            "+15550000001:Alex," + ALEX_UUID + ":Alex,"
            "+16660000002:Sarah," + SARAH_UUID + ":Sarah",
        )

        # Task by Alex, self-assigned (assignee=None)
        mid_self = _insert_task(search_db, author=ALEX_UUID, due_at="2026-09-01",
                                 assignee=None, text="alex self")
        # Task by Alex, assigned to Sarah
        mid_assigned = _insert_task(search_db, author=ALEX_UUID, due_at="2026-09-01",
                                     assignee="Sarah", text="alex to sarah")
        # Task by unknown author (no TROVE_PEOPLE mapping)
        mid_unknown = _insert_task(search_db, author="+9999999999", due_at="2026-09-01",
                                    assignee=None, text="unknown author")

        results = nugget_tasks(db_path=search_db)
        by_id = {r["message_id"]: r for r in results}

        # Self-assigned Alex task: author_label=Alex, assignee_display=Alex
        assert by_id[mid_self]["author_label"] == "Alex"
        assert by_id[mid_self]["assignee_display"] == "Alex"

        # Assigned to Sarah: author_label=Alex, assignee_display=Sarah
        assert by_id[mid_assigned]["author_label"] == "Alex"
        assert by_id[mid_assigned]["assignee_display"] == "Sarah"

        # Unknown author: author_label=None, assignee_display=None
        assert by_id[mid_unknown]["author_label"] is None
        assert by_id[mid_unknown]["assignee_display"] is None

    def test_unset_people_solo_flat(self, search_db, monkeypatch):
        """Without TROVE_PEOPLE, author_label and assignee_display are None for self tasks."""
        monkeypatch.delenv("TROVE_PEOPLE", raising=False)

        mid = _insert_task(search_db, author=ALEX_UUID, due_at="2026-09-01",
                            assignee=None, text="solo task")

        results = nugget_tasks(db_path=search_db)
        assert len(results) == 1
        assert results[0]["author_label"] is None
        assert results[0]["assignee_display"] is None

    def test_parse_people_env(self, monkeypatch):
        """_parse_people_env parses TROVE_PEOPLE correctly."""
        monkeypatch.setenv("TROVE_PEOPLE", "+1555:Alex, +1666:Sarah , +bad, :no-id")
        people = _parse_people_env()
        assert people == {"+1555": "Alex", "+1666": "Sarah"}
        # Malformed pairs are skipped silently

        # Dual-format: the same person under phone AND UUID keys (the
        # post-1b shape) — a flat dict, no code change needed.
        monkeypatch.setenv(
            "TROVE_PEOPLE",
            "+1555:Alex," + ALEX_UUID + ":Alex," + SARAH_UUID + ":Sarah",
        )
        people = _parse_people_env()
        assert people == {
            "+1555": "Alex",
            ALEX_UUID: "Alex",
            SARAH_UUID: "Sarah",
        }

        monkeypatch.setenv("TROVE_PEOPLE", "")
        assert _parse_people_env() == {}

        monkeypatch.delenv("TROVE_PEOPLE", raising=False)
        assert _parse_people_env() == {}
