"""Smoke test: verify all trove modules import cleanly.

Smoke test: every module must be importable without error.
No fixtures needed (no DB, no Hermes).
"""


def test_imports():
    """All trove modules import without error."""
    import trove
    import trove.schema
    import trove.db
    import trove.plugin
    import trove.capture
    import trove.tools
