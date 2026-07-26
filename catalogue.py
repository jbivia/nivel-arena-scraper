"""PostgreSQL persistence for the scraper.

Two tables are involved, with different owners. ``scraped_cards`` is the
scraper's own -- created here -- and tracks what has been downloaded. ``cards``
is the catalogue the sibling ``nivel-arena-collection-tracker`` app reads, and
its shape belongs to that app's drizzle migrations: this module fills it, keyed
on ``wr_id``, but never creates or alters it.

Every value that reaches SQL is bound as a parameter; no statement is built by
interpolating scraped text. The connection string is read from
``SCRAPER_DATABASE_URL`` and is deliberately not a CLI flag -- argv is
world-readable in ``/proc`` and the URL carries the password.

This module knows nothing about HTTP: it takes a wr_id and a parsed-details
dict and stores them, which is what makes it testable against a throwaway
schema without a network in sight.
"""

import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import psycopg

log = logging.getLogger("scraper.catalogue")

# Environment variable holding the libpq connection URL, e.g.
# postgres://user:password@nivel-db:5432/nivel
DATABASE_URL_ENV = "SCRAPER_DATABASE_URL"

# Seconds to wait for the PostgreSQL TCP connect. Without it a wedged host
# leaves the scraper hanging on startup indefinitely.
DB_CONNECT_TIMEOUT = 10

# Shows up in pg_stat_activity, so a long-running scrape is identifiable from
# the tracker's side of the same database.
DB_APPLICATION_NAME = "nivel-arena-scraper"

# Columns the scraper writes into the tracker app's `cards` table. Checked at
# startup so a database that has not had the app's migrations applied says so,
# instead of failing on the first insert.
CARD_COLUMNS = frozenset(
    {
        "wr_id",
        "number",
        "set_code",
        "name",
        "type",
        "type_en",
        "element",
        "element_en",
        "cost",
        "power",
        "hit",
        "rarity",
        "affiliation",
        "keywords",
        "effect",
        "trigger_text",
        "product_name",
        "ip",
        "image_filename",
    }
)


class DatabaseNotConfigured(RuntimeError):
    """Raised when no PostgreSQL connection string is available."""


class CatalogueTableMissing(RuntimeError):
    """Raised when the app's ``cards`` table is absent or predates this scraper."""


def redact_conninfo(url):
    """Return ``url`` with any password replaced, safe to log.

    Anything unparseable is reduced to a placeholder rather than echoed: a
    malformed URL is exactly the case where the password might land in a
    surprising position.
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return "<unparseable connection string>"

    if not parts.hostname:
        return "<connection string>"

    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        netloc = f"{parts.username}:***@{netloc}" if parts.password else f"{parts.username}@{netloc}"

    return urlunparse((parts.scheme, netloc, parts.path, "", "", ""))


def read_legacy_history(sqlite_path):
    """Read ``(wr_id, card_id, image_filename)`` out of a pre-PostgreSQL scraper.db.

    Versions before 2.0.0 tracked progress in SQLite. Opened through a
    read-only URI: this is a legacy file being retired, never written to.
    """
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"No SQLite database at {sqlite_path}")

    legacy = sqlite3.connect(f"file:{quote(str(sqlite_path))}?mode=ro", uri=True)
    try:
        return legacy.execute("SELECT wr_id, card_id, image_filename FROM scraped_cards").fetchall()
    finally:
        legacy.close()


class CardStore:
    """Every read and write the scraper makes against PostgreSQL."""

    def __init__(self, database_url=None):
        self.connection = None

        self.database_url = database_url or os.environ.get(DATABASE_URL_ENV, "")
        if not self.database_url:
            raise DatabaseNotConfigured(
                f"No PostgreSQL connection configured -- set {DATABASE_URL_ENV}, e.g. "
                f"{DATABASE_URL_ENV}=postgres://user:password@nivel-db:5432/nivel"
            )

        self.connection = psycopg.connect(
            self.database_url,
            connect_timeout=DB_CONNECT_TIMEOUT,
            application_name=DB_APPLICATION_NAME,
        )
        # Autocommit keeps a long scrape from holding an idle transaction open
        # against a database the tracker app is also using; the two multi-row
        # operations open explicit transactions instead.
        self.connection.autocommit = True

        # A schema check that fails must not strand the connection: the caller
        # of a constructor that raised never receives an object to close.
        try:
            self.ensure_schema()
        except BaseException:
            self.close()
            raise

    def close(self):
        """Release the connection. Safe to call more than once."""
        connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()

    # --- schema -----------------------------------------------------------

    def ensure_schema(self):
        """Create the scraper's own table and verify the app's catalogue table."""
        # scraped_cards is ours, created here. `cards` is not: it belongs to the
        # tracker app's drizzle migrations, so it is verified rather than
        # created -- writing our own version of it would leave two definitions
        # to drift apart, and would break the app's next migration.
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scraped_cards (
                wr_id TEXT PRIMARY KEY,
                card_id TEXT,
                image_filename TEXT,
                scraped_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        self._verify_cards_table()
        log.info("Database ready at %s", redact_conninfo(self.database_url))

    def _verify_cards_table(self):
        """Fail early and clearly if the catalogue table is missing or stale.

        Without this the first insert fails deep into a scrape with a bare
        ``UndefinedColumn``, which says nothing about the migration that has not
        been run.
        """
        present = {
            name
            for (name,) in self.connection.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'cards' AND table_schema = current_schema()"
            ).fetchall()
        }

        if not present:
            raise CatalogueTableMissing(
                "No 'cards' table in this database. Its schema belongs to the "
                "nivel-arena-collection-tracker app -- run `make db-migrate` there first."
            )

        missing = sorted(CARD_COLUMNS - present)
        if missing:
            raise CatalogueTableMissing(
                f"The 'cards' table is missing {', '.join(missing)}. It is probably an older "
                "revision -- run `make db-migrate` in nivel-arena-collection-tracker."
            )

    # --- scrape history ---------------------------------------------------

    def is_already_scraped(self, wr_id):
        cursor = self.connection.execute("SELECT 1 FROM scraped_cards WHERE wr_id = %s", (wr_id,))
        return cursor.fetchone() is not None

    def mark_as_scraped(self, wr_id, card_id, image_filename):
        # DO NOTHING so a concurrent run cannot crash the scrape on a PK clash.
        self.connection.execute(
            "INSERT INTO scraped_cards (wr_id, card_id, image_filename) VALUES (%s, %s, %s)"
            " ON CONFLICT (wr_id) DO NOTHING",
            (wr_id, card_id, image_filename),
        )

    def scraped_rows(self):
        """Every history row, as ``(wr_id, card_id, image_filename)``."""
        return self.connection.execute("SELECT wr_id, card_id, image_filename FROM scraped_cards").fetchall()

    def known_wr_ids(self, wr_ids):
        """The subset of ``wr_ids`` already present in the history."""
        return {
            wr_id
            for (wr_id,) in self.connection.execute(
                "SELECT wr_id FROM scraped_cards WHERE wr_id = ANY(%s)",
                ([str(wr_id) for wr_id in wr_ids],),
            ).fetchall()
        }

    def update_filenames(self, updates):
        """Repoint history rows at their real on-disk filenames.

        One transaction for the whole pass: a partially-applied repair is
        harder to reason about than one that either lands or does not.
        ``updates`` is a sequence of ``(image_filename, wr_id)``.
        """
        if not updates:
            return 0

        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.executemany(
                "UPDATE scraped_cards SET image_filename = %s WHERE wr_id = %s",
                updates,
            )
            return cursor.rowcount

    def import_history(self, rows):
        """Insert legacy history rows, leaving any that already exist alone."""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO scraped_cards (wr_id, card_id, image_filename) VALUES (%s, %s, %s)"
                " ON CONFLICT (wr_id) DO NOTHING",
                [(str(wr_id), card_id, image_filename) for wr_id, card_id, image_filename in rows],
            )
            return cursor.rowcount

    # --- catalogue --------------------------------------------------------

    def pending_backfill(self, force=False, limit=None):
        """History rows whose catalogue entry is missing, as ``(wr_id, filename)``.

        With ``force``, every row is returned instead -- which is what to run
        after adding a missing value to ``card_metadata``'s lookup tables.
        """
        if force:
            query = "SELECT wr_id, image_filename FROM scraped_cards ORDER BY wr_id"
        else:
            query = (
                "SELECT s.wr_id, s.image_filename FROM scraped_cards s"
                " LEFT JOIN cards c USING (wr_id)"
                " WHERE c.wr_id IS NULL ORDER BY s.wr_id"
            )

        if limit is not None:
            return self.connection.execute(f"{query} LIMIT %s", (limit,)).fetchall()
        return self.connection.execute(query).fetchall()

    def upsert_card(self, wr_id, details, image_filename=None):
        """Write one card's catalogue fields, refreshing an existing row.

        DO UPDATE rather than DO NOTHING: the site corrects card text after
        release, and a re-scrape should carry the correction through. The
        filename is coalesced because a metadata backfill knows the row it is
        filling but not necessarily the file it was saved as.

        ``name`` and ``number`` are NOT NULL in the app's schema, so a card
        whose header would not parse is skipped rather than half-written.

        Lists map to PostgreSQL ``TEXT[]`` directly under psycopg 3; every
        value is bound, none is interpolated.
        """
        if not details["card_number"] or not details["name"]:
            log.warning(
                "Skipping catalogue row for wr_id %s: no %s parsed.",
                wr_id,
                "card number" if not details["card_number"] else "name",
            )
            return False

        self.connection.execute(
            """
            INSERT INTO cards (
                wr_id, number, set_code, name, type, type_en,
                element, element_en, cost, power, hit, rarity, affiliation,
                keywords, effect, trigger_text, product_name, ip, image_filename
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (wr_id) DO UPDATE SET
                number = EXCLUDED.number,
                set_code = EXCLUDED.set_code,
                name = EXCLUDED.name,
                type = EXCLUDED.type,
                type_en = EXCLUDED.type_en,
                element = EXCLUDED.element,
                element_en = EXCLUDED.element_en,
                cost = EXCLUDED.cost,
                power = EXCLUDED.power,
                hit = EXCLUDED.hit,
                rarity = EXCLUDED.rarity,
                affiliation = EXCLUDED.affiliation,
                keywords = EXCLUDED.keywords,
                effect = EXCLUDED.effect,
                trigger_text = EXCLUDED.trigger_text,
                product_name = EXCLUDED.product_name,
                ip = EXCLUDED.ip,
                image_filename = COALESCE(EXCLUDED.image_filename, cards.image_filename),
                updated_at = now()
            """,
            (
                wr_id,
                details["card_number"],
                details["set_code"],
                details["name"],
                details["card_type"],
                details["card_type_en"],
                details["element"],
                details["element_en"],
                details["cost"],
                details["power"],
                details["hit"],
                details["rarity"],
                details["affiliation"],
                details["keywords"],
                details["effect"],
                details["trigger_text"],
                details["product_name"],
                details["ip"],
                image_filename,
            ),
        )
        return True
