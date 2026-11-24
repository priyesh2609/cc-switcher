"""``python -m cc_switcher`` runs the same CLI as ``ccs``."""

import sys

from cc_switcher.cli import main

if __name__ == "__main__":
    sys.exit(main())
