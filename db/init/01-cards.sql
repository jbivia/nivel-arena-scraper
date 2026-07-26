-- Mirror of the `cards` table as nivel-arena-collection-tracker's drizzle
-- migrations leave it (its server/db/schema.ts, through 0003_scraped_catalogue).
--
-- The scraper does not own this shape. Wherever the tracker app is deployed it
-- is that app's `npx drizzle-kit migrate` that creates the table, and
-- main._verify_cards_table() only checks the result. This file exists for the
-- two situations where no app is present to create it:
--
--   1. compose.nas.yaml's bundled PostgreSQL, which has no app to migrate it.
--   2. The test suite's throwaway schemas -- tests/conftest.py reads this file
--      rather than keeping a second copy of the DDL.
--
-- It is a copy, and a copy can drift. tests/test_nas_schema.py is the gate: it
-- fails if this DDL stops covering main.CARD_COLUMNS, and -- when the sibling
-- repository is checked out alongside this one -- if it stops matching that
-- app's schema.ts. Re-sync this file rather than editing around it.
--
-- Deliberately absent: `card_sets`, `collection_entries`, `preferences` and the
-- app's enums. A database bootstrapped from this file is scraper-only. Do not
-- later point the Nuxt app at it: drizzle would find `cards` already present
-- with no migration journal to explain it, and its first migration would fail.
-- Point the scraper at the app's database instead -- see compose.nas.tracker.yaml
-- and the Synology section of README.md.

-- IF NOT EXISTS so that re-running this against a database the app has already
-- migrated is a no-op rather than an error.
CREATE TABLE IF NOT EXISTS cards (
    id SERIAL PRIMARY KEY,
    -- The scraper upserts with ON CONFLICT (wr_id), so the UNIQUE constraint is
    -- load-bearing, not documentation.
    wr_id TEXT NOT NULL UNIQUE,
    -- Not unique: the same card is reissued at several rarities and each
    -- reissue is its own board entry with its own artwork.
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
);
