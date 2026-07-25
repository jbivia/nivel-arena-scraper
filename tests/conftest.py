"""Shared fixtures.

The database-backed tests run against a real PostgreSQL. The scraper's SQL is
dialect-specific (``ON CONFLICT``, ``TIMESTAMPTZ``, ``ANY``), so a stand-in
would only ever test the stand-in. Point `SCRAPER_TEST_DATABASE_URL` at a
throwaway instance -- `make test-db-up` starts one -- or the tests skip.

Each test gets its own PostgreSQL schema, so a run leaves nothing behind and
two tests can never see each other's rows.
"""

import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pytest

TEST_DATABASE_URL_ENV = "SCRAPER_TEST_DATABASE_URL"

# The scraper writes the catalogue but does not own its shape: `cards` belongs
# to nivel-arena-collection-tracker's drizzle migrations (0003_scraped_catalogue).
# Mirrored here so the tests can stand it up in their throwaway schema. Keep it
# in step with that app's `server/db/schema.ts` -- `main._verify_cards_table`
# is what catches the two drifting apart at runtime.
CARDS_TABLE_DDL = """
CREATE TABLE cards (
    id SERIAL PRIMARY KEY,
    wr_id TEXT NOT NULL UNIQUE,
    number TEXT NOT NULL,
    set_code TEXT,
    name TEXT NOT NULL,
    type TEXT,
    type_en TEXT,
    element TEXT,
    element_en TEXT,
    cost INTEGER,
    power INTEGER,
    hit INTEGER,
    rarity TEXT,
    affiliation TEXT[] NOT NULL DEFAULT '{}',
    keywords TEXT[] NOT NULL DEFAULT '{}',
    effect TEXT,
    trigger_text TEXT,
    product_name TEXT,
    ip TEXT,
    image_filename TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _with_search_path(url, schema):
    """Return ``url`` with libpq's ``options`` pinning the search path."""
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-csearch_path={schema}"
    return urlunparse(parts._replace(query=urlencode(query)))


@pytest.fixture
def database_url():
    base = os.environ.get(TEST_DATABASE_URL_ENV)
    if not base:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set; skipping PostgreSQL-backed tests")

    import psycopg
    from psycopg import sql

    schema = f"scraper_test_{uuid.uuid4().hex}"
    ident = sql.Identifier(schema)

    try:
        conn = psycopg.connect(base, connect_timeout=5, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is set but unreachable: {exc}")

    with conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
        conn.execute(sql.SQL("SET search_path TO {}").format(ident))
        conn.execute(CARDS_TABLE_DDL)

    yield _with_search_path(base, schema)

    with psycopg.connect(base, connect_timeout=5, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(ident))


@pytest.fixture
def database_url_without_cards(database_url):
    """A database where the app's migrations have not been run."""
    import psycopg

    with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as conn:
        conn.execute("DROP TABLE cards")

    return database_url
