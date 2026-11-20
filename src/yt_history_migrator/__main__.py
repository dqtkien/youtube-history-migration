"""Module execution entry point for `python -m yt_history_migrator`."""

import sys

from yt_history_migrator.cli import main

if __name__ == "__main__":
    sys.exit(main())
