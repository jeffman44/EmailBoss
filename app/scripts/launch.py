"""PyInstaller entry point. The bundle must import the package by name;
running plannerboard/start.py as a loose script breaks its relative imports
("attempted relative import with no known parent package")."""
import sys

from plannerboard.start import main

if __name__ == "__main__":
    sys.exit(main() or 0)
