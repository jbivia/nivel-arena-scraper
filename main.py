"""Scrape trading card images and metadata from a GnuBoard5 card board.

The target site is served over plaintext HTTP only (it has no TLS listener), so
every response has to be treated as untrusted input: sizes are capped, content
types are checked, and cross-host redirects are refused.

Two tables are written. ``scraped_cards`` is the scraper's own and tracks what
has been downloaded. ``cards`` is the catalogue the sibling
``nivel-arena-collection-tracker`` app reads and whose shape its drizzle
migrations own -- this scraper fills it, keyed on ``wr_id``, but never creates
or alters it. The connection string is read from ``SCRAPER_DATABASE_URL`` and is
deliberately not exposed as a CLI flag: argv is world-readable in ``/proc``, and
the URL carries the password.
"""

import argparse
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import psycopg
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import card_metadata
from nivel.domain.nikke.entity.card import Card
from nivel.domain.nikke.exception.catalogue import CatalogueTableMissing, DatabaseNotConfigured
from nivel.domain.nikke.value_object.card_naming import parse_card_link, safe_stem
from nivel.infrastructure.nikke.persistence.postgres_card_repository import PostgresCardRepository
from nivel.infrastructure.nikke.persistence.postgres_scrape_history_repository import (
    PostgresScrapeHistoryRepository,
)
from nivel.infrastructure.persistence.connection import connect, redact_conninfo, resolve_database_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
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

# Give up on a board after this many consecutive page failures rather than
# aborting the whole run on the first transient error.
MAX_CONSECUTIVE_PAGE_FAILURES = 3


class OffHostRedirect(RuntimeError):
    """Raised when a response came from a host other than the configured board."""


class ResponseTooLarge(RuntimeError):
    """Raised when a response body exceeded its size cap."""


class NivelArenaScraper:
    def __init__(
        self,
        base_url,
        board_id,
        database_url=None,
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

        self.database_url = resolve_database_url(database_url)

        self.downloads_dir = Path(downloads_dir or os.environ.get("SCRAPER_DOWNLOADS_DIR", "/app/downloads"))
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self._conn = connect(self.database_url)
        self._cards = PostgresCardRepository(self._conn)
        self._history = PostgresScrapeHistoryRepository(self._conn)

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

        # Anything that can fail goes here, behind a cleanup: the caller of a
        # constructor that raised never receives an object to close, so a
        # failure past this point would otherwise strand the connection open
        # against the database the tracker app is also using.
        try:
            self._init_db()
            self._robots = self._load_robots() if obey_robots else None
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        """Explicitly release all held resources. Safe to call more than once."""
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

        session, self.session = self.session, None
        if session is not None:
            session.close()

    def _polite_sleep(self, low, high):
        """Pause for a jittered interval so requests are never evenly spaced."""
        delay = random.uniform(low, high)  # noqa: S311 - politeness jitter, not a secret
        log.info("Sleeping %.2fs to respect rate limits...", delay)
        time.sleep(delay)

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
        # Two tables, two owners. The history is ours and its repository creates
        # it; `cards` belongs to the tracker app's drizzle migrations, so it is
        # verified rather than created -- writing our own version of it would
        # leave two definitions to drift apart, and would break the app's next
        # migration.
        self._history.ensure_schema()
        self._cards.verify_schema()
        log.info("Database ready at %s", redact_conninfo(self.database_url))

    def is_already_scraped(self, wr_id):
        return self._history.is_already_scraped(wr_id)

    def mark_as_scraped(self, wr_id, card_id, image_filename):
        self._history.mark_as_scraped(wr_id, card_id, image_filename)

    def upsert_card(self, wr_id, details, image_filename=None):
        """Store one parsed card. Returns whether it landed."""
        return self._cards.upsert(Card.from_details(wr_id, details, image_filename))

    # --- http -------------------------------------------------------------

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

    # --- orchestration ----------------------------------------------------

    def scrape_board(self, max_pages=None):
        # Every card goes through the detail endpoint, so a board walk that is
        # not allowed to call it has nothing to do. Asked once here rather than
        # once per card, as the backfill does.
        if not self._may_fetch(self.ajax_url):
            log.error("robots.txt disallows the detail endpoint %s; nothing to scrape.", self.ajax_url)
            return

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
                self._polite_sleep(self.min_delay, self.max_delay)

            page += 1

    def scrape_card(self, wr_id, img_filename):
        try:
            detail_soup = self.get_card_details(wr_id)
        except Exception as exc:
            log.error("Failed to retrieve details for wr_id %s: %s", wr_id, exc)
            return

        # A card whose metadata will not parse is still worth downloading, so a
        # parse failure degrades to "image only" instead of losing the card.
        details = None
        try:
            details = card_metadata.parse_card_details(detail_soup)
        except Exception as exc:
            log.error("Could not parse metadata for wr_id %s: %s", wr_id, exc)

        card_id = f"unknown_{wr_id}"
        if details and details["card_number"]:
            card_id = safe_stem(details["card_number"], fallback=f"unknown_{wr_id}")

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
            if details:
                self.upsert_card(wr_id, details, actual_filename)

    def backfill_metadata(self, dry_run=True, limit=None, force=False):
        """Fetch catalogue fields for cards already downloaded.

        Images are never re-downloaded: this only re-hits the detail endpoint,
        which is what the scraper does anyway for every card it walks past. The
        connection is autocommit, so an interrupted run keeps the rows it has
        already written and the next run picks up where it stopped.

        Returns ``(processed, failures)``.
        """
        if force:
            rows = self._history.downloaded_entries(limit)
        else:
            rows = self._cards.wr_ids_without_metadata(limit)

        if dry_run:
            # Deliberately makes no requests: a preview that hammered the site
            # for 500 cards would be worse than the operation it previews.
            log.info(
                "Dry run: metadata would be fetched for %d card(s)%s.",
                len(rows),
                " (refreshing rows that already have it)" if force else "",
            )
            return len(rows), 0

        if not rows:
            log.info("Nothing to backfill: every scraped card already has metadata.")
            return 0, 0

        # One robots check for the endpoint, rather than the same question 500
        # times over.
        if not self._may_fetch(self.ajax_url):
            log.error("robots.txt disallows the detail endpoint; nothing to do.")
            return 0, 0

        processed, failures, consecutive_failures = 0, 0, 0
        for index, (wr_id, image_filename) in enumerate(rows):
            if index:
                self._polite_sleep(self.min_delay, self.max_delay)

            try:
                details = card_metadata.parse_card_details(self.get_card_details(wr_id))
            except Exception as exc:
                failures += 1
                consecutive_failures += 1
                log.error(
                    "Failed to fetch metadata for wr_id %s (%d/%d consecutive failures): %s",
                    wr_id,
                    consecutive_failures,
                    MAX_CONSECUTIVE_PAGE_FAILURES,
                    exc,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    log.error("Giving up after %d consecutive failures.", consecutive_failures)
                    break
                continue

            consecutive_failures = 0
            if self.upsert_card(wr_id, details, image_filename):
                processed += 1
                log.info("Stored metadata for wr_id %s (%s).", wr_id, details["card_number"])
            else:
                failures += 1

        log.info("Backfill complete: %d stored, %d failed (of %d rows).", processed, failures, len(rows))
        return processed, failures

    # --- maintenance ------------------------------------------------------

    def repair_filenames(self, dry_run=True):
        """Repoint DB rows written before the saved-filename fix.

        Early versions recorded the *source* filename instead of the name the
        image was saved under, so those rows point at files that do not exist.
        A row is only repaired when exactly one unclaimed file on disk matches
        its card_id, so ambiguous variants are left alone.
        """
        rows = self._history.all_entries()

        on_disk = {p.name for p in self.downloads_dir.glob("*.jpg")}
        claimed = {name for _, _, name in rows if name in on_disk}

        repaired, ambiguous, unresolved = 0, 0, 0
        updates = []
        for wr_id, card_id, image_filename in rows:
            if image_filename in on_disk:
                continue

            pattern = re.compile(rf"^{re.escape(card_id)}(-\d{{2}})?\.jpg$")
            candidates = sorted(n for n in on_disk - claimed if pattern.match(n))

            if len(candidates) == 1:
                new_name = candidates[0]
                log.info("Repair wr_id %s: %r -> %r", wr_id, image_filename, new_name)
                updates.append((new_name, wr_id))
                claimed.add(new_name)
                repaired += 1
            elif len(candidates) > 1:
                log.warning("wr_id %s (%s): %d candidates, leaving alone.", wr_id, card_id, len(candidates))
                ambiguous += 1
            else:
                log.warning("wr_id %s (%s): no matching file on disk.", wr_id, card_id)
                unresolved += 1

        if not dry_run:
            self._history.repoint_filenames(updates)

        log.info(
            "%s: %d repaired, %d ambiguous, %d unresolved (of %d rows).",
            "Dry run" if dry_run else "Repair complete",
            repaired,
            ambiguous,
            unresolved,
            len(rows),
        )
        return repaired, ambiguous, unresolved

    def import_sqlite_history(self, sqlite_path, dry_run=True):
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
            existing = self._history.known_wr_ids(str(wr_id) for wr_id, _, _ in rows)
            new = sum(1 for wr_id, _, _ in rows if str(wr_id) not in existing)
            log.info("Dry run: %d of %d rows from %s would be imported.", new, len(rows), sqlite_path)
            return new

        imported = self._history.import_entries(
            [(str(wr_id), card_id, image_filename) for wr_id, card_id, image_filename in rows]
        )

        log.info("Imported %d of %d rows from %s.", imported, len(rows), sqlite_path)
        return imported


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name, default):
    """Read an integer setting. A malformed value falls back to ``default``.

    Not a raise: these are read while the parser is being built, where an
    exception surfaces as a traceback with no indication of which variable was
    at fault.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default

    try:
        return int(raw)
    except ValueError:
        log.warning("Ignoring %s=%r: not an integer.", name, raw)
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
        default=_env_int("SCRAPER_MAX_PAGES", None),
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
        "--import-sqlite",
        metavar="PATH",
        help="Import scrape history from a pre-PostgreSQL scraper.db, then exit.",
    )
    parser.add_argument(
        "--backfill-metadata",
        action="store_true",
        help="Fetch catalogue metadata for cards already downloaded, then exit. No images are re-downloaded.",
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        metavar="N",
        help="With --backfill-metadata, stop after N cards.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --backfill-metadata, refresh rows that already have metadata instead of skipping them.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With --repair-filenames, --import-sqlite or --backfill-metadata, write the changes"
        " instead of previewing.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.min_delay > args.max_delay:
        parser.error("--min-delay must not exceed --max-delay")

    if args.backfill_limit is not None and args.backfill_limit < 1:
        parser.error("--backfill-limit must be at least 1")

    # These maintenance modes never touch the network, so they never need
    # robots.txt. --backfill-metadata is not among them: it makes real requests,
    # and so stays subject to the same crawl rules as a scrape.
    maintenance = args.repair_filenames or args.import_sqlite

    try:
        scraper = NivelArenaScraper(
            args.base_url,
            args.board_id,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            obey_robots=not args.ignore_robots and not maintenance,
        )
    except (DatabaseNotConfigured, CatalogueTableMissing) as exc:
        log.error("%s", exc)
        return 2
    except psycopg.OperationalError as exc:
        # The URL is in the exception text on some failures; keep it out of logs.
        log.error("Could not connect to PostgreSQL: %s", str(exc).strip().splitlines()[0])
        return 2

    with scraper:
        if args.repair_filenames:
            scraper.repair_filenames(dry_run=not args.apply)
            return 0

        if args.import_sqlite:
            try:
                scraper.import_sqlite_history(args.import_sqlite, dry_run=not args.apply)
            except (FileNotFoundError, sqlite3.Error) as exc:
                log.error("Import failed: %s", exc)
                return 1
            return 0

        try:
            if args.backfill_metadata:
                scraper.backfill_metadata(
                    dry_run=not args.apply,
                    limit=args.backfill_limit,
                    force=args.force,
                )
            else:
                scraper.scrape_board(max_pages=args.max_pages)
        except KeyboardInterrupt:
            log.warning("Interrupted by user; shutting down cleanly.")
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
