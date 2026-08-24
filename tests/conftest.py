"""Test configuration for Trove.

Fixtures for DB isolation using tmp_path-based trove.db.
Fixtures are shared across the unit/integration suites.
"""

import pytest
from trove.schema import init_db
from tests.fixtures import FakeMessageEvent


# ── DB fixtures ────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    """Yield a path under tmp_path for a per-test trove.db.

    Never uses ~/.hermes/ — each test gets an isolated DB.
    """
    return tmp_path / "trove.db"


@pytest.fixture
def trove_db(db_path):
    """Yield an open connection to a freshly initialized trove.db.

    Runs init_db(db_path) so every schema test exercises init/migrate
    on a fresh DB. The connection is returned to the caller.
    """
    conn = init_db(db_path)
    yield conn
    conn.close()


# ── Capture fixtures ───────────────────────────────────────────────


@pytest.fixture
def capture_db(db_path, monkeypatch):
    """Set up trove_capture to write to a temp DB.

    Initializes the schema at db_path and monkeypatches
    trove.db.get_trove_db_path() so the capture hook writes to the
    temp DB instead of ~/.hermes/trove.db.

    Also sets TROVE_PEOPLE to the operator's phone AND UUID plus the
    partner's UUID — the two-person partner household (the post-Phase-3
    deploy shape): the derived capture allowlist (TROVE_PEOPLE keys)
    allows both senders, and the derived partner map pairs the two
    people (the explicit TROVE_PARTNERS override is exercised
    deliberately by the tests that need it, T34–T37 and T50). TROVE_CAPTURE_SENDERS
    is cleared so the fixture models the derived path — the explicit
    override is exercised deliberately by the tests that need it (T29,
    T31b, T37, T47). The default FakeMessageEvent source carries the
    UUID (the partner-world sender ID); the phone entry models the
    note-to-self shape.
    """
    import trove.db

    from tests.fixtures import OPERATOR_ID, OPERATOR_UUID, PARTNER_UUID

    init_db(db_path)
    monkeypatch.setattr(trove.db, "get_trove_db_path", lambda: db_path)
    monkeypatch.setenv(
        "TROVE_PEOPLE",
        f"{OPERATOR_ID}:Alex,{OPERATOR_UUID}:Alex,{PARTNER_UUID}:Sam",
    )
    monkeypatch.delenv("TROVE_CAPTURE_SENDERS", raising=False)
    monkeypatch.delenv("SIGNAL_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TROVE_PARTNERS", raising=False)
    return db_path


# ── Enrichment fixtures ────────────────────────────────────────────


@pytest.fixture
def enrich_db(db_path, monkeypatch):
    """Set up nugget_enrich to write to a temp DB.

    Initializes the schema at db_path and monkeypatches
    trove.db.get_trove_db_path() so the enrichment tool writes to the
    temp DB instead of ~/.hermes/trove.db.

    Yields the db_path.
    """
    import trove.db

    init_db(db_path)
    monkeypatch.setattr(trove.db, "get_trove_db_path", lambda: db_path)
    return db_path


# ── Retrieval fixtures ──────────────────────────────────────────────


@pytest.fixture
def search_db(db_path, monkeypatch):
    """Set up nugget_search to read from a temp DB.

    Initializes the schema at db_path and monkeypatches
    trove.db.get_trove_db_path() so the search tool reads from the
    temp DB instead of ~/.hermes/trove.db.

    Yields the db_path.
    """
    import trove.db

    init_db(db_path)
    monkeypatch.setattr(trove.db, "get_trove_db_path", lambda: db_path)
    return db_path


# ── Dashboard fixtures ──────────────────────────────────────────────


@pytest.fixture
def dashboard_db(db_path, monkeypatch):
    """Set up the dashboard FastAPI app against a temp DB.

    Initializes the schema at db_path, monkeypatches
    trove.db.get_trove_db_path(), and yields a FastAPI TestClient
    wrapping create_app(db_path=...). Tests use the real app + real
    SQLite (no mocking of FastAPI).

    Yields:
        Tuple of (TestClient, db_path).
    """
    import trove.db

    init_db(db_path)
    monkeypatch.setattr(trove.db, "get_trove_db_path", lambda: db_path)

    from fastapi.testclient import TestClient
    from trove.dashboard.server import create_app

    client = TestClient(create_app(db_path=str(db_path)))
    return client, db_path

