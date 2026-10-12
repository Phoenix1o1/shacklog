"""Entry script for the frozen executable (PyInstaller needs a plain script, not -m)."""

from shacklog.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
