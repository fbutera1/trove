"""Trove CLI.

Provides the `trove` console script with a `dashboard` subcommand
that launches the Nugget browser on loopback port 9120.

Usage:
    trove dashboard
    trove dashboard --host 127.0.0.1 --port 9120
    trove dashboard --db-path /path/to/trove.db
"""

import argparse
import logging
import sys


def main(argv=None) -> int:
    """Entry point for the `trove` console script.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 on success).
    """
    parser = argparse.ArgumentParser(
        prog="trove",
        description="Trove — AI second brain for Hermes Agent",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── dashboard subcommand ────────────────────────────────────────

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Launch the Nugget browser dashboard",
    )
    dashboard_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1, loopback only)",
    )
    dashboard_parser.add_argument(
        "--port",
        type=int,
        default=9120,
        help="Bind port (default: 9120)",
    )
    dashboard_parser.add_argument(
        "--db-path",
        default=None,
        help="Path to trove.db (default: ~/.hermes/trove.db)",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "dashboard":
        return _run_dashboard(args)

    return 0


def _run_dashboard(args) -> int:
    """Start the dashboard server.

    Loads the trove-only env file (``.env.trove``) first, when present:
    the dashboard service runs with no Hermes env wiring, so
    ``TROVE_PEOPLE`` / ``TROVE_DB`` arrive via the systemd unit's
    ``EnvironmentFile`` in production and via this loader on a manual
    ``trove dashboard`` run. Explicit process env always wins over the
    file; a missing file is a no-op.
    """
    from trove.db import get_trove_db_path
    from trove.env import load_trove_env_file

    loaded = load_trove_env_file()
    if loaded is not None:
        logger = logging.getLogger("trove.cli")
        logger.info("Loaded trove env from %s", loaded)

    db_path = args.db_path if args.db_path else get_trove_db_path()

    logger = logging.getLogger("trove.cli")
    logger.info("Trove Nugget browser on http://%s:%s", args.host, args.port)

    from trove.dashboard.server import run

    run(host=args.host, port=args.port, db_path=db_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
