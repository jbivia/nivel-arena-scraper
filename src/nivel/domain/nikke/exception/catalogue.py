"""Failures that stop a run before it can do any work.

Both are raised while the scraper is being constructed, which is the only point
where the operator can still be told something actionable: once a board walk is
under way, the same conditions surface as an ``UndefinedColumn`` several hundred
requests in, saying nothing about the migration that was never run.
"""


class DatabaseNotConfigured(RuntimeError):
    """Raised when no PostgreSQL connection string is available."""


class CatalogueTableMissing(RuntimeError):
    """Raised when the app's ``cards`` table is absent or predates this scraper."""
