"""Contract for the catalogue the tracker app reads.

The scraper fills this table but does not own its shape: ``cards`` belongs to
nivel-arena-collection-tracker's drizzle migrations. ``verify_schema`` is what
turns that split ownership into an error the operator can act on, rather than a
failed INSERT several hundred requests into a run.
"""

from abc import ABC, abstractmethod


class CardRepository(ABC):
    """Stores parsed cards, keyed on their write-ID."""

    @abstractmethod
    def verify_schema(self):
        """Raise ``CatalogueTableMissing`` if the table is absent or stale."""

    @abstractmethod
    def upsert(self, card):
        """Store one card, refreshing an existing row. Returns whether it landed.

        False means the card was skipped for lacking a NOT NULL field, which is
        a normal outcome for an unparseable header, not an error.
        """

    @abstractmethod
    def wr_ids_without_metadata(self, limit=None):
        """Write-IDs that have been downloaded but never had a catalogue row."""
