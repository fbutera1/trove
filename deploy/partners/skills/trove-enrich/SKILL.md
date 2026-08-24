---
name: trove-enrich
description: "Enrich every inbound household Trove thought with classification, entities, summary, confidence, and status."
---

# trove-enrich

Every inbound user message is a **Nugget** — a captured thought stored in Trove.
This skill governs the enrichment step: given a Nugget whose `raw_content` is
already persisted (by the capture hook), stamp it with AI-generated
metadata so it becomes searchable and actionable.

The enrichment is **strictly additive** — it never modifies the original text.
The database row already exists with `status='captured'`; your job is to update
only the AI metadata fields via the `nugget_enrich` tool.

## Scope & Verification

This skill covers the complete enrichment taxonomy, entity extraction rules,
confidence scoring, status transitions, and the tool-call contract for
`nugget_enrich`.

**Verification targets:**
- Tool definition: `trove/tools.py` → `nugget_enrich`
- Plugin registration: `trove/plugin.py`
- Unit tests: `tests/unit/test_tools_enrich.py`
- Integration tests: `tests/integration/test_enrich.py`
- Fixtures: `tests/conftest.py`

---

## Classification Taxonomy

Classify each Nugget into **exactly one** category from the taxonomy below.

| Classification | Definition |
|---|---|
| `task` | Something the user needs to do, complete, or follow up on |
| `fact` | A verifiable piece of information, data point, or observation |
| `note` | General information, reminder, or reference without action required |
| `idea` | A creative thought, proposal, or possibility worth exploring |
| `question` | Something the user is wondering about or needs answered |
| `decision` | A choice made, conclusion reached, or commitment confirmed |

---

## Entity Extraction

Pull out the key entities from the message and return them as a JSON array of
strings. Include people, spouses, pets, vendors, service providers, household
items, places, dates, appointments, and other useful topics.

| Entity type | Examples |
|---|---|
| **People** | Alex, Sam, family members, friends, contacts |
| **Pets** | Names, species, groomers, veterinarians, boarding providers |
| **Vendors / Providers** | Stores, tradespeople, groomers, HVAC companies, doctors, schools |
| **Household items** | Milk, sugar, medication, appliances, tools, clothing, supplies |
| **Places** | Home, stores, offices, schools, clinics, appointment locations |
| **Dates / Times** | Friday, next Thursday, recurring schedules, deadlines |
| **Topics** | Groceries, errands, repairs, appointments, travel, budgeting, family logistics |

Return as a flat string array, e.g.:

```json
["Rover", "groomer", "next Thursday", "appointment"]
```

---

## Summary

Write a **one-line summary** that faithfully captures the essence of the original
message. Do not add information not present in the original text.

---

## Task association

If the message may update, complete, schedule, or duplicate an existing task,
defer to **`trove-task-association`** for the full workflow. Use
**`nugget_task_update`** (not `nugget_enrich`) to apply a `resolved`, `due_at`,
or `assignee` change to the **original** task's `message_id`; pass the follow-up
as `completion_message_id` to record a bidirectional link. Key reminders:
search across all statuses (not just `captured`), update the original task
record, and classify outcome messages as `fact`, `note`, or `decision` unless
they contain a distinct remaining action. For simple thoughts with no apparent
relationship, enrich and acknowledge without forcing an investigation.

---

## Confidence

Set a score from `0.0` to `1.0` reflecting how confident you are in the
classification and summary. Use `null` only if no enrichment was possible at all.

| Score range | Meaning |
|---|---|
| `0.9–1.0` | Clear, unambiguous classification |
| `0.7–0.9` | Confident but some ambiguity |
| `0.5–0.7` | Reasonable guess with notable uncertainty |
| `0.0–0.5` | Low confidence, classification is speculative |

---

## Status Rules

| Status | When to use |
|---|---|
| **`enriched`** _(default)_ | The Nugget was successfully classified and summarized |
| **`failed`** | You could not classify or summarize the message (gibberish, empty, etc.). Still call the tool — set `status="failed"` and leave other fields `null` |
| **`resolved`** | Set **explicitly** only when a Task is completed or a Decision is finalized. Never set by routine enrichment |

---

## Task due date & assignee

For Nuggets classified as `task`, extract these additional fields when the
message text provides them:

**`due_at`** — a deadline date, as an ISO 8601 date string (`YYYY-MM-DD`).
Extract when the text names a deadline:
- "by Friday" → resolve to the next Friday's date
- "end of next week" → resolve to the Sunday of next week
- "Aug 15" → `2026-08-15`
- If no deadline is stated, pass `due_at=None` (open-ended task — the digest
  will show it in "needs fleshing out")

**`assignee`** — a display NAME (e.g. "Sam"), or `None` for self.
Extract **only when the text explicitly names someone other than the speaker**:
- "Remind Sam to..." → `assignee="Sam"`
- "have Sarah call..." → `assignee="Sarah"`
- "I need to order clasps" → `assignee=None` (self = the author)
- "remind me to..." → `assignee=None` (self = the author)

**Do not invent a name the text does not contain.** The common case is
`assignee=None` (self = the author of the message).

---

## Action

Call the `nugget_enrich` tool with the Nugget's `message_id` and the computed fields:

```python
nugget_enrich(
    message_id="<the Trove message_id>",
    classification="<one of the taxonomy>",
    entities=["entity1", "entity2"],
    summary="<one-line summary>",
    status="enriched",
    confidence=0.85,
    links=[],  # populate with related message ID(s) when trove-task-association finds a match
    due_at="2026-08-15",  # for tasks with a deadline (ISO date string)
    assignee="Sam",     # for tasks assigned to someone (name, or None for self)
)
```

For a task with no deadline and self-assigned (the common case):

```python
nugget_enrich(
    message_id="<the Trove message_id>",
    classification="task",
    entities=["lobster clasps"],
    summary="Reorder lobster clasps",
    status="enriched",
    confidence=0.9,
    links=[],
    due_at=None,
    assignee=None,
)
```

For failed enrichment:

```python
nugget_enrich(
    message_id="<the Trove message_id>",
    classification=None,
    entities=None,
    summary=None,
    status="failed",
    confidence=None,
    links=[],
)
```

## Hard Rules

1. **Never alter the original text.** Enrichment is strictly additive — it only
   stamps metadata on the existing row.
2. **The row already exists.** The capture hook has already inserted
   the Nugget with `status='captured'` and `raw_content` set. You only update
   the AI fields.
3. **`raw_content` is immutable.** The database enforces this — you cannot and
   must not change it.
4. **If unsure, fail gracefully.** Leave fields `null` and call the tool with
   `status="failed"`. It is better to mark a Nugget as failed than to assign a
   wrong classification.
5. **One classification per Nugget.** Never combine categories — pick the single
   best match from the taxonomy.
6. **Internal terminology.** "Nugget" is an internal term for a captured
   thought. Never use it in user-facing replies — use "thought," "note," or
   plain language.
