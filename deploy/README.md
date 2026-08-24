# deploy/ — Trove deployment profiles

Trove ships persona/context profiles plus shared, deploy-agnostic skills.
Each deploy type is a self-contained `context/` + `skills/` set; the shared
skills apply to every deploy type.

```
deploy/
├── setup.sh                # wire a deploy type into a Hermes profile
├── solo/                   # single-operator Trove
│   ├── context/             #   SOUL.md + AGENTS.md
│   └── skills/              #   trove-enrich, trove-daily-digest
├── partners/                # shared multi-person Trove (e.g. a household)
│   ├── context/             #   SOUL.md + AGENTS.md
│   └── skills/              #   trove-enrich, trove-daily-digest
└── shared/
    └── skills/              # deploy-agnostic skills
        └── trove-task-association/   # maintains one task identity across follow-ups
```

## Why a deploy/ layout

Two reasons drove this out of the old flat `context/` + `skills/`:

1. **Solo vs. partners need different context.** The partners deployment
   (a shared household) needs a different SOUL, different entity vocabulary,
   and a different AGENTS domain context than the solo jewelry-supply
   business. One flat `context/` could not serve both.
2. **Agent-written "learning" skills belong in version control.** Hermes
   agents save reusable procedures as skills (e.g. `trove-task-association`,
   written by the running agent after observing the task-completion bug).
   Those skills need to be source-controlled, not just living in
   `~/.hermes/skills/`. `deploy/shared/skills/` is where they live.

## Setup

`deploy/setup.sh` wires a chosen deploy type's context and skills (plus
the shared skills) into a Hermes profile so Hermes auto-loads them. It is
idempotent and backs up any pre-existing file it replaces.

It supports two install modes:

### Symlink mode (default)

Creates symlinks from the profile back into the Trove repo. Files live in
the repo, so agent-written skills are source-controlled alongside the
project and stay in sync automatically. Use this when the Trove repo is
your single source of truth for these files.

```bash
./deploy/setup.sh solo
./deploy/setup.sh partners
HERMES_HOME=/path/to/profile ./deploy/setup.sh partners
```

### Copy mode (`--copy`)

Copies files into the Hermes profile. The profile owns the files, so they
live alongside your other Hermes skills and context. Use this when you
version-control your Hermes profile as a single repo, or when the same
profile is shared across multiple deployments and you want Trove skills
managed there rather than in the Trove checkout.

```bash
./deploy/setup.sh solo --copy
./deploy/setup.sh partners --copy
HERMES_HOME=/path/to/profile ./deploy/setup.sh partners --copy
```

### Mixed mode (`--mixed`)

Symlinks `AGENTS.md` (both profile and repo root) so the repo stays the
source of truth for domain context, but copies `SOUL.md` and skills into
the profile so you can customize names, phone numbers, and skill behavior
without touching the repo. Use this when the trove-agent runs from the
trove project folder and you want per-profile customization.

```bash
./deploy/setup.sh solo --mixed
./deploy/setup.sh partners --mixed
HERMES_HOME=/path/to/profile ./deploy/setup.sh partners --mixed
```

### What gets installed

| Profile path | Symlink mode | Copy mode | Mixed mode |
|---|---|---|---|
| `<profile>/SOUL.md` | → `deploy/<type>/context/SOUL.md` | ← copied | ← copied |
| `<profile>/AGENTS.md` | → `deploy/<type>/context/AGENTS.md` | ← copied | → `deploy/<type>/context/AGENTS.md` |
| `<repo-root>/AGENTS.md` | → `deploy/<type>/context/AGENTS.md` | ← copied | → `deploy/<type>/context/AGENTS.md` |
| `<profile>/skills/trove-enrich` | → `deploy/<type>/skills/trove-enrich` | ← copied | ← copied |
| `<profile>/skills/trove-daily-digest` | → `deploy/<type>/skills/trove-daily-digest` | ← copied | ← copied |
| `<profile>/skills/trove-task-association` | → `deploy/shared/skills/trove-task-association` | ← copied | ← copied |

Hermes loads `AGENTS.md` from the cwd only, so the repo-root link/copy is
what actually delivers the domain context to the agent; the
`<profile>/AGENTS.md` keeps it discoverable from the profile too. Restart
Hermes (or `/reset`) after running setup for the new SOUL/AGENTS/skills to
load.

### Choosing a mode

Both modes let you version-control the files — they just live in different
repos. Pick based on where you want Trove's context and skills to be
managed:

| Choose… | When… |
|---|---|
| **Symlink (default)** | You want Trove skills and context versioned in the Trove repo, and always in sync with the latest commit |
| **`--copy`** | You version-control your Hermes profile (perhaps shared across multiple projects), or you want Trove skills managed alongside your other Hermes skills |
| **`--mixed`** | The trove-agent runs from the trove project folder (so AGENTS.md loads from cwd), but you want to customize SOUL.md and skills per-profile without touching the repo |

With `--copy` and `--mixed`, re-run setup to pull in updated files from the Trove repo.
All modes back up any existing files they replace.

## Person identity: SOUL.md and TROVE_PEOPLE

In multi-person deploys, two surfaces encode the same name↔sender mapping:

- The **SOUL.md person table** (the `Person | Signal sender ID` table at the
  top of `deploy/<type>/context/SOUL.md`) is *prompt context*: it tells the
  agent who is messaging it so it can use names in replies.
- **`TROVE_PEOPLE`** in the profile's `.env` is the *programmatic* source
  of truth: the capture allowlist, partner-notification routing, and
  name-label resolution all resolve through it at runtime.

They serve different consumers and change rarely (only when a person is
added or removed), so both are kept by design — but **keep them in sync**:
when you add or remove a person, update the SOUL.md table and
`TROVE_PEOPLE` in the same change.

Notes:

- `TROVE_PEOPLE` uses dual-format keys (`phone:name,uuid:name`, since
  inbound senders arrive as Signal UUIDs). The phone in the SOUL table is
  only the human-readable form; capture and routing depend on the
  `TROVE_PEOPLE` keys being complete, so that side must always be the
  fuller of the two.
- The bot's own number must never appear in either surface. That
  exclusion is the echo-capture guard: the bot must not capture its own
  notifications and digests as nuggets.
- The repo's `SOUL.md` files are templates with placeholder people/IDs.
  In copy/mixed mode you customize the deployed copy for your own people
  (the repo keeps its placeholders); in symlink mode the template itself
  is the live file, so customize it in the repo.

## Verifying `.env` edits on the live host

When you hand-edit the profile's `.env` (e.g. adding a person to
`TROVE_PEOPLE`), verify the **on-disk file the gateway actually reads** —
never the shell environment:

1. **Re-read the file on disk**, not `os.getenv` from a shell:

   ```bash
   grep TROVE_PEOPLE ~/.hermes/profiles/<profile>/.env
   ```

   The profile env is injected into the *gateway process only*, so a bare
   `python3 -c "import os; print(os.getenv('TROVE_PEOPLE'))"` in a shell
   prints `None` regardless of what the `.env` contains — it proves
   nothing.
2. **Parse-check with the real parser before and after the edit.**
   Before writing, parse the exact new/removed line with the same parser
   the runtime uses (`python-dotenv` / trove's people parser) so a
   malformed line is caught pre-write; after writing, re-parse the whole
   file to confirm the full key set is what you expect.
3. **Do not self-verify in the process that performed the write.**
   Re-read and re-parse from a fresh process (the `grep` above, a new
   `python3` invocation) — a check inside the editing process can only
   report what that process believes it wrote.

This discipline exists because two real incidents (a `.env` edit that
appeared to silently revert, and a corrupted `.env` line that passed
shell-level checks) were both caught only by re-reading the on-disk file
with the real parser.

## trove-task-association

A shared skill born from a real incident: the running agent observed that
completion/update messages were being captured as *separate* resolved tasks
instead of resolving the *original* task (a car-service completion, a
subscription cancellation, and a triplicated pet-grooming task). The agent wrote `trove-task-association` to encode the fix
(search across all statuses, update the original record, classify the
follow-up as a note/fact, link the two). See
`shared/skills/trove-task-association/references/completion-association-incident.md`
for the incident record and regression scenarios.

The skill was the original **behavioral** fix for that incident. The
durable fix is now shipped as the `nugget_task_update` tool (see the skill's
"Tool-design boundary" section), which atomically updates an existing task's
status, due date, and/or assignee by its original `message_id`.
