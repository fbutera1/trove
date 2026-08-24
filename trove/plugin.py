"""Plugin registration for Trove.

The `register(ctx)` function registers:
  - The `pre_gateway_dispatch` capture hook
  - The `nugget_enrich` tool (AI enrichment)
  - The `nugget_search` tool (FTS5 retrieval)
  - The `nugget_tasks` tool (open-task enumeration)
  - The `nugget_task_update` tool (atomic task update/resolve)
"""

from trove.capture import trove_capture
from trove.tools import (
    NUGGET_ENRICH_SCHEMA,
    NUGGET_SEARCH_SCHEMA,
    NUGGET_TASKS_SCHEMA,
    NUGGET_TASK_UPDATE_SCHEMA,
    _handle_nugget_enrich,
    _handle_nugget_search,
    _handle_nugget_tasks,
    _handle_nugget_task_update,
)


def register(ctx):
    """Register Trove hooks and tools with the Hermes Agent plugin context.

    Args:
        ctx: PluginContext from Hermes Agent.
    """
    ctx.register_hook("pre_gateway_dispatch", trove_capture)
    ctx.register_tool(
        name="nugget_enrich",
        toolset="hermes-signal",
        schema=NUGGET_ENRICH_SCHEMA,
        handler=_handle_nugget_enrich,
        description="Stamp AI enrichment fields (classification, entities, "
                    "summary, status, confidence, links) on a captured Nugget.",
    )
    ctx.register_tool(
        name="nugget_search",
        toolset="hermes-signal",
        schema=NUGGET_SEARCH_SCHEMA,
        handler=_handle_nugget_search,
        description="FTS5 keyword search over Nuggets with optional "
                    "classification/status/source filters; returns Nugget "
                    "rows including raw_content.",
    )
    ctx.register_tool(
        name="nugget_tasks",
        toolset="hermes-signal",
        schema=NUGGET_TASKS_SCHEMA,
        handler=_handle_nugget_tasks,
        description="Enumerate open tasks (classification='task', status != 'resolved') "
                    "ordered by due date. Supports horizon and assignee filters. "
                    "Resolves author/assignee names via TROVE_PEOPLE.",
    )
    ctx.register_tool(
        name="nugget_task_update",
        toolset="hermes-signal",
        schema=NUGGET_TASK_UPDATE_SCHEMA,
        handler=_handle_nugget_task_update,
        description="Atomically update an EXISTING task's status, due date, and/or "
                    "assignee by its original message_id, optionally linking the "
                    "follow-up/completion message. Use this — not nugget_enrich on "
                    "the follow-up — to complete or reschedule a task.",
    )
