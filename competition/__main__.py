"""Enable ``python -m competition ...`` → dispatches to the CLI."""
from __future__ import annotations

import sys

from .sdk.cli import main

if __name__ == "__main__":
    sys.exit(main())
