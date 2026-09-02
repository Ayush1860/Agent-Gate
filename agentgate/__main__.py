"""Allow ``python -m agentgate`` as well as the installed ``agentgate`` command."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
