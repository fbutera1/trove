---
name: trove-daily-digest
description: "Produce a board-wide daily digest of open tasks, delivered by a Hermes cron job."
---

# trove-daily-digest

This skill produces **one board-wide daily digest** of open tasks. It is
preloaded by a Hermes cron job (typically `0 5 * * *` for 05:00 Central)
and delivers to every participant via the cron job's `delivery` config.

**Trove produces one digest.** Delivery to every participant is the Hermes
cron job's `delivery` config — do not call `send` or messaging tools from
this skill. The cron prompt is simply "run the trove-daily-digest skill and
send me the digest."

## Scope & Verification

This skill governs the digest assembly behavior: calling `nugget_tasks` for
each bucket, structuring the output, and maintaining the SOUL voice.


---

## Digest assembly

Call `nugget_tasks` once per bucket and assemble in this order:

### 1. Today

`nugget_tasks(horizon="today")` — tasks due today. List each with its
summary, assignee (if someone other than the author), and a gentle
"this is due today" note.

### 2. This week

`nugget_tasks(horizon="week")` — tasks due in the next 7 days (today through 6 days out).
Exclude any ids already shown in bucket 1, then group the remaining tasks by
day if there are multiple, or list them plainly.

### 3. Next few weeks

`nugget_tasks(horizon="month")` — tasks due in the next 30 days, then
**exclude any ids already shown in buckets 1 or 2** (the `month` horizon
overlaps `week`). These are the "on the radar" items.

### 4. Needs fleshing out

`nugget_tasks(horizon="unscheduled")` — enriched tasks with no due date.
These are open-ended items that haven't been scheduled yet. Always list
every open-ended item so the household can decide what to schedule next.
Include the count as a short heading, but do not truncate this section.

### 5. Raw inbox

Tasks that are still `status='captured'` (never enriched). These are the
"hasn't landed anywhere" pile. Show a count and a couple of examples.
This is the 'Trove never forgets' promise — even the ones that didn't
get enriched.

### 6. (Optional) Resolved since yesterday

A tiny win-line at the end: "You resolved X task(s) since yesterday."
Only include if there are any. This is a morale boost, not a requirement.

---

## Voice

Warm, plain-spoken, brief — match the SOUL voice. Each bucket gets a few
lines at most, no wall of text.

**Conscious format decision:** The SOUL says "no bullet reports unless they
ask." A daily digest is inherently a list, but the cron setup IS their
standing opt-in, so light scannable structure is right here. Keep it warm
and conversational — not a spreadsheet, not a standup agenda.

**Never use software jargon.** Words like "Nugget," "FTS5," "horizon,"
"enrichment," or "classification" never appear in the digest. Use the
user's vocabulary: tasks, things to do, vendors, orders, items.

---

## Solo vs. dual rendering

- **Dual mode** (`TROVE_PEOPLE` set): person labels resolve. Show
  `assignee_display` when non-null — "Sam is handling this" or
  "Sarah's task."
- **Solo mode** (`TROVE_PEOPLE` unset): no per-person grouping. Just
  show the board — "you have X tasks today."

**One board-wide digest.** Do not split per participant. The user asked
for one board the solo or family sees together — "who's all doing what so
they're on the same page."

---

## Cron-session notes

Cron runs pass `skip_memory=True` and deliver with a header/footer. The
digest must be **self-contained** — no reliance on session memory or prior
turns. Do not assume the digest turn is part of a continuing conversation.

---

## Hard rules

1. **Read-only.** The digest never mutates a Nugget. It enumerates and
   reports — that's it.
2. **One digest.** Trove produces one board-wide digest. Delivery routing
   is the cron job's `delivery` config, not Trove code.
3. **Self-contained.** No session memory assumptions. The digest stands
   alone.
4. **Show, don't hide.** A missing due date or un-enriched thought is
   shown (buckets 4/5), not hidden. This is the 'Trove never forgets' promise.
