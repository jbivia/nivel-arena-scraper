"""Turn the board's AJAX card-detail response into catalogue fields.

Everything printed on a card face -- cost, power, hit, effect, rarity and the
rest -- is already served as HTML by ``get_info.php``, so none of it has to be
read off the artwork. See RULES.md for the mapping from the printed layout to
these fields.

The response is a fragment of a GnuBoard5 skin: an ``#subject``/``#type``
header, a two-column ``<td class='h3'>label</td><td>value</td>`` table, and an
``#content`` div holding the effect text. It is hand-written PHP output, so the
parser stays forgiving -- a missing row yields ``None``, never an exception.

This module is deliberately free of I/O: no network, no database. It takes a
parsed soup and returns a dict, which is what makes it testable against the
fixtures in ``tests/fixtures/``.
"""

import copy
import logging
import re

log = logging.getLogger("scraper.metadata")

# The site prints a bare hyphen where a field does not apply -- a skill card has
# no power, an item has no affiliation. It is a null, not a value.
NULL_SENTINEL = "-"

# Row labels, exactly as the skin emits them. Matched against stripped text:
# the 히트 label carries a trailing space in the markup.
LABEL_COST = "코스트"
LABEL_RARITY = "레어도"
LABEL_POWER = "파워"
LABEL_HIT = "히트"
LABEL_AFFILIATION = "소속"
LABEL_KEYWORDS = "키워드"
LABEL_PRODUCT = "제품명"
LABEL_IP = "IP"

# Normalised English alongside the Korean the site prints. An unmapped value
# leaves the ``_en`` column NULL rather than guessing: the raw Korean is always
# stored, so a new card type surfaces as a NULL to fill in, not as lost data.
#
# Both vocabularies were enumerated from a 70-card sample spanning the board's
# five IPs, not guessed. Rarity is deliberately absent: its codes (C, R, SR, UR,
# SPR, SBR, L, SPL, SBL, ANL, P) are already Latin, and the set keeps growing
# with each promo printing.
CARD_TYPE_EN = {
    "유닛": "unit",
    "스킬": "skill",
    "아이템": "item",
    "리더": "leader",
}

ELEMENT_EN = {
    "화염": "fire",
    "번개": "lightning",
    "폭풍": "storm",
    "파도": "wave",
    "대지": "earth",
}

# 'ST08-014' -> 'ST08'. Anchored so a card number the site formats differently
# yields no set code instead of a wrong one.
_SET_CODE_RE = re.compile(r"^([A-Za-z]{1,4}\d{1,3})-\d+")

_INTEGER_RE = re.compile(r"^[+-]?\d+$")

# Horizontal whitespace only: line structure is meaningful in effect text.
_HORIZONTAL_WS_RE = re.compile(r"[^\S\n]+")

# The trigger box repeats its own label as '<b>트리거</b> / ...'.
_TRIGGER_LABEL_RE = re.compile(r"^트리거\s*/\s*")

# Affiliations are slash-separated, keywords comma-separated; accept both
# everywhere rather than depending on which field is being read.
_LIST_SEPARATOR_RE = re.compile(r"[,/]")

# Values already reported as unmapped, so a full backfill logs each unknown
# once instead of once per card.
_warned_values = set()


def _clean_text(raw):
    """Collapse horizontal whitespace and drop blank lines. '' becomes None."""
    if raw is None:
        return None

    # The skin emits non-breaking spaces inside Korean text; they are ordinary
    # word spacing here and would otherwise survive into the database.
    lines = raw.replace("\xa0", " ").replace("\r", "\n").split("\n")
    cleaned = [_HORIZONTAL_WS_RE.sub(" ", line).strip() for line in lines]
    return "\n".join(line for line in cleaned if line) or None


def _text_or_none(value):
    """Normalise a cell value, mapping the site's '-' sentinel to None."""
    cleaned = _clean_text(value)
    return None if cleaned is None or cleaned == NULL_SENTINEL else cleaned


def _int_or_none(value):
    """Parse an integer cell. '-', blanks and anything non-numeric give None.

    Never falls back to 0: a skill card genuinely has no power, and 0 is a
    legitimate cost.
    """
    text = _text_or_none(value)
    if text is None:
        return None

    candidate = text.replace(",", "")
    if not _INTEGER_RE.match(candidate):
        log.debug("Ignoring non-numeric value %r", text)
        return None
    return int(candidate)


def _split_list(value):
    """Split a comma/slash-separated cell into a list. '-' gives []."""
    text = _text_or_none(value)
    if text is None:
        return []

    parts = (part.strip() for part in _LIST_SEPARATOR_RE.split(text))
    return [part for part in parts if part and part != NULL_SENTINEL]


def _normalise(value, mapping, kind):
    """Map a Korean value to its English form, warning once when unmapped."""
    if value is None:
        return None

    english = mapping.get(value)
    if english is None:
        key = (kind, value)
        if key not in _warned_values:
            _warned_values.add(key)
            log.warning(
                "No English mapping for %s %r; storing the Korean value only. Add it to card_metadata.%s.",
                kind,
                value,
                "CARD_TYPE_EN" if kind == "card type" else "ELEMENT_EN",
            )
    return english


def set_code(card_number):
    """'ST08-014' -> 'ST08'. None when the number is missing or unrecognised."""
    if not card_number:
        return None

    match = _SET_CODE_RE.match(card_number)
    return match.group(1) if match else None


def _direct_text(tag):
    """Text belonging to ``tag`` itself, ignoring nested elements.

    The skin opens the header as ``<h2>`` and closes it as ``</h3>``, so a
    lenient parser nests ``#type`` *inside* ``#subject``. Reading only the
    direct strings keeps the card number out of the card name.
    """
    if tag is None:
        return None
    return _clean_text("".join(tag.find_all(string=True, recursive=False)))


def _split_type(soup):
    """Split '#type' into (card_number, card_type, element).

    The header reads 'ST08-014 / 유닛 / 화염', but promo and older cards do not
    always carry all three parts, so each is indexed defensively.
    """
    header = _direct_text(soup.select_one("#type"))
    if header is None:
        return None, None, None

    parts = [part.strip() for part in header.split("/")]
    parts += [None] * (3 - len(parts))
    return (parts[0] or None), (parts[1] or None), (parts[2] or None)


def _labelled_values(soup):
    """Map each ``td.h3`` label to the text of the cell that follows it.

    Rows carry either two label/value pairs (코스트 + 레어도) or one spanning
    pair (소속), and the 효과 heading has no value cell at all -- walking
    siblings handles all three without hard-coding the table geometry.
    """
    values = {}
    for label_cell in soup.select("td.h3"):
        label = label_cell.get_text(strip=True)
        if not label:
            continue
        value_cell = label_cell.find_next_sibling("td")
        values[label] = value_cell.get_text(strip=True) if value_cell else None
    return values


def _effect_and_trigger(soup):
    """Extract (effect, trigger) from the ``#content`` div.

    Keyword icons are images: dropping the tags would silently delete the
    '패시브' or '장착 조건: 이브' that the sentence depends on, so each becomes
    a ``[alt]`` marker. The trigger box is a separate printed block and is
    returned separately rather than run together with the effect.
    """
    content = soup.select_one("#content")
    if content is None:
        return None, None

    # Work on a copy: extracting the trigger box would otherwise mutate the
    # caller's soup, which is also used to read the header fields.
    content = copy.copy(content)

    trigger = None
    trigger_box = content.select_one("p.triger_box")
    if trigger_box is not None:
        trigger_box.extract()
        trigger = _clean_text(trigger_box.get_text(" "))
        if trigger is not None:
            trigger = _TRIGGER_LABEL_RE.sub("", trigger).strip() or None

    for image in content.find_all("img"):
        alt = (image.get("alt") or "").strip()
        image.replace_with(f"[{alt}] " if alt else " ")

    for line_break in content.find_all("br"):
        line_break.replace_with("\n")

    return _clean_text(content.get_text()), trigger


def parse_card_details(soup):
    """Return the catalogue fields carried by one detail response.

    Every key is always present; a field the response omits is None (or an
    empty list), so callers can store the row without checking for absence.
    """
    card_number, card_type, element = _split_type(soup)
    values = _labelled_values(soup)
    effect, trigger = _effect_and_trigger(soup)

    return {
        "card_number": card_number,
        "set_code": set_code(card_number),
        "name": _text_or_none(_direct_text(soup.select_one("#subject"))),
        "card_type": card_type,
        "card_type_en": _normalise(card_type, CARD_TYPE_EN, "card type"),
        "element": element,
        "element_en": _normalise(element, ELEMENT_EN, "element"),
        "cost": _int_or_none(values.get(LABEL_COST)),
        "power": _int_or_none(values.get(LABEL_POWER)),
        "hit": _int_or_none(values.get(LABEL_HIT)),
        "rarity": _text_or_none(values.get(LABEL_RARITY)),
        "affiliation": _split_list(values.get(LABEL_AFFILIATION)),
        "keywords": _split_list(values.get(LABEL_KEYWORDS)),
        "effect": effect,
        "trigger_text": trigger,
        "product_name": _text_or_none(values.get(LABEL_PRODUCT)),
        "ip": _text_or_none(values.get(LABEL_IP)),
    }
