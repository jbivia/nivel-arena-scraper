# NivelArena Card Scraper

Containerized Python scraper for `nivelarena.co.kr` (GnuBoard5). Entry points are `main.py`
(scrape) and `convert_to_png.py` (post-process); both are shims over `src/nivel/`, which is
layered domain / application / infrastructure / interface. `make help` lists every target,
`--help` every flag, `README.md` covers setup and deployment.

## Architecture

- The board is walked through `/bbs/board.php?bo_table=…&page=N`. Board links encode
  `{image_filename}♬{wr_id}` — U+266C is the site's own delimiter.
- Every printed field comes from the AJAX endpoint `/skin/board/card_list_new/get_info.php`
  as text. **No OCR** — the images are artwork only. `RULES.md` has the field mapping and
  the site's markup quirks (malformed tags, `-` meaning null, labels with trailing spaces).
- Images download from `/data/file/{board_id}/{image_filename}` and are saved as
  `{card_id}.jpg`.

## Things the code will not tell you

- **The `nivel` package is never installed.** `src` is on the path because three places
  name it: `pythonpath` in `pyproject.toml`, `ENV PYTHONPATH` in the `Containerfile`, and
  `LOAD_ENV_HOST` in the `Makefile`. A bare `python main.py` in a shell needs `PYTHONPATH=src`.
- **The composition root exposes its collaborators, it does not wrap them.**
  `NivelArenaScraper` hands out `.scrape`, `.backfill`, `.repair`, `.sqlite_import`, `.cards`,
  `.history` and `.board`. Adding a forwarding method to it puts the signature in two places;
  call the collaborator instead.
- **`png_converter.convert_one` must stay importable.** Python 3.14 starts processes with
  `forkserver` on Linux, so the pool's child re-imports the submitted function by name. It
  lives in a normal module for that reason, not in the entry point.
- **`convert_to_png.py` masks geometrically, not by colour.** It projects a background mask
  onto each axis to find the card rectangles, then cuts an antialiased rounded rectangle. A
  colour flood fill was tried first and escaped through light artwork and through the gutter
  of two-card composites such as SB02-001.
- **Two tables, two owners.** `scraped_cards` is the scraper's and it creates it. `cards`
  belongs to the sibling `nivel-arena-collection-tracker` app's drizzle migrations — this
  repository upserts on `wr_id` and verifies the columns at startup, never creates or alters
  the table. Run `make db-migrate` there before the first scrape, or startup stops and says so.
- **The database instance is the tracker's too.** `compose.yaml` joins that stack's external
  network instead of declaring a database, which is what makes the `nivel-db` hostname
  resolve. It also opts out of podman-compose's pod, which Podman refuses alongside the
  `--userns=keep-id` that `userns_mode` asks for.
- **The connection is autocommit**, so a long scrape never holds an idle transaction open
  against a database the tracker app shares. `repair_filenames` and the SQLite import open
  explicit transactions instead.
- **`SCRAPER_DATABASE_URL` has no CLI flag on purpose** — `argv` is world-readable through
  `/proc`. `redact_conninfo()` strips the password before anything reaches a log line.
- **`make purge-db` leaves `cards` alone.** The app's `collection_entries` cascade off it, so
  truncating the catalogue would take the collection with it.
- **A scrape does not produce PNGs.** The image's `CMD` is `python main.py`, which only
  downloads JPGs. `convert_to_png.py` is a second invocation that nothing runs automatically.

## Conventions

- The target site is plaintext HTTP, so every response is treated as untrusted: size caps on
  images *and* HTML, content-type and magic-byte checks, cross-host redirect refusal,
  filename sanitization. Keep it that way.
- `requirements*.txt` hold the direct pins and are what you edit; `requirements*.lock` are
  the compiled, SHA-256-pinned trees the container, CI and `make venv` install from. Run
  `make lock` after any change — CI fails on lock drift, on an unhashed pin, and on a known
  CVE in either tree. Renovate runs `make lock` itself through `postUpgradeTasks`, which
  only works because the instance is self-hosted and lists the command in its global
  `allowedCommands`; the hosted app could not.
- `pytest` treats warnings as errors: a deprecation out of a pinned dependency fails the
  build rather than scrolling past. Tests import `main` / `convert_to_png` directly; the
  OpenCV ones skip without `cv2`, the database ones without `SCRAPER_TEST_DATABASE_URL`.
- `:Z` relabels and `userns_mode: keep-id` handle SELinux and rootless Podman UID mapping.
- Commit messages are written in French.
