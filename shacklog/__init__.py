"""ShackLog — a personal amateur radio logbook, station archive and document library.

The package is organised in layers (see PLAN.md §4):

* ``shacklog.models`` / ``shacklog.db``  — data model and database plumbing
* ``shacklog.services``                  — pure logic (ADIF, CHIRP, geo, sync, files…)
* ``shacklog.web``                       — Flask web interface and sync API
* ``shacklog.cli``                       — command-line entry point (desktop/server modes)
"""

#: Display name. Change it here and everywhere else follows (data folder name included).
APP_NAME = "ShackLog"

#: Semantic version. Bump the minor number when the database schema changes.
__version__ = "1.1.0"

#: Default TCP port. 8573 has no well-known service on it.
DEFAULT_PORT = 8573
