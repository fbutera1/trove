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

for arg in "$@"; do
    case "$arg" in
        solo|partners) DEPLOY_TYPE="$arg" ;;
        --copy)        INSTALL_MODE="copy" ;;
        --mixed)       INSTALL_MODE="mixed" ;;
        *)             echo "Usage: $0 <solo|partners> [--copy|--mixed]" >&2; exit 1 ;;
    esac
done

if [[ "$DEPLOY_TYPE" != "solo" && "$DEPLOY_TYPE" != "partners" ]]; then
    echo "Usage: $0 <solo|partners> [--copy|--mixed]" >&2
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

echo
echo "Done. Restart Hermes (or /reset) for the new SOUL/AGENTS/skills to load."
