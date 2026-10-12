"""Allows ``python -m shacklog``. The executable built by PyInstaller starts here too."""

from shacklog.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
