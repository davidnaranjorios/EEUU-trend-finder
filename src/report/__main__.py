"""`uv run python -m src.report ...` (ver src/report/build.py)."""

import sys

from src.report.build import main

if __name__ == "__main__":
    sys.exit(main())
