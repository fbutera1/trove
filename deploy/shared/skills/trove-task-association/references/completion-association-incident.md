# Completion and association incident reference

This document records the concrete examples, observed records, and
regression-test cases that motivated `trove-task-association`. The examples
below are **synthetic reconstructions** of real failure modes — the original
incident names, IDs, and message texts have been replaced with neutral
placeholders. Each illustrates a distinct association failure that the skill
and the `nugget_task_update` tool were designed to prevent.

## Root cause

`nugget_enrich` updates only the exact `message_id` it receives; it does not
infer a parent task or move completion state between records. The agent was
treating a completion/update message as the task record itself: it searched
for a matching record with `status="captured"`, found the newly captured
completion message, and called `nugget_enrich` with that new message's ID
and `status="resolved"`. Two problems result:

1. The original task is never resolved.
2. The completion message is incorrectly classified as a second resolved task.

## Reproduction records (synthetic reconstructions)

### 1. Car service completion

Original task:

- ID `task-A`
- text: `take the car in for an oil change`
- status `enriched`, classification `task`, due_at NULL

Later update:

- ID `followup-A`
- text: `the oil change is done, you can mark that resolved`
- status `resolved`, classification `task`

The later message was resolved instead of the original task.

### 2. Subscription cancellation

Original task:

- ID `task-B`
- text: `remember to cancel the streaming subscription`
- status `enriched`, classification `task`, due_at NULL

Later update:

- ID `followup-B`
- text: `I checked the subscription. It will cancel next month. Task done.`
- status `resolved`, classification `task`

The later message was resolved instead of the original task. "It will cancel
next month" is a useful future fact and should not be confused with the
identity of the completed cancellation-checking task.

### 3. Pet grooming duplicate scheduling

Three separate open task records referred to the same grooming action:

- `task-C1`: `book the dog's grooming`
- `task-C2`: `grooming appointment for next week`
- `task-C3`: `put the dog grooming on the calendar for next week`

A confident scheduling follow-up should have updated the original grooming
task with `due_at`; if the match were not confident, it should have asked
rather than creating another open task.

### 4. Chore scheduling follow-up (same class)

- Original task: `task-D` —
  `clean the gutters on Thursday` (no `due_at`).
- Scheduling follow-up: `followup-D` —
  `the gutter cleaning is tomorrow.`
  (enriched as a `task` with due_at set, assignee NULL).

The follow-up should have stamped `due_at` onto the original gutters task and
been classified as a `note`/`fact` itself, not left as a second open task.

## Observed system facts

- A live database had records with `entities` but zero records with
  non-empty `links` — so entity extraction is not the same as association.
- The dashboard's related view uses exact entity overlap and is limited to
  five rows. It can produce false positives, such as connecting unrelated
  records because they share a name or date.
- `nugget_enrich` updates only the supplied `message_id`; it does not infer
  a parent task.
- Observed agent searches sometimes filtered to `status='captured'`, which
  excludes already-enriched original tasks.

## Regression scenarios

A future implementation (prompt+skill, and the durable task-update tool)
should pass:

1. A car-service completion report resolves the **original** task ID, not the
   later message ID.
2. A subscription-cancellation report resolves the original task ID and
   preserves the future cancellation date as a fact.
3. A pet-grooming scheduling follow-up updates one existing grooming task
   with `due_at` instead of creating a second open task.
4. A chore scheduling follow-up stamps `due_at` onto the original chore task
   and is itself classified as a `note`/`fact`.
5. A separate assignment statement updates or links to the existing task.
6. Ambiguous entity overlap does not trigger an automatic merge or resolution.
7. Completion and update messages remain captured with immutable original text.
8. After updating the original task, `nugget_tasks` no longer returns it as open.
9. A successful association records a `links` entry connecting follow-up and
   original when supported.
