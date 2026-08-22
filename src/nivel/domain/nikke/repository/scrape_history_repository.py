"""Contract for the scraper's own record of what it has already downloaded.

Unlike the catalogue, this table belongs to the scraper: it creates it, owns its
shape, and is free to migrate it. It exists so a re-run skips the several
hundred cards it already has instead of walking the whole board again, against a
site that is served over plaintext HTTP and does not deserve the traffic.
"""

from abc import ABC, abstractmethod


class ScrapeHistoryRepository(ABC):
    """Remembers which write-IDs have been downloaded, and as which file."""

    @abstractmethod
    def ensure_schema(self):
        """Create the history table if it is not there yet."""

    @abstractmethod
    def is_already_scraped(self, wr_id):
        """Whether this write-ID has been downloaded before."""

    @abstractmethod
    def mark_as_scraped(self, wr_id, card_id, image_filename):
        """Record a completed download. A repeat is a no-op, never an error."""

    @abstractmethod
    def all_entries(self):
        """Every ``(wr_id, card_id, image_filename)`` recorded so far."""

    @abstractmethod
    def downloaded_entries(self, limit=None):
        """Every download as ``(wr_id, image_filename)``, oldest write-ID first."""

    @abstractmethod
    def known_wr_ids(self, wr_ids):
        """Which of ``wr_ids`` are already recorded."""

    @abstractmethod
    def repoint_filenames(self, updates):
        """Apply ``(image_filename, wr_id)`` pairs in a single transaction."""

    @abstractmethod
    def import_entries(self, entries):
        """Insert history rows, leaving any write-ID already present alone.

        Returns the number of rows actually inserted.
        """
