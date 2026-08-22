"""Entry point for the scraper. The code lives in ``nivel.interface.cli.nikke``.

This shim stays at the repository root because `python main.py` is what the
container's CMD, the Makefile recipes, the CI smoke check and the Synology task
scheduler all invoke. Moving the implementation was a refactoring; renaming this
file would have been a deployment change.
"""

from nivel.interface.cli.nikke import main

if __name__ == "__main__":
    raise SystemExit(main())
