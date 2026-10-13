"""Entry script for the frozen executable (PyInstaller needs a plain script, not -m).

Also used, from a source checkout, by "start when I sign in" (services/autostart.py):
run as a file, it puts the project folder on the import path itself, so it works from
any working directory.
"""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shacklog.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
