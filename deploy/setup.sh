#!/usr/bin/env bash
# deploy/setup.sh — wire a Trove deploy type (solo|partners) into a Hermes profile.
#
# Trove ships two persona/context profiles plus a set of shared, deploy-agnostic
# skills:
#
#   deploy/solo/      context + skills for a single-operator Trove
#   deploy/partners/ context + skills for a shared multi-person Trove
#   deploy/shared/    skills that apply to every deploy type
#
# This script wires the chosen deploy type's context/ and skills/, together
# with the shared skills, into the Hermes profile so Hermes auto-loads them.
# It is idempotent and prints every action it takes.
#
# Usage:
#   ./deploy/setup.sh solo
#   ./deploy/setup.sh partners
#   ./deploy/setup.sh solo --copy
#   ./deploy/setup.sh partners --mixed
#   HERMES_HOME=/path/to/profile ./deploy/setup.sh partners --copy
#   ./deploy/setup.sh partners --no-dashboard
#   ./deploy/setup.sh partners --profile trove-agent
#
# Dashboard (installed in every mode unless --no-dashboard):
#   <profile>/.env.trove                             <- generated: TROVE_* keys
#                                                        only, copied from the
#                                                        profile's .env at setup
#                                                        time (a snapshot — re-run
#                                                        setup after any TROVE_*
#                                                        change). The profile is
#                                                        detected as: --profile
#                                                        <name> if given, else
#                                                        profiles/<type>,
#                                                        profiles/trove-<type>,
#                                                        the profile whose .env
#                                                        carries TROVE_PEOPLE,
#                                                        or the sole profile.
#   ~/.config/systemd/user/trove-dashboard.service   <- rendered from
#                                                        deploy/systemd/trove-dashboard.service
#                                                        (loopback bind per the
#                                                        repo default; remote
#                                                        access via
#                                                        `ssh -L 9120:127.0.0.1:9120`)
#
# Modes:
#
#   Symlink (default): creates symlinks from the profile back into the Trove
#   repo. Skills, SOUL, and AGENTS files live in the repo, so agent-written
#   "learning" skills are source-controlled alongside the project. Use this
#   when you are iterating on Trove and want the profile to track the repo.
#
#   Copy (--copy): copies files into the Hermes profile. The profile owns the
#   files, so if you version-control your Hermes profile, everything lives in
#   one repo. Use this when the profile is your single source of truth.
#
#   Mixed (--mixed): symlinks AGENTS.md (both profile and repo root) so the
#   repo stays the source of truth for domain context, but copies SOUL.md and
#   skills into the profile so you can customize names, phone numbers, and
#   skill behavior without touching the repo. Use this when the trove-agent
#   runs from the trove project folder and you want per-profile customization.
#
# Symlinks created (default mode):
#   <profile>/SOUL.md                        -> <repo>/deploy/<type>/context/SOUL.md
#   <profile>/AGENTS.md                      -> <repo>/deploy/<type>/context/AGENTS.md
#   <repo-root>/AGENTS.md                    -> <repo>/deploy/<type>/context/AGENTS.md
#   <profile>/skills/trove-enrich            -> <repo>/deploy/<type>/skills/trove-enrich
#   <profile>/skills/trove-daily-digest      -> <repo>/deploy/<type>/skills/trove-daily-digest
#   <profile>/skills/trove-task-association  -> <repo>/deploy/shared/skills/trove-task-association
#
# Files copied (--copy mode):
#   <profile>/SOUL.md                        <- <repo>/deploy/<type>/context/SOUL.md
#   <profile>/AGENTS.md                      <- <repo>/deploy/<type>/context/AGENTS.md
#   <repo-root>/AGENTS.md                    <- <repo>/deploy/<type>/context/AGENTS.md
#   <profile>/skills/trove-enrich/*         <- <repo>/deploy/<type>/skills/trove-enrich/*
#   <profile>/skills/trove-daily-digest/*   <- <repo>/deploy/<type>/skills/trove-daily-digest/*
#   <profile>/skills/trove-task-association/* <- <repo>/deploy/shared/skills/trove-task-association/*
#
# Mixed mode (--mixed):
#   <profile>/SOUL.md                        <- <repo>/deploy/<type>/context/SOUL.md  (copy)
#   <profile>/AGENTS.md                      -> <repo>/deploy/<type>/context/AGENTS.md (link)
#   <repo-root>/AGENTS.md                    -> <repo>/deploy/<type>/context/AGENTS.md (link)
#   <profile>/skills/trove-enrich/*         <- <repo>/deploy/<type>/skills/trove-enrich/* (copy)
#   <profile>/skills/trove-daily-digest/*   <- <repo>/deploy/<type>/skills/trove-daily-digest/* (copy)
#   <profile>/skills/trove-task-association/* <- <repo>/deploy/shared/skills/trove-task-association/* (copy)

set -euo pipefail

# ── Resolve repo root (this script lives in <repo>/deploy/) ──────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Args ──────────────────────────────────────────────────────────────────
DEPLOY_TYPE="${1:-}"
INSTALL_MODE="link"  # default
INSTALL_DASHBOARD=1  # default on; --no-dashboard skips env file + unit
PROFILE_NAME=""      # explicit profile dir name; default = auto-detect

while [[ $# -gt 0 ]]; do
    case "$1" in
        solo|partners) DEPLOY_TYPE="$1"; shift ;;
        --copy)        INSTALL_MODE="copy"; shift ;;
        --mixed)       INSTALL_MODE="mixed"; shift ;;
        --no-dashboard) INSTALL_DASHBOARD=0; shift ;;
        --profile)     PROFILE_NAME="$2"; shift 2 ;;
        *)             echo "Usage: $0 <solo|partners> [--copy|--mixed] [--no-dashboard] [--profile <name>]" >&2; exit 1 ;;
    esac
done

if [[ "$DEPLOY_TYPE" != "solo" && "$DEPLOY_TYPE" != "partners" ]]; then
    echo "Usage: $0 <solo|partners> [--copy|--mixed] [--no-dashboard]" >&2
    exit 1
fi

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEPLOY_DIR="$REPO_ROOT/deploy/$DEPLOY_TYPE"
SHARED_SKILLS_DIR="$REPO_ROOT/deploy/shared/skills"

if [[ ! -d "$DEPLOY_DIR/context" ]]; then
    echo "ERROR: $DEPLOY_DIR/context not found" >&2
    exit 1
fi
if [[ ! -d "$DEPLOY_DIR/skills" ]]; then
    echo "ERROR: $DEPLOY_DIR/skills not found" >&2
    exit 1
fi

# ── Helpers ──────────────────────────────────────────────────────────────

link() {
    # link <target> <link_path>  — force-idempotent symlink
    local target="$1"
    local link_path="$2"
    local parent
    parent="$(dirname "$link_path")"
    mkdir -p "$parent"
    if [[ -L "$link_path" || -e "$link_path" ]]; then
        if [[ -L "$link_path" ]] && [[ "$(readlink -f "$link_path")" == "$(readlink -f "$target")" ]]; then
            echo "  ok   $link_path (already linked)"
            return 0
        fi
        # Back up any existing non-symlink file so we never destroy data.
        local backup="${link_path}.pre-trove-setup.$(date +%Y%m%d-%H%M%S)"
        echo "  backup $link_path -> $backup (was not our symlink)"
        mv "$link_path" "$backup"
    fi
    ln -s "$target" "$link_path"
    echo "  link $link_path -> $target"
}

copy_file() {
    # copy_file <source> <dest>  — idempotent file copy
    local source="$1"
    local dest="$2"
    local parent
    parent="$(dirname "$dest")"
    mkdir -p "$parent"
    if [[ -f "$dest" ]] && cmp -s "$source" "$dest"; then
        echo "  ok   $dest (already up to date)"
        return 0
    fi
    # Back up any existing file so we never destroy data.
    if [[ -f "$dest" ]]; then
        local backup="${dest}.pre-trove-setup.$(date +%Y%m%d-%H%M%S)"
        echo "  backup $dest -> $backup"
        mv "$dest" "$backup"
    fi
    cp "$source" "$dest"
    echo "  copy $dest <- $source"
}

copy_skill() {
    # copy_skill <source_dir> <dest_dir>  — idempotent directory copy for a skill
    local source_dir="${1%/}"  # strip trailing slash
    local dest_dir="${2%/}"
    mkdir -p "$dest_dir"
    local changed=0
    # Copy each file in the source skill directory
    while IFS= read -r -d '' src_file; do
        local rel_path="${src_file#"$source_dir"/}"
        local dest_file="$dest_dir/$rel_path"
        local dest_parent
        dest_parent="$(dirname "$dest_file")"
        mkdir -p "$dest_parent"
        if [[ -f "$dest_file" ]] && cmp -s "$src_file" "$dest_file"; then
            : # already up to date
        else
            if [[ -f "$dest_file" ]]; then
                local backup="${dest_file}.pre-trove-setup.$(date +%Y%m%d-%H%M%S)"
                echo "  backup $dest_file -> $backup"
                mv "$dest_file" "$backup"
            fi
            cp "$src_file" "$dest_file"
            echo "  copy $dest_file <- $src_file"
            changed=1
        fi
    done < <(find "$source_dir" -type f -print0)
    if [[ $changed -eq 0 ]]; then
        echo "  ok   $dest_dir (already up to date)"
    fi
}

echo "Trove deploy: $DEPLOY_TYPE"
echo "Mode:        $INSTALL_MODE"
echo "Repo:         $REPO_ROOT"
echo "Hermes home:  $HERMES_HOME"
echo

# ── Context files (SOUL + AGENTS) ────────────────────────────────────────
echo "Context:"
if [[ "$INSTALL_MODE" == "mixed" ]]; then
    # Mixed: copy SOUL.md, link AGENTS.md
    copy_file "$DEPLOY_DIR/context/SOUL.md" "$HERMES_HOME/SOUL.md"
    link "$DEPLOY_DIR/context/AGENTS.md" "$HERMES_HOME/AGENTS.md"
    link "$DEPLOY_DIR/context/AGENTS.md" "$REPO_ROOT/AGENTS.md"
elif [[ "$INSTALL_MODE" == "link" ]]; then
    link "$DEPLOY_DIR/context/SOUL.md" "$HERMES_HOME/SOUL.md"
    link "$DEPLOY_DIR/context/AGENTS.md" "$HERMES_HOME/AGENTS.md"
    # Hermes loads AGENTS.md from the cwd only; the repo root is the terminal cwd,
    # so link it there too so the domain context actually reaches the agent.
    link "$DEPLOY_DIR/context/AGENTS.md" "$REPO_ROOT/AGENTS.md"
else
    copy_file "$DEPLOY_DIR/context/SOUL.md" "$HERMES_HOME/SOUL.md"
    copy_file "$DEPLOY_DIR/context/AGENTS.md" "$HERMES_HOME/AGENTS.md"
    copy_file "$DEPLOY_DIR/context/AGENTS.md" "$REPO_ROOT/AGENTS.md"
fi

# ── Deploy-specific skills ───────────────────────────────────────────────
echo
echo "Skills ($DEPLOY_TYPE):"
for skill_dir in "$DEPLOY_DIR"/skills/*/; do
    [[ -d "$skill_dir" ]] || continue
    skill_name="$(basename "$skill_dir")"
    if [[ "$INSTALL_MODE" == "link" ]]; then
        link "$skill_dir" "$HERMES_HOME/skills/$skill_name"
    else
        copy_skill "$skill_dir" "$HERMES_HOME/skills/$skill_name"
    fi
done

# ── Shared (deploy-agnostic) skills ───────────────────────────────────────
echo
echo "Skills (shared):"
for skill_dir in "$SHARED_SKILLS_DIR"/*/; do
    [[ -d "$skill_dir" ]] || continue
    skill_name="$(basename "$skill_dir")"
    if [[ "$INSTALL_MODE" == "link" ]]; then
        link "$skill_dir" "$HERMES_HOME/skills/$skill_name"
    else
        copy_skill "$skill_dir" "$HERMES_HOME/skills/$skill_name"
    fi
done

# ── Dashboard: trove-only env file + systemd user unit ────────────────────
if [[ "$INSTALL_DASHBOARD" -eq 1 ]]; then
    echo
    echo "Dashboard:"
    echo "  NOTE: the profile .env is a PII file — read it only as"
    echo "  KEY=VALUE lines on this host; values never leave the machine."

    # ── Identify the profile (the gateway profile that owns the .env) ──
    # Detection chain:
    #   1. --profile <name>            (explicit; must exist)
    #   2. profiles/<type>             (e.g. profiles/solo)
    #   3. profiles/trove-<type>       (e.g. profiles/trove-partners)
    #   4. the profile whose .env carries TROVE_PEOPLE (name-agnostic:
    #      live deploys use `trove-agent` for both deploy types)
    #   5. the sole profile dir
    PROFILES_DIR="$HERMES_HOME/profiles"
    PROFILE=""
    if [[ -n "$PROFILE_NAME" ]]; then
        candidate="$PROFILES_DIR/$PROFILE_NAME"
        if [[ -d "$candidate" ]]; then
            PROFILE="$candidate"
        else
            echo "  warn: --profile $PROFILE_NAME: $candidate does not exist."
        fi
    elif [[ -d "$PROFILES_DIR" ]]; then
        # Candidate 1: the profile dir whose name matches the deploy type.
        for candidate in "$PROFILES_DIR/$DEPLOY_TYPE" "$PROFILES_DIR/trove-$DEPLOY_TYPE"; do
            if [[ -d "$candidate" ]]; then PROFILE="$candidate"; break; fi
        done
        # Candidate 2: the profile whose .env carries TROVE_PEOPLE.
        if [[ -z "$PROFILE" ]]; then
            for profile_dir in "$PROFILES_DIR"/*/; do
                [[ -d "$profile_dir" ]] || continue
                if grep -qE '^TROVE_PEOPLE=' "${profile_dir%/}/.env" 2>/dev/null; then
                    PROFILE="${profile_dir%/}"; break
                fi
            done
        fi
        # Candidate 3: exactly one profile dir exists.
        if [[ -z "$PROFILE" ]]; then
            mapfile -t profile_dirs < <(find "$PROFILES_DIR" -mindepth 1 -maxdepth 1 -type d | sort)
            if [[ ${#profile_dirs[@]} -eq 1 ]]; then PROFILE="${profile_dirs[0]}"; fi
        fi
    fi

    if [[ -z "$PROFILE" ]]; then
        echo "  warn: could not auto-detect a Hermes profile under $PROFILES_DIR"
        echo "        (tried --profile, $DEPLOY_TYPE, trove-$DEPLOY_TYPE,"
        echo "        TROVE_PEOPLE in any profile .env, and a sole profile)."
        echo "        Skipping the dashboard env file + unit. Create the profile"
        echo "        and re-run setup, or pass --profile <name>."
    else
        PROFILE_ENV="$PROFILE/.env"
        DASHBOARD_ENV="$PROFILE/.env.trove"

        # ── Generate the trove-only env file (TROVE_* keys only) ──
        if [[ ! -f "$PROFILE_ENV" ]]; then
            echo "  warn: $PROFILE_ENV not found — nothing to copy TROVE_* keys"
            echo "        from. Creating an empty $DASHBOARD_ENV; add TROVE_*"
            echo "        keys to $PROFILE_ENV, then re-run setup."
            : > "$DASHBOARD_ENV"
            echo "  new  $DASHBOARD_ENV (empty — no $PROFILE_ENV found)"
        else
            # Extract TROVE_* KEY=VALUE lines, write atomically (temp + mv).
            # Back up any pre-existing file so a hand-edited .env.trove is
            # never destroyed (same discipline as link()/copy_file()).
            tmp_env="$(mktemp "${DASHBOARD_ENV}.tmp.XXXXXX")"
            grep -E '^TROVE_[A-Za-z0-9_]*=' "$PROFILE_ENV" | grep -vE '^[A-Za-z0-9_]+=$' > "$tmp_env" || true
            # (Drop empty values: an unset TROVE_PEOPLE means "no people";
            #  an empty line in the unit's EnvironmentFile is harmless but
            #  the key is more meaningful when it is simply absent.)
            if [[ -f "$DASHBOARD_ENV" ]]; then
                backup="${DASHBOARD_ENV}.pre-trove-setup.$(date +%Y%m%d-%H%M%S)"
                echo "  backup $DASHBOARD_ENV -> $backup"
                mv "$DASHBOARD_ENV" "$backup"
            fi
            mv "$tmp_env" "$DASHBOARD_ENV"
            trove_keys="$(grep -cE '^TROVE_[A-Za-z0-9_]*=' "$DASHBOARD_ENV" 2>/dev/null || true)"
            echo "  gen  $DASHBOARD_ENV ($trove_keys TROVE_* keys from $PROFILE_ENV)"
        fi

        # ── Render + install the systemd user unit ──
        UNIT_TEMPLATE="$REPO_ROOT/deploy/systemd/trove-dashboard.service"
        UNIT_PATH="$HOME/.config/systemd/user/trove-dashboard.service"
        if [[ ! -f "$UNIT_TEMPLATE" ]]; then
            echo "  warn: $UNIT_TEMPLATE not found — skipping unit install."
        else
            # trove bin: prefer the repo venv console script (self-contained;
            # the unit has no Environment=PATH), fall back to PATH.
            if [[ -x "$REPO_ROOT/.venv/bin/trove" ]]; then
                TROVE_BIN="$REPO_ROOT/.venv/bin/trove"
            else
                TROVE_BIN="$(command -v trove || echo trove)"
                echo "  warn: no $REPO_ROOT/.venv/bin/trove — using \`trove\` from"
                echo "        PATH ($TROVE_BIN). User services get a minimal"
                echo "        PATH; `uv sync` in the repo if the unit fails."
            fi
            mkdir -p "$HOME/.config/systemd/user"
            rendered="$(mktemp "$HOME/.config/systemd/user/.trove-dashboard.XXXXXX")"
            sed -e "s|__TROVE_BIN__|${TROVE_BIN}|g" \
                -e "s|__ENV_FILE__|${DASHBOARD_ENV}|g" \
                -e "s|__REPO__|${REPO_ROOT}|g" \
                "$UNIT_TEMPLATE" > "$rendered"
            if [[ -f "$UNIT_PATH" ]]; then
                if cmp -s "$rendered" "$UNIT_PATH"; then
                    echo "  ok   $UNIT_PATH (already up to date)"
                else
                    backup="${UNIT_PATH}.pre-trove-setup.$(date +%Y%m%d-%H%M%S)"
                    echo "  backup $UNIT_PATH -> $backup (was not our render)"
                    mv "$UNIT_PATH" "$backup"
                    mv "$rendered" "$UNIT_PATH"
                    echo "  inst $UNIT_PATH (from template)"
                fi
            else
                mv "$rendered" "$UNIT_PATH"
                echo "  inst $UNIT_PATH (from template)"
            fi
            echo "  next: systemctl --user daemon-reload && systemctl --user enable --now trove-dashboard"
            echo "        (remote access: ssh -L 9120:127.0.0.1:9120 <user>@<host>)"
            echo "  NOTE: .env.trove is a SNAPSHOT of the TROVE_* keys — re-run"
            echo "        setup after ANY TROVE_* change to $PROFILE_ENV, then"
            echo "        systemctl --user restart trove-dashboard."
        fi
    fi
fi

echo
echo "Done. Restart Hermes (or /reset) for the new SOUL/AGENTS/skills to load."
