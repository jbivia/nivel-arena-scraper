"""HTTP access to the GnuBoard5 card board, plus the naming rules around it.

The target site is served over plaintext HTTP only (it has no TLS listener), so
every response is treated as untrusted input: sizes are capped, content types
are checked, magic bytes are verified, and a redirect that lands on another
host is refused. Crawl politeness -- robots.txt and jittered delays -- lives
here too, because it is a property of talking to the board rather than of what
the scraper does with what comes back.

This module holds no database state: it fetches, validates and writes files,
and hands parsed soup back to the caller.
"""

import logging
import random
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("scraper.board")

# The site's JavaScript encodes card links as "{image_filename}♬{wr_id}".
# U+266C (beamed sixteenth notes) is the custom delimiter between the image
# file on disk and the GnuBoard5 write-ID used by the AJAX detail endpoint.
HREF_DELIMITER = "♬"

# (connect, read) timeouts. Without these a half-open socket hangs the run
# forever -- urllib3's Retry only covers responses, never a stalled read.
REQUEST_TIMEOUT = (10, 30)

# Hard ceiling on a single download. The largest observed card is ~200 KB;
# 32 MB leaves generous headroom while bounding a hostile/broken response.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 65536

# Same idea for HTML. A board page is ~50 KB and a detail fragment a few KB, so
# 8 MB never refuses a real response while keeping a hostile one from being
# read into memory in full.
MAX_HTML_BYTES = 8 * 1024 * 1024

# Charset out of a Content-Type header, e.g. 'text/html; charset=utf-8'.
_CHARSET_RE = re.compile(r"charset=([\w.:-]+)", re.IGNORECASE)

# Jitter around a list-page fetch. Shorter than the per-card delay: one page
# yields many cards, each of which is already rate-limited on its own.
LIST_PAGE_DELAY = (2.0, 5.0)

# JPEG SOI marker. Guards against an HTML error page served with a 200.
JPEG_MAGIC = b"\xff\xd8\xff"

# Longest filename stem we will write, in UTF-8 bytes (ext4 caps names at 255).
MAX_STEM_BYTES = 100

# Anything outside this set is dropped from a card ID before it becomes a
# filename. \w is Unicode-aware, so Korean card names survive, while path
# separators, dots and control characters cannot.
_UNSAFE_NAME_CHARS = re.compile(r"[^\w-]", re.UNICODE)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

DEFAULT_RETRY = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)


class OffHostRedirect(RuntimeError):
    """Raised when a response came from a host other than the configured board."""


class ResponseTooLarge(RuntimeError):
    """Raised when a response body exceeded its size cap."""


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


class BoardClient:
    """Every request the scraper makes to the board, and every file it writes."""

    def __init__(
        self,
        base_url,
        board_id,
        downloads_dir=None,
        min_delay=5.0,
        max_delay=10.0,
        obey_robots=True,
        user_agent=None,
    ):
        self.session = None

        self.base_url = base_url.rstrip("/")
        self.board_id = board_id
        self.ajax_url = f"{self.base_url}/skin/board/card_list_new/get_info.php"
        self.base_host = urlparse(self.base_url).netloc
        self.min_delay = min_delay
        self.max_delay = max_delay

        self.downloads_dir = Path(downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        adapter = HTTPAdapter(max_retries=DEFAULT_RETRY)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": user_agent or DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            }
        )

        # Behind a cleanup: a constructor that raises never hands back an object
        # to close, so the session it opened would otherwise be stranded.
        try:
            self._robots = self._load_robots() if obey_robots else None
        except BaseException:
            self.close()
            raise

    def close(self):
        """Release the HTTP session. Safe to call more than once."""
        session, self.session = self.session, None
        if session is not None:
            session.close()

    # --- urls -------------------------------------------------------------

    def list_page_url(self, page):
        return f"{self.base_url}/bbs/board.php?bo_table={self.board_id}&page={page}"

    def image_url(self, img_filename):
        # quote() keeps a hostile filename from injecting query/fragment parts
        # into the URL; parse_card_link has already rejected path separators.
        return f"{self.base_url}/data/file/{self.board_id}/{quote(img_filename, safe='')}"

    # --- politeness -------------------------------------------------------

    def _polite_sleep(self, low, high):
        """Pause for a jittered interval so requests are never evenly spaced."""
        delay = random.uniform(low, high)  # noqa: S311 - politeness jitter, not a secret
        log.info("Sleeping %.2fs to respect rate limits...", delay)
        time.sleep(delay)

    def pause_between_cards(self):
        self._polite_sleep(self.min_delay, self.max_delay)

    def _load_robots(self):
        """Fetch robots.txt. A missing file means 'no restrictions'."""
        parser = RobotFileParser()
        robots_url = f"{self.base_url}/robots.txt"
        try:
            response = self.session.get(robots_url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("Could not fetch %s (%s); proceeding without robots rules.", robots_url, exc)
            return None

        if response.status_code >= 400:
            log.info(
                "No robots.txt at %s (HTTP %s); no crawl restrictions.", robots_url, response.status_code
            )
            parser.parse([])
        else:
            parser.parse(response.text.splitlines())
        return parser

    def may_fetch(self, url):
        if self._robots is None:
            return True
        agent = self.session.headers.get("User-Agent", "*")
        allowed = self._robots.can_fetch(agent, url)
        if not allowed:
            log.warning("robots.txt disallows %s -- skipping.", url)
        return allowed

    # --- documents --------------------------------------------------------

    def _fetch_soup(self, method, url, **kwargs):
        """Fetch one HTML document from the board and parse it.

        Streamed and capped rather than read in one go: ``response.text`` pulls
        the whole body into memory before anything can object to its size, and
        over plaintext HTTP the length is whatever the wire says it is. The
        final host is re-checked for the same reason downloads check it --
        a redirect is trivially injected on an unauthenticated hop.
        """
        with self.session.request(method, url, stream=True, timeout=REQUEST_TIMEOUT, **kwargs) as response:
            response.raise_for_status()

            final_host = urlparse(response.url).netloc
            if final_host != self.base_host:
                raise OffHostRedirect(f"{url} redirected off-host to {response.url}")

            body = bytearray()
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                body += chunk
                if len(body) > MAX_HTML_BYTES:
                    raise ResponseTooLarge(f"{url} body exceeded {MAX_HTML_BYTES} bytes")

            declared = _CHARSET_RE.search(response.headers.get("Content-Type", ""))

        # Handed over as bytes: requests decodes an undeclared text/* body as
        # ISO-8859-1 per the HTTP spec, which would mangle the Korean this board
        # is written in. BeautifulSoup reads the meta charset instead.
        return BeautifulSoup(
            bytes(body), "html.parser", from_encoding=declared.group(1) if declared else None
        )

    def get_html(self, url):
        log.info("Fetching list page: %s", url)
        self._polite_sleep(*LIST_PAGE_DELAY)
        return self._fetch_soup("GET", url)

    def get_card_details(self, wr_id):
        payload = {"bo_table": self.board_id, "wr_id": wr_id}
        # Per-request headers; session-level headers are merged automatically,
        # so this never mutates shared session state.
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/bbs/board.php?bo_table={self.board_id}",
        }

        log.info("Fetching details for wr_id %s", wr_id)
        return self._fetch_soup("POST", self.ajax_url, data=payload, headers=headers)

    # --- images -----------------------------------------------------------

    def _reserve_filename(self, filename):
        """Return an unused path for ``filename``, suffixing -01..-99 on clash."""
        filepath = self.downloads_dir / filename
        if not filepath.exists():
            return filepath

        for i in range(1, 100):
            candidate = self.downloads_dir / f"{filepath.stem}-{i:02d}{filepath.suffix}"
            if not candidate.exists():
                log.info("Duplicate found. Saving %s as %s", filename, candidate.name)
                return candidate

        log.error("No available filename for %s after 99 attempts.", filename)
        return None

    def download_image(self, img_url, filename):
        """Download an image and return the saved filename, or None on failure."""
        if not self.may_fetch(img_url):
            return None

        filepath = self._reserve_filename(filename)
        if filepath is None:
            return None

        log.info("Downloading %s -> %s", img_url, filepath.name)
        # Redirects are followed, but the final host must still be the board's:
        # over plaintext HTTP a hop can be injected, and we will not stream an
        # unbounded body from wherever it points.
        with self.session.get(
            img_url, stream=True, timeout=REQUEST_TIMEOUT, allow_redirects=True
        ) as response:
            response.raise_for_status()

            final_host = urlparse(response.url).netloc
            if final_host != self.base_host:
                log.error("Refusing cross-host redirect: %s -> %s", img_url, response.url)
                return None

            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                log.error("Refusing non-image response for %s (Content-Type: %r)", img_url, content_type)
                return None

            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                log.error(
                    "Refusing %s: declared size %s exceeds %d bytes", img_url, declared, MAX_IMAGE_BYTES
                )
                return None

            # Stream to a sibling .part file and rename only on success, so an
            # interrupted download never leaves a truncated .jpg behind that a
            # later run would mistake for a completed one.
            tmp_path = filepath.with_name(filepath.name + ".part")
            written = 0
            try:
                with tmp_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if not chunk:
                            continue
                        if written == 0 and not chunk.startswith(JPEG_MAGIC):
                            log.error("Refusing %s: payload is not a JPEG.", img_url)
                            return None
                        written += len(chunk)
                        if written > MAX_IMAGE_BYTES:
                            log.error("Refusing %s: body exceeded %d bytes.", img_url, MAX_IMAGE_BYTES)
                            return None
                        handle.write(chunk)

                if written == 0:
                    log.error("Refusing %s: empty response body.", img_url)
                    return None

                tmp_path.replace(filepath)
            finally:
                tmp_path.unlink(missing_ok=True)

        time.sleep(1)
        return filepath.name
