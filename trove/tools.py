"""Tool definitions for Trove.

This module holds:
  - `nugget_enrich` — the enrichment tool (AI classification/summary/etc.)
  - `nugget_search` — the retrieval/search tool (FTS5 keyword search)
  - `nugget_tasks` — the open-task enumerator
  - `nugget_task_update` — the atomic task-update/resolve tool

The `trove/` package must import cleanly with no Hermes installed.
Hermes-specific types are never imported.
"""

import json
import sqlite3
import time
from datetime import datetime
from typing import Optional, Union

from trove import db as trove_db
from trove.people import parse_people_env
from trove.schema import init_db

# ── Taxonomy constants ───────────────────────────────────────────────

CLASSIFICATIONS = ("task", "fact", "note", "idea", "question", "decision")
ENRICH_STATUSES = ("enriched", "failed", "resolved")
# Task lifecycle statuses a task_update may move a task into. `failed` is not
# a task lifecycle state, and `captured` is a pre-enrichment state owned by the
# capture hook — neither is valid here.
TASK_UPDATE_STATUSES = ("enriched", "resolved")

# Sentinel for "argument not provided — leave the field unchanged". Distinct
# from an explicit None, which means "clear the field to NULL". JSON callers
# cannot pass this sentinel; it exists so Python callers can update a single
# field without touching the others.
_UNSET = object()

# ── Tool schema (Hermes JSON-schema shape) ──────────────────────────

NUGGET_ENRICH_SCHEMA = {
    "name": "nugget_enrich",
    "description": "Stamp AI enrichment fields (classification, entities, summary, status, confidence, links) on a captured Nugget.",
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "Trove-generated message_id of the captured Nugget to enrich.",
            },
            "classification": {
                "type": "string",
                "enum": list(CLASSIFICATIONS),
                "description": "Classification of the Nugget: task, fact, note, idea, question, or decision.",
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Extracted entities (names, products, vendors, topics) as a JSON array of strings.",
            },
            "summary": {
                "type": "string",
                "description": "One-line AI summary of the Nugget content.",
            },
            "status": {
                "type": "string",
                "enum": ["enriched", "failed", "resolved"],
                "default": "enriched",
                "description": "New status for the Nugget: enriched, failed, or resolved.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence score (0.0–1.0) for the classification and summary.",
            },
            "links": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IDs of related Nuggets as a JSON array of strings.",
            },
            "due_at": {
                "type": ["string", "number", "null"],
                "description": "Due date for task Nuggets: ISO date string (YYYY-MM-DD), ISO datetime, or numeric epoch seconds. Null for open-ended tasks.",
            },
            "assignee": {
                "type": ["string", "null"],
                "description": "Assignee name for task Nuggets (e.g. 'Sam'). Null means self (the author).",
            },
        },
        "required": ["message_id"],
    },
}


# ── Helpers ──────────────────────────────────────────────────────────


def _tool_result(data: dict) -> str:
    """Serialize a result dict to a JSON string for the Hermes tool handler.

    Does NOT import Hermes' tools.registry.tool_result — trove/ must
    import cleanly in the isolated dev venv with no Hermes installed.

    Args:
        data: Dict to serialize.

    Returns:
        JSON string.
    """
    return json.dumps(data, ensure_ascii=False)


def _load_metadata(existing: str | None) -> dict:
    """Parse an existing metadata JSON string into a dict.

    Returns {} for None, empty, or malformed JSON (never raises).

    Args:
        existing: The current metadata column value (JSON string or None).

    Returns:
        Parsed metadata dict (empty dict on any failure).
    """
    if existing and existing.strip():
        try:
            return json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _fetch_full_row(conn, message_id: str, original_raw_content: str) -> dict:
    """Fetch the full nugget row as a dict and assert raw_content immutability.

    Args:
        conn: An open sqlite3.Connection.
        message_id: The message_id to fetch.
        original_raw_content: The raw_content before the write — used for
            the immutability guard.

    Returns:
        Dict of all nugget columns for the row.

    Raises:
        RuntimeError: If raw_content was modified during the write.
    """
    cursor = conn.execute(
        "SELECT * FROM nuggets WHERE message_id = ?",
        (message_id,),
    )
    columns = [desc[0] for desc in cursor.description]
    row = cursor.fetchone()
    result = dict(zip(columns, row))

    if result["raw_content"] != original_raw_content:
        raise RuntimeError(
            "raw_content was modified during write — this should never happen"
        )
    return result


def _normalize_due_at(due_at) -> Optional[float]:
    """Normalize a due_at value to epoch seconds (float) or None.

    Accepts:
      - None → None (no due date)
      - int/float → float() (epoch seconds)
      - ISO 8601 date/datetime string → midnight local → .timestamp()

    Raises:
        ValueError: If the string is not a valid ISO 8601 date/datetime.
    """
    if due_at is None:
        return None
    if isinstance(due_at, (int, float)):
        return float(due_at)
    if isinstance(due_at, str):
        try:
            dt = datetime.fromisoformat(due_at)
            return dt.timestamp()
        except (ValueError, TypeError):
            raise ValueError(
                f"due_at must be an ISO 8601 date/datetime string, "
                f"numeric epoch seconds, or None. Got: {due_at!r}"
            )
    raise ValueError(
        f"due_at must be an ISO 8601 date/datetime string, "
        f"numeric epoch seconds, or None. Got: {type(due_at).__name__}"
    )


# ── nugget_enrich tool ───────────────────────────────────────────


def nugget_enrich(
    message_id: str,
    classification: Optional[str] = None,
    entities: Optional[list] = None,
    summary: Optional[str] = None,
    status: str = "enriched",
    confidence: Optional[float] = None,
    links: Optional[list] = None,
    due_at: Optional[Union[str, int, float]] = None,
    assignee: Optional[str] = None,
    *,
    model: Optional[str] = None,
    db_path=None,
) -> dict:
    """Stamp AI enrichment fields on a captured Nugget row.

    Validates inputs, loads the existing row, updates only AI fields,
    bumps `updated_at`, and records provenance in `metadata`.
    Idempotent on `message_id` — re-enrich overwrites AI fields, never
    touches `raw_content`.

    Args:
        message_id: Trove-generated id of the captured Nugget.
        classification: task|fact|note|idea|question|decision.
        entities: List of extracted entity strings.
        summary: One-line AI summary.
        status: 'enriched' | 'failed' | 'resolved'.
        confidence: Float 0..1 (nullable).
        links: List of related Nugget IDs.
        due_at: Task due date — ISO date/datetime string, numeric epoch seconds, or None.
        assignee: Display name of the person assigned, or None for self (the author).
        model: Model name for provenance (optional).
        db_path: Override DB path (for testing).

    Returns:
        Dict with the updated row's columns.

    Raises:
        ValueError: If classification, status, or confidence is invalid.
        KeyError: If message_id does not exist (capture must have run first).
    """
    # ── Resolve DB path ────────────────────────────────────────────
    if db_path is None:
        db_path = trove_db.get_trove_db_path()

    # ── Validate (before any DB access) ────────────────────────────
    if classification is not None and classification not in CLASSIFICATIONS:
        raise ValueError(
            f"Invalid classification '{classification}'. "
            f"Must be one of: {', '.join(CLASSIFICATIONS)}"
        )
    if status not in ENRICH_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. "
            f"Must be one of: {', '.join(ENRICH_STATUSES)}"
        )
    if confidence is not None and not (0.0 <= confidence <= 1.0):
        raise ValueError(
            f"Invalid confidence {confidence}. Must be a float in [0.0, 1.0]."
        )

    # ── Validate and normalize due_at ──────────────────────────────
    due_at_normalized = _normalize_due_at(due_at)

    # ── Validate assignee ──────────────────────────────────────────
    if assignee is not None:
        if not isinstance(assignee, str) or not assignee.strip():
            raise ValueError(
                "assignee must be a non-empty string or None (None = self/the author)."
            )
        assignee = assignee.strip()

    # ── Normalize entities/links to JSON strings ───────────────────
    entities_json = json.dumps(entities) if entities else None
    links_json = json.dumps(links) if links else None

    # ── Load existing row ──────────────────────────────────────────
    conn = trove_db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT message_id, metadata, raw_content FROM nuggets WHERE message_id = ?",
            (message_id,),
        ).fetchone()

        if row is None:
            raise KeyError(
                f"No Nugget found with message_id='{message_id}'. "
                "Capture must run before enrichment."
            )

        existing_metadata = row[1]
        original_raw_content = row[2]

        # ── Build metadata provenance ──────────────────────────────
        metadata = _load_metadata(existing_metadata)

        if model is not None:
            metadata["model"] = model
        metadata["attempts"] = metadata.get("attempts", 0) + 1
        metadata["last_enriched_at"] = time.time()
        metadata_json = json.dumps(metadata)

        # ── Write AI fields (never raw_content) ────────────────────
        now = time.time()
        with trove_db.write_transaction(conn):
            conn.execute(
                """UPDATE nuggets
                   SET classification = ?,
                       entities = ?,
                       summary = ?,
                       status = ?,
                       confidence = ?,
                       links = ?,
                       metadata = ?,
                       updated_at = ?,
                       due_at = ?,
                       assignee = ?
                   WHERE message_id = ?""",
                (
                    classification,
                    entities_json,
                    summary,
                    status,
                    confidence,
                    links_json,
                    metadata_json,
                    now,
                    due_at_normalized,
                    assignee,
                    message_id,
                ),
            )

        # ── Return updated row ─────────────────────────────────────
        result = _fetch_full_row(conn, message_id, original_raw_content)

        return result
    finally:
        conn.close()


# ── Hermes handler wrapper ───────────────────────────────────────────


def _handle_nugget_enrich(args: dict, **kw) -> str:
    """Hermes tool handler for nugget_enrich.

    Unpacks the agent's JSON args, calls nugget_enrich, and returns
    a JSON result string. Never raises — catches all exceptions and
    returns an error JSON so the agent turn can continue.

    Args:
        args: Dict from Hermes with tool arguments.
        **kw: Extra keyword args from Hermes (ignored).

    Returns:
        JSON string with the result or error information.
    """
    try:
        result = nugget_enrich(
            message_id=args["message_id"],
            classification=args.get("classification"),
            entities=args.get("entities"),
            summary=args.get("summary"),
            status=args.get("status", "enriched"),
            confidence=args.get("confidence"),
            links=args.get("links"),
            due_at=args.get("due_at"),
            assignee=args.get("assignee"),
        )
        return _tool_result(result)
    except Exception as e:
        return _tool_result({
            "error": str(e),
            "message_id": args.get("message_id"),
        })


# ── nugget_search tool ─────────────────────────────────

# ── NUGGET_SEARCH_SCHEMA (Hermes JSON-schema shape) ───────────────────

NUGGET_SEARCH_SCHEMA = {
    "name": "nugget_search",
    "description": (
        "FTS5 keyword search over Nuggets with optional "
        "classification/status/source filters; returns Nugget rows incl. raw_content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search string (FTS5 query language).",
            },
            "classification": {
                "type": "string",
                "description": "Filter by classification (task, fact, note, idea, question, decision).",
            },
            "status": {
                "type": "string",
                "description": "Filter by status (captured, enriched, failed, resolved).",
            },
            "source": {
                "type": "string",
                "description": "Filter by source (e.g. signal).",
            },
            "limit": {
                "type": "integer",
                "default": 50,
                "description": "Maximum number of results (default 50, max 200).",
            },
        },
        "required": ["query"],
    },
}


def nugget_search(
    query: str,
    classification: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 50,
    *,
    db_path=None,
) -> list:
    """FTS5 keyword search over Nuggets with optional field filters.

    Searches `nuggets_fts` (FTS5 over message_id, raw_content, summary, entities),
    joins back to `nuggets` for the full row (incl. raw_content), applies
    optional classification/status/source filters, orders by FTS5 BM25 rank,
    and returns up to `limit` results.

    Never raises — returns `[]` for empty query, no match, or malformed FTS input.
    Reads `trove.db` only — never touches `state.db`.

    Args:
        query: FTS5 search string.
        classification: Optional filter by classification.
        status: Optional filter by status.
        source: Optional filter by source.
        limit: Maximum results (clamped 1–200, default 50).
        db_path: Override DB path (for testing).

    Returns:
        List of dicts (full Nugget rows ordered by relevance).
    """
    # ── Resolve DB path ────────────────────────────────────────────
    if db_path is None:
        db_path = trove_db.get_trove_db_path()

    # ── Short-circuit empty/whitespace query ───────────────────────
    if query is None or not query.strip():
        return []

    # ── Clamp limit ────────────────────────────────────────────────
    limit = max(1, min(int(limit), 200))

    # ── Build parameterized SQL ────────────────────────────────────
    conditions = ["nuggets_fts MATCH ?"]
    params: list = [query]

    if classification is not None:
        conditions.append("n.classification = ?")
        params.append(classification)
    if status is not None:
        conditions.append("n.status = ?")
        params.append(status)
    if source is not None:
        conditions.append("n.source = ?")
        params.append(source)

    where_clause = " AND ".join(conditions)
    sql = (
        "SELECT n.* FROM nuggets_fts f "
        "JOIN nuggets n ON n.message_id = f.message_id "
        f"WHERE {where_clause} "
        "ORDER BY rank LIMIT ?"
    )
    params.append(limit)

    # ── Execute (never raises) ─────────────────────────────────────
    conn = trove_db.connect(db_path)
    try:
        cursor = conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except sqlite3.OperationalError:
        # Malformed FTS query (unbalanced quotes, bare operators)
        return []
    finally:
        conn.close()


# ── Hermes handler wrapper ───────────────────────────────────────────


def _handle_nugget_search(args: dict, **kw) -> str:
    """Hermes tool handler for nugget_search.

    Unpacks the agent's JSON args, calls nugget_search, and returns
    a JSON result string. Never raises — catches all exceptions and
    returns an error JSON so the agent turn can continue.

    Args:
        args: Dict from Hermes with tool arguments.
        **kw: Extra keyword args from Hermes (ignored).

    Returns:
        JSON string with the result or error information.
    """
    try:
        results = nugget_search(
            query=args["query"],
            classification=args.get("classification"),
            status=args.get("status"),
            source=args.get("source"),
            limit=args.get("limit", 50),
        )
        return _tool_result({
            "query": args["query"],
            "count": len(results),
            "results": results,
        })
    except Exception as e:
        return _tool_result({
            "error": str(e),
            "query": args.get("query"),
        })


# ── nugget_tasks tool ─────────────────────────────────


def _parse_people_env() -> dict:
    """Parse TROVE_PEOPLE env var into a sender_id -> name mapping.

    Kept as a thin alias of `trove.people.parse_people_env` (the single
    definition of the format — shared with the capture allowlist and the
    dashboard) so existing `from trove.tools import _parse_people_env`
    call sites keep working. See `trove.people.parse_people_env` for the
    full format documentation.
    """
    return parse_people_env()


NUGGET_TASKS_SCHEMA = {
    "name": "nugget_tasks",
    "description": (
        "Enumerate open tasks (classification='task', status != 'resolved') "
        "ordered by due date. Supports horizon and assignee filters. "
        "Resolves author/assignee names via TROVE_PEOPLE."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "horizon": {
                "type": "string",
                "enum": ["overdue", "today", "week", "month", "unscheduled", "all"],
                "default": "all",
                "description": (
                    "Time horizon filter: 'overdue' (past due), 'today' (due today), "
                    "'week' (today + next 7 days), 'month' (today + next 30 days), "
                    "'unscheduled' (no due date), 'all' (no filter)."
                ),
            },
            "assignee": {
                "type": ["string", "null"],
                "description": (
                    "Filter by assignee display name (exact match). "
                    "None means no filter."
                ),
            },
            "limit": {
                "type": "integer",
                "default": 200,
                "description": "Maximum results (default 200, range 1-500).",
            },
        },
        "required": [],
    },
}


def nugget_tasks(
    horizon: str = "all",
    assignee: Optional[str] = None,
    limit: int = 200,
    *,
    db_path=None,
) -> list:
    """Enumerate open tasks ordered by due date.

    Reads open tasks (classification='task', status != 'resolved') from trove.db,
    applies horizon and assignee filters, orders by due_at ASC NULLS LAST,
    and resolves author/assignee names via TROVE_PEOPLE.

    Never raises — returns [] on any error.

    Args:
        horizon: Time horizon filter (overdue/today/week/month/unscheduled/all).
        assignee: Filter by assignee display name (exact match, None = no filter).
        limit: Maximum results (clamped 1-500, default 200).
        db_path: Override DB path (for testing).

    Returns:
        List of dicts with Nugget columns plus author_label and assignee_display.
    """
    if db_path is None:
        db_path = trove_db.get_trove_db_path()

    try:
        from datetime import timedelta

        # Clamp limit
        limit = max(1, min(int(limit), 500))

        # Compute local midnight boundaries
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_ts = today_start.timestamp()
        tomorrow_start = today_start + timedelta(days=1)
        tomorrow_ts = tomorrow_start.timestamp()
        week_end_ts = (today_start + timedelta(days=7)).timestamp()
        month_end_ts = (today_start + timedelta(days=30)).timestamp()

        # Build query
        conditions = [
            "n.classification = 'task'",
            "n.status != 'resolved'",
        ]
        params: list = []

        # Horizon filter
        valid_horizons = {"overdue", "today", "week", "month", "unscheduled", "all"}
        if horizon not in valid_horizons:
            horizon = "all"  # defensive: unknown horizon -> all

        if horizon == "overdue":
            conditions.append("n.due_at IS NOT NULL AND n.due_at < ?")
            params.append(today_ts)
        elif horizon == "today":
            conditions.append("n.due_at >= ? AND n.due_at < ?")
            params.extend([today_ts, tomorrow_ts])
        elif horizon == "week":
            conditions.append("n.due_at >= ? AND n.due_at < ?")
            params.extend([today_ts, week_end_ts])
        elif horizon == "month":
            conditions.append("n.due_at >= ? AND n.due_at < ?")
            params.extend([today_ts, month_end_ts])
        elif horizon == "unscheduled":
            conditions.append("n.due_at IS NULL")

        # Assignee filter
        if assignee is not None:
            conditions.append("n.assignee = ?")
            params.append(assignee)

        where_clause = " AND ".join(conditions)
        sql = (
            f"SELECT n.* FROM nuggets n "
            f"WHERE {where_clause} "
            f"ORDER BY (n.due_at IS NULL), n.due_at ASC "
            f"LIMIT ?"
        )
        params.append(limit)

        # Execute
        conn = trove_db.connect(db_path)
        try:
            cursor = conn.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()

            # Resolve people names
            people = _parse_people_env()

            results = []
            for row in rows:
                row_dict = dict(zip(columns, row))
                author_id = row_dict.get("author")
                author_label = people.get(author_id) if author_id else None
                assignee_display = (
                    row_dict.get("assignee")
                    if row_dict.get("assignee")
                    else author_label
                )
                row_dict["author_label"] = author_label
                row_dict["assignee_display"] = assignee_display
                results.append(row_dict)

            return results
        finally:
            conn.close()

    except (sqlite3.OperationalError, sqlite3.Error):
        return []


def _handle_nugget_tasks(args: dict, **kw) -> str:
    """Hermes tool handler for nugget_tasks.

    Unpacks the agent's JSON args, calls nugget_tasks, and returns
    a JSON result string. Never raises — catches all exceptions and
    returns an error JSON so the agent turn can continue.

    Args:
        args: Dict from Hermes with tool arguments.
        **kw: Extra keyword args from Hermes (ignored).

    Returns:
        JSON string with {"count", "results"} or error information.
    """
    try:
        results = nugget_tasks(
            horizon=args.get("horizon", "all"),
            assignee=args.get("assignee"),
            limit=args.get("limit", 200),
        )
        return _tool_result({
            "count": len(results),
            "results": results,
        })
    except Exception as e:
        return _tool_result({
            "error": str(e),
            "count": 0,
            "results": [],
        })


# ── nugget_task_update tool ────────────────────────────────────
#
# A dedicated, atomic operation for updating an EXISTING task from a
# completion / scheduling / assignment follow-up message. This is the
# durable fix for the task-completion-association incident: `nugget_enrich`
# updates only the message_id it is handed and cannot infer a parent task, so
# the agent was resolving the newly captured follow-up message instead of the
# original task. `nugget_task_update` takes the ORIGINAL task's id, validates
# it is actually a task, updates only task metadata, and optionally records a
# bidirectional link to the follow-up/completion message — all in one
# transaction. See deploy/shared/skills/trove-task-association/SKILL.md.

NUGGET_TASK_UPDATE_SCHEMA = {
    "name": "nugget_task_update",
    "description": (
        "Atomically update an EXISTING task's status, due date, and/or "
        "assignee by its original message_id, optionally recording a link to "
        "the follow-up/completion message that prompted the update. Use this "
        "instead of calling nugget_enrich on a newly captured completion "
        "message — the task identity is the ORIGINAL task's message_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "message_id of the EXISTING task to update. Must already "
                    "be classification='task'. This is the original task's "
                    "id, not the follow-up message's id."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["enriched", "resolved"],
                "description": (
                    "New task status: 'resolved' to complete the task, "
                    "'enriched' to reopen/keep it open. Omit to leave "
                    "unchanged."
                ),
            },
            "due_at": {
                "type": ["string", "number", "null"],
                "description": (
                    "New due date: ISO date string (YYYY-MM-DD), ISO "
                    "datetime, or numeric epoch seconds. Pass null to clear "
                    "the due date (open-ended). Omit to leave unchanged."
                ),
            },
            "assignee": {
                "type": ["string", "null"],
                "description": (
                    "New assignee display name, or null to clear (self/the "
                    "author). Omit to leave unchanged."
                ),
            },
            "completion_message_id": {
                "type": "string",
                "description": (
                    "Optional message_id of the follow-up/completion message "
                    "that prompted this update. Records a bidirectional link "
                    "between the task and the follow-up. Does NOT reclassify the "
                    "follow-up — classify it separately with nugget_enrich."
                ),
            },
        },
        "required": ["task_id"],
    },
}


def _append_link(existing_links: Optional[str], link_id: str) -> str:
    """Return a JSON links array string with link_id appended (dedup).

    Preserves any existing links. Never raises — a malformed existing
    links value is replaced with a clean single-element list.

    Args:
        existing_links: Existing JSON-encoded list string (or None/empty).
        link_id: message_id to append.

    Returns:
        JSON-encoded list string.
    """
    links = []
    if existing_links and existing_links.strip():
        try:
            parsed = json.loads(existing_links)
            if isinstance(parsed, list):
                links = [str(x) for x in parsed if isinstance(x, (str, int))]
        except (json.JSONDecodeError, TypeError):
            links = []
    if link_id not in links:
        links.append(link_id)
    return json.dumps(links)


def nugget_task_update(
    task_id: str,
    status: Union[str, object] = _UNSET,
    due_at: Union[str, int, float, None, object] = _UNSET,
    assignee: Union[str, None, object] = _UNSET,
    completion_message_id: Optional[str] = None,
    *,
    model: Optional[str] = None,
    db_path=None,
) -> dict:
    """Atomically update an existing task's metadata by its original id.

    Validates that ``task_id`` exists and is classification='task', then
    updates only the task-metadata fields (status, due_at, assignee) the
    caller supplied, bumps ``updated_at``, and records provenance in
    ``metadata``. Never touches ``raw_content`` (the DB trigger enforces
    immutability anyway) and never reclassifies the row.

    If ``completion_message_id`` is given, it must exist, and a bidirectional
    ``links`` entry is recorded between the task and the follow-up message in
    the same transaction. The follow-up is NOT reclassified — that is the
    agent's job via ``nugget_enrich``.

    Sentinel semantics: ``_UNSET`` (the default) means "leave the field
    unchanged"; an explicit ``None`` for ``due_at`` or ``assignee`` means
    "clear to NULL". JSON callers express "leave unchanged" by omitting the
    field (Hermes omits unset keys, which arrive as None; see the handler for
    the None-vs-omitted disambiguation).

    Args:
        task_id: message_id of the existing task to update.
        status: 'enriched' | 'resolved', or _UNSET to leave unchanged.
        due_at: ISO date/datetime string, numeric epoch seconds, None to clear,
            or _UNSET to leave unchanged.
        assignee: display name, None to clear, or _UNSET to leave unchanged.
        completion_message_id: Optional follow-up message_id to link.
        model: Model name for provenance (optional).
        db_path: Override DB path (for testing).

    Returns:
        Dict with the updated task row's columns plus
        ``completion_message_linked`` (bool).

    Raises:
        ValueError: If status is not a valid task-update status, or if
n            due_at/assignee is malformed.
        KeyError: If task_id (or completion_message_id) does not exist.
        TypeError: If task_id is not classification='task'.
    """
    # ── Resolve DB path ────────────────────────────────────────────
    if db_path is None:
        db_path = trove_db.get_trove_db_path()

    # ── Validate status (before any DB access) ─────────────────────
    if status is not _UNSET:
        if not isinstance(status, str) or status not in TASK_UPDATE_STATUSES:
            raise ValueError(
                f"Invalid status {status!r}. "
                f"Must be one of: {', '.join(TASK_UPDATE_STATUSES)} "
                f"(a task_update moves a task to 'enriched' or 'resolved')."
            )

    # ── Normalize due_at (raises ValueError on bad strings) ────────
    due_at_normalized: Union[float, None, object]
    if due_at is _UNSET:
        due_at_normalized = _UNSET
    else:
        due_at_normalized = _normalize_due_at(due_at)

    # ── Validate assignee ─────────────────────────────────────────
    if assignee is not _UNSET and assignee is not None:
        if not isinstance(assignee, str) or not assignee.strip():
            raise ValueError(
                "assignee must be a non-empty string or None (None = clear "
                "to self/the author)."
            )
        assignee = assignee.strip()

    # ── Load and validate the task row ─────────────────────────────
    conn = trove_db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT message_id, classification, status, due_at, assignee, "
            "links, metadata, raw_content FROM nuggets WHERE message_id = ?",
            (task_id,),
        ).fetchone()

        if row is None:
            raise KeyError(
                f"No Nugget found with task_id='{task_id}'. "
                "The task must already be captured."
            )

        (existing_mid, existing_class, existing_status, existing_due,
         existing_assignee, existing_links, existing_metadata,
         original_raw_content) = row

        if existing_class != "task":
            raise TypeError(
                f"task_id='{task_id}' is classification='{existing_class}', "
                f"not 'task'. nugget_task_update only updates task records. "
                "This is the original task's id, not a follow-up message's id."
            )

        # ── Validate completion_message_id if given ────────────────
        completion_exists = False
        if completion_message_id is not None:
            if not isinstance(completion_message_id, str) \
                    or not completion_message_id.strip():
                raise ValueError(
                    "completion_message_id must be a non-empty string."
                )
            completion_message_id = completion_message_id.strip()
            comp_row = conn.execute(
                "SELECT message_id, links FROM nuggets WHERE message_id = ?",
                (completion_message_id,),
            ).fetchone()
            if comp_row is None:
                raise KeyError(
                    f"completion_message_id='{completion_message_id}' does "
                    "not exist. It must already be captured."
                )
            completion_exists = True
            comp_existing_links = comp_row[1]

        # ── Build metadata provenance ──────────────────────────────
        metadata = _load_metadata(existing_metadata)

        if model is not None:
            metadata["model"] = model
        metadata["task_updates"] = metadata.get("task_updates", 0) + 1
        metadata["last_task_update_at"] = time.time()
        if completion_message_id is not None:
            metadata["completion_message_id"] = completion_message_id
        metadata_json = json.dumps(metadata)

        # ── Resolve final field values ────────────────────────────
        final_status = existing_status if status is _UNSET else status
        final_due = existing_due if due_at_normalized is _UNSET \
            else due_at_normalized
        final_assignee = existing_assignee if assignee is _UNSET \
            else assignee

        # ── Resolve links (append completion_message_id bidirectionally)
        final_links = existing_links
        final_comp_links = comp_existing_links if completion_exists else None
        if completion_exists and completion_message_id != task_id:
            final_links = _append_link(existing_links, completion_message_id)
            final_comp_links = _append_link(comp_existing_links, task_id)

        # ── Write in one transaction ──────────────────────────────
        now = time.time()
        with trove_db.write_transaction(conn):
            conn.execute(
                """UPDATE nuggets
                   SET status = ?,
                       due_at = ?,
                       assignee = ?,
                       links = ?,
                       metadata = ?,
                       updated_at = ?
                   WHERE message_id = ?""",
                (
                    final_status,
                    final_due,
                    final_assignee,
                    final_links,
                    metadata_json,
                    now,
                    task_id,
                ),
            )
            if completion_exists and final_comp_links is not None \
                    and completion_message_id != task_id:
                conn.execute(
                    """UPDATE nuggets
                       SET links = ?,
                           updated_at = ?
                       WHERE message_id = ?""",
                    (final_comp_links, now, completion_message_id),
                )

        # ── Return updated task row ───────────────────────────────
        result = _fetch_full_row(conn, task_id, original_raw_content)
        result["completion_message_linked"] = completion_exists
        return result
    finally:
        conn.close()


def _handle_nugget_task_update(args: dict, **kw) -> str:
    """Hermes tool handler for nugget_task_update.

    Unpacks the agent's JSON args, calls nugget_task_update, and returns
    a JSON result string. Never raises — catches all exceptions and
    returns an error JSON so the agent turn can continue.

    JSON omits unset keys, so a field that arrives as None is ambiguous: it
    could mean "clear" or "leave unchanged". We treat a missing key as
    "leave unchanged" (the common intent) and an explicit JSON null as
    "clear to NULL" — so we remap absent keys to the _UNSET sentinel before
    calling the function.

    Args:
        args: Dict from Hermes with tool arguments.
        **kw: Extra keyword args from Hermes (ignored).

    Returns:
        JSON string with the result or error information.
    """
    try:
        # Distinguish "key absent" (leave unchanged) from "explicit null"
        # (clear). args.get() returns None for both, so check membership.
        result = nugget_task_update(
            task_id=args["task_id"],
            status=args["status"] if "status" in args else _UNSET,
            due_at=args["due_at"] if "due_at" in args else _UNSET,
            assignee=args["assignee"] if "assignee" in args else _UNSET,
            completion_message_id=args.get("completion_message_id"),
        )
        return _tool_result(result)
    except Exception as e:
        return _tool_result({
            "error": str(e),
            "task_id": args.get("task_id"),
        })
