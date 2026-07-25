# NivelArena Card Scraper

A Python web scraper that extracts high-resolution trading card images from `nivelarena.co.kr` (a GnuBoard5-based site). Containerized, with a PostgreSQL database tracking progress to prevent redundant downloads.

## Project Overview

- **Purpose**: Automate the collection of trading card images and metadata.
- **Main Technologies**:
  - **Python 3.13**: Core scripting language.
  - **Requests**: HTTP requests and AJAX calls.
  - **BeautifulSoup4**: HTML parsing.
  - **OpenCV** (`opencv-python-headless`): Background removal.
  - **PostgreSQL** (via `psycopg` 3): Tracks scraped cards via `wr_id`, in a `scraped_cards`
    table the scraper creates itself. The server is the one from the sibling
    `nivel-arena-collection-tracker` stack (container `nivel-db`, database `nivel`);
    `compose.yaml` joins that stack's external network instead of declaring its own database.
  - **Podman/Docker Compose**: Containerized execution.
- **Architecture**:
  - The scraper iterates through the board's pagination (`/bbs/board.php?bo_table=…&page=N`).
  - Board links encode `{image_filename}♬{wr_id}` — U+266C is the site's custom delimiter.
  - Card metadata comes from an AJAX endpoint (`/skin/board/card_list_new/get_info.php`).
  - Images are downloaded from `/data/file/{board_id}/{image_filename}` and saved as `{card_id}.jpg`.
  - `convert_to_png.py` flood-fills the white background to transparent, in parallel.

## Building and Running

### Prerequisites
- Podman with `podman compose`

### Using the Makefile (recommended)

| Command | Description |
| --- | --- |
| `make up` | Build and start the scraper |
| `make convert` | Convert downloaded JPGs to transparent PNGs |
| `make down` | Stop the scraper |
| `make logs` | Follow container logs |
| `make purge-db` | Reset scraping history (needs `CONFIRM=yes`) |
| `make import-sqlite` | Import history from the pre-2.0.0 `data/scraper.db` |
| `make help` | List all targets |

### Manual quick start

Without `make`:

```bash
mkdir -p downloads processed
cp .env.example .env    # then set SCRAPER_DATABASE_URL
podman compose up --build          # scrape
podman compose run --rm scraper python convert_to_png.py   # convert
podman compose down
```

Or run directly on the host:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SCRAPER_DATABASE_URL=postgres://nivel:...@localhost:5432/nivel \
SCRAPER_DOWNLOADS_DIR=downloads \
SCRAPER_PROCESSED_DIR=processed \
  .venv/bin/python main.py --max-pages 1
```

## Development

```bash
make venv        # create .venv with dev dependencies
make test-db-up  # throwaway PostgreSQL for the DB tests, on port 55432
make check       # lint + tests + dependency CVE audit
```

Tests live in `tests/` and import `main` / `convert_to_png` directly (`pythonpath = ["."]` in `pyproject.toml`). The OpenCV tests skip automatically if `cv2` is not installed; the database tests skip unless `SCRAPER_TEST_DATABASE_URL` points at a reachable PostgreSQL, and each one runs in its own throwaway schema.

## Configuration

All settings are CLI flags or environment variables — nothing requires editing source. See the table in [README.md](README.md#configuration). Both entry points support `--help`.

## Development Conventions

- **Security**: The target site is HTTP-only, so all responses are treated as untrusted: size caps, content-type and magic-byte validation, cross-host redirect refusal, and filename sanitization. The container runs as non-root with a read-only rootfs and all capabilities dropped.
- **Persistence**: Images in `./downloads`, PNGs in `./processed`, history in the `scraped_cards` PostgreSQL table. The connection is autocommit so a long scrape never holds an idle transaction open against a database the tracker app shares; `repair_filenames` and the SQLite import open explicit transactions.
- **Credentials**: `SCRAPER_DATABASE_URL` is environment-only and deliberately has no CLI flag (`argv` is world-readable via `/proc`). It comes from `.env`, which is gitignored; `redact_conninfo()` strips the password before anything is logged.
- **Error Handling**: All HTTP calls carry timeouts; transient errors (429, 5xx) retry with exponential backoff; a run survives isolated page failures and gives up after `MAX_CONSECUTIVE_PAGE_FAILURES`.
- **Resource Management**: `NivelArenaScraper` is a context manager, closing both the DB connection and the HTTP session.
- **Atomicity**: Downloads stream to a `.part` file and are renamed only on success.
- **Image Conversion**: `ProcessPoolExecutor` with `cv2.setNumThreads(1)` per worker to avoid CPU oversubscription. Already-converted images are skipped unless `--force`.
- **Volume Mounting**: `:Z` labels and `userns_mode: keep-id` handle SELinux and rootless Podman UID mapping on Fedora/RHEL.
