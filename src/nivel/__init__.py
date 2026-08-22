"""Nivel Arena scraping, in layers.

``domain`` holds the vocabulary and the rules and imports nothing from the
outside world. ``application`` orchestrates it. ``infrastructure`` is where
PostgreSQL, HTTP and the filesystem live. ``interface`` is what a human or a
scheduler actually invokes.

The dependency arrow only ever points inward: infrastructure knows about the
domain, never the reverse. That is what lets the domain be tested without a
database, a network or a container.
"""
