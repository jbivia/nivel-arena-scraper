"""HTTP access to the GnuBoard5 card board, and the politeness around it.

The target site is served over plaintext HTTP only -- it has no TLS listener --
so every response is treated as untrusted input: sizes are capped, content types
and magic bytes are checked, and a redirect that lands on another host is
refused. Crawl politeness lives here too, because obeying robots.txt and pacing
requests are properties of talking to this board rather than of what the scraper
later does with what comes back.

This module holds no database state: it fetches, validates and writes files, and
hands parsed soup back to the caller.
"""

import logging
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nivel.infrastructure.nikke.http.exception import OffHostRedirect, ResponseTooLarge

log = logging.getLogger("scraper")

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

DEFAULT_RETRY = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


class BoardClient:
    """One board, one session, one downloads directory."""

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
        self.base_url = base_url.rstrip("/")
        self.board_id = board_id
        self.ajax_url = f"{self.base_url}/skin/board/card_list_new/get_info.php"
        self.base_host = urlparse(self.base_url).netloc
        self.min_delay = min_delay
        self.max_delay = max_delay

        self.downloads_dir = Path(downloads_dir or os.environ.get("SCRAPER_DOWNLOADS_DIR", "/app/downloads"))
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        adapter = HTTPAdapter(max_retries=DEFAULT_RETRY)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": user_agent or os.environ.get("SCRAPER_USER_AGENT", DEFAULT_USER_AGENT),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            }
        )

        self.robots = self._load_robots() if obey_robots else None

    def close(self):
        """Release the session. Safe to call more than once."""
        session, self.session = self.session, None
        if session is not None:
            session.close()

    # --- politeness -------------------------------------------------------

    def polite_sleep(self, low, high):
        """Pause for a jittered interval so requests are never evenly spaced."""
        delay = random.uniform(low, high)  # noqa: S311 - politeness jitter, not a secret
        log.info("Sleeping %.2fs to respect rate limits...", delay)
        time.sleep(delay)

    def sleep_between_cards(self):
        """Wait the configured per-card interval."""
        self.polite_sleep(self.min_delay, self.max_delay)

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
        if self.robots is None:
            return True
        agent = self.session.headers.get("User-Agent", "*")
        allowed = self.robots.can_fetch(agent, url)
        if not allowed:
            log.warning("robots.txt disallows %s -- skipping.", url)
        return allowed

    # --- fetching ---------------------------------------------------------

    def fetch_soup(self, method, url, **kwargs):
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

    def list_page_url(self, page):
        return f"{self.base_url}/bbs/board.php?bo_table={self.board_id}&page={page}"

    def image_url(self, img_filename):
        """The board's URL for one card image.

        quote() keeps a hostile filename from injecting query/fragment parts
        into the URL; parse_card_link has already rejected path separators.
        """
        return f"{self.base_url}/data/file/{self.board_id}/{quote(img_filename, safe='')}"

    def get_html(self, url):
        log.info("Fetching list page: %s", url)
        self.polite_sleep(*LIST_PAGE_DELAY)
        return self.fetch_soup("GET", url)

    def get_card_details(self, wr_id):
        payload = {"bo_table": self.board_id, "wr_id": wr_id}
        # Per-request headers; session-level headers are merged automatically,
        # so this never mutates shared session state.
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/bbs/board.php?bo_table={self.board_id}",
        }

        log.info("Fetching details for wr_id %s", wr_id)
        return self.fetch_soup("POST", self.ajax_url, data=payload, headers=headers)

    # --- downloading ------------------------------------------------------

    def reserve_filename(self, filename):
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

        filepath = self.reserve_filename(filename)
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
