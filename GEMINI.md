# NivelArena Card Scraper

A Python web scraper that extracts high-resolution trading card images from `nivelarena.co.kr` (a GnuBoard5-based site). Containerized, with a PostgreSQL database tracking progress to prevent redundant downloads.

## Project Overview

- **Purpose**: Automate the collection of trading card images and metadata.
- **Main Technologies**:
  - **Python 3.12+** (3.14 in the container image): Core scripting language. The floor is the
    lowest version on which every dependency can be held at its newest release.
  - **Requests**: HTTP requests and AJAX calls.
  - **BeautifulSoup4**: HTML parsing.
  - **OpenCV** (`opencv-python-headless`): Background removal.
  - **PostgreSQL** (via `psycopg` 3): Tracks scraped cards via `wr_id` in a `scraped_cards`
    table it creates itself, and fills the `cards` catalogue table, which belongs to the
    tracker app's drizzle migrations — the scraper upserts on `wr_id` and verifies the
    table's columns at startup rather than creating it. The server is the one from the sibling
    `nivel-arena-collection-tracker` stack (container `nivel-db`, database `nivel`);
    `compose.yaml` joins that stack's external network instead of declaring its own database.
  - **Podman/Docker Compose**: Containerized execution.
- **Architecture**:
  - The scraper iterates through the board's pagination (`/bbs/board.php?bo_table=…&page=N`).
  - Board links encode `{image_filename}♬{wr_id}` — U+266C is the site's custom delimiter.
  - Card metadata comes from an AJAX endpoint (`/skin/board/card_list_new/get_info.php`), which
    serves every printed field as text; `card_metadata.py` parses it into the `cards` table. No OCR
    is involved — see `RULES.md` for the field mapping and the site's markup quirks.
  - Images are downloaded from `/data/file/{board_id}/{image_filename}` and saved as `{card_id}.jpg`.
  - `convert_to_png.py` masks the white background to transparent, in parallel. It finds the
    card rectangles by projecting a background mask onto each axis (which separates the outer
    margins and, on two-card composites such as SB02-001, the gutter between them), then cuts
    an antialiased rounded-rectangle alpha. Geometric rather than a colour flood fill, which
    escaped through light artwork and through the gutter.

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
| `make backfill-metadata` | Preview fetching metadata for cards already downloaded |
| `make backfill-metadata-apply` | Fetch that metadata (images are not re-downloaded) |
| `make purge-db` | Reset scraping history — leaves `cards` alone (needs `CONFIRM=yes`) |
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
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock
SCRAPER_DATABASE_URL=postgres://nivel:...@localhost:5432/nivel \
SCRAPER_DOWNLOADS_DIR=downloads \
SCRAPER_PROCESSED_DIR=processed \
  .venv/bin/python main.py --max-pages 1
```

## Development

```bash
make venv        # create .venv from the hash-pinned dev lock
make test-db-up  # throwaway PostgreSQL for the DB tests, on port 55432
make check       # lint + tests + lock drift check + dependency CVE audit
make lock        # recompile the locks after editing requirements*.txt
```

Tests live in `tests/` and import `main` / `convert_to_png` directly (`pythonpath = ["."]` in `pyproject.toml`). The OpenCV tests skip automatically if `cv2` is not installed; the database tests skip unless `SCRAPER_TEST_DATABASE_URL` points at a reachable PostgreSQL, and each one runs in its own throwaway schema.

## Configuration

All settings are CLI flags or environment variables — nothing requires editing source. See the table in [README.md](README.md#configuration). Both entry points support `--help`.

## Development Conventions

- **Security**: The target site is HTTP-only, so all responses are treated as untrusted: size caps (images *and* HTML), content-type and magic-byte validation, cross-host redirect refusal on every request, and filename sanitization. The container runs as non-root with a read-only rootfs and all capabilities dropped.
- **Dependencies**: `requirements*.txt` hold the direct pins and are what you edit; `requirements*.lock` are the compiled, fully-resolved, SHA-256-pinned trees that the container, CI and `make venv` install from with `pip install --require-hashes`. Run `make lock` after any dependency change — CI fails on lock drift, on an unhashed pin, and on a known CVE in either tree.
- **Persistence**: Images in `./downloads`, PNGs in `./processed`, history in the `scraped_cards` PostgreSQL table and card fields in `cards` (owned by the tracker app's migrations; run `make db-migrate` there before the first scrape). The connection is autocommit so a long scrape never holds an idle transaction open against a database the tracker app shares; `repair_filenames` and the SQLite import open explicit transactions.
- **Credentials**: `SCRAPER_DATABASE_URL` is environment-only and deliberately has no CLI flag (`argv` is world-readable via `/proc`). It comes from `.env`, which is gitignored; `redact_conninfo()` strips the password before anything is logged.
- **Error Handling**: All HTTP calls carry timeouts; transient errors (429, 5xx) retry with exponential backoff; a run survives isolated page failures and gives up after `MAX_CONSECUTIVE_PAGE_FAILURES`.
- **Resource Management**: `NivelArenaScraper` is a context manager, closing both the DB connection and the HTTP session.
- **Atomicity**: Downloads stream to a `.part` file and are renamed only on success.
- **Image Conversion**: `ProcessPoolExecutor` with `cv2.setNumThreads(1)` per worker to avoid CPU oversubscription. Already-converted images are skipped unless `--force`.
- **Volume Mounting**: `:Z` labels and `userns_mode: keep-id` handle SELinux and rootless Podman UID mapping on Fedora/RHEL.
