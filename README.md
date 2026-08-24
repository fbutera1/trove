# Trove

**Trove — an AI second-brain plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

Trove captures every message you send to your agent, enriches it, and remembers
it forever. You text your agent thoughts throughout the day; Trove makes sure
none of them are lost — even across sessions, restarts, and context resets.

> **Trove never forgets.** Every message is saved *before* the agent replies, so
> the reply itself is your confirmation it landed.

## Scope

Trove is built for **Signal** via the Hermes `hermes-signal` platform toolset. It
hardcodes the Signal source tag, registers tools under the `hermes-signal`
toolset, and uses Signal adapters for partner notifications. It will not work
out of the box on other messaging platforms.

This is a personal project maintained for my own use. If you like the pattern,
feel free to fork it and adapt it to your stack — the capture hook, enrichment
tools, and SQLite schema are all channel-agnostic and should port cleanly. See
the [License](LICENSE) for details.

## What it does

Trove ships as a Hermes plugin with four tools, a capture hook, and a dashboard web UI.

Every captured message is called a **Nugget** — it lives in a SQLite database
(`trove.db`, default `~/.hermes/trove.db`) using WAL mode with concurrent-writer
protection. (SQLite is bundled with Python, so no separate install is needed.)

| Piece | What it is |
|---|---|
| **`pre_gateway_dispatch` hook** | Captures every inbound message as a Nugget in `trove.db` *before* the agent responds (the "never forgets" invariant). Optional sender-allowlist gate. |
| **`nugget_enrich` tool** | Stamps AI enrichment on a Nugget: classification, entities, summary, status, confidence, links. |
| **`nugget_search` tool** | FTS5 keyword search over Nuggets with classification/status/source filters. |
| **`nugget_tasks` tool** | Enumerates open tasks (ordered by due date) with horizon/assignee filters. |
| **`nugget_task_update` tool** | Atomically updates an existing task's status, due date, and/or assignee by its original `message_id` — used to complete or reschedule a task. |
| **Dashboard** | A loopback-only web UI (`trove dashboard`) that serves a static frontend via FastAPI. Browse, filter, and search Nuggets through a table-based interface at `http://127.0.0.1:9120`. |

## Requirements

- Python ≥ 3.11
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [uv](https://docs.astral.sh/uv/) (recommended for install)

## Install

Trove is pip-installable and registers itself with Hermes via the
`hermes_agent.plugins` entry-point group.

### Step 1: Install the package

```bash
# From this checkout, into Hermes' managed venv:
~/.hermes/bin/uv pip install -e . --python ~/.hermes/hermes-agent/venv/bin/python
```

This installs the package once — it's shared across all Hermes profiles.

### Step 2: Enable the plugin in your profile's config

Each Hermes profile that should use Trove needs `plugins.enabled` in its
`config.yaml`. Use `hermes config set` to write it:

```bash
# For the default profile:
hermes config set plugins.enabled '["trove"]'

# For a specific profile (HERMES_HOME targets the profile directory):
HERMES_HOME=/path/to/profile hermes config set plugins.enabled '["trove"]'
```

> **Note:** `hermes config set` always writes to the active profile's
> `config.yaml` — `HERMES_PROFILE` does not redirect it. Use
> `HERMES_HOME=/path/to/profile` instead. It also serializes JSON arrays
> as literal strings (e.g., `'["trove"]'` instead of a proper YAML list).
> Fix with a quick Python round-trip:
>
> ```bash
> python3 -c "
> import yaml, json
> path = '/path/to/profile/config.yaml'
> with open(path) as f: cfg = yaml.safe_load(f)
> if isinstance(cfg['plugins']['enabled'], str):
>     cfg['plugins']['enabled'] = json.loads(cfg['plugins']['enabled'])
> with open(path, 'w') as f: yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
> "
> ```

If you also use Signal, add the platform toolset:

```yaml
platform_toolsets:
  signal:
    - hermes-signal
```

### Step 3: Verify

```bash
~/.hermes/hermes-agent/venv/bin/python -c "
import importlib.metadata as m
eps = list(m.entry_points(group='hermes_agent.plugins', name='trove'))
print(f'Found: {eps}')
mod = eps[0].load()
print(f'Loaded: {mod} (has register: {hasattr(mod, \"register\")})')
"
```

### Step 4: Restart the gateway

`hermes gateway restart` often claims success without actually replacing the
running process. Do a hard restart instead:

```bash
# Kill the old gateway
PID=$(python3 -c "import json; print(json.load(open('~/.hermes/profiles/your-profile/gateway.pid'))['pid'])")
kill $PID
sleep 2
# Start fresh
HERMES_PROFILE=your-profile hermes gateway run
```

Check the logs for `Plugin discovery complete` — the enabled count should
be one higher than before Trove:

```bash
grep "Plugin discovery complete" ~/.hermes/profiles/your-profile/logs/agent.log | tail -1
```

> **Note:** Trove registers via Python entry-points, not the bundled plugin
> system — it will **not** appear in `hermes plugins list` output. That's
> expected. Verify via the plugin discovery count in the gateway log instead.

## Quick start

After install and setup, send your agent a thought:

> "Remember: pick up lobster clasps from Acme Beads by Friday."

Trove captures it before the agent replies. The reply itself is your confirmation
it landed. Later you can ask "what do I need to do?" and Trove will surface the
task via `nugget_tasks`.

## Choose a deployment profile

Trove ships two persona/context profiles plus a set of shared skills. `deploy/setup.sh`
wires a chosen profile's context and skills into your Hermes profile so Hermes
auto-loads them (idempotent; backs up anything it replaces).

```bash
./deploy/setup.sh solo        # single-operator Trove
./deploy/setup.sh partners    # shared multi-person Trove (e.g. a household)

# Install modes:
./deploy/setup.sh partners --copy   # copy all files into profile
./deploy/setup.sh partners --mixed  # copy SOUL/skills, symlink AGENTS.md

# or point at a non-default profile:
HERMES_HOME=/path/to/profile ./deploy/setup.sh partners --mixed
```

Each profile wires in:

- `SOUL.md` / `AGENTS.md` — the persona and domain context the agent loads
- `trove-enrich` — the enrichment skill
- `trove-daily-digest` — generates a daily summary of open tasks, recent Nuggets, and pending items
- `trove-task-association` (shared) — keeps one task identity across follow-ups

Restart Hermes (or `/reset`) after running setup so the new SOUL/AGENTS/skills load.
See [`deploy/README.md`](deploy/README.md) for the full layout and link table.

### Post-install customization

The deploy profiles ship with placeholder names and phone numbers. After
running setup, customize the files in your profile:

1. **Edit `SOUL.md`** — replace the placeholder names (e.g., "Alex", "Sam")
   and Signal sender IDs (e.g., `+131****0101`) with real ones.
2. **Edit skill files** — swap out placeholder names in
   `skills/trove-enrich/SKILL.md` and `skills/trove-daily-digest/SKILL.md`.
3. **Set environment variables** in your profile's `.env`:

#### Core Trove variables

| Variable | Purpose | Example |
|---|---|---|
| `TROVE_PEOPLE` | Maps Signal IDs to display names **and** defines who is captured (the capture allowlist is derived from its keys) | `"+155****4567:Alice,8f1c2a3e-9b4d-4e6f-8a01-2c3d4e5f6a7b:Alice"` |
| `TROVE_CAPTURE_SENDERS` | (Optional) strict capture-allowlist override | See Signal note below |
| `TROVE_PARTNERS` | (Optional) strict partner-routing override — derived from `TROVE_PEOPLE` when unset | See Signal note below |
| `TROVE_DB` | Override the default SQLite path (`~/.hermes/trove.db`) | `/var/lib/trove.db` |

#### Signal-specific variables

If the profile uses Signal, add these to `.env`:

| Variable | Purpose | Example |
|---|---|---|
| `SIGNAL_HTTP_URL` | URL of the signal-cli HTTP daemon | `http://127.0.0.1:8080` |
| `SIGNAL_ACCOUNT` | The **single bot phone number** registered in signal-cli | `+1334567890` |
| `SIGNAL_ALLOWED_USERS` | Comma-separated list of phone numbers/UUIDs allowed to message the bot | See Signal note below |
| `SIGNAL_GROUP_ALLOWED_USERS` | (Optional) Comma-separated list of group IDs the bot should participate in | `group-id-1,group-id-2` |

Also ensure `config.yaml` has:

```yaml
platforms:
  signal:
    enabled: true
```

#### Signal UUID requirements (important)

Signal delivers inbound messages with sender **UUIDs** (ACI/PNI), not phone
numbers. This affects three configuration points:

- **`SIGNAL_ALLOWED_USERS`** — Must include each operator's Signal UUID
  alongside their phone number, otherwise the Hermes auth layer rejects
  inbound messages with `Unauthorized user: <uuid>`. Extract UUIDs from
  signal-cli's database:
  ```bash
  strings ~/.local/share/signal-cli/data/<account-dir>.d/account.db \
    | grep -E "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
  ```
- **`TROVE_PEOPLE`** — Include each person's UUID **alongside** their phone
  number. This is the **single list to maintain**: capture stores the raw
  sender ID in the `author` column (UUID for partner DMs, phone for
  note-to-self), `nugget_tasks` / the dashboard resolve display names by
  looking that ID up in this map, and the capture allowlist is **derived
  from these keys** — so every ID shape a person can arrive under must be
  here both for capture and for name resolution:
  ```
  TROVE_PEOPLE="+155****4567:Alice,uuid1:Alice,+155****6543:Bob,uuid2:Bob"
  ```
- **`TROVE_CAPTURE_SENDERS`** (optional) — An explicit, **strict**
  capture-allowlist override. When set, it is the *only* capture
  allowlist (it does not widen `TROVE_PEOPLE`). Leave it unset for the
  common case; set it only to capture from fewer (or more) people than
  `TROVE_PEOPLE` defines. If set, include both phone numbers **and**
  UUIDs — phones first, then UUIDs:
  ```
  TROVE_CAPTURE_SENDERS="+155****4567,+155****6543,uuid1,uuid2"
  ```
- **`TROVE_PARTNERS`** (optional) — An explicit, **strict**
  partner-routing override. When set, it is the *only* routing source
  (it does not widen `TROVE_PEOPLE`). Leave it unset for the common
  case: in a multi-person household the routing is **derived from
  `TROVE_PEOPLE`** (each person's keys map to the first-listed other
  person's UUID key — the household model; 2-person homes get both
  directions automatically), and a solo (1-person)
  `TROVE_PEOPLE` derives no routing at all. Set it only for routing
  the identity map can't express (e.g., a 3+ person household with
  subgroups). **Use UUIDs** (not phone numbers) for reliable delivery —
  signal-cli rejects phone sends with `UNREGISTERED_FAILURE`:
  ```
  TROVE_PARTNERS="uuid1:uuid2"
  ```

> **Warning:** `SIGNAL_ACCOUNT` must be a **single phone number** — not a
> comma-separated list. Multiple numbers cause the Signal adapter to use the
> last entry as the bot account, which doesn't exist in signal-cli, producing
> an infinite SSE reconnect loop. Put allowed users in
> `SIGNAL_ALLOWED_USERS` instead.

With `--mixed` mode, only `SOUL.md` and skills are copied (editable in the
profile). `AGENTS.md` stays symlinked to the repo. Re-run setup to pull in
updated files from the Trove repo.

## The dashboard

```bash
trove dashboard                                   # http://127.0.0.1:9120
trove dashboard --host 127.0.0.1 --port 9120
trove dashboard --db-path /path/to/trove.db
```

Browse, filter, and search Nuggets over the API (`/api/nuggets`,
`/api/nuggets/search`, `/api/nuggets/{message_id}`). It binds to loopback only.

## Configuration (environment variables)

### Core Trove variables

| Variable | Purpose |
|---|---|
| `TROVE_PEOPLE` | Comma-separated `"id:Name"` map — the **single source of truth** for person identity. `id` can be a phone number **or** a Signal UUID — capture stores the raw sender ID in the `author` column (UUID for partner DMs, phone for note-to-self), so include **both** identifiers per person for name resolution to work in every mode. The **capture allowlist is derived from these keys**: a sender is captured if and only if their ID is a key here (unless `TROVE_CAPTURE_SENDERS` is set). The bot's own number (`SIGNAL_ACCOUNT`) must never be an entry — its absence is the echo-capture guard. |
| `TROVE_CAPTURE_SENDERS` | (Optional) Comma-separated sender-allowlist override. Fallback chain: if set, it is the **only** capture allowlist (strict — it does not widen `TROVE_PEOPLE`); otherwise the allowlist is the `TROVE_PEOPLE` keys; if those are also unset, it falls back to `SIGNAL_ALLOWED_USERS`. |
| `TROVE_PARTNERS` | (Optional) Comma-separated `"id1:id2"` pairs routing messages between partners in a multi-person deployment. Each pair is bidirectional — `A:B` means A notifies B and B notifies A. When set, it is the **only** routing source (strict — it does not widen `TROVE_PEOPLE`). When unset, routing is **derived from `TROVE_PEOPLE`** (the household model: each person's keys map to the first-listed other person's UUID key; a solo `TROVE_PEOPLE` derives no routing). **Use UUIDs** (not phone numbers) for reliable Signal delivery. |
| `TROVE_DB` | Override the default SQLite path (`~/.hermes/trove.db`). |

### Signal-specific variables

| Variable | Purpose |
|---|---|
| `SIGNAL_HTTP_URL` | URL of the signal-cli HTTP daemon (e.g., `http://127.0.0.1:8080`). |
| `SIGNAL_ACCOUNT` | The **single bot phone number** registered in signal-cli. Must not be comma-separated. |
| `SIGNAL_ALLOWED_USERS` | Comma-separated list of phone numbers and/or UUIDs allowed to message the bot. Include UUIDs for Signal operators. |
| `SIGNAL_GROUP_ALLOWED_USERS` | (Optional) Comma-separated list of group IDs the bot should participate in. |

The `trove.db` path defaults to `~/.hermes/trove.db`; override it with
`TROVE_DB` (plugin) or `trove dashboard --db-path` (dashboard only).

## Troubleshooting

| Problem | Fix |
|---|---|
| `uv pip install` says "No virtual environment found" | Use `--python ~/.hermes/hermes-agent/venv/bin/python` instead of `--python-preference only-managed`. The Hermes venv lives at `~/.hermes/hermes-agent/venv/`. |
| Entry point not discovered | Re-run the install command above and verify with Step 3 in the Install section. |
| `hermes config set` writes to the wrong profile | It always writes to the active profile. Use `HERMES_HOME=/path/to/profile` to target another profile — `HERMES_PROFILE` does not redirect it. |
| `plugins.enabled` is a string instead of a YAML list | `hermes config set` serializes JSON arrays as literal strings. Fix with the Python round-trip shown in Step 2 of the Install section. |
| Gateway restart doesn't take effect | `hermes gateway restart` can appear to succeed but leave the old process running. Kill the old PID from `gateway.pid` and start fresh (see Step 4 of the Install section). |
| Hermes not loading the plugin | Check the profile's `config.yaml` has `plugins.enabled: [trove]` as a proper YAML list, and restart the gateway. |
| `hermes plugins list` doesn't show Trove | Trove registers via entry-points, not the bundled plugin system — that's expected. Verify via the plugin discovery count in the gateway log instead. |
| Dashboard won't start | Ensure port 9120 is free; use `--port` to pick another. Check `trove.db` is readable. |
| Skills not loading | Run `./deploy/setup.sh` again (idempotent) and restart Hermes. |
| Signal SSE reconnect loop (`Signal SSE: connected` every ~2s) | `SIGNAL_ACCOUNT` must be a **single phone number**, not comma-separated. Also check signal-cli systemd service includes `--account +NUMBER` in `ExecStart`. |
| `Unauthorized user: <uuid>` in gateway log | Signal sends UUIDs as sender IDs. Add each operator's UUID to `SIGNAL_ALLOWED_USERS` alongside their phone numbers. Extract UUIDs from signal-cli's DB (see Signal UUID section above). |
| Partner notifications not firing | Check `grep "trove notification" ~/.hermes/profiles/<profile>/logs/agent.log`. First make sure the sender's messages are captured at all: their UUID (and phone) must be keys in `TROVE_PEOPLE` — or in `TROVE_CAPTURE_SENDERS` if that strict override is set. When `TROVE_PARTNERS` is unset, routing is derived from those keys (the other person's UUID key must be present in `TROVE_PEOPLE`). If you do set `TROVE_PARTNERS` explicitly, use UUIDs in it (not phone numbers). |
| Partner notification fails with `UNREGISTERED_FAILURE` | signal-cli rejects sends to phone numbers. On the derived path, make sure each person's UUID is in `TROVE_PEOPLE`; if you set `TROVE_PARTNERS` explicitly, set it to UUIDs directly (e.g., `TROVE_PARTNERS="uuid1:uuid2"`). |

## Development

```bash
uv sync --extra dev          # install dev deps (pytest, pytest-asyncio, httpx)
uv run pytest -q            # unit + integration suite
```

End-to-end acceptance requires live Signal + Hermes infrastructure —
see `tests/acceptance/RUNBOOK.md`.

## Project layout

```
trove/            capture hook, tools, schema, db, plugin registration, CLI
trove/dashboard/  Nugget browser backend + static frontend
deploy/           solo/partners profiles + shared skills + setup.sh
tests/            unit, integration, and deploy smoke tests
```

## License

[MIT](LICENSE) © 2026 frankyboots
