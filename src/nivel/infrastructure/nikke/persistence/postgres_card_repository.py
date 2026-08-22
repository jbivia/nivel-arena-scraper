"""The catalogue table, which this repository fills but does not own.

``cards`` belongs to nivel-arena-collection-tracker's drizzle migrations. This
class writes it, keyed on ``wr_id``, and never creates or alters it: two
definitions of one table are two definitions that drift, and our own would break
the app's next migration.
"""

import logging

from nivel.domain.nikke.exception.catalogue import CatalogueTableMissing
from nivel.domain.nikke.repository.card_repository import CardRepository

log = logging.getLogger("scraper")

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


class PostgresCardRepository(CardRepository):
    """Upserts catalogue rows over an autocommit connection."""

    def __init__(self, conn):
        self._conn = conn

    def verify_schema(self):
        """Fail early and clearly if the catalogue table is missing or stale.

        Without this the first insert fails deep into a scrape with a bare
        ``UndefinedColumn``, which says nothing about the migration that has not
        been run.
        """
        present = {
            name
            for (name,) in self._conn.execute(
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

    def upsert(self, card):
        """Write one card's catalogue fields, refreshing an existing row.

        DO UPDATE rather than DO NOTHING: the site corrects card text after
        release, and a re-scrape should carry the correction through. The
        filename is coalesced because a metadata backfill knows the row it is
        filling but not necessarily the file it was saved as.

        ``name`` and ``number`` are NOT NULL in the app's schema, so a card
        whose header would not parse is skipped rather than half-written --
        which field is missing is the entity's business, not this method's.

        Lists map to PostgreSQL ``TEXT[]`` directly under psycopg 3; every
        value is bound, none is interpolated.
        """
        missing = card.missing_required_field()
        if missing:
            log.warning("Skipping catalogue row for wr_id %s: no %s parsed.", card.wr_id, missing)
            return False

        self._conn.execute(
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
                card.wr_id,
                card.number,
                card.set_code,
                card.name,
                card.card_type,
                card.card_type_en,
                card.element,
                card.element_en,
                card.cost,
                card.power,
                card.hit,
                card.rarity,
                card.affiliation,
                card.keywords,
                card.effect,
                card.trigger_text,
                card.product_name,
                card.ip,
                card.image_filename,
            ),
        )
        return True

    def wr_ids_without_metadata(self, limit=None):
        """Downloaded cards that never got a catalogue row, oldest write-ID first."""
        query = (
            "SELECT s.wr_id, s.image_filename FROM scraped_cards s"
            " LEFT JOIN cards c USING (wr_id)"
            " WHERE c.wr_id IS NULL ORDER BY s.wr_id"
        )
        if limit is not None:
            return self._conn.execute(f"{query} LIMIT %s", (limit,)).fetchall()
        return self._conn.execute(query).fetchall()
