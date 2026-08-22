"""Carry scrape history over from the pre-PostgreSQL SQLite file."""

import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("scraper")


class ImportSqliteHistory:
    """Reads a retired ``scraper.db`` and tops up the history table."""

    def __init__(self, history):
        self.history = history

    def execute(self, sqlite_path, dry_run=True):
        """Copy scrape history out of a pre-PostgreSQL ``scraper.db``.

        Versions before 2.0.0 tracked progress in SQLite. Without this the
        switch to PostgreSQL looks like an empty history and the whole board
        gets re-downloaded, which the target site does not deserve.

        Rows already present in PostgreSQL are left as they are.
        """
        sqlite_path = Path(sqlite_path)
        if not sqlite_path.exists():
            raise FileNotFoundError(f"No SQLite database at {sqlite_path}")

        # Read-only URI: this is a legacy file being retired, never written to.
        legacy = sqlite3.connect(f"file:{quote(str(sqlite_path))}?mode=ro", uri=True)
        try:
            rows = legacy.execute("SELECT wr_id, card_id, image_filename FROM scraped_cards").fetchall()
        finally:
            legacy.close()

        if not rows:
            log.info("Nothing to import: %s has no rows.", sqlite_path)
            return 0

        if dry_run:
            existing = self.history.known_wr_ids(str(wr_id) for wr_id, _, _ in rows)
            new = sum(1 for wr_id, _, _ in rows if str(wr_id) not in existing)
            log.info("Dry run: %d of %d rows from %s would be imported.", new, len(rows), sqlite_path)
            return new

        imported = self.history.import_entries(
            [(str(wr_id), card_id, image_filename) for wr_id, card_id, image_filename in rows]
        )

        log.info("Imported %d of %d rows from %s.", imported, len(rows), sqlite_path)
        return imported
