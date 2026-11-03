"""CLI: ``python -m rackscout migrate``, ``collect`` and ``refresh-worker``."""
from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from . import collector, db, refresh
from .config import ConfigError, load_settings

log = logging.getLogger("rackscout")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rackscout")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply schema migrations and sync cited reference data")
    collect = sub.add_parser("collect", help="run one discovery + bounded enrichment collection")
    collect.add_argument("--max-details", type=int, default=None, help="detail checks this run (default RACKSCOUT_MAX_DETAILS)")
    collect.add_argument("--no-browser", action="store_true", help="skip Playwright eBay page checks")
    sub.add_parser("refresh-worker", help="poll for manual refresh requests and run them under the collector lock")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    if args.command == "migrate":
        try:
            conn = db.wait_for_connection(settings.migrate_wait_seconds, settings.db_connect_timeout)
        except db.DatabaseUnavailable:
            log.error("database unavailable after %ss", settings.migrate_wait_seconds)
            return 1
        try:
            applied = db.migrate(conn)
        except psycopg.Error as exc:
            detail = getattr(getattr(exc, "diag", None), "message_primary", None)
            log.error("migration failed: %s%s", type(exc).__name__, f" ({detail})" if detail else "")
            return 1
        except db.ReferenceDataError as exc:
            log.error("migration failed: %s", exc)
            return 1
        finally:
            conn.close()
        log.info("migrations applied: %s; reference data synced", applied or "none pending")
        return 0

    if args.command == "refresh-worker":
        return refresh.run_worker(settings)

    if args.max_details is not None and not 0 <= args.max_details <= 500:
        log.error("--max-details must be between 0 and 500")
        return 2
    try:
        return collector.collect(settings, max_details=args.max_details, browser=not args.no_browser)
    except db.DatabaseUnavailable:
        log.error("database unavailable")
        return 1
    except psycopg.Error as exc:
        log.error("database error: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
