"""Scrape trading card images and metadata from a GnuBoard5 card board.

This module is the composition root and the CLI: it opens the connection, wires
the repositories to the board client, hands both to the use cases and parses
argv. The pieces themselves live under ``nivel`` -- the rules in
``domain.nikke``, the orchestration in ``application.nikke``, HTTP and SQL in
``infrastructure.nikke``.

Two tables are written. ``scraped_cards`` is the scraper's own and tracks what
has been downloaded. ``cards`` is the catalogue the sibling
``nivel-arena-collection-tracker`` app reads and whose shape its drizzle
migrations own -- this scraper fills it, keyed on ``wr_id``, but never creates
or alters it. The connection string is read from ``SCRAPER_DATABASE_URL`` and is
deliberately not exposed as a CLI flag: argv is world-readable in ``/proc``, and
the URL carries the password.
"""

import argparse
import logging
import os
import sqlite3

import psycopg

from nivel.application.nikke.use_case.backfill_metadata import BackfillMetadata
from nivel.application.nikke.use_case.import_sqlite_history import ImportSqliteHistory
from nivel.application.nikke.use_case.repair_filenames import RepairFilenames
from nivel.application.nikke.use_case.scrape_board import ScrapeBoard
from nivel.domain.nikke.exception.catalogue import CatalogueTableMissing, DatabaseNotConfigured
from nivel.infrastructure.nikke.http.board_client import BoardClient
from nivel.infrastructure.nikke.persistence.postgres_card_repository import PostgresCardRepository
from nivel.infrastructure.nikke.persistence.postgres_scrape_history_repository import (
    PostgresScrapeHistoryRepository,
)
from nivel.infrastructure.persistence.connection import connect, redact_conninfo, resolve_database_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("scraper")


class NivelArenaScraper:
    """Composition root: owns the connection and the board session, wires the rest.

    It exposes its collaborators rather than forwarding to them. ``scraper.scrape``
    walks the board, ``scraper.cards`` is the catalogue, ``scraper.board`` is the
    HTTP client -- so a caller reaches the object that owns the behaviour it
    wants, and this class stays a wiring diagram instead of turning into a
    second copy of every signature underneath it.

    Used as a context manager: the connection it opens is against a database the
    tracker app shares, so it is closed deterministically rather than whenever a
    garbage collector gets to it.
    """

    def __init__(
        self,
        base_url,
        board_id,
        database_url=None,
        downloads_dir=None,
        min_delay=5.0,
        max_delay=10.0,
        obey_robots=True,
        user_agent=None,
    ):
        self.database_url = resolve_database_url(database_url)

        self._conn = connect(self.database_url)
        self.cards = PostgresCardRepository(self._conn)
        self.history = PostgresScrapeHistoryRepository(self._conn)
        self.board = None

        # Anything that can fail goes here, behind a cleanup: the caller of a
        # constructor that raised never receives an object to close, so a
        # failure past this point would otherwise strand the connection open
        # against the database the tracker app is also using.
        try:
            self._init_db()
            self.board = BoardClient(
                base_url,
                board_id,
                downloads_dir=downloads_dir,
                min_delay=min_delay,
                max_delay=max_delay,
                obey_robots=obey_robots,
                user_agent=user_agent,
            )
        except BaseException:
            self.close()
            raise

        self.scrape = ScrapeBoard(self.board, self.cards, self.history)
        self.backfill = BackfillMetadata(self.board, self.cards, self.history)
        self.repair = RepairFilenames(self.board.downloads_dir, self.history)
        self.sqlite_import = ImportSqliteHistory(self.history)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        """Explicitly release all held resources. Safe to call more than once."""
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

        board, self.board = self.board, None
        if board is not None:
            board.close()

    def _init_db(self):
        # Two tables, two owners. The history is ours and its repository creates
        # it; `cards` belongs to the tracker app's drizzle migrations, so it is
        # verified rather than created -- writing our own version of it would
        # leave two definitions to drift apart, and would break the app's next
        # migration.
        self.history.ensure_schema()
        self.cards.verify_schema()
        log.info("Database ready at %s", redact_conninfo(self.database_url))


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name, default):
    """Read an integer setting. A malformed value falls back to ``default``.

    Not a raise: these are read while the parser is being built, where an
    exception surfaces as a traceback with no indication of which variable was
    at fault.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default

    try:
        return int(raw)
    except ValueError:
        log.warning("Ignoring %s=%r: not an integer.", name, raw)
        return default


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Scrape trading card images from a GnuBoard5 board.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SCRAPER_BASE_URL", "http://nivelarena.co.kr"),
        help="Board site root (env: SCRAPER_BASE_URL).",
    )
    parser.add_argument(
        "--board-id",
        default=os.environ.get("SCRAPER_BOARD_ID", "cardlists"),
        help="GnuBoard bo_table value (env: SCRAPER_BOARD_ID).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=_env_int("SCRAPER_MAX_PAGES", None),
        help="Stop after N pages (env: SCRAPER_MAX_PAGES). Default: all pages.",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=_env_float("SCRAPER_MIN_DELAY", 5.0),
        help="Minimum seconds between cards (env: SCRAPER_MIN_DELAY).",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=_env_float("SCRAPER_MAX_DELAY", 10.0),
        help="Maximum seconds between cards (env: SCRAPER_MAX_DELAY).",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Do not fetch or honour robots.txt.",
    )
    parser.add_argument(
        "--repair-filenames",
        action="store_true",
        help="Repoint DB rows at their real on-disk filenames, then exit.",
    )
    parser.add_argument(
        "--import-sqlite",
        metavar="PATH",
        help="Import scrape history from a pre-PostgreSQL scraper.db, then exit.",
    )
    parser.add_argument(
        "--backfill-metadata",
        action="store_true",
        help="Fetch catalogue metadata for cards already downloaded, then exit. No images are re-downloaded.",
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        metavar="N",
        help="With --backfill-metadata, stop after N cards.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --backfill-metadata, refresh rows that already have metadata instead of skipping them.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With --repair-filenames, --import-sqlite or --backfill-metadata, write the changes"
        " instead of previewing.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.min_delay > args.max_delay:
        parser.error("--min-delay must not exceed --max-delay")

    if args.backfill_limit is not None and args.backfill_limit < 1:
        parser.error("--backfill-limit must be at least 1")

    # These maintenance modes never touch the network, so they never need
    # robots.txt. --backfill-metadata is not among them: it makes real requests,
    # and so stays subject to the same crawl rules as a scrape.
    maintenance = args.repair_filenames or args.import_sqlite

    try:
        scraper = NivelArenaScraper(
            args.base_url,
            args.board_id,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            obey_robots=not args.ignore_robots and not maintenance,
        )
    except (DatabaseNotConfigured, CatalogueTableMissing) as exc:
        log.error("%s", exc)
        return 2
    except psycopg.OperationalError as exc:
        # The URL is in the exception text on some failures; keep it out of logs.
        log.error("Could not connect to PostgreSQL: %s", str(exc).strip().splitlines()[0])
        return 2

    with scraper:
        if args.repair_filenames:
            scraper.repair.execute(dry_run=not args.apply)
            return 0

        if args.import_sqlite:
            try:
                scraper.sqlite_import.execute(args.import_sqlite, dry_run=not args.apply)
            except (FileNotFoundError, sqlite3.Error) as exc:
                log.error("Import failed: %s", exc)
                return 1
            return 0

        try:
            if args.backfill_metadata:
                scraper.backfill.execute(
                    dry_run=not args.apply,
                    limit=args.backfill_limit,
                    force=args.force,
                )
            else:
                scraper.scrape.execute(max_pages=args.max_pages)
        except KeyboardInterrupt:
            log.warning("Interrupted by user; shutting down cleanly.")
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
