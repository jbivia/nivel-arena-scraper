"""Transport failures.

These describe how a response arrived, not what the domain makes of it, so they
live with the client that raises them rather than in ``nivel.domain``.
"""


class OffHostRedirect(RuntimeError):
    """Raised when a response came from a host other than the configured board."""


class ResponseTooLarge(RuntimeError):
    """Raised when a response body exceeded its size cap."""
