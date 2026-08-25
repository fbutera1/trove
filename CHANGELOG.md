# Changelog

All notable changes to Trove are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Dashboard Tasks view**: the frontend now has a Nuggets view (browse,
  filter, search) and a Tasks view — open tasks ordered by due date with
  horizon (overdue/today/week/month/unscheduled) and assignee filters.
  Backed by a new `GET /api/nuggets/tasks` endpoint that reuses the
  `nugget_tasks` tool query path, so the UI and the agent cannot diverge.
  Cards and the drawer now show the author label plus assignee/due chips
  on tasks.
- **Dashboard deployment as a systemd user service**: `deploy/setup.sh`
  installs a rendered unit (`deploy/systemd/trove-dashboard.service`)
  from the repo, and generates a trove-only env file
  (`<profile>/.env.trove`, `TROVE_*` keys only — no `SIGNAL_*` exposure)
  that the unit loads via `EnvironmentFile`. This is how the live
  dashboard resolves author labels (`TROVE_PEOPLE`) and the DB path
  (`TROVE_DB`). Re-run `setup.sh` after any `TROVE_*` change, then
  `systemctl --user restart trove-dashboard`.
- **Loopback-only dashboard bind in production**: live units now bind
  `127.0.0.1` (the repo default) instead of `0.0.0.0`; remote access is
  via `ssh -L 9120:127.0.0.1:9120 <user>@<host>`.
- **`trove dashboard` loads `.env.trove` on manual runs**: a manual
  (non-systemd) run picks up a `.env.trove` found in the working
  directory or any parent; an explicit process environment variable
  always wins over the file.

## [0.1.0] - 2026-08-24

First public release. Trove is an AI second-brain plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) that captures
every inbound Signal message as a Nugget and never forgets it.

### Added
- **Capture hook** (`pre_gateway_dispatch`): persists every inbound Signal
  message as a Nugget *before* the agent turn runs — the "Trove never forgets"
  ordering invariant. Deterministic, LLM-free, synchronous, never blocks
  dispatch.
- **`TROVE_PEOPLE` as the single source of person identity**: a
  comma-separated `"id:Name"` map with dual-format keys (phone number **and**
  Signal UUID per person, since inbound senders arrive as UUIDs). It drives
  capture, partner routing, and display-name resolution in one place. The
  bot's own number must never be an entry — its absence is the echo-capture
  guard.
- **Capture allowlist derived from `TROVE_PEOPLE`**: a sender is captured if
  and only if their ID is a `TROVE_PEOPLE` key, with an optional **strict**
  `TROVE_CAPTURE_SENDERS` override (when set, it is the *only* capture
  allowlist) and a `SIGNAL_ALLOWED_USERS` fallback; fail-safe deny when no
  allowlist resolves.
- **Partner notification with derived routing**: in a shared multi-person
  deployment, routing pairs are **derived from `TROVE_PEOPLE`** (the
  household model — each person's keys map to the first-listed other
  person's UUID key; a solo `TROVE_PEOPLE` derives no routing at all), with
  an optional **strict** `TROVE_PARTNERS` override. Use UUIDs (not phone
  numbers) for reliable Signal delivery.
- **`TROVE_DB`**: override the default SQLite path (`~/.hermes/trove.db`).
- **`nugget_enrich` tool**: stamps AI enrichment on a Nugget (classification,
  entities, summary, status, confidence, links, due date, assignee);
  idempotent re-enrich with metadata provenance.
- **`nugget_search` tool**: FTS5 keyword search over Nuggets with
  classification/status/source filters.
- **`nugget_tasks` tool**: enumerates open tasks ordered by due date with
  horizon (overdue/today/week/month/unscheduled/all) and assignee filters;
  resolves author/assignee display names via `TROVE_PEOPLE`.
- **`nugget_task_update` tool**: atomically updates an existing task's status,
  due date, and/or assignee by its original `message_id`, optionally linking
  the follow-up/completion message.
- **Dashboard**: a loopback-only Nugget browser (`trove dashboard`,
  default `127.0.0.1:9120`) with a read-only API
  (`/api/nuggets`, `/api/nuggets/search`, `/api/nuggets/{message_id}`) whose
  rows carry an `author_label` resolved via `TROVE_PEOPLE`, plus a static
  frontend.
- **Deployment profiles**: `deploy/setup.sh` wires a `solo` or `partners`
  persona/context profile plus shared skills (`trove-enrich`,
  `trove-daily-digest`, `trove-task-association`) into a Hermes profile via
  symlink (default), copy, or mixed mode.
- **Plugin registration** via the `hermes_agent.plugins` entry-point group;
  enable with `plugins.enabled` in `~/.hermes/config.yaml`.

### Storage
- SQLite (`trove.db`, default `~/.hermes/trove.db`, overridable with
  `TROVE_DB`) in WAL mode with `BEGIN IMMEDIATE` + busy-timeout/retry for
  concurrent writers, FTS5 sync triggers, and version-gated migrations
  through schema v4: v2 adds nullable `due_at` / `assignee`, v3 drops dead
  columns, v4 normalizes composite author values to bare sender IDs.

[Unreleased]: https://github.com/frankyboots/trove/compare/v0.1.0...main
[0.1.0]: https://github.com/frankyboots/trove/releases/tag/v0.1.0
