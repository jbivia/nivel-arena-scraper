"""Business vocabulary and rules, free of infrastructure.

Nothing under here may import ``psycopg``, ``requests`` or ``bs4``. A rule that
needs one of those is not a rule, it is a detail, and it belongs in
``nivel.infrastructure``.
"""
