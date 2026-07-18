"""Entry point for ``python -m shell``."""

from __future__ import annotations

import sys

from shell.cli import main

if __name__ == "__main__":
    sys.exit(main())
