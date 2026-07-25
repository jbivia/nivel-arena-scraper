# NivelArena Card Scraper

A containerized Python scraper that collects high-resolution trading card images from [nivelarena.co.kr](http://nivelarena.co.kr), tracks progress in a local SQLite database to avoid redundant downloads, and post-processes the results into transparent PNGs.

## Features

- **Automated scraping**: Walks the board's pagination, resolves each card through the site's AJAX detail endpoint, and downloads the full-resolution image.
- **Duplicate prevention**: A SQLite database keyed on `wr_id` means re-runs only fetch what is new.
- **Respectful crawling**: Randomized delays, `robots.txt` compliance, and automatic backoff on `429`/`5xx`.
- **Hardened downloads**: Size caps, content-type and magic-byte checks, cross-host redirect refusal, and atomic writes.
- **Containerized execution**: Read-only root filesystem, all capabilities dropped, non-root user.
- **Image post-processing**: OpenCV converts JPGs to transparent PNGs, with a leak guard that refuses to damage artwork.

## Project Structure

```text
├── main.py                 # Scraper (Requests + BeautifulSoup)
├── convert_to_png.py       # Image processing (OpenCV)
├── tests/                  # Test suite
├── compose.yaml            # Container orchestration
├── Containerfile           # Container image definition
├── Makefile                # Command shortcuts
├── pyproject.toml          # Lint/test configuration
├── requirements.txt        # Runtime dependencies (pinned)
├── requirements-dev.txt    # Dev/CI dependencies (pinned)
├── data/                   # SQLite database (host-mounted)
├── downloads/              # Raw JPG downloads (host-mounted)
└── processed/              # Transparent PNGs (host-mounted)
```

## Prerequisites

- **Podman** (or Docker) with **Compose**
- **Make** (optional but recommended)

## Quick Start

```bash
make setup   # create host directories
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
| `SCRAPER_BASE_URL` | `--base-url` | `http://nivelarena.co.kr` | Board site root |
| `SCRAPER_BOARD_ID` | `--board-id` | `cardlists` | GnuBoard `bo_table` value |
| `SCRAPER_MAX_PAGES` | `--max-pages` | all pages | Stop after N pages |
| `SCRAPER_MIN_DELAY` | `--min-delay` | `5.0` | Minimum seconds between cards |
| `SCRAPER_MAX_DELAY` | `--max-delay` | `10.0` | Maximum seconds between cards |
| `SCRAPER_USER_AGENT` | — | Chrome 142 on Linux | Request `User-Agent` |
| `SCRAPER_DB_PATH` | — | `/app/data/scraper.db` | SQLite database location |
| `SCRAPER_DOWNLOADS_DIR` | — | `/app/downloads` | Raw JPG output |
| `SCRAPER_PROCESSED_DIR` | — | `/app/processed` | PNG output |

Both scripts support `--help`. `main.py` also accepts `--ignore-robots` and `--verbose`; `convert_to_png.py` accepts `--workers`, `--force`, `--downloads-dir` and `--processed-dir`.

## Development

```bash
make venv    # create .venv with dev dependencies
make test    # run the test suite
make lint    # ruff check + format check
make fmt     # auto-format and auto-fix
make audit   # scan pinned dependencies for known CVEs
make check   # lint + test + audit
```

CI runs the same checks on Python 3.11–3.13, plus a container build, on every push and pull request, and weekly so newly-disclosed CVEs surface without a push.

## Maintenance

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
- Redirects to a host other than the configured board are refused.
- Card IDs are sanitized before becoming filenames, so a hostile page cannot write outside `downloads/`.
- All HTTP calls carry connect and read timeouts.
- Images are streamed to a `.part` file and renamed only on success, so an interrupted run cannot leave a truncated JPG that a later run mistakes for complete.
- The container runs as a non-root user with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, and memory and PID limits. OpenCV's native decoders are the largest attack surface in the pipeline, and `OPENCV_IO_MAX_IMAGE_PIXELS` bounds them further.

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
