"""One module per command. The shims at the repository root delegate here.

The shims exist because `python main.py` is what the container's CMD, the
Makefile recipes, the CI smoke checks and the Synology task scheduler all run.
Renaming them would be an operational change dressed up as a refactoring.
"""
