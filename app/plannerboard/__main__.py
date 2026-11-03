"""`python -m plannerboard` is the same as `python -m plannerboard.start`."""

import sys

from .start import main

if __name__ == "__main__":
    sys.exit(main() or 0)
