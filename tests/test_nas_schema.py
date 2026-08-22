"""Drift gates for the mirrored `cards` DDL.

`cards` belongs to nivel-arena-collection-tracker. This repository keeps one
mirror of it -- db/init/01-cards.sql -- because two deployments have no app to
migrate the table into place: the standalone stack in compose.nas.yaml, and the
throwaway schemas the database-backed tests run in.

A mirror is a copy, and the failure mode of a copy is silent divergence. These
tests are what makes it loud:

* against this repository, always -- the mirror must satisfy the startup check
  in `PostgresCardRepository.verify_schema`, so a column added to `CARD_COLUMNS` without a
  matching column here fails the suite instead of a NAS scrape at 3am;
* against the app, when it is checked out alongside -- the mirror must match
  `server/db/schema.ts` exactly, in both directions.

The second gate cannot run in CI, which checks out one repository. It skips
there, the way the PostgreSQL-backed tests skip without a server.
"""

import os
import re
from pathlib import Path

import pytest

from nivel.infrastructure.nikke.persistence.postgres_card_repository import CARD_COLUMNS

REPO_ROOT = Path(__file__).resolve().parent.parent
CARDS_TABLE_SQL = REPO_ROOT / "db" / "init" / "01-cards.sql"

TRACKER_REPO_ENV = "NIVEL_TRACKER_REPO"
DEFAULT_TRACKER_REPO = REPO_ROOT.parent / "nivel-arena-collection-tracker"
TRACKER_SCHEMA_RELPATH = Path("server") / "db" / "schema.ts"

# `id` is the app's surrogate key and `updated_at` is maintained by the upsert's
# `updated_at = now()`, so neither is in CARD_COLUMNS -- the scraper never names
# them in an INSERT column list. They are still part of the shape the app
# expects, so the comparison against schema.ts includes them.
NOT_INSERTED_BY_SCRAPER = frozenset({"id", "updated_at"})


def _strip_sql_comments(sql):
    return re.sub(r"--[^\n]*", "", sql)


def mirrored_columns():
    """Column names declared by the mirrored CREATE TABLE."""
    body = re.search(
        r"CREATE TABLE IF NOT EXISTS cards\s*\((.*)\)\s*;",
        _strip_sql_comments(CARDS_TABLE_SQL.read_text(encoding="utf-8")),
        re.DOTALL,
    )
    assert body, "No `CREATE TABLE IF NOT EXISTS cards (...)` in db/init/01-cards.sql"

    columns = set()
    for line in body.group(1).splitlines():
        # Column definitions open a line with the name; constraint lines (UNIQUE,
        # PRIMARY KEY, CHECK) and continuations do not.
        match = re.match(r"\s+([a-z_]+)\s+[A-Z]", line)
        if match and match.group(1).upper() not in {"UNIQUE", "PRIMARY", "CHECK", "CONSTRAINT"}:
            columns.add(match.group(1))
    return columns


def tracker_schema_columns(schema_ts):
    """Column names drizzle declares for `cards` in the app's schema.ts."""
    source = schema_ts.read_text(encoding="utf-8")
    start = source.index("pgTable('cards'")
    # Every table in that file closes on a `})` at column zero.
    end = source.index("\n})", start)
    block = source[start:end]

    # `text('wr_id')`, `integer('cost')`, `serial('id')`,
    # `timestamp('updated_at', { withTimezone: true })`.
    return set(re.findall(r"\b(?:text|integer|serial|timestamp|boolean)\(\s*'([a-z_]+)'", block))


def test_mirror_covers_every_column_the_scraper_writes():
    """The startup check must pass against a database built from the mirror.

    `PostgresCardRepository.verify_schema` refuses to run when any of `CARD_COLUMNS` is
    absent. A standalone NAS database is created from this file and nothing
    else, so anything missing here is a scrape that dies at startup.
    """
    missing = sorted(CARD_COLUMNS - mirrored_columns())
    assert not missing, f"db/init/01-cards.sql is missing {', '.join(missing)}"


def test_mirror_declares_wr_id_unique():
    """`ON CONFLICT (wr_id)` needs the constraint, not just the column.

    Without it the first upsert fails with `there is no unique or exclusion
    constraint matching the ON CONFLICT specification` -- after the image has
    already been downloaded and `scraped_cards` written.
    """
    sql = _strip_sql_comments(CARDS_TABLE_SQL.read_text(encoding="utf-8"))
    assert re.search(r"\bwr_id\s+TEXT\s+NOT NULL\s+UNIQUE\b", sql), (
        "db/init/01-cards.sql must declare wr_id UNIQUE; the catalogue upsert conflicts on it"
    )


def test_mirror_matches_the_app_schema():
    """The mirror and the app's drizzle schema must agree, both directions.

    Skips unless nivel-arena-collection-tracker is checked out next to this
    repository (or `NIVEL_TRACKER_REPO` points at it), which is never the case
    in CI.
    """
    tracker = Path(os.environ.get(TRACKER_REPO_ENV, DEFAULT_TRACKER_REPO))
    schema_ts = tracker / TRACKER_SCHEMA_RELPATH
    if not schema_ts.is_file():
        pytest.skip(f"{schema_ts} not found; set {TRACKER_REPO_ENV} to compare the mirror against the app")

    mirrored = mirrored_columns()
    declared = tracker_schema_columns(schema_ts)

    assert mirrored == declared, (
        "db/init/01-cards.sql has drifted from the app's server/db/schema.ts.\n"
        f"  only in the mirror: {sorted(mirrored - declared) or 'none'}\n"
        f"  only in schema.ts:  {sorted(declared - mirrored) or 'none'}\n"
        "The app owns this shape -- re-sync the mirror, and check whether "
        "CARD_COLUMNS needs the new column too."
    )


def test_scraper_writes_every_app_column_it_should():
    """Catch a column the app added that the scraper silently never fills.

    The runtime check only looks for columns the scraper already knows about, so
    a new `cards` column would leave every row NULL there with nothing
    complaining. This is the test that notices.
    """
    tracker = Path(os.environ.get(TRACKER_REPO_ENV, DEFAULT_TRACKER_REPO))
    schema_ts = tracker / TRACKER_SCHEMA_RELPATH
    if not schema_ts.is_file():
        pytest.skip(f"{schema_ts} not found; set {TRACKER_REPO_ENV} to compare against the app")

    unwritten = sorted(tracker_schema_columns(schema_ts) - CARD_COLUMNS - NOT_INSERTED_BY_SCRAPER)
    assert not unwritten, (
        f"The app's `cards` table has columns the scraper never writes: {', '.join(unwritten)}. "
        "Either add them to CARD_COLUMNS and the catalogue upsert, or add them to "
        "NOT_INSERTED_BY_SCRAPER with a reason."
    )
