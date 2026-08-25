"""Dashboard server for Trove.

FastAPI application serving the Nugget browser on loopback port 9120.
Standalone (not bolted onto `hermes dashboard`).

Read-only API over `trove.db` — never writes, never touches `state.db`.
"""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from trove import db as trove_db

# ── App factory ─────────────────────────────────────────────────────


def _attach_author_label(items: list) -> None:
    """Add `author_label` (resolved via TROVE_PEOPLE) to row dicts in-place.

    Mirrors the name resolution in `trove.tools.nugget_tasks`: the
    `author` column stores the raw sender ID (phone for note-to-self,
    UUID for partner DMs), and TROVE_PEOPLE maps every reachable ID
    shape to a display name. Without this, the dashboard would show
    bare UUIDs/phones after the v4 author migration.
    """
    from trove.people import parse_people_env

    people = parse_people_env()
    for item in items:
        author = item.get("author")
        item["author_label"] = people.get(author) if author else None


def create_app(db_path=None):
    """Create and configure the FastAPI application.

    Resolves the DB path: uses the provided `db_path` or falls back to
    `trove.db.get_trove_db_path()`. Stores it as app state so handlers
    are testable with a temp DB (no `~/.hermes` access).

    Optionally mounts `trove/dashboard/static` at `/` for the `index.html` frontend;
    guarded with try/except so a missing static dir never breaks the API.

    Does NOT call `init_db` — the schema must already exist; tests
    init their own temp DBs.

    Args:
        db_path: Optional path to trove.db. Falls back to get_trove_db_path().

    Returns:
        Configured FastAPI instance.
    """
    if db_path is None:
        db_path = trove_db.get_trove_db_path()

    app = FastAPI(
        title="Trove Nugget Browser",
        description="Read-only API over trove.db for the Nugget browser dashboard.",
        version="0.1.0",
    )

    # Store resolved DB path in app state
    app.state.db_path = db_path

    # ── Dependency: get_db_path ─────────────────────────────────────

    def get_db_path() -> Path:
        """Return the configured DB path from app state."""
        return Path(app.state.db_path)

    # ── GET /api/nuggets ────────────────────────────────────────────

    @app.get("/api/nuggets")
    def list_nuggets(
        classification: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        date: Optional[str] = None,
        sort: str = "newest",
        limit: int = 50,
        offset: int = 0,
        _db_path: Path = Depends(get_db_path),
    ):
        """Return recent Nuggets, paginated, filtered, and sorted.

        Query params:
          classification: Filter by classification (task, fact, note, idea, question, decision).
          status: Filter by status (captured, enriched, failed, resolved).
          source: Filter by source (e.g. signal).
          date: Filter by date — 'today' (start of today) or 'this-week' (7 days ago).
          sort: 'newest' (default), 'oldest', or 'relevant' (falls back to newest).
          limit: Max results (1–200, default 50).
          offset: Skip count (≥ 0, default 0).

        Returns:
          {"count": int, "limit": int, "offset": int, "items": [dict…]}
        """
        # Clamp limit to 1–200, offset to ≥ 0
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        # Build WHERE clauses (all parameterized)
        conditions = []
        params = []

        if classification is not None:
            conditions.append("n.classification = ?")
            params.append(classification)
        if status is not None:
            conditions.append("n.status = ?")
            params.append(status)
        if source is not None:
            conditions.append("n.source = ?")
            params.append(source)
        if date is not None:
            now = time.time()
            if date == "today":
                # Start of today (local time)
                today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                cutoff = today_start.timestamp()
                conditions.append("n.created_at >= ?")
                params.append(cutoff)
            elif date == "this-week":
                cutoff = now - 7 * 86400
                conditions.append("n.created_at >= ?")
                params.append(cutoff)
            # Unknown date values are no-ops (defensive, single-user)

        # Build ORDER BY
        # "relevant" falls back to newest on the list endpoint
        # (no query term to rank by here; FTS5 rank lives on search endpoint)
        if sort == "oldest":
            order_by = "ORDER BY n.created_at ASC"
        else:
            # 'newest' or 'relevant' → newest
            order_by = "ORDER BY n.created_at DESC"

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        # Count query
        count_sql = f"SELECT COUNT(*) FROM nuggets n {where_clause}"
        # Data query
        sql = f"SELECT n.* FROM nuggets n {where_clause} {order_by} LIMIT ? OFFSET ?"
        data_params = params + [limit, offset]

        conn = trove_db.connect(_db_path)
        try:
            conn.row_factory = sqlite3.Row

            # Get total count
            count_row = conn.execute(count_sql, params).fetchone()
            total_count = count_row[0]

            # Get paginated items
            rows = conn.execute(sql, data_params).fetchall()
            items = [dict(row) for row in rows]
            _attach_author_label(items)

            return {
                "count": total_count,
                "limit": limit,
                "offset": offset,
                "items": items,
            }
        finally:
            conn.close()

    # ── GET /api/nuggets/search ────────────────────────────────────────
    # NOTE: Must be defined BEFORE /api/nuggets/{message_id} so that
    # the literal path "/search" is matched before the catch-all param.

    @app.get("/api/nuggets/search")
    def search_nuggets(
        q: str = "",
        classification: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
        _db_path: Path = Depends(get_db_path),
    ):
        """FTS5 search over Nuggets, reusing the nugget_search tool path.

        Reuses `trove.tools.nugget_search` so "the UI and the agent share
        one query path". Passes db_path explicitly so
        tests read the same temp DB.

        Returns:
            {"query": str, "count": int, "results": [dict…]}
        """
        from trove.tools import nugget_search as _nugget_search

        results = _nugget_search(
            query=q,
            classification=classification,
            status=status,
            source=source,
            limit=limit,
            db_path=_db_path,
        )

        _attach_author_label(results)

        return {
            "query": q,
            "count": len(results),
            "results": results,
        }

    # ── GET /api/nuggets/tasks ────────────────────────────────────
    # NOTE: Must be defined BEFORE /api/nuggets/{message_id} so that
    # the literal path "/tasks" is matched before the catch-all param.

    @app.get("/api/nuggets/tasks")
    def list_tasks(
        horizon: str = "all",
        assignee: Optional[str] = None,
        limit: int = 200,
        _db_path: Path = Depends(get_db_path),
    ):
        """Enumerate open tasks, reusing the `nugget_tasks` tool path.

        Mirrors the search endpoint: "the UI and the agent share one
        query path". Open = classification='task', status !=
        'resolved'; ordered by due_at ASC NULLS LAST; author/assignee
        names resolved via TROVE_PEOPLE (author_label, assignee_display).

        The existing list/search endpoints cannot compose this view:
        status is a single-value filter (open spans captured/enriched/
        failed), and there is no due-date ordering.

        Query params:
          horizon: 'overdue'|'today'|'week'|'month'|'unscheduled'|'all'.
          assignee: Exact assignee display-name filter (None = no filter).
          limit: Max results (clamped 1–500 by the tool, default 200).

        Returns:
          {"horizon": str, "assignee": Optional[str], "count": int,
           "items": [dict…]}
        """
        from trove.tools import nugget_tasks as _nugget_tasks

        items = _nugget_tasks(
            horizon=horizon,
            assignee=assignee,
            limit=limit,
            db_path=_db_path,
        )

        return {
            "horizon": horizon,
            "assignee": assignee,
            "count": len(items),
            "items": items,
        }

    # ── GET /api/nuggets/{message_id} ──────────────────────────────

    @app.get("/api/nuggets/{message_id}")
    def get_nugget(
        message_id: str,
        _db_path: Path = Depends(get_db_path),
    ):
        """Return full detail for a single Nugget including related Nuggets.

        Args:
            message_id: Trove-generated message_id.

        Returns:
            Dict with full row (incl. raw_content, metadata) plus
            "related": list of up to 5 Nuggets sharing any entity.
        """
        conn = trove_db.connect(_db_path)
        try:
            conn.row_factory = sqlite3.Row

            # Fetch the primary Nugget
            row = conn.execute(
                "SELECT * FROM nuggets WHERE message_id = ?",
                (message_id,),
            ).fetchone()

            if row is None:
                raise HTTPException(status_code=404, detail="Nugget not found")

            nugget = dict(row)

            # ── Related Nuggets (entity overlap, limit 5) ──────────
            related = []
            entities = None
            if nugget.get("entities"):
                try:
                    entities = json.loads(nugget["entities"])
                except (json.JSONDecodeError, TypeError):
                    entities = None

            if entities and isinstance(entities, list) and len(entities) > 0:
                # Use json_each to find Nuggets sharing any entity
                placeholders = ",".join(["?" for _ in entities])
                related_sql = (
                    f"SELECT DISTINCT n2.* FROM nuggets n2 "
                    f"JOIN json_each(n2.entities) je "
                    f"WHERE n2.message_id != ? "
                    f"AND je.value IN ({placeholders}) "
                    f"LIMIT 5"
                )
                related_params = [message_id] + entities
                try:
                    related_rows = conn.execute(related_sql, related_params).fetchall()
                    related = [dict(r) for r in related_rows]
                except sqlite3.OperationalError:
                    # Fallback: if json_each is awkward, skip related
                    related = []

            _attach_author_label(related)
            _attach_author_label([nugget])

            return {**nugget, "related": related}
        finally:
            conn.close()

    # ── Static files (optional frontend mount) ────────────
    # Mounted AFTER API routes so /api/* paths are not intercepted.
    try:
        from starlette.staticfiles import StaticFiles

        static_dir = Path(__file__).parent / "static"
        app.mount("/", StaticFiles(directory=str(static_dir), html=True, check_dir=False), name="static")
    except Exception:
        # Missing static dir or starlette not installed — no-op.
        # API-only setups need no static dir.
        pass

    return app


# ── Uvicorn launcher ────────────────────────────────────────


def run(host: str = "127.0.0.1", port: int = 9120, db_path=None, **uvicorn_kwargs):
    """Start the Trove dashboard server via Uvicorn.

    Default host is "127.0.0.1" (loopback only) — this is the only
    default; do not default to "0.0.0.0".

    This launcher is reused by the `trove dashboard` CLI runner.

    Args:
        host: Bind address (default "127.0.0.1").
        port: Bind port (default 9120).
        db_path: Optional path to trove.db.
        **uvicorn_kwargs: Extra kwargs forwarded to uvicorn.run().
    """
    import uvicorn

    uvicorn.run(
        create_app(db_path),
        host=host,
        port=port,
        **uvicorn_kwargs,
    )
