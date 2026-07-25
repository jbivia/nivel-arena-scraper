"""Scrape trading card images from a GnuBoard5 card board.

The target site is served over plaintext HTTP only (it has no TLS listener), so
every response has to be treated as untrusted input: sizes are capped, content
types are checked, and cross-host redirects are refused.
"""

import argparse
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("scraper")

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

# JPEG SOI marker. Guards against an HTML error page served with a 200.
JPEG_MAGIC = b"\xff\xd8\xff"

# Longest filename stem we will write, in UTF-8 bytes (ext4 caps names at 255).
MAX_STEM_BYTES = 100

# Anything outside this set is dropped from a card ID before it becomes a
# filename. \w is Unicode-aware, so Korean card names survive, while path
# separators, dots and control characters cannot.
_UNSAFE_NAME_CHARS = re.compile(r"[^\w-]", re.UNICODE)

DEFAULT_RETRY = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)

# Give up on a board after this many consecutive page failures rather than
# aborting the whole run on the first transient error.
MAX_CONSECUTIVE_PAGE_FAILURES = 3


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


class NivelArenaScraper:
    def __init__(
        self,
        base_url,
        board_id,
        db_path=None,
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

        self.db_path = Path(db_path or os.environ.get("SCRAPER_DB_PATH", "/app/data/scraper.db"))
        self.downloads_dir = Path(downloads_dir or os.environ.get("SCRAPER_DOWNLOADS_DIR", "/app/downloads"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path)
        self._init_db()

        self.session = requests.Session()
        adapter = HTTPAdapter(max_retries=DEFAULT_RETRY)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or os.environ.get(
                    "SCRAPER_USER_AGENT",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            }
        )

        self._robots = self._load_robots() if obey_robots else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        """Explicitly release all held resources."""
        if self._conn:
            self._conn.close()
            self._conn = None
        if self.session:
            self.session.close()
            self.session = None

    # --- robots -----------------------------------------------------------

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

    def _may_fetch(self, url):
        if self._robots is None:
            return True
        agent = self.session.headers.get("User-Agent", "*")
        allowed = self._robots.can_fetch(agent, url)
        if not allowed:
            log.warning("robots.txt disallows %s -- skipping.", url)
        return allowed

    # --- database ---------------------------------------------------------

    def _init_db(self):
        # WAL keeps a reader from blocking the writer if the DB is inspected
        # while a scrape is running.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scraped_cards (
                wr_id TEXT PRIMARY KEY,
                card_id TEXT,
                image_filename TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.commit()
        log.info("Database initialized at %s", self.db_path)

    def is_already_scraped(self, wr_id):
        cursor = self._conn.execute("SELECT 1 FROM scraped_cards WHERE wr_id = ?", (wr_id,))
        return cursor.fetchone() is not None

    def mark_as_scraped(self, wr_id, card_id, image_filename):
        # OR IGNORE so a concurrent run cannot crash the scrape on a PK clash.
        self._conn.execute(
            "INSERT OR IGNORE INTO scraped_cards (wr_id, card_id, image_filename) VALUES (?, ?, ?)",
            (wr_id, card_id, image_filename),
        )
        self._conn.commit()

    # --- http -------------------------------------------------------------

    def get_html(self, url):
        log.info("Fetching list page: %s", url)
        time.sleep(random.uniform(2, 5))  # noqa: S311 - politeness jitter, not a secret
        response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def get_card_details(self, wr_id):
        payload = {"bo_table": self.board_id, "wr_id": wr_id}
        # Per-request headers; session-level headers are merged automatically,
        # so this never mutates shared session state.
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/bbs/board.php?bo_table={self.board_id}",
        }

        log.info("Fetching details for wr_id %s", wr_id)
        response = self.session.post(self.ajax_url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def _reserve_filename(self, filename):
        """Return an unused path for ``filename``, suffixing -01..-99 on clash."""
        filepath = self.downloads_dir / filename
        if not filepath.exists():
            return filepath

        stem, ext = os.path.splitext(filename)
        for i in range(1, 100):
            candidate = self.downloads_dir / f"{stem}-{i:02d}{ext}"
            if not candidate.exists():
                log.info("Duplicate found. Saving %s as %s", filename, candidate.name)
                return candidate

        log.error("No available filename for %s after 99 attempts.", filename)
        return None

    def download_image(self, img_url, filename):
        """Download an image and return the saved filename, or None on failure."""
        if not self._may_fetch(img_url):
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
                with open(tmp_path, "wb") as handle:
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

                os.replace(tmp_path, filepath)
            finally:
                tmp_path.unlink(missing_ok=True)

        time.sleep(1)
        return filepath.name

    # --- orchestration ----------------------------------------------------

    def scrape_board(self, max_pages=None):
        page = 1
        consecutive_failures = 0

        while True:
            if max_pages is not None and page > max_pages:
                break

            list_url = f"{self.base_url}/bbs/board.php?bo_table={self.board_id}&page={page}"
            if not self._may_fetch(list_url):
                break

            try:
                soup = self.get_html(list_url)
            except Exception as exc:
                consecutive_failures += 1
                log.error(
                    "Failed to retrieve page %d (%d/%d consecutive failures): %s",
                    page,
                    consecutive_failures,
                    MAX_CONSECUTIVE_PAGE_FAILURES,
                    exc,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    log.error("Giving up after %d consecutive page failures.", consecutive_failures)
                    break
                page += 1
                continue

            consecutive_failures = 0
            card_links = soup.select("div.gall_img a")
            if not card_links:
                log.info("No more items found on page %d. Board complete.", page)
                break

            for link in card_links:
                parsed = parse_card_link(link.get("href"))
                if parsed is None:
                    log.debug("Skipping unrecognised link: %r", link.get("href"))
                    continue

                img_filename, wr_id = parsed
                if self.is_already_scraped(wr_id):
                    log.info("Skipping wr_id %s - already scraped.", wr_id)
                    continue

                self.scrape_card(wr_id, img_filename)

                delay = random.uniform(self.min_delay, self.max_delay)  # noqa: S311 - politeness jitter
                log.info("Sleeping %.2fs to respect rate limits...", delay)
                time.sleep(delay)

            page += 1

    def scrape_card(self, wr_id, img_filename):
        try:
            detail_soup = self.get_card_details(wr_id)
        except Exception as exc:
            log.error("Failed to retrieve details for wr_id %s: %s", wr_id, exc)
            return

        card_id = f"unknown_{wr_id}"
        type_header = detail_soup.select_one("#type")
        if type_header:
            raw_text = type_header.get_text(strip=True).split("/")[0]
            card_id = safe_stem(raw_text, fallback=f"unknown_{wr_id}")

        # quote() keeps a hostile filename from injecting query/fragment parts
        # into the URL; parse_card_link has already rejected path separators.
        full_src = f"{self.base_url}/data/file/{self.board_id}/{quote(img_filename, safe='')}"

        try:
            actual_filename = self.download_image(full_src, f"{card_id}.jpg")
        except Exception as exc:
            log.error("Failed to download %s: %s", full_src, exc)
            return

        if actual_filename:
            self.mark_as_scraped(wr_id, card_id, actual_filename)

    # --- maintenance ------------------------------------------------------

    def repair_filenames(self, dry_run=True):
        """Repoint DB rows written before the saved-filename fix.

        Early versions recorded the *source* filename instead of the name the
        image was saved under, so those rows point at files that do not exist.
        A row is only repaired when exactly one unclaimed file on disk matches
        its card_id, so ambiguous variants are left alone.
        """
        rows = self._conn.execute("SELECT wr_id, card_id, image_filename FROM scraped_cards").fetchall()

        on_disk = {p.name for p in self.downloads_dir.glob("*.jpg")}
        claimed = {name for _, _, name in rows if name in on_disk}

        repaired, ambiguous, unresolved = 0, 0, 0
        for wr_id, card_id, image_filename in rows:
            if image_filename in on_disk:
                continue

            pattern = re.compile(rf"^{re.escape(card_id)}(-\d{{2}})?\.jpg$")
            candidates = sorted(n for n in on_disk - claimed if pattern.match(n))

            if len(candidates) == 1:
                new_name = candidates[0]
                log.info("Repair wr_id %s: %r -> %r", wr_id, image_filename, new_name)
                if not dry_run:
                    self._conn.execute(
                        "UPDATE scraped_cards SET image_filename = ? WHERE wr_id = ?",
                        (new_name, wr_id),
                    )
                claimed.add(new_name)
                repaired += 1
            elif len(candidates) > 1:
                log.warning("wr_id %s (%s): %d candidates, leaving alone.", wr_id, card_id, len(candidates))
                ambiguous += 1
            else:
                log.warning("wr_id %s (%s): no matching file on disk.", wr_id, card_id)
                unresolved += 1

        if not dry_run:
            self._conn.commit()

        log.info(
            "%s: %d repaired, %d ambiguous, %d unresolved (of %d rows).",
            "Dry run" if dry_run else "Repair complete",
            repaired,
            ambiguous,
            unresolved,
            len(rows),
        )
        return repaired, ambiguous, unresolved


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Scrape trading card images from a GnuBoard5 board.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SCRAPER_BASE_URL", "http://nivelarena.co.kr"),
        help="Board site root (env: SCRAPER_BASE_URL).",
    )
    parser.add_argument(
        "--board-id",
        default=os.environ.get("SCRAPER_BOARD_ID", "cardlists"),
        help="GnuBoard bo_table value (env: SCRAPER_BOARD_ID).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.environ["SCRAPER_MAX_PAGES"]) if os.environ.get("SCRAPER_MAX_PAGES") else None,
        help="Stop after N pages (env: SCRAPER_MAX_PAGES). Default: all pages.",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=_env_float("SCRAPER_MIN_DELAY", 5.0),
        help="Minimum seconds between cards (env: SCRAPER_MIN_DELAY).",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=_env_float("SCRAPER_MAX_DELAY", 10.0),
        help="Maximum seconds between cards (env: SCRAPER_MAX_DELAY).",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Do not fetch or honour robots.txt.",
    )
    parser.add_argument(
        "--repair-filenames",
        action="store_true",
        help="Repoint DB rows at their real on-disk filenames, then exit.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With --repair-filenames, write the changes instead of previewing.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.min_delay > args.max_delay:
        build_arg_parser().error("--min-delay must not exceed --max-delay")

    with NivelArenaScraper(
        args.base_url,
        args.board_id,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        obey_robots=not args.ignore_robots and not args.repair_filenames,
    ) as scraper:
        if args.repair_filenames:
            scraper.repair_filenames(dry_run=not args.apply)
            return 0

        try:
            scraper.scrape_board(max_pages=args.max_pages)
        except KeyboardInterrupt:
            log.warning("Interrupted by user; shutting down cleanly.")
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
