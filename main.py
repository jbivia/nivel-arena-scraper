import os
import time
import random
import sqlite3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import logging
from urllib.parse import urljoin

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# REF-5: Default retry strategy for transient HTTP errors
DEFAULT_RETRY = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)


class NivelArenaScraper:
    def __init__(self, base_url, board_id, db_path=None, downloads_dir=None):
        self.base_url = base_url
        self.board_id = board_id
        self.ajax_url = f"{self.base_url}/skin/board/card_list_new/get_info.php"

        # REF-2: Use environment variables with sensible container/local fallbacks
        self.db_path = db_path or os.environ.get("SCRAPER_DB_PATH", "/app/data/scraper.db")
        self.downloads_dir = downloads_dir or os.environ.get("SCRAPER_DOWNLOADS_DIR", "/app/downloads")

        # Ensure directories exist
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)

        # OPT-1: Persistent DB connection instead of opening one per query
        self._conn = sqlite3.connect(self.db_path)
        self._init_db()

        # REF-5: Session with automatic retry on transient errors
        self.session = requests.Session()
        adapter = HTTPAdapter(max_retries=DEFAULT_RETRY)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Simulate a legitimate browser
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        })

    # REF-7: Context manager support for clean resource lifecycle
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

    def _init_db(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS scraped_cards (
                wr_id TEXT PRIMARY KEY,
                card_id TEXT,
                image_filename TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.commit()
        logging.info("Database initialized.")

    def is_already_scraped(self, wr_id):
        cursor = self._conn.execute("SELECT 1 FROM scraped_cards WHERE wr_id = ?", (wr_id,))
        return cursor.fetchone() is not None

    def mark_as_scraped(self, wr_id, card_id, image_filename):
        self._conn.execute(
            "INSERT INTO scraped_cards (wr_id, card_id, image_filename) VALUES (?, ?, ?)",
            (wr_id, card_id, image_filename)
        )
        self._conn.commit()

    def get_html(self, url):
        logging.info(f"Fetching List Page: {url}")
        # Add a small delay between page requests as well
        time.sleep(random.uniform(2, 5))
        response = self.session.get(url)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')

    def download_image(self, img_url, filename):
        """Download an image and return the final saved filename, or None on failure."""
        filepath = os.path.join(self.downloads_dir, filename)

        # Handle duplicates if the file already exists
        if os.path.exists(filepath):
            name, ext = os.path.splitext(filename)
            found_new_name = False
            for i in range(1, 100):
                new_filename = f"{name}-{i:02d}{ext}"
                new_filepath = os.path.join(self.downloads_dir, new_filename)
                if not os.path.exists(new_filepath):
                    logging.info(f"Duplicate found. Saving {filename} as {new_filename}")
                    filename = new_filename
                    filepath = new_filepath
                    found_new_name = True
                    break

            if not found_new_name:
                logging.error(f"Could not find an available filename for {filename} after 99 attempts.")
                return None

        logging.info(f"Downloading image: {img_url} to {filename}")
        response = self.session.get(img_url, stream=True)
        response.raise_for_status()

        # OPT-2: Use 64KB chunks instead of 8KB to reduce syscall overhead
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=65536):
                f.write(chunk)
        time.sleep(1)  # Delay after download

        # BUG-1: Return the actual saved filename so the caller can track it
        return filename

    def get_card_details(self, wr_id):
        payload = {
            'bo_table': self.board_id,
            'wr_id': wr_id
        }
        # BUG-3: Pass AJAX headers directly to the request instead of mutating
        # session state. Session-level headers (User-Agent, etc.) are merged
        # automatically by requests.
        headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'{self.base_url}/bbs/board.php?bo_table={self.board_id}'
        }

        logging.info(f"Fetching details for wr_id: {wr_id}")
        response = self.session.post(self.ajax_url, data=payload, headers=headers)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')

    def scrape_board(self, max_pages=None):
        page = 1
        while True:
            if max_pages is not None and page > max_pages:
                break

            list_url = f"{self.base_url}/bbs/board.php?bo_table={self.board_id}&page={page}"
            try:
                soup = self.get_html(list_url)
            except Exception as e:
                logging.error(f"Failed to retrieve page {page}: {e}")
                break

            # Find all card links
            card_links = soup.select('div.gall_img a')

            if not card_links:
                logging.warning(f"No more items found on page {page}.")
                break

            for link in card_links:
                href = link.get('href')
                # REF-4: The site's JavaScript encodes card links as
                # "{image_filename}♬{wr_id}" — the ♬ (musical note) character
                # acts as a custom delimiter between the image file and the
                # GnuBoard5 write-ID used by the AJAX detail endpoint.
                if href and '♬' in href:
                    parts = href.split('♬')
                    if len(parts) == 2:
                        img_filename = parts[0]
                        wr_id = parts[1]

                        # Check DB before processing
                        if self.is_already_scraped(wr_id):
                            logging.info(f"Skipping wr_id {wr_id} - already scraped.")
                            continue

                        self.scrape_card(wr_id, img_filename)

                        # Random delay between 5 to 10 seconds
                        delay = random.uniform(5, 10)
                        logging.info(f"Sleeping for {delay:.2f} seconds to respect rate limits...")
                        time.sleep(delay)

            page += 1

    def scrape_card(self, wr_id, img_filename):
        try:
            detail_soup = self.get_card_details(wr_id)
        except Exception as e:
            logging.error(f"Failed to retrieve details for wr_id {wr_id}: {e}")
            return

        # 1. Extract Metadata for Filenaming
        card_id = f"unknown_{wr_id}"
        type_header = detail_soup.select_one('#type')
        if type_header:
            raw_text = type_header.get_text(strip=True)
            card_id = raw_text.split('/')[0].strip()
            # Clean up filename
            card_id = "".join(c for c in card_id if c.isalnum() or c in ('-', '_')).rstrip()

        # 2. Construct High-Res Image URL directly from the list view data
        full_src = f"{self.base_url}/data/file/{self.board_id}/{img_filename}"
        save_filename = f"{card_id}.jpg"
        try:
            # BUG-2: Use the actual saved filename (which may have been
            # deduplicated) when recording to the database.
            actual_filename = self.download_image(full_src, save_filename)
            if actual_filename:
                self.mark_as_scraped(wr_id, card_id, actual_filename)
        except Exception as e:
            logging.error(f"Failed to download {full_src}: {e}")

if __name__ == "__main__":
    BASE_URL = "http://nivelarena.co.kr"
    BOARD_ID = "cardlists"

    # REF-7: Use context manager to ensure clean resource cleanup
    with NivelArenaScraper(BASE_URL, BOARD_ID) as scraper:
        scraper.scrape_board()
