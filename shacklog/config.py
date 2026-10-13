"""Where ShackLog keeps its data, and the small ``Paths`` object passed around the app.

Resolution order for the data directory (PLAN.md §2):

1. an explicit path (``--data-dir`` or the ``SHACKLOG_DATA`` environment variable);
2. a ``ShackLogData`` folder next to the executable or script (portable mode, e.g. a
   USB stick — create the empty folder once and ShackLog will use it);
3. the operating system's per-user data folder.

Everything is created on first use, so a fresh computer needs no setup.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from shacklog import APP_NAME

PORTABLE_DIR_NAME = f"{APP_NAME}Data"
DB_FILENAME = "shacklog.sqlite3"


def _executable_dir() -> Path:
    """Folder containing the running program.

    When frozen by PyInstaller, ``sys.executable`` is the .exe itself. When running from
    source, use the project root (the folder that contains the ``shacklog`` package).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _os_data_dir() -> Path:
    """Per-user data folder for this OS (platformdirs knows the conventions)."""
    try:
        from platformdirs import user_data_dir

        return Path(user_data_dir(APP_NAME, appauthor=False, roaming=False))
    except ImportError:  # platformdirs missing: fall back to a dot-folder in $HOME
        return Path.home() / f".{APP_NAME.lower()}"


def resolve_data_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Return the data directory according to the rules in the module docstring."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("SHACKLOG_DATA")
    if env:
        return Path(env).expanduser().resolve()
    return default_data_dir()


def default_data_dir() -> Path:
    """The folder used when neither --data-dir nor SHACKLOG_DATA says otherwise."""
    portable = _executable_dir() / PORTABLE_DIR_NAME
    if portable.is_dir():
        return portable
    return _os_data_dir()


@dataclass(frozen=True)
class Paths:
    """All filesystem locations derived from the data directory — the one place that
    names them. The data folder holds::

        shacklog.sqlite3    the logbook (the only thing that syncs, with files/)
        files/              library files, content-addressed (services/filestore.py)
        backups/            automatic and manual database backups
        reference/          downloaded reference data, re-creatable: country list,
                            callbook cache
        maps/               offline map files (*.mbtiles) and cache/ of viewed tiles
        incoming/           bundles or databases put here by hand, offered as one-click
                            imports (how a big bundle reaches the hub: pct push)
        control/            update status and requests between the program and the
                            hub's shacklog-ctl (which hard-codes "control" too)
        tmp/                scratch for uploads and imports, emptied at every start
        exports/            hub only: archives made by "shacklog-ctl backup"
        secret.key          web session key        shacklog.log   the program's log
    """

    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def files(self) -> Path:
        """Content-addressed blob store for the library (see services/filestore.py)."""
        return self.data_dir / "files"

    @property
    def backups(self) -> Path:
        return self.data_dir / "backups"

    @property
    def tmp(self) -> Path:
        """Scratch space for uploads and imports; emptied at startup."""
        return self.data_dir / "tmp"

    @property
    def reference(self) -> Path:
        """Downloaded reference data: country list, callbook cache. Safe to delete."""
        return self.data_dir / "reference"

    @property
    def maps(self) -> Path:
        """Offline map files (*.mbtiles) and the tile cache in ``maps/cache``."""
        return self.data_dir / "maps"

    @property
    def incoming(self) -> Path:
        """Files to import, copied here by hand (Settings › Import, export & backups)."""
        return self.data_dir / "incoming"

    @property
    def control(self) -> Path:
        """Update status and server requests (services/updates.py, system.py)."""
        return self.data_dir / "control"

    @property
    def secret_key_file(self) -> Path:
        """Flask session signing key. Local to this instance, never synced."""
        return self.data_dir / "secret.key"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "shacklog.log"

    def ensure(self) -> "Paths":
        """Create every folder that must exist. Safe to call repeatedly."""
        for folder in (self.data_dir, self.files, self.backups, self.tmp, self.incoming):
            folder.mkdir(parents=True, exist_ok=True)
        return self


def load_secret_key(paths: Paths) -> bytes:
    """Read the session key, creating a random one on first launch."""
    if paths.secret_key_file.exists():
        return paths.secret_key_file.read_bytes()
    key = os.urandom(32)
    paths.secret_key_file.write_bytes(key)
    try:  # best effort: keep it private on POSIX systems
        os.chmod(paths.secret_key_file, 0o600)
    except OSError:
        pass
    return key
