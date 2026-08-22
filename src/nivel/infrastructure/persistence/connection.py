"""Opening the one PostgreSQL connection everything else borrows.

The connection string is read from the environment and is deliberately not
exposed as a CLI flag: argv is world-readable through ``/proc`` and the URL
carries the password. ``redact_conninfo`` is the other half of that rule --
nothing that might contain the URL reaches a log line without going through it.
"""

import os
from urllib.parse import urlparse, urlunparse

import psycopg

from nivel.domain.nikke.exception.catalogue import DatabaseNotConfigured

# Environment variable holding the libpq connection URL, e.g.
# postgres://user:password@nivel-db:5432/nivel
DATABASE_URL_ENV = "SCRAPER_DATABASE_URL"

# Seconds to wait for the PostgreSQL TCP connect. Without it a wedged host
# leaves the scraper hanging on startup indefinitely.
DB_CONNECT_TIMEOUT = 10

# Shows up in pg_stat_activity, so a long-running scrape is identifiable from
# the tracker's side of the same database.
DB_APPLICATION_NAME = "nivel-arena-scraper"


def redact_conninfo(url):
    """Return ``url`` with any password replaced, safe to log.

    Anything unparseable is reduced to a placeholder rather than echoed: a
    malformed URL is exactly the case where the password might land in a
    surprising position.
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return "<unparseable connection string>"

    if not parts.hostname:
        return "<connection string>"

    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        netloc = f"{parts.username}:***@{netloc}" if parts.password else f"{parts.username}@{netloc}"

    return urlunparse((parts.scheme, netloc, parts.path, "", "", ""))


def resolve_database_url(explicit=None):
    """Return the connection URL, or say which variable was supposed to hold it."""
    database_url = explicit or os.environ.get(DATABASE_URL_ENV, "")
    if not database_url:
        raise DatabaseNotConfigured(
            f"No PostgreSQL connection configured -- set {DATABASE_URL_ENV}, e.g. "
            f"{DATABASE_URL_ENV}=postgres://user:password@nivel-db:5432/nivel"
        )
    return database_url


def connect(database_url):
    """Open an autocommit connection to ``database_url``.

    Autocommit keeps a long scrape from holding an idle transaction open
    against a database the tracker app is also using; the operations that need
    all-or-nothing open an explicit transaction instead.
    """
    conn = psycopg.connect(
        database_url,
        connect_timeout=DB_CONNECT_TIMEOUT,
        application_name=DB_APPLICATION_NAME,
    )
    conn.autocommit = True
    return conn
