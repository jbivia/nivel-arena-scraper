"""The naming rules that turn scraped text into something safe to store.

Both functions are pure and take no I/O, which is what makes them domain rather
than infrastructure: they encode what a card link and a card filename *are*,
independently of the HTTP client that happens to fetch them.

Neither is wrapped in a ``WrId`` or ``CardNumber`` class. Those identifiers
travel as strings all the way into bound SQL parameters and dictionary keys, so
a wrapper would be unwrapped at every boundary and would buy no invariant that
the validation below does not already enforce.
"""

import re
import unicodedata

# The site's JavaScript encodes card links as "{image_filename}♬{wr_id}".
# U+266C (beamed sixteenth notes) is the custom delimiter between the image
# file on disk and the GnuBoard5 write-ID used by the AJAX detail endpoint.
HREF_DELIMITER = "♬"

# Longest filename stem we will write, in UTF-8 bytes (ext4 caps names at 255).
MAX_STEM_BYTES = 100

# Anything outside this set is dropped from a card ID before it becomes a
# filename. \w is Unicode-aware, so Korean card names survive, while path
# separators, dots and control characters cannot.
_UNSAFE_NAME_CHARS = re.compile(r"[^\w-]", re.UNICODE)


def safe_stem(raw, fallback):
    """Turn scraped text into a filename stem that cannot escape its directory.

    Returns ``fallback`` when nothing usable survives sanitisation.
    """
    normalised = unicodedata.normalize("NFKC", raw or "")
    cleaned = _UNSAFE_NAME_CHARS.sub("", normalised).strip("-_")

    # Truncate on a byte boundary so multi-byte names stay valid UTF-8.
    encoded = cleaned.encode("utf-8")[:MAX_STEM_BYTES]
    cleaned = encoded.decode("utf-8", errors="ignore").strip("-_")

    return cleaned or fallback


def parse_card_link(href):
    """Split a board link into ``(image_filename, wr_id)``.

    Returns ``None`` when the href is not a well-formed card link. The image
    filename is validated as a bare name so it cannot walk the remote path.
    """
    if not href or HREF_DELIMITER not in href:
        return None

    parts = href.split(HREF_DELIMITER)
    if len(parts) != 2:
        return None

    img_filename, wr_id = parts[0].strip(), parts[1].strip()
    if not img_filename or not wr_id:
        return None

    # wr_id goes into a POST body and a DB key; the board only ever emits digits.
    if not wr_id.isdigit():
        return None

    # Reject anything that is not a plain filename before it reaches a URL.
    if "/" in img_filename or "\\" in img_filename or img_filename.startswith("."):
        return None

    return img_filename, wr_id
