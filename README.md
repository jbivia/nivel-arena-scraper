# NivelArena Card Scraper

A containerized Python scraper that collects high-resolution trading card images from [nivelarena.co.kr](http://nivelarena.co.kr), tracks progress in PostgreSQL to avoid redundant downloads, and post-processes the results into transparent PNGs.

## Features

- **Automated scraping**: Walks the board's pagination, resolves each card through the site's AJAX detail endpoint, and downloads the full-resolution image.
- **Card metadata**: The same detail response carries every printed field — cost, power, hit, rarity, effect, element, affiliation, keywords — which is parsed into a `cards` catalogue table. No OCR; see [RULES.md](RULES.md).
- **Duplicate prevention**: A PostgreSQL table keyed on `wr_id` means re-runs only fetch what is new.
- **Respectful crawling**: Randomized delays, `robots.txt` compliance, and automatic backoff on `429`/`5xx`.
- **Hardened downloads**: Size caps, content-type and magic-byte checks, cross-host redirect refusal, and atomic writes.
- **Containerized execution**: Read-only root filesystem, all capabilities dropped, non-root user.
- **Image post-processing**: OpenCV converts JPGs to transparent PNGs, with a leak guard that refuses to damage artwork.

## Project Structure

```text
├── main.py                 # Scraper (Requests + BeautifulSoup)
├── card_metadata.py        # Detail-response parser (no I/O)
├── convert_to_png.py       # Image processing (OpenCV)
├── RULES.md                # Card layout and metadata field reference
├── tests/                  # Test suite
├── compose.yaml            # Container orchestration
├── Containerfile           # Container image definition
├── Makefile                # Command shortcuts
├── pyproject.toml          # Project metadata + lint/test configuration
├── requirements.txt        # Direct runtime dependencies (edit this)
├── requirements-dev.txt    # Direct dev/CI dependencies (edit this)
├── requirements.lock       # Resolved + hash-pinned; what actually installs
├── requirements-dev.lock   # Same, for the dev tree
├── .env.example            # Connection settings template
├── downloads/              # Raw JPG downloads (host-mounted)
└── processed/              # Transparent PNGs (host-mounted)
```

## Prerequisites

- **Podman** (or Docker) with **Compose**
- **Make** (optional but recommended)
- A reachable **PostgreSQL** instance — see below

## Database

Two tables are involved, with different owners.

`scraped_cards` belongs to the scraper, which creates it on first run, and tracks what has been downloaded:

| Column | Type | Purpose |
| --- | --- | --- |
| `wr_id` | `TEXT PRIMARY KEY` | GnuBoard write-ID; the deduplication key |
| `card_id` | `TEXT` | Sanitized card number, also the filename stem |
| `image_filename` | `TEXT` | Name the image was actually saved under |
| `scraped_at` | `TIMESTAMPTZ` | Insertion time, defaulted by the server |

`cards` is the catalogue **the tracker app owns**. Its shape comes from that app's drizzle
migrations; the scraper only fills it, upserting on `wr_id`, and never creates or alters it. Run
`make db-migrate` in [nivel-arena-collection-tracker](../nivel-arena-collection-tracker) before the
first scrape — otherwise the scraper stops at startup and says so.

| Column | Type | Purpose |
| --- | --- | --- |
| `id` | `SERIAL PRIMARY KEY` | The app's key; the collection references it |
| `wr_id` | `TEXT UNIQUE` | GnuBoard write-ID — what the scraper upserts on |
| `number` | `TEXT` | Printed number, `ST08-014`. **Not unique** — see below |
| `set_code` | `TEXT` | Prefix of the number, `ST08` |
| `name` | `TEXT` | Card name, as printed |
| `type` / `type_en` | `TEXT` | 유닛 / 스킬 / 아이템 / 리더, plus its English form |
| `element` / `element_en` | `TEXT` | 화염 / 번개 / 폭풍 / 파도 / 대지, plus its English form |
| `cost`, `power`, `hit` | `INTEGER` | `NULL` where the card prints `-` |
| `rarity` | `TEXT` | `C`, `R`, `SR`, `UR`, `SPR`, `SBR`, `L`, `P`, … |
| `affiliation` | `TEXT[]` | `{이펙트,레기온}` |
| `keywords` | `TEXT[]` | `{패시브,액티브}` |
| `effect` | `TEXT` | Effect text, icons kept as `[패시브]` markers |
| `trigger_text` | `TEXT` | The trigger box, stored separately |
| `product_name`, `ip` | `TEXT` | Release pack and franchise |
| `image_filename` | `TEXT` | The file in `downloads/` |
| `updated_at` | `TIMESTAMPTZ` | Last refresh; a re-scrape updates the row |

`number` is deliberately not unique: the same card is reprinted at several rarities (`BT05-071`
exists as both UR and SPR), and each printing is its own board entry with its own image. The board
also covers several franchises, so filter on `ip` — `승리의 여신: 니케` for NIKKE.

`make purge-db` clears only `scraped_cards`. It leaves `cards` alone on purpose: the app's
`collection_entries` cascade off it, so truncating the catalogue would delete the collection with it.

The field-by-field mapping from the printed card face is in [RULES.md](RULES.md).

The instance itself belongs to the sibling [nivel-arena-collection-tracker](../nivel-arena-collection-tracker) stack (container `nivel-db`, database `nivel`). Start it there before scraping; `compose.yaml` joins that stack's network rather than declaring a database of its own, which is what makes the `nivel-db` hostname resolve. The scraper's table sits alongside the tracker's drizzle-managed tables and is never touched by its migrations.

To point at some other PostgreSQL instead, set `SCRAPER_DATABASE_URL` accordingly and drop the `networks:` block from `compose.yaml`.

## Quick Start

```bash
make setup   # create host directories and .env
$EDITOR .env # set SCRAPER_DATABASE_URL
make up      # build and run the scraper in the foreground
make up-d    # ...or run it detached
make logs    # follow progress
make convert # convert downloaded JPGs to transparent PNGs
make down    # stop and remove containers
```

Run `make help` for the full target list.

## Configuration

Nothing needs to be edited in source. Every setting is available as a CLI flag or an environment variable:

| Environment variable | Flag | Default | Purpose |
| --- | --- | --- | --- |
| `SCRAPER_DATABASE_URL` | — | *(required)* | PostgreSQL connection URL |
| `SCRAPER_BASE_URL` | `--base-url` | `http://nivelarena.co.kr` | Board site root |
| `SCRAPER_BOARD_ID` | `--board-id` | `cardlists` | GnuBoard `bo_table` value |
| `SCRAPER_MAX_PAGES` | `--max-pages` | all pages | Stop after N pages |
| `SCRAPER_MIN_DELAY` | `--min-delay` | `5.0` | Minimum seconds between cards |
| `SCRAPER_MAX_DELAY` | `--max-delay` | `10.0` | Maximum seconds between cards |
| `SCRAPER_USER_AGENT` | — | Chrome 142 on Linux | Request `User-Agent` |
| `SCRAPER_DOWNLOADS_DIR` | — | `/app/downloads` | Raw JPG output |
| `SCRAPER_PROCESSED_DIR` | — | `/app/processed` | PNG output |

`SCRAPER_DATABASE_URL` has **no flag on purpose**: `argv` is world-readable through `/proc`, and the URL carries the password. It is read from the environment only, which under Compose means `.env` (gitignored; `make setup` seeds it from `.env.example`).

The container reaches the database as `nivel-db:5432`; host-side tooling reaches the same server as `localhost:5432`. `.env.example` carries both, and the `make repair-db` / `make import-sqlite` recipes prefer `SCRAPER_DATABASE_URL_LOCAL` when it is set.

Both scripts support `--help`. `main.py` also accepts `--ignore-robots` and `--verbose`; `convert_to_png.py` accepts `--workers`, `--force`, `--downloads-dir` and `--processed-dir`.

### Backfilling metadata

Cards downloaded before the catalogue existed have a `scraped_cards` row but no `cards` row. The
backfill re-hits the detail endpoint for exactly those, at the usual crawl delays, and **never
re-downloads an image**:

```bash
make backfill-metadata        # preview: reports how many rows would be filled, makes no requests
make backfill-metadata-apply  # fetch and store
make backfill-metadata-apply ARGS="--backfill-limit 5"   # or just the first few
```

It is resumable — the connection is autocommit, so an interrupted run keeps what it stored and the
next one continues where it stopped. Add `--force` to refresh rows that already have metadata, which
is what to run after adding a missing value to `card_metadata.CARD_TYPE_EN` or `ELEMENT_EN`.

## Development

```bash
make venv         # create .venv from the hash-pinned dev lock
make test-db-up   # start a throwaway PostgreSQL on 55432
make test         # run the test suite
make test-db-down # stop it again
make lint         # ruff check + format check
make fmt          # auto-format and auto-fix
make audit        # scan both dependency trees for known CVEs
make verify-locks # fail if a lock has drifted from its requirements file
make check        # lint + test + verify-locks + audit
```

The database-backed tests need a real server — the scraper's SQL is PostgreSQL-specific, so a stand-in would only test the stand-in. Point `SCRAPER_TEST_DATABASE_URL` at one (`make test-db-up` prints the URL) or those tests skip. Each test runs in its own throwaway schema, so a run leaves nothing behind.

CI runs the same checks on Python 3.12–3.14 against a PostgreSQL service container, plus the dependency gate below and a container build, on every push and pull request, and weekly so newly-disclosed CVEs surface without a push.

The floor is **Python 3.12** — the lowest version on which every dependency can be held at its newest
release (numpy 2.5 dropped 3.11). The container image runs 3.14. `pytest` treats warnings as errors,
so a deprecation out of a pinned dependency fails the build rather than scrolling past.

### Changing a dependency

`requirements.txt` and `requirements-dev.txt` hold the *direct* dependencies and are the files to
edit. `requirements.lock` and `requirements-dev.lock` are compiled from them — the full resolved
tree, every artefact pinned by SHA-256 — and are what the container, CI and `make venv` install
from. After editing either requirements file:

```bash
make lock          # recompile, keeping existing transitive pins where possible
make lock-upgrade  # ...or take the newest allowed version of everything
```

Commit the regenerated locks alongside the change; CI fails if they are out of step.

## Maintenance

### Migrating from the SQLite database

Scraper versions before `2.0.0` tracked progress in `data/scraper.db`. Import that history before the first PostgreSQL run, or the whole board looks unscraped and gets downloaded again:

```bash
make import-sqlite        # preview
make import-sqlite-apply  # write
```

Rows already present in PostgreSQL are left untouched.

### Repairing legacy database rows

Scraper versions before `1.0.0` recorded the *source* filename in `image_filename` instead of the name the file was actually saved under, so those rows point at files that do not exist on disk. To fix them:

```bash
make repair-db        # preview the changes
make repair-db-apply  # write them
```

Rows whose `card_id` matches more than one on-disk variant are reported and left untouched rather than guessed at.

## Security Notes

The target site has **no HTTPS listener**, so all traffic is necessarily plaintext and observable or modifiable in transit. The scraper is written on the assumption that every response is untrusted:

- Downloads are capped at 32 MB and rejected unless the `Content-Type` is `image/*` and the body starts with the JPEG magic bytes.
- HTML responses are streamed and capped at 8 MB rather than read whole, and decoded from the declared charset (or the page's own `meta`) so a Korean page is never silently mangled.
- Redirects to a host other than the configured board are refused — for image downloads, list pages and the AJAX detail endpoint alike.
- Card IDs are sanitized before becoming filenames, so a hostile page cannot write outside `downloads/`.
- All HTTP calls carry connect and read timeouts, as does the PostgreSQL connect.
- Database credentials are environment-only, never a CLI flag, and the connection URL is redacted before it reaches a log line.
- Everything scraped from the site reaches the database as a bound parameter; no SQL is built by string interpolation.
- Images are streamed to a `.part` file and renamed only on success, so an interrupted run cannot leave a truncated JPG that a later run mistakes for complete.
- The container runs as a non-root user with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, and memory and PID limits. OpenCV's native decoders are the largest attack surface in the pipeline; `OPENCV_IO_MAX_IMAGE_PIXELS` bounds them further, and a decode it rejects is caught rather than taking the worker process down.
- `.env` carries the database password, so `make setup` gives it mode `600` on every run rather than leaving it world-readable.

### Dependency integrity

Supply chain is treated as a build requirement, not a convention. Every install — container, CI and
`make venv` — goes through `pip install --require-hashes` against a lock file that carries a
SHA-256 for each artefact in the resolved tree, so a substituted or tampered wheel fails the install
instead of executing. `--require-hashes` also implies `--no-deps`, so nothing outside the lock can be
pulled in.

CI's `dependencies` job fails the build when any of the following is true:

1. A lock has drifted from the requirements file it was compiled from.
2. Any pin in either tree lacks a hash.
3. `pip-audit --strict` reports a known vulnerability in the runtime **or** the dev tree.

GitHub Actions are pinned to commit SHAs rather than tags: a tag is mutable, and whoever controls the
action repository could otherwise repoint it at code that runs with this workflow's token.

## Legal & Copyright Disclaimer

This project is a technical tool developed for educational and personal use only.

- **Code:** The scraper and processing scripts are open-source (see License).
- **Assets & Intellectual Property:** All trading card images, character designs, logos, text, and artwork downloaded or processed by this tool remain the exclusive intellectual property of their respective creators, publishers, and copyright holders.

This includes, but is strictly not limited to:
- **Nivel / Nivel Arena** (TCG Publisher)
- **Shift Up** (*Goddess of Victory: NIKKE*)
- **Neowiz** (*Brown Dust 2*)
- **Smilegate** (*Epic Seven*)
- **Nimble Neuron** (*Eternal Return*)
- *Any other studio, publisher, or intellectual property featured in current or future Nivel Arena collaborations.*

This project is an independent, community-driven tool and is **not** affiliated with, endorsed by, sponsored by, or associated with any of these companies. Users of this tool are solely responsible for ensuring their usage complies with the target website's Terms of Service and local copyright or fair use laws.

## License

MIT — see [LICENSE](LICENSE).
