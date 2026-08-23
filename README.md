# NivelArena Card Scraper

A containerized Python scraper that collects high-resolution trading card images from [nivelarena.co.kr](http://nivelarena.co.kr), tracks progress in PostgreSQL to avoid redundant downloads, and post-processes the results into transparent PNGs.

## Features

- **Automated scraping**: Walks the board's pagination, resolves each card through the site's AJAX detail endpoint, and downloads the full-resolution image.
- **Card metadata**: The same detail response carries every printed field — cost, power, hit, rarity, effect, element, affiliation, keywords — which is parsed into a `cards` catalogue table. No OCR; see [RULES.md](RULES.md).
- **Duplicate prevention**: A PostgreSQL table keyed on `wr_id` means re-runs only fetch what is new.
- **Respectful crawling**: Randomized delays, `robots.txt` compliance, and automatic backoff on `429`/`5xx`.
- **Hardened downloads**: Size caps, content-type and magic-byte checks, cross-host redirect refusal, and atomic writes.
- **Containerized execution**: Read-only root filesystem, all capabilities dropped, non-root user.
- **Image post-processing**: OpenCV converts JPGs to transparent PNGs by detecting the card
  rectangles geometrically -- outer margins, rounded corners, and the white gutter of a
  two-card composite all become transparent, and the mask cannot reach into the artwork.

## Project Structure

Two entry points, layered code under `src/`, and one compose file per deployment:

```text
main.py                   # Entry point: scrape          -- delegates to src/
convert_to_png.py         # Entry point: PNG conversion   -- delegates to src/
src/nivel/
  domain/nikke/           # Entities, value objects, repository contracts. No I/O.
  application/nikke/      # Use cases: scrape, backfill, repair, SQLite import
  infrastructure/
    persistence/          # Connection handling, credential redaction
    nikke/persistence/    # PostgreSQL repositories
    nikke/http/           # Board client: robots, rate limiting, size caps
    nikke/parsing/        # Detail-response parser (no I/O)
    nikke/image/          # JPG -> transparent PNG (OpenCV)
  interface/cli/          # Argument parsing and composition
RULES.md                  # Card layout and metadata field reference
compose.yaml              # Development stack (Podman), satellite of the tracker
compose.nas.yaml          # Standalone deployment: scraper + its own PostgreSQL
compose.nas.tracker.yaml  # Satellite deployment: scraper only, tracker owns the DB
db/init/                  # `cards` DDL mirror, for databases no app migrates
requirements*.txt         # Direct dependencies (edit these)
requirements*.lock        # Resolved + hash-pinned; what actually installs
downloads/                # Raw JPG downloads (host-mounted)
processed/                # Transparent PNGs (host-mounted)
```

The package is never installed: `src` is named explicitly by `pythonpath` in
`pyproject.toml` for pytest, by `ENV PYTHONPATH` in the `Containerfile`, and by the
host-side `make` recipes. Running an entry point by path only ever puts the repository
root on `sys.path`, so a direct `python main.py` from a shell needs `PYTHONPATH=src`.

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

### The schema mirror

Two deployments have no app to migrate `cards` into place: the standalone stack in
`compose.nas.yaml`, and the throwaway schemas the test suite runs in. Both bootstrap the table from
**`db/init/01-cards.sql`** — one mirror of the app's shape, read by both, so a re-sync cannot leave
the tests agreeing with a schema the deployed database does not have.

It is still a copy, and the failure mode of a copy is silent divergence, so `tests/test_nas_schema.py`
gates it from both sides:

| Gate | Catches | Runs |
| --- | --- | --- |
| Mirror covers `main.CARD_COLUMNS` | A column the scraper writes that the mirror lacks — a standalone scrape that dies at startup | Always |
| `wr_id` declared `UNIQUE` | A mirror the catalogue's `ON CONFLICT (wr_id)` cannot upsert against | Always |
| Mirror matches the app's `schema.ts` | The two definitions drifting apart, in either direction | When the tracker repo is checked out alongside |
| Every app column is one the scraper writes | A column the app added that the scraper silently leaves `NULL` forever | When the tracker repo is checked out alongside |

The last two need both repositories, so they skip in CI — the same way the PostgreSQL-backed tests
skip without a server. Set `NIVEL_TRACKER_REPO` if the tracker is not the sibling directory.

None of this makes the scraper an owner of `cards`. Where the app exists, the app migrates the table
and `main._verify_cards_table` only checks the result.

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
| `SCRAPER_DOWNLOADS_DIR` | `--downloads-dir` ᶜ | `/app/downloads` | Raw JPG output |
| `SCRAPER_PROCESSED_DIR` | `--processed-dir` ᶜ | `/app/processed` | PNG output |

ᶜ On `convert_to_png.py`; `main.py` reads these two from the environment only.

`SCRAPER_DATABASE_URL` has **no flag on purpose**: `argv` is world-readable through `/proc`, and the URL carries the password. It is read from the environment only, which under Compose means `.env` (gitignored; `make setup` seeds it from `.env.example`).

The container reaches the database as `nivel-db:5432`; host-side tooling reaches the same server as `localhost:5432`. `.env.example` carries both, and the `make repair-db` / `make import-sqlite` recipes prefer `SCRAPER_DATABASE_URL_LOCAL` when it is set.

Both scripts support `--help` and `--verbose`. `main.py` also accepts `--ignore-robots`; `convert_to_png.py` accepts `--workers` and `--force`.

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
is what to run after adding a missing value to `CARD_TYPE_EN` or `ELEMENT_EN` in
`src/nivel/infrastructure/nikke/parsing/card_metadata.py`.

## Deploying to a Synology NAS

`compose.yaml` is the Podman development stack and does not run under Docker: `userns_mode: keep-id`
is Podman-only and Docker rejects it, and the `:Z` volume relabels are meaningless without SELinux.
DSM's Container Manager is Docker, so there are two Docker-native files instead. Pick by whether the
Nuxt app is on the same box:

| | File | Database |
| --- | --- | --- |
| Tracker app **is** deployed here | `compose.nas.tracker.yaml` | The app's, joined over its network |
| Tracker app is **not** deployed here | `compose.nas.yaml` | Bundled, bootstrapped from `db/init/` |

**Prefer the satellite file whenever the app is present.** Two databases means two catalogues, and
the collection is attached to only one of them. A database bootstrapped from `db/init/` is
scraper-only — do not later point the app at it, since drizzle would find `cards` already there with
no migration journal to explain it.

Both files stay within Compose v2.9, which is what DSM 7.2 ships — no `depends_on.required`, no
`develop` block.

### Setup

Over SSH, from the project directory (Container Manager puts projects under `/volume1/docker/`):

```bash
cp .env.nas.example .env && chmod 600 .env
$EDITOR .env                              # set PUID/PGID and the credentials

mkdir -p downloads processed
id "$USER"                                # DSM's first user is usually 1026, group `users` 100
sudo chown -R 1026:100 downloads processed

# standalone
docker compose -f compose.nas.yaml up -d db
docker compose -f compose.nas.yaml run --rm scraper

# ...or satellite — apply the app's migrations first, or the scraper stops at startup and says so
docker compose -f compose.nas.tracker.yaml run --rm scraper
```

Then convert, once there are images. Same image, same compose file — whichever of the two you just
used — so this needs no configuration of its own: the container already carries
`SCRAPER_DOWNLOADS_DIR` and `SCRAPER_PROCESSED_DIR`, which are the defaults the script reads.

```bash
docker compose -f compose.nas.yaml run --rm scraper python convert_to_png.py
```

### Scheduling

The scraper is a **one-shot batch job** — it walks the board, exits, and re-runs only fetch what is
new. Neither file gives it a restart policy on purpose: `unless-stopped` reads a completed scrape as
a crash and would re-scrape the board in a loop, forever, against a site that has no HTTPS listener.

Drive it from **DSM → Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script**, running as `root`. Scrape, then convert — the conversion needs the JPGs the run before it
downloaded:

```bash
cd /volume1/docker/nivel-arena-scraper && \
  docker compose -f compose.nas.yaml run --rm scraper && \
  docker compose -f compose.nas.yaml run --rm scraper python convert_to_png.py
```

At the default 5–10 s delays a full board takes hours, which is what overnight is for.

In the satellite arrangement the same task is also where the app's migrations belong, so a schema
added between two releases is in place before the next scrape needs it:

```bash
cd /volume1/docker/nivel-arena-scraper && \
  docker compose -f compose.nas.tracker.yaml run --rm scraper && \
  docker compose -f compose.nas.tracker.yaml run --rm scraper python convert_to_png.py && \
  cd /volume1/docker/nivel-arena-collection-tracker && \
  docker compose -f compose.nas.yaml run --rm migrate
```

`&&` between the scrape and the conversion is deliberate: a scrape that failed to start leaves the
conversion nothing new to do, and it would exit 0 having reported that there was nothing to convert.
The last one is the arguable link. `convert_to_png.py` exits non-zero when *no* image at all could be
converted (or `downloads/` is missing), which would then hold back a migration that has nothing to do
with images — use `;` before the tracker's `cd` if you would rather the migration always run.

### Notes for the 920+

- **Fits comfortably.** The scraper is capped at 2 GB and PostgreSQL adds ~200 MB, against 8 GB. The
  work is I/O-bound on the crawl delays, so the J4125 idles through it. `convert_to_png.py` defaults
  to `cpu_count() // 2` — 2 of the 4 cores — and `--workers` overrides it.
- **Nothing compiles.** Every dependency has an `x86_64` manylinux wheel, and the wheels' SSE4.2
  baseline is met by Gemini Lake. Building the image on the NAS works; it only adds `libglib2.0-0`.
- **`db/init/` runs once.** The postgres entrypoint executes it only against an empty data
  directory. Editing the SQL later changes nothing — apply it by hand, or remove the `pgdata` volume
  and start over.
- **Permissions are the usual failure.** The root filesystem is read-only, so `downloads/` and
  `processed/` are the only paths the container writes to. If `PUID`/`PGID` do not match what owns
  them on the host, every download fails.

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

Renovate does this for itself: `renovate.json` gives it a `postUpgradeTasks` hook that runs
`make lock` on the branch, so its pull requests arrive with the locks already recompiled. That
hook is only honoured because Renovate runs self-hosted here and the command is listed in that
instance's global `allowedCommands` — a repository cannot grant itself the right to run commands.
Python bumps are grouped into a single pull request for the same reason the locks matter: two
concurrent branches would each recompile the same lock and conflict as soon as one merged.

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
