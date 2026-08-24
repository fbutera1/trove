"""Person-identity parsing for Trove.

`TROVE_PEOPLE` is the single source of truth for person identity: a
comma-separated list of `sender_id:Name` pairs, where `sender_id` is a
raw Signal sender ID (a phone number for note-to-self / solo senders,
a UUID for partner DMs). A person can appear under MULTIPLE identifier
keys — one entry per identifier they are reachable by — repeating the
same display name:

    TROVE_PEOPLE="+155****4567:Alice,<alice-uuid>:Alice,+155****6543:Bob"

Every consumer of person identity (name resolution in `nugget_tasks`
and the dashboard, the derived capture allowlist in `capture`) reads
through `parse_people_env` so the format has exactly one definition.
"""

import os


def parse_people_env() -> dict:
    """Parse the TROVE_PEOPLE env var into a sender_id -> name mapping.

    Format: comma-separated sender_id:name pairs. Malformed pairs (no
    `:` separator, empty key, empty name) are skipped silently so a bad
    entry cannot break capture or name resolution.

    Returns:
        Dict mapping sender_id strings to display name strings.
        Empty dict if TROVE_PEOPLE is unset or empty.
    """
    raw = os.getenv("TROVE_PEOPLE", "")
    if not raw or not raw.strip():
        return {}
    people = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue  # skip malformed pairs silently
        sender_id, name = pair.split(":", 1)
        sender_id = sender_id.strip()
        name = name.strip()
        if sender_id and name:
            people[sender_id] = name
    return people
