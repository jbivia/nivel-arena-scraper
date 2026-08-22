"""The scraper's own record of what it has already downloaded.

Unlike the catalogue, this table is ours: created here, shaped here. It is what
makes a re-run cheap, and cheap matters -- the board is served over plaintext
HTTP by a site that does not deserve to be walked twice.
"""

from nivel.domain.nikke.repository.scrape_history_repository import ScrapeHistoryRepository


class PostgresScrapeHistoryRepository(ScrapeHistoryRepository):
    """Tracks downloads over an autocommit connection."""

    def __init__(self, conn):
        self._conn = conn

    def ensure_schema(self):
        """Create the history table. Safe to call on every startup."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scraped_cards (
                wr_id TEXT PRIMARY KEY,
                card_id TEXT,
                image_filename TEXT,
                scraped_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

    def is_already_scraped(self, wr_id):
        cursor = self._conn.execute("SELECT 1 FROM scraped_cards WHERE wr_id = %s", (wr_id,))
        return cursor.fetchone() is not None

    def mark_as_scraped(self, wr_id, card_id, image_filename):
        # DO NOTHING so a concurrent run cannot crash the scrape on a PK clash.
        self._conn.execute(
            "INSERT INTO scraped_cards (wr_id, card_id, image_filename) VALUES (%s, %s, %s)"
            " ON CONFLICT (wr_id) DO NOTHING",
            (wr_id, card_id, image_filename),
        )

    def all_entries(self):
        return self._conn.execute("SELECT wr_id, card_id, image_filename FROM scraped_cards").fetchall()

    def downloaded_entries(self, limit=None):
        """Every download as ``(wr_id, image_filename)``, oldest write-ID first."""
        query = "SELECT wr_id, image_filename FROM scraped_cards ORDER BY wr_id"
        if limit is not None:
            return self._conn.execute(f"{query} LIMIT %s", (limit,)).fetchall()
        return self._conn.execute(query).fetchall()

    def repoint_filenames(self, updates):
        """Apply ``(image_filename, wr_id)`` pairs in one transaction.

        One transaction for the whole repair: a partially-applied rename pass is
        harder to reason about than one that either lands or does not.
        """
        if not updates:
            return

        with self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.executemany(
                "UPDATE scraped_cards SET image_filename = %s WHERE wr_id = %s",
                updates,
            )

    def known_wr_ids(self, wr_ids):
        """Which of ``wr_ids`` are already recorded."""
        return {
            wr_id
            for (wr_id,) in self._conn.execute(
                "SELECT wr_id FROM scraped_cards WHERE wr_id = ANY(%s)", (list(wr_ids),)
            ).fetchall()
        }

    def import_entries(self, entries):
        """Insert history rows, leaving any write-ID already present alone."""
        with self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO scraped_cards (wr_id, card_id, image_filename) VALUES (%s, %s, %s)"
                " ON CONFLICT (wr_id) DO NOTHING",
                entries,
            )
            return cursor.rowcount
