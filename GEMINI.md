# NivelArena Card Scraper

A robust Python-based web scraper designed to extract high-resolution trading card images from `nivelarena.co.kr` (a GnuBoard5-based site). The project is containerized for easy deployment and includes a SQLite database to track progress and prevent redundant downloads.

## Project Overview

- **Purpose**: Automate the collection of trading card images and metadata.
- **Main Technologies**:
  - **Python 3.13**: Core scripting language.
  - **Requests**: For handling HTTP requests and AJAX calls.
  - **BeautifulSoup4**: For parsing HTML and extracting data.
  - **SQLite3**: Lightweight database for tracking scraped cards via `wr_id`.
  - **Podman/Docker Compose**: Orchestration for containerized execution.
- **Architecture**:
  - The scraper iterates through the board's pagination.
  - For each card, it fetches metadata via an AJAX endpoint (`get_info.php`).
  - High-resolution images are reconstructed from list view data and downloaded.
  - **Makefile**: Orchestrates common tasks (build, run, purge-db).
  - **Podman/Docker Compose**: Orchestration for containerized execution.

  ## Building and Running

  ### Prerequisites
  - Podman
  - `podman compose` (standard in modern Podman versions)

  ### Using the Makefile (Recommended)
  The project includes a `Makefile` to simplify common operations.

  1.  **Start Scraping**:
      ```bash
      make up
      ```
  2.  **Stop Scraper**:
      ```bash
      make down
      ```
  3.  **View Logs**:
      ```bash
      make logs
      ```
  4.  **Purge Database** (Reset history):
      ```bash
      make purge-db
      ```
  5.  **Help**:
      ```bash
      make help
      ```

  ### Manual Quick Start
  If you don't have `make` installed:


### Configuration
- **Base URL & Board ID**: Configurable in `main.py` under the `if __name__ == "__main__":` block.
- **Max Pages**: The `scrape_board(max_pages=X)` call in `main.py` controls how many pages to scrape. If `max_pages` is omitted (default), it will scrape all available pages until no more items are found.
- **Rate Limiting**: Randomized delays (5-10 seconds) are implemented in `main.py` to respect the server's limits.
- **Environment Variables**: Paths are configurable via env vars with container fallbacks:
  - `SCRAPER_DB_PATH` (default: `/app/data/scraper.db`)
  - `SCRAPER_DOWNLOADS_DIR` (default: `/app/downloads`)
  - `SCRAPER_PROCESSED_DIR` (default: `/app/processed`)

## Development Conventions

- **Security**: The container runs as a non-root `scraperuser`.
- **Persistence**: 
  - Images are saved to the `./downloads` directory on the host.
  - Scraping history is stored in `./data/scraper.db`.
- **Error Handling**: Comprehensive logging is used to track fetches, downloads, and any extraction failures. HTTP requests include automatic retry with exponential backoff for transient errors (429, 5xx).
- **Resource Management**: `NivelArenaScraper` implements the context manager protocol (`with` statement) for clean session and database lifecycle management.
- **Image Conversion**: `convert_to_png.py` processes images in parallel using `ProcessPoolExecutor` for faster batch conversion.
- **User Agent**: The scraper simulates a legitimate browser session to avoid basic bot detection.
- **Volume Mounting**: Uses `:Z` labels and `userns_mode: keep-id` in `compose.yaml` to handle permission mapping correctly on SELinux-enabled systems (like Fedora/RHEL) and rootless Podman environments.
