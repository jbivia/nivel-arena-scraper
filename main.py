import os
import time
import random
import sqlite3
import requests
from bs4 import BeautifulSoup
import logging
from urllib.parse import urljoin

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class NivelArenaScraper:
    def __init__(self, base_url, board_id, db_path="/app/data/scraper.db", downloads_dir="/app/downloads"):
        self.base_url = base_url
        self.board_id = board_id
        self.ajax_url = f"{self.base_url}/skin/board/card_list_new/get_info.php"
        self.session = requests.Session()
        self.db_path = db_path
        
        # Ensure data directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.init_db()
        
        # Simulate a legitimate browser
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        })
        
        # Define downloads directory
        self.downloads_dir = downloads_dir
        os.makedirs(self.downloads_dir, exist_ok=True)

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scraped_cards (
                    wr_id TEXT PRIMARY KEY,
                    card_id TEXT,
                    image_filename TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
        logging.info("Database initialized.")

    def is_already_scraped(self, wr_id):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT 1 FROM scraped_cards WHERE wr_id = ?", (wr_id,))
            return cursor.fetchone() is not None

    def mark_as_scraped(self, wr_id, card_id, image_filename):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO scraped_cards (wr_id, card_id, image_filename) VALUES (?, ?, ?)",
                (wr_id, card_id, image_filename)
            )

    def get_html(self, url):
        logging.info(f"Fetching List Page: {url}")
        # Add a small delay between page requests as well
        time.sleep(random.uniform(2, 5))
        response = self.session.get(url)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')

    def download_image(self, img_url, filename):
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
                return

        logging.info(f"Downloading image: {img_url} to {filename}")
        response = self.session.get(img_url, stream=True)
        response.raise_for_status()
        
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        time.sleep(1) # Delay after download

    def get_card_details(self, wr_id):
        payload = {
            'bo_table': self.board_id,
            'wr_id': wr_id
        }
        headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'{self.base_url}/bbs/board.php?bo_table={self.board_id}'
        }
        # Update session headers temporarily
        old_headers = dict(self.session.headers)
        self.session.headers.update(headers)
        
        logging.info(f"Fetching details for wr_id: {wr_id}")
        response = self.session.post(self.ajax_url, data=payload)
        
        # Restore headers
        self.session.headers.clear()
        self.session.headers.update(old_headers)
        
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
            self.download_image(full_src, save_filename)
            # Record in DB after successful download
            self.mark_as_scraped(wr_id, card_id, img_filename)
        except Exception as e:
            logging.error(f"Failed to download {full_src}: {e}")

if __name__ == "__main__":
    BASE_URL = "http://nivelarena.co.kr"
    BOARD_ID = "cardlists"
    
    scraper = NivelArenaScraper(BASE_URL, BOARD_ID)
    scraper.scrape_board() 
