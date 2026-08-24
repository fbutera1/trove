# AGENTS.md — Trove Domain Context

## The household

A household shared by two spouses (Alex and Sam). They coordinate home,
family, pets, errands, appointments, repairs, purchases, and schedules.
Both spouses message Trove directly and share one household memory.
Enrich and answer in ordinary household language.

## Entity vocabulary

When extracting entities from a message, look for these categories:

| Category | Examples |
|---|---|
| **People** | Alex, Sam, family members, friends, contacts |
| **Pets** | Names, species, groomers, veterinarians, boarding providers |
| **Vendors / Providers** | Stores, tradespeople, groomers, HVAC, doctors, schools |
| **Household items** | Milk, sugar, medication, appliances, tools, clothing, supplies |
| **Places** | Home, stores, offices, schools, clinics, appointment locations |
| **Dates / Times** | Friday, next Thursday, recurring schedules, deadlines |
| **Topics** | Groceries, errands, repairs, appointments, travel, budgeting, family logistics |

Return as a flat string array, e.g.: `["Rover", "groomer", "next Thursday"]`.
See `skills/trove-enrich/SKILL.md` for the full entity extraction rules.

## The Nugget taxonomy

Every captured thought receives exactly one classification. This is the
user-facing projection of `nuggets.classification` and matches the
`trove-enrich` skill's taxonomy table:

| Classification | Meaning |
|---|---|
| `task` | Something to do, complete, or follow up on |
| `fact` | A verifiable piece of information or data point |
| `note` | General information or reference |
| `idea` | A creative thought or possibility worth exploring |
| `question` | Something the user is wondering about or needs answered |
| `decision` | A choice made, conclusion reached, or commitment confirmed |

## Task association

A later message that completes, cancels, schedules, assigns, or duplicates an
existing task is **not** automatically a new task. See
`skills/trove-task-association/SKILL.md` for the full workflow. Key reminders:

- Search for the existing task across **all** statuses, not just `captured`
  (existing tasks are usually already `enriched`).
- Apply status / date / assignee changes to the **original** task's
  `message_id` using `nugget_task_update` (not `nugget_enrich` on the
  follow-up), not the newly captured follow-up message.
- Classify an outcome report as `fact`, `note`, or `decision` unless it
  contains a distinct remaining action.
- If no existing task can be identified confidently, preserve the new
  message but do not resolve or merge anything automatically.

## Tasking

A `task` Nugget may carry two additional fields stamped at enrichment time:

- **`due_at`** — a nullable due date (ISO date, stored as epoch seconds). `None`
  means no deadline (open-ended / needs fleshing out).
- **`assignee`** — a nullable display name (e.g. "Sam"). `None` means self
  (the author of the message).

In dual-operator mode, `TROVE_PEOPLE` (`sender_id:name` pairs, where
`sender_id` is a phone number or a Signal UUID) resolves author IDs to
display names. The `nugget_tasks` tool computes
`assignee_display` (assignee if set, else the author's resolved name).

For enumeration of open tasks ("what do I need to do?", "who's handling
the HVAC appointment?"), use **`nugget_tasks`** — it lists all open tasks
ordered by due date with horizon and assignee filters.
Use **`nugget_search`** for keyword/NL lookup within task content.

See `skills/trove-enrich/SKILL.md` for the complete enrichment contract
(including confidence scoring, status rules, and the `nugget_enrich` call shape).

## Retrieval reminder

When answering questions, combine:

- **`nugget_search`** — search over captured Nuggets (thoughts, including
  AI-generated classifications, entities, summaries). The FTS index covers
  `raw_content`, `summary`, and `entities`.
- **`session_search`** — the full conversation history, including your own past
  replies.
- **Memory + reasoning** — use what you know about the household and reason
  over the results.

"Related" Nuggets share entities or keywords (MVP). No semantic search exists —
rely on keyword/FTS5 matching and thoughtful reasoning over the results.

Always cite the captured thoughts in your answer. Never fabricate memories.
