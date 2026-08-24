# Trove Acceptance RUNBOOK

> Manual acceptance steps requiring live infrastructure (Hermes Agent +
> Signal + model). Run after all automated tests pass.
>
> Automated test coverage:
> - `tests/unit/` — schema, tools, capture logic (no live infra)
> - `tests/integration/` — capture hook, enrichment, dashboard API (temp DB)
> - `tests/acceptance/test_deploy_smoke.py` — pre-deploy env checks
>
> These manual steps prove the system works end-to-end with real Signal
> messages. Before starting, run:
>
>     uv run pytest --tb=short
>
> Then run the deploy smoke tests against your live config:
>
>     uv run pytest tests/acceptance/ -m deploy_smoke --tb=short

## Capture hook

### Step 1: Basic capture

1. Ensure Hermes Agent is running with Trove plugin enabled.
2. Send a Signal message to Hermes (e.g., "remind me to call dentist").
3. Immediately inspect `~/.hermes/trove.db`:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT message_id, status, raw_content, created_at FROM nuggets ORDER BY created_at DESC LIMIT 1;"
   ```
4. **Expected:** A row exists with `status='captured'` and `raw_content`
   matching the exact text sent.

### Step 2: Ordering invariant (kill the model mid-turn)

1. Disable the model (e.g., set model to a non-existent one in config).
2. Send a Signal message.
3. Inspect `trove.db` — the row should exist with `status='captured'`.
4. **Expected:** The Nugget is captured even though enrichment never ran.
   This proves capture is deterministic and precedes the LLM.

### Step 3: Verify raw_content immutability

1. Send a message, note its `message_id`.
2. Try to UPDATE `raw_content`:
   ```bash
   sqlite3 ~/.hermes/trove.db "UPDATE nuggets SET raw_content='hacked' WHERE message_id='<id>';"
   ```
3. **Expected:** The `raw_content_guard` trigger aborts with
   `raw_content is immutable`.

## Enrichment tool + skill

### Step 4: Enrichment success

1. Ensure Hermes Agent is running with Trove plugin enabled and the `trove-enrich`
   skill discoverable (in `skills.external_dirs` or `~/.hermes/skills/`).
2. Send a Signal message to Hermes (e.g., "remind me to call dentist on Thursday").
3. After the agent replies, inspect `~/.hermes/trove.db`:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT status, classification, entities, summary, confidence FROM nuggets ORDER BY created_at DESC LIMIT 1;"
   ```
4. **Expected:** A row with `status='enriched'`, a classification from the taxonomy
   (e.g., `task`), populated `entities` (JSON array), a one-line `summary`, and a
   `confidence` between 0.0 and 1.0.
5. Verify `raw_content` still matches the sent text exactly:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT raw_content FROM nuggets ORDER BY created_at DESC LIMIT 1;"
   ```

### Step 5: Enrichment failure isolation

1. Force a failed enrichment (e.g., temporarily configure a bad model, or send a
   payload the skill can't classify).
2. Inspect the latest row:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT message_id, status, raw_content FROM nuggets ORDER BY created_at DESC LIMIT 1;"
   ```
3. **Expected:** The row is intact, `status='captured'` or `'failed'`,
   `raw_content` unchanged. No partial writes or corruption.

## Retrieval

> _Finalized with the live stack (needs live Hermes + Signal + model)._
> The `nugget_search` tool ships here; the live NL-retrieval demonstration
> requires the persona to instruct the agent to use it.

### Step 6: NL retrieval — "what do I need to order?"

1. Ensure Hermes Agent is running with Trove plugin enabled.
2. Send several real messages, some tasks (e.g., "need to order lobster clasps")
   and some facts mentioning entities (e.g., "Acme Beads has the best
   sterling chain").
3. Wait for enrichment to complete on those Nuggets.
4. Ask the agent: "what do I need to order?"
5. **Expected:** The agent uses `nugget_search` and answers citing captured
   **task** Nuggets.

### Step 7: NL retrieval — entity-matched Nuggets

1. Using the same messages from Step 6, ask the agent:
   "everything about Acme Beads"
2. **Expected:** The agent uses `nugget_search` with the entity name and
   returns entity-matched Nuggets (the FTS index includes `entities`).

## Nugget browser backend

### Step 8: Verify the API serves captured Nuggets

1. Ensure you have at least a few captured Nuggets in `~/.hermes/trove.db`
   (from real Signal messages or test data).
2. Start the dashboard server:
   ```bash
   uv run uvicorn trove.dashboard.server:create_app --factory --host 127.0.0.1 --port 9120
   ```
3. In another terminal, verify the list endpoint:
   ```bash
   curl http://localhost:9120/api/nuggets
   ```
4. **Expected:** JSON response with `"count"`, `"items"` array containing
   Nugget rows with `raw_content`, `classification`, etc.
5. Verify the search endpoint:
   ```bash
   curl "http://localhost:9120/api/nuggets/search?q=lobster"
   ```
6. **Expected:** JSON response with `"query"`, `"count"`, `"results"` array
   containing matching Nuggets.

> Full UI acceptance (browse/filter/detail in browser) is the frontend runbook step.

## Nugget browser frontend + runner

### Step 9: Launch the dashboard via CLI

1. Start the dashboard using the CLI:
   ```bash
   uv run trove dashboard
   # or equivalently:
   uv run python -m trove.cli dashboard
   ```
2. **Expected:** The process starts Uvicorn and prints
   `Trove Nugget browser on http://127.0.0.1:9120`.

### Step 10: Browse the Nugget browser UI

1. Open `http://127.0.0.1:9120` in a browser.
2. **Expected:** The Nugget browser renders (no 404, no framework load error).
   Recent Nuggets appear as cards with classification badges, timestamps,
   and previews of `raw_content`.

### Step 11: Test filters

1. Apply each filter in turn:
   - **Classification:** Open Tasks / Facts / Notes / Ideas / Questions / Decisions
   - **Status:** Captured / Enriched / Failed / Resolved
   - **Date:** Today / This Week
   - **Source:** Signal
2. **Expected:** The list updates to show only matching Nuggets.

### Step 12: Test sort

1. Cycle the sort control: Newest → Oldest → Most Relevant.
2. **Expected:** The list ordering changes (newest-first, oldest-first).
   "Most Relevant" on the unfiltered list falls back to newest (server-side
   behavior — no query term to rank by).

### Step 13: Test search

1. Type a keyword into the search box (e.g., a known entity name).
2. **Expected:** Search results appear, marked with a "Search results for: …"
   banner. Results are FTS5-ranked (BM25).
3. Type an obviously non-matching query.
   **Expected:** "No nuggets found" is displayed; no error thrown.
4. Clear the search (click "Show recent nuggets" or empty the search box).
   **Expected:** Returns to the recent Nuggets list.

### Step 14: Test detail drawer

1. Click any Nugget card.
2. **Expected:** A slide-in drawer opens showing:
   - **Original text** (verbatim, in a `<pre>` block)
   - **Timestamp** (formatted local date/time)
   - **Classification** badge and label
   - **Status** badge
   - **AI summary** (if enriched)
   - **Confidence** percentage (if enriched)
   - **Entities** (parsed from JSON string, shown as badges)
   - **Links** (related Nugget IDs)
   - **Metadata** (enrichment provenance, read-only)
   - **Related nuggets** (clickable; clicking opens that Nugget's detail)
3. Click a related Nugget in the drawer.
   **Expected:** The drawer refreshes with the related Nugget's detail.
4. Close the drawer (click ×, overlay, or press Escape).

### Step 15: Verify read-only invariant

1. Inspect the page — there should be no edit buttons, no delete buttons,
   no forms that POST/PUT data.
2. Open browser dev tools → Network tab.
3. **Expected:** All requests to the server are `GET`. No `POST`, `PUT`, or
   `DELETE` requests.
4. Confirm the footer reads "Read-only — capture happens over Signal".

### Step 16: Verify raw_content immutability in the UI

1. In the detail drawer, compare the "Original text" with the captured text
   (check via `sqlite3 ~/.hermes/trove.db "SELECT raw_content FROM nuggets WHERE message_id='<id>';"`).
2. **Expected:** The text matches exactly — no transformation, truncation,
   or HTML-escaping artifacts (the content is rendered verbatim in `<pre>`).

## Config, deployment, and the full end-to-end walk

> This section is the consolidated deploy + acceptance procedure.
> Run it after all automated tests pass (`uv run pytest` green).
> Steps A–D are deploy hygiene; Step E is the end-to-end acceptance walk.

### Step A — Deploy the person-identity list (capture-scope allowlist)

1. Confirm `TROVE_PEOPLE` is set in `~/.hermes/.env` to a comma-separated
   `sender_id:Name` list covering every person whose messages should
   become Nuggets — include **both** the phone number **and** the Signal
   UUID for each person (UUIDs are extracted from signal-cli's database —
   see the README "Signal UUID requirements" section). Example:
   `TROVE_PEOPLE="+131****0101:Frank,uuid1:Frank"`
2. Leave `TROVE_CAPTURE_SENDERS` **unset** unless you need a strict
   override. The capture allowlist is derived from `TROVE_PEOPLE` keys;
   when `TROVE_CAPTURE_SENDERS` is set it becomes the *only* allowlist
   (it does not widen the derived one). Partner notification routing is
   derived from `TROVE_PEOPLE` the same way (the household model: each
   person's keys map to the first-listed other person's UUID key). Leave
   `TROVE_PARTNERS` unset unless you need routing the identity map can't
   express (e.g., a 3+ person household with subgroups); when set, it is
   the *only* routing source.
3. The bot's own number (`SIGNAL_ACCOUNT`) belongs in
   `SIGNAL_ALLOWED_USERS` (self-chat auth) but **never** in
   `TROVE_PEOPLE` — that exclusion is the echo-capture guard (the bot
   must not capture its own notifications as Nuggets).
4. Restart the Hermes gateway so `load_hermes_dotenv()` picks up the new
   env var.
5. Verify the value is in the profile `.env` the gateway actually reads
   (the gateway reloads this file into `os.environ` every turn — a bare
   `os.getenv` in a shell sees nothing, so check the file on disk):
   ```bash
   grep TROVE_PEOPLE ~/.hermes/profiles/trove-agent/.env
   ```
6. **Expected:** the `id:Name` list (non-empty).

### Step B — Reset `trove.db` to a clean start

1. Archive the existing database (one-off deploy cleanup):
   ```bash
   mv ~/.hermes/trove.db ~/.hermes/trove.db.dev-archive-$(date +%Y%m%d)
   ```
2. Confirm it is absent:
   ```bash
   ls -la ~/.hermes/trove.db
   ```
3. **Expected:** `No such file or directory`.
   Trove will reinitialize on the first capture (`schema init` is idempotent).

### Step C — Confirm config wiring

1. Verify `~/.hermes/config.yaml` has the following:
   - `plugins.enabled` includes `trove`
   - `skills.external_dirs` includes `<your-hermes-project>/skills`
   - `trove-enrich` is **not** in `skills.disabled`
   - `platform_toolsets.signal` includes `hermes-signal`
2. Quick checks:
   ```bash
   grep -A 2 'plugins:' ~/.hermes/config.yaml | grep trove
   grep 'external_dirs' ~/.hermes/config.yaml
   grep trove-enrich ~/.hermes/config.yaml  # should return nothing
   grep -A 1 'signal:' ~/.hermes/config.yaml | grep hermes-signal
   ```
3. **Expected:** All four checks pass.

### Step D — Confirm plugin install

1. Install Trove into Hermes' UV environment:
   ```bash
   cd <path-to-your-trove-checkout>
   ~/.hermes/bin/uv pip install -e . --python-preference only-managed
   ```
2. Verify the entry point is discoverable:
   ```bash
   ~/.hermes/bin/uv run --project ~/.hermes/hermes-agent python -c "
   import importlib.metadata as m
   eps = [e for e in m.entry_points(group='hermes_agent.plugins') if e.name=='trove']
   print(eps)
   "
   ```
3. **Expected:** `[EntryPoint(name='trove', value='trove.plugin', ...)]`.

### Step E — The full end-to-end acceptance walk

> Execute these in order. Each row maps to an acceptance criterion.
> Record pass/fail + evidence (DB query output, screenshot, or log line).

**Prerequisites:** Hermes Agent running with Trove plugin enabled, Signal
connected, model configured, `TROVE_PEOPLE` set (the capture allowlist is
derived from its keys), `trove.db` clean.

#### E1 — Send a Signal message → Nugget row exists

> Acceptance criterion: "A user can send a Signal message" / "Trove stores the Nugget"

1. From the allowlisted Signal number, send a message to Hermes
   (e.g., "remind me to order lobster clasps from Acme Beads").
2. Immediately inspect `~/.hermes/trove.db` **before** the agent replies:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT message_id, status, raw_content, created_at FROM nuggets ORDER BY created_at DESC LIMIT 1;"
   ```
3. **Expected:** A row exists with `status='captured'` and `raw_content` matching
   the exact text sent. **Where to look:** DB query output.
4. **Pass/Fail:** [ ]

#### E2 — Agent reply IS the ack (no separate bubble)

> Acceptance criterion: "Trove acknowledges successful storage"

1. Observe the Signal conversation after sending the message in E1.
2. **Expected:** The agent's conversational reply is the acknowledgement.
   No separate "saved" bubble. **Where to look:** Signal chat window.
3. **Pass/Fail:** [ ]

#### E3 — Nugget appears in dashboard

> Acceptance criterion: "The Nugget appears in the dashboard"

1. Start the dashboard: `uv run trove dashboard`
2. Open `http://127.0.0.1:9120` in a browser.
3. **Expected:** The Nugget card is visible with classification badge,
   timestamp, and `raw_content` preview. **Where to look:** Browser UI.
4. **Pass/Fail:** [ ]

#### E4 — Nugget is searchable

> Acceptance criterion: "The Nugget is searchable"

1. In the dashboard, use the search box with a keyword from the Nugget
   (e.g., "lobster").
2. Also verify via API:
   ```bash
   curl "http://localhost:9120/api/nuggets/search?q=lobster"
   ```
3. **Expected:** Search results appear in both the UI and API response.
   **Where to look:** Browser search results + curl output.
4. **Pass/Fail:** [ ]

#### E5 — Trove answers questions using stored Nuggets

> Acceptance criterion: "Trove answers questions using stored Nuggets"

1. Send a task Nugget (e.g., "need to order lobster clasps"),
   then ask: "what do I need to order?"
2. **Expected:** The agent uses `nugget_search` and answers citing the
   captured task Nugget. **Where to look:** Agent reply in Signal.
3. **Pass/Fail:** [ ]

#### E6 — Original content unchanged after enrichment

> Acceptance criterion: "Original content remains unchanged"

1. After enrichment completes, inspect the latest Nugget:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT raw_content FROM nuggets ORDER BY created_at DESC LIMIT 1;"
   ```
2. **Expected:** `raw_content` matches the sent text exactly.
   **Where to look:** DB query output.
3. **Pass/Fail:** [ ]

#### E7 — AI classifications visible

> Acceptance criterion: "AI classifications are visible"

1. After enrichment, inspect the latest Nugget:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT classification, entities, summary, confidence FROM nuggets ORDER BY created_at DESC LIMIT 1;"
   ```
2. Open the dashboard and click the Nugget card to open the detail drawer.
3. **Expected:** Classification (e.g. `task`), entities (JSON array), summary,
   and confidence (0.0–1.0) are populated. Dashboard detail drawer shows them.
   **Where to look:** DB query + browser detail drawer.
4. **Pass/Fail:** [ ]

#### E8 — Enrichment failure does not affect storage

> Acceptance criterion: "Enrichment failures do not affect storage"

1. Force a failed enrichment (e.g., disable the model temporarily).
2. Send a Signal message.
3. Inspect the latest row:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT message_id, status, raw_content FROM nuggets ORDER BY created_at DESC LIMIT 1;"
   ```
4. **Expected:** The row is intact, `status='captured'` or `'failed'`,
   `raw_content` unchanged. **Where to look:** DB query output.
5. **Pass/Fail:** [ ]

#### E9 — Non-allowlisted sender writes no Nugget

> (implicit via the capture-scope filter)

1. Send a Signal message from a non-allowlisted number (a third-party DM
   or a secondary number not in `TROVE_PEOPLE` — and not in
   `TROVE_CAPTURE_SENDERS` if that strict override is set).
2. Inspect the DB:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT count(*) FROM nuggets WHERE author='<non-allowlisted-sender>';"
   ```
3. **Expected:** Count is 0. The capture-scope gate denied capture.
   **Where to look:** DB query output.
4. **Pass/Fail:** [ ]

## Tasking + daily digest

> Requires: live Hermes Agent with Trove plugin, `nugget_tasks` registered,
> `trove-daily-digest` skill discoverable, and at least a few task Nuggets
> with varying due dates in `trove.db`.

### Step F1 — Verify `nugget_tasks` tool works

1. Ensure you have task Nuggets in `trove.db` with `due_at` set:
   ```bash
   sqlite3 ~/.hermes/trove.db "SELECT message_id, classification, status, due_at, assignee FROM nuggets WHERE classification='task' AND status!='resolved';"
   ```
2. **Expected:** At least one row with `due_at` populated (epoch seconds).
3. **Pass/Fail:** [ ]

### Step F2 — Create the cron job

1. Create a cron job in Hermes:
   ```bash
   hermes cron create '0 5 * * *' \
     'Run the trove-daily-digest skill and send me the digest.' \
     --deliver signal:<uuid1>,signal:<uuid2>
   ```
   The skill is invoked via the prompt, **not** a `--skill` flag — the
   `--skill` flag does not work for this job and must be omitted.
   Use the `signal:<uuid>` delivery format (one per participant, comma-separated).
   Each UUID is the operator's Signal ACI/PNI — **not** a phone number.
   signal-cli rejects sends to phone numbers with `UNREGISTERED_FAILURE`;
   UUIDs are the only reliable delivery target. Extract UUIDs from signal-cli's
   database (see the Signal UUID section in README.md).
2. Note the cron job ID from the output.
3. **Pass/Fail:** [ ]

### Step F3 — Trigger a manual run

1. Trigger the cron job immediately:
   ```bash
   hermes cron run <cron_job_id>
   ```
2. Wait for the run to complete (check `hermes cron list` or the Hermes logs).
3. **Expected:** The cron job runs, the agent loads the `trove-daily-digest`
   skill from the prompt, and assembles a digest.
4. **Pass/Fail:** [ ]

### Step F4 — Verify digest delivery

1. Check that **each participant** receives one board-wide digest message.
2. The digest should cover these buckets (if tasks exist for them):
   - **Today** — tasks due today
   - **This week** — tasks due in the next 7 days
   - **Next few weeks** — tasks due in the next 30 days (excluding week items)
   - **Needs fleshing out** — enriched tasks with no due date
   - **Raw inbox** — captured but un-enriched tasks
3. The digest should be in the SOUL voice: warm, plain-spoken, brief.
   No software jargon ("Nugget", "FTS5", "horizon" must not appear).
4. **Expected:** One digest per participant, covering the same board-wide
   content. No per-partner separate digests.
5. **Pass/Fail:** [ ]
