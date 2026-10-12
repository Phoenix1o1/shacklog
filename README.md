# ShackLog

A personal amateur radio logbook, station archive and document library.
Runs on Windows, Linux and macOS as a single executable with a browser interface, and as
a server on Proxmox that the other instances sync with.

**Status: under construction.** See `PLAN.md` for the architecture and roadmap,
`MANDATES.md` for the directives the project follows, and `CONVENTIONS.md` for how the
code is written.

## Run from source

```bash
pip install -r requirements.txt
python -m shacklog            # desktop mode: opens the browser
python -m pytest -q           # tests (pip install -r requirements-dev.txt)
```
