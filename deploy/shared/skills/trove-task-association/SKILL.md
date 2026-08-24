---
name: trove-task-association
description: "Maintain one task identity across completion, cancellation, scheduling, assignment, and duplicate follow-up messages."
---

# trove-task-association

Use this skill when an inbound message may refer to an existing task,
appointment, assignment, decision, or prior update. The goal is to preserve
every message while maintaining one reliable task identity.

This skill is **deploy-agnostic** — it applies equally to a solo Trove and a
multi-person/partners Trove. It complements `trove-enrich` (which classifies
individual messages) and `trove-daily-digest` (which enumerates task state);
do not duplicate their taxonomy or digest formatting rules here.

## Core rule

A later completion, cancellation, scheduling, assignment, or status message
is **not** automatically a new task. The newly captured message must remain
immutable, but the existing task record should receive the
status / date / assignee update when the match is confident.

Never use the newest message's ID as the task ID merely because its wording
resembles an older task.

## Workflow

1. **Capture first.** Assume the inbound message already exists as its own
   immutable record. Do not rewrite or merge raw text.
2. **Search broadly.** Search for related records across all statuses,
   especially `enriched` and `resolved`; do not restrict association searches
   to `status='captured'`. (Existing tasks are usually already `enriched`.)
3. **Compare task identity.** Use the action, item/vendor, place, pet/person,
   and surrounding context together. Shared names or dates alone are weak
   evidence.
4. **Choose the original task.** Prefer the earlier actionable task record
   over the later report/update record.
5. **Update the original.** Apply `resolved`, `due_at`, and/or `assignee` to
   the original task's `message_id` using **`nugget_task_update`** (the atomic
   task-update tool). Pass the follow-up as `completion_message_id` to record
   the relationship. Do **not** call `nugget_enrich` on the follow-up to move
   its status — that updates the wrong record.
6. **Classify the follow-up appropriately.** An outcome report is normally a
   `fact`, `note`, or `decision`. Use `task` only when it contains a distinct
   remaining action. A future event such as "it will cancel next month"
   may be a fact/date even if the checking task is complete.
7. **Record the relationship.** `nugget_task_update` writes the `links` entry
   bidirectionally when you pass `completion_message_id`. Do not pass an empty
   link list when a confident match exists.
8. **Handle ambiguity conservatively.** If multiple tasks are plausible, do
   not resolve or merge any record. Ask for clarification when interaction is
   available; otherwise preserve the new message and leave the existing tasks
   open. Report the ambiguity rather than guessing.
9. **Verify.** Re-enumerate open tasks with `nugget_tasks` and confirm the
   original task has the intended status/date/assignee. Confirm the follow-up
   message remains present and unchanged.

For simple thoughts with no apparent relationship to an existing item,
enrich and acknowledge without forcing an investigation — the SOUL's
"capture first, don't over-organize" promise still holds.

## Duplicate and update signals

Treat these as likely association candidates:

- "mark it complete/resolved"
- "I checked/called/confirmed …"
- "it will cancel on [date]"
- "put that task on [date]"
- "[name] is the assignee"
- a repeated action/item with added date or assignee detail

A scheduling or assignment follow-up should update the existing task rather
than create a second open task when the action and subject match confidently.

## Association quality checks

Entity extraction alone is not association: exact shared entities can create
false positives, such as two unrelated records sharing a pet's name, a vehicle
word, or a calendar date. A display-time "related" result must not be treated
as proof that two records are the same task.

For a completion/update operation, verify all of the following:

- the original task ID was updated;
- the follow-up message ID was not accidentally marked as the task being completed;
- the original raw text is unchanged;
- the follow-up raw text is unchanged;
- open-task enumeration no longer includes the completed original;
- the `links` field is populated when a confident match was found.

## Tool-design boundary

`nugget_enrich` updates only a supplied `message_id` and cannot safely infer
or enforce parent-task identity. The dedicated atomic operation for this is
**`nugget_task_update`**:

```text
nugget_task_update(
    task_id=<original task message_id>,
    status="resolved",            # 'enriched' to reopen/keep open, 'resolved' to complete
    due_at=<optional date>,       # ISO date string or epoch seconds; null clears; omit to leave
    assignee=<optional name>,     # null clears; omit to leave
    completion_message_id=<new message_id>,  # optional; records a bidirectional link
)
```

It validates that `task_id` is classification=`task`, updates only task
metadata (`status`, `due_at`, `assignee`), records provenance in `metadata`,
and (when `completion_message_id` is given) writes a bidirectional `links`
entry between the task and the follow-up in one transaction. It does **not**
reclassify the follow-up message — classify the outcome as `fact`, `note`, or
`decision` separately with `nugget_enrich`.

Use `nugget_task_update` (not `nugget_enrich` on the follow-up) whenever a
later message completes, cancels, reschedules, or reassigns an existing task.

## Reference

See `references/completion-association-incident.md` for the concrete
examples, observed records, and regression-test cases from the initial
task-association investigation.
