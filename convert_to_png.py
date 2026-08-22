"""Entry point for the PNG conversion. See ``nivel.interface.cli.convert_png``.

Kept at the repository root for the same reason as main.py: `make convert` and
the documented `docker compose run --rm scraper python convert_to_png.py` both
name this path.
"""

from nivel.interface.cli.convert_png import main

if __name__ == "__main__":
    raise SystemExit(main())
