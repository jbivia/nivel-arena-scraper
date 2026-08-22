"""The catalogue card: one printing, as the tracker app reads it.

Identity is ``wr_id``, the GnuBoard5 write-ID. Not the card number: the same
card is reissued at several rarities and each reissue is its own board entry
with its own artwork, so numbers repeat and write-IDs do not.

Field names follow the detail-response parser rather than the database columns
(``card_type`` here, ``type`` in SQL). The parser is the domain's way in; the
column names are a persistence detail and stay in the repository.
"""

from dataclasses import dataclass, field, replace

# Written as `name` and `number` in the app's schema, where both are NOT NULL.
# A card missing either cannot be stored at all, so it is named here rather
# than discovered by a failing INSERT.
REQUIRED_FIELDS = (("number", "card number"), ("name", "name"))


@dataclass(frozen=True, slots=True)
class Card:
    """A parsed card, before or after it has an image on disk.

    Frozen because a scrape never edits a card in place: it either stores what
    it parsed or replaces it with a fresher parse. ``with_image_filename``
    returns a new instance rather than mutating this one.

    Only the fields are annotated; the methods follow the repository's
    convention of docstrings over signatures.
    """

    wr_id: str
    number: str | None = None
    set_code: str | None = None
    name: str | None = None
    card_type: str | None = None
    card_type_en: str | None = None
    element: str | None = None
    element_en: str | None = None
    cost: int | None = None
    power: int | None = None
    hit: int | None = None
    rarity: str | None = None
    # Both land in a PostgreSQL TEXT[], which is NOT NULL DEFAULT '{}' -- the
    # empty list is a real value here, never None.
    affiliation: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    effect: str | None = None
    trigger_text: str | None = None
    product_name: str | None = None
    ip: str | None = None
    image_filename: str | None = None

    @classmethod
    def from_details(cls, wr_id, details, image_filename=None):
        """Build a card from the detail-response parser's dictionary."""
        return cls(
            wr_id=wr_id,
            number=details["card_number"],
            set_code=details["set_code"],
            name=details["name"],
            card_type=details["card_type"],
            card_type_en=details["card_type_en"],
            element=details["element"],
            element_en=details["element_en"],
            cost=details["cost"],
            power=details["power"],
            hit=details["hit"],
            rarity=details["rarity"],
            affiliation=details["affiliation"],
            keywords=details["keywords"],
            effect=details["effect"],
            trigger_text=details["trigger_text"],
            product_name=details["product_name"],
            ip=details["ip"],
            image_filename=image_filename,
        )

    def with_image_filename(self, image_filename):
        """Return a copy carrying the name the artwork was actually saved as."""
        return replace(self, image_filename=image_filename)

    def missing_required_field(self):
        """Name the NOT NULL field this card lacks, or ``None`` if it has both.

        The name is the human-readable one, because the only caller puts it
        straight into the log line that explains why a card was skipped.
        """
        for attribute, label in REQUIRED_FIELDS:
            if not getattr(self, attribute):
                return label
        return None
