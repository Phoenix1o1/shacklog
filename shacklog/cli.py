"""Command-line entry point: desktop mode, server mode and maintenance commands.

``shacklog`` with no arguments (or a double-click on the executable) starts **desktop
mode**: a local web server on 127.0.0.1 and the browser pointed at it. ``shacklog serve``
starts **server mode** for the Proxmox hub. See PLAN.md §2 and ``shacklog --help``.

Everything shares the same startup: resolve the data folder, create/upgrade the
database, empty the scratch folder, take the daily backup, then run.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from shacklog import APP_NAME, DEFAULT_PORT, __version__
from shacklog.config import Paths, resolve_data_dir
from shacklog.db import Database, get_setting, set_setting

log = logging.getLogger("shacklog")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def setup_logging(paths: Paths, verbose: bool = False) -> None:
    """Log to the console and to a rotating file in the data folder."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            paths.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:  # read-only medium: console logging is enough
        pass
    logging.getLogger("waitress").setLevel(logging.WARNING)


def open_database(args) -> tuple[Paths, Database]:
    """Resolve the data folder and create or upgrade the database (directive 2)."""
    paths = Paths(resolve_data_dir(getattr(args, "data_dir", None))).ensure()
    db = Database(paths.database).init()
    return paths, db


def clean_tmp(paths: Paths) -> None:
    """Remove leftovers of interrupted uploads/imports from the scratch folder."""
    for entry in paths.tmp.iterdir():
        try:
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        except OSError:
            pass


def startup_maintenance(db: Database, paths: Paths) -> None:
    clean_tmp(paths)
    daily_backup(db, paths)


def daily_backup(db: Database, paths: Paths) -> None:
    """The daily backup, if today's isn't there yet (at start, then hourly while running)."""
    from shacklog.services import backup

    try:
        made = backup.daily_backup_if_due(db, paths.backups)
        if made:
            with db.transaction() as s:
                keep_daily = int(get_setting(s, "backup_keep_daily", 14) or 14)
                keep_other = int(get_setting(s, "backup_keep_import", 20) or 20)
            backup.prune(paths.backups, keep_daily, keep_other)
            log.info("Daily backup: %s", made.name)
    except Exception:  # a failed backup must not stop the logbook from opening
        log.exception("Daily backup failed")


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
        if os.name != "nt":
            # Same as waitress: a socket left in TIME_WAIT by the last run is reusable.
            # (On Windows SO_REUSEADDR would allow hijacking a live port, so skip it.)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def running_instance(port: int, database: Path) -> bool:
    """True if a ShackLog using this same database already answers on ``port``."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    return info.get("app") == APP_NAME and info.get("database") == str(database)


def free_port_near(host: str, start: int) -> int:
    for port in range(start, start + 50):
        if port_is_free(host, port):
            return port
    raise SystemExit("No free TCP port found near %d." % start)


def serve(app, host: str, port: int, behind_proxy: bool = False) -> None:
    """Run the app with waitress (pure-Python, works the same on every OS)."""
    from waitress import serve as waitress_serve

    options = {"host": host, "port": port, "threads": 8, "ident": APP_NAME,
               "max_request_body_size": app.config["MAX_CONTENT_LENGTH"]}
    if behind_proxy:
        # Trust X-Forwarded-* from a reverse proxy (HTTPS termination on Proxmox).
        # Only a proxy on this same machine/container is trusted: anyone else sending
        # X-Forwarded-For could otherwise pretend to be 127.0.0.1 (login lockout, SDR).
        proxy = os.environ.get("SHACKLOG_TRUSTED_PROXY", "127.0.0.1")
        options.update(trusted_proxy=proxy, trusted_proxy_count=1,
                       trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto",
                                              "x-forwarded-host"},
                       clear_untrusted_proxy_headers=True)
    waitress_serve(app, **options)


def start_sync_worker(app):
    from shacklog.services.sync_client import SyncWorker

    return SyncWorker(app).start()


def make_cleanup(worker, db: Database):
    """Stop sync, fold the WAL journal into the database file, close it. Runs once."""
    done = threading.Event()

    def cleanup() -> None:
        if done.is_set():
            return
        done.set()
        from shacklog.services import system

        try:
            if worker is not None:
                worker.stop(timeout=5)
        finally:
            system.checkpoint(db)
            db.dispose()

    return cleanup


def make_shutdown(cleanup, exit_code: int = 0):
    """What "Quit ShackLog" (and "Restart" on the hub) call: clean up, then exit."""

    def shutdown(code: int = exit_code) -> None:
        log.info("Shutting down on request")
        try:
            from shacklog import tray

            tray.hide_icon()
            cleanup()
        finally:
            os._exit(code)  # waitress has no clean stop from another thread

    return shutdown


def prepare_run(db: Database, worker, paths: Paths | None = None, check_updates: bool = False):
    """Checkpoint now and every few minutes, and on every way the process can end.
    With ``paths``, the daily backup is also checked every hour while running, and with
    ``check_updates`` (desktop) GitHub is checked every 6 hours for the "update" badge."""
    from shacklog.services import system, updates

    hours = [0]

    def hourly() -> None:
        daily_backup(db, paths)
        hours[0] += 1
        if check_updates and hours[0] % 6 == 0:
            updates.check_now(paths.data_dir)

    system.checkpoint(db)
    system.start_checkpoint_timer(db, hourly=hourly if paths is not None else None)
    cleanup = make_cleanup(worker, db)
    system.on_exit(cleanup)
    return cleanup


def banner(lines: list[str]) -> None:
    width = max(len(line) for line in lines) + 4
    print("\n  " + "-" * width)
    for line in lines:
        print(f"    {line}")
    print("  " + "-" * width + "\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_desktop(args) -> int:
    """Desktop mode: local server + browser. Login only if Andrea enabled it."""
    paths, db = open_database(args)
    setup_logging(paths, args.verbose)
    host, port = "127.0.0.1", args.port
    url = f"http://{host}:{port}/"
    if running_instance(port, paths.database):
        # A second double-click (or a start at sign-in while it already runs): just show it.
        print(f"{APP_NAME} is already running at {url} — opening the browser.")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    from shacklog.services import updates

    with db.transaction() as s:
        auto_update = bool(get_setting(s, "auto_update", True))
    # Explorer restarts the program without arguments, so only an explicit data folder
    # has to be handed on (SHACKLOG_DATA and the portable folder resolve by themselves).
    relaunch = ["--data-dir", str(paths.data_dir)] if getattr(args, "data_dir", None) else []
    if updates.update_at_launch(paths.data_dir, auto_update, relaunch):
        # The update helper waits for this process to exit, rebuilds and starts again.
        print(f"Updating {APP_NAME} from GitHub before starting…")
        db.dispose()
        return 0
    if not port_is_free(host, port):
        port = free_port_near(host, port + 1)
        url = f"http://{host}:{port}/"
    startup_maintenance(db, paths)

    from shacklog.web import create_app

    app = create_app(db, paths, mode="desktop")
    worker = None if args.no_sync else start_sync_worker(app)
    cleanup = prepare_run(db, worker, paths, check_updates=True)
    app.config["SHUTDOWN"] = make_shutdown(cleanup)
    app.config["SYNC_WORKER"] = worker
    app.config["RELAUNCH_ARGS"] = relaunch

    from shacklog import tray

    if tray.available() and tray.windowed():
        # Windows executable: no console window; ShackLog lives in the notification area.
        log.info("%s %s running in the background at %s (data: %s)",
                 APP_NAME, __version__, url, paths.data_dir)
        tray.run(url, paths.data_dir, app.config["SHUTDOWN"],
                 serve=lambda: serve(app, host, port), open_browser=not args.no_browser)
        return 0

    banner([
        f"{APP_NAME} {__version__} is running at {url}",
        "",
        f"Your data (database, library files, backups):",
        f"  {paths.data_dir}",
        f"  database file: {paths.database.name}",
        "",
        "Settings › Program shows this folder and opens it.",
        "Stop with Settings › Quit, or close this window.",
    ])
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        serve(app, host, port)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
    return 0


def cmd_serve(args) -> int:
    """Server mode (Proxmox hub): listen on the network, always require a login."""
    paths, db = open_database(args)
    setup_logging(paths, args.verbose)
    startup_maintenance(db, paths)

    setup_code = None
    with db.transaction() as s:
        if not get_setting(s, "password_hash"):
            initial = os.environ.get("SHACKLOG_PASSWORD", "")
            if initial:
                from werkzeug.security import generate_password_hash

                set_setting(s, "password_hash", generate_password_hash(initial))
                log.info("Password set from SHACKLOG_PASSWORD")
            else:
                setup_code = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"

    from shacklog.web import create_app

    app = create_app(db, paths, mode="server", setup_code=setup_code)
    worker = None if args.no_sync else start_sync_worker(app)
    cleanup = prepare_run(db, worker, paths)
    # "Restart" in Settings › Program exits with status 75; systemd (Restart=always)
    # starts the service again. Stop/update go through the control request file.
    app.config["SHUTDOWN"] = make_shutdown(cleanup, exit_code=75)
    app.config["SYNC_WORKER"] = worker

    shown_host = args.host if args.host not in ("0.0.0.0", "::") else socket.gethostname()
    print(f"\n  {APP_NAME} {__version__} server on http://{shown_host}:{args.port}/")
    print(f"  Data folder: {paths.data_dir}")
    if setup_code:
        message = (f"No password yet. Open the page above and enter setup code {setup_code} "
                   "to choose one.")
        print(f"  {message}\n")
        log.warning(message)
    try:
        serve(app, args.host, args.port, behind_proxy=args.behind_proxy)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
    return 0


def cmd_sync(args) -> int:
    """One sync with the configured hub (for cron or systemd timers)."""
    from shacklog.services.filestore import FileStore
    from shacklog.services.sync_client import SyncClient, SyncError, summary_text

    paths, db = open_database(args)
    setup_logging(paths, args.verbose)
    client = SyncClient.from_settings(db, FileStore(paths.files))
    if client is None:
        print("Sync isn't set up on this instance (Settings › Sync).")
        return 2
    try:
        stats = client.run()
    except SyncError as exc:
        print(f"Sync failed: {exc}")
        return 1
    print(summary_text(stats))
    return 0


def cmd_import(args) -> int:
    """Import an ADIF file, a bundle (.zip) or another instance's database."""
    from shacklog.services import backup

    paths, db = open_database(args)
    setup_logging(paths, args.verbose)
    source = Path(args.file)
    if not source.is_file():
        print(f"No such file: {source}")
        return 2
    if source.suffix.lower() in (".adi", ".adif"):
        from shacklog.models import ImportLog, Station
        from shacklog.services import qso_io

        backup.create_backup(db, paths.backups, reason="before-adif-import")
        with db.transaction() as s:
            station_id = None
            if args.station:
                st = (s.query(Station).filter(Station.deleted_at.is_(None))
                      .filter(Station.name == args.station).first())
                if st is None:
                    print(f"No station named “{args.station}”.")
                    return 2
                station_id = st.id
            summary = qso_io.import_adif(s, source.read_bytes(), station_id=station_id,
                                         filename=source.name)
            s.add(ImportLog(kind="adif", filename=source.name,
                            summary=qso_io.summary_json(summary)))
        print(f"Added {summary.created} QSOs, {summary.duplicates} already present, "
              f"{summary.skipped} skipped.")
        for error in summary.errors[:20]:
            print(f"  {error}")
        return 0
    from shacklog.services import bundle

    try:
        summary = bundle.import_upload(db, paths, source, source.name)
    except Exception as exc:
        print(f"Import failed: {exc}")
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_export_bundle(args) -> int:
    from shacklog.services import bundle

    paths, db = open_database(args)
    out = Path(args.out) if args.out else Path.cwd() / bundle.default_export_name(db)
    bundle.export_bundle(db, paths, out)
    print(f"Bundle written to {out}")
    return 0


def cmd_backup(args) -> int:
    from shacklog.services import backup

    paths, db = open_database(args)
    made = backup.create_backup(db, paths.backups, reason="manual")
    print(f"Backup written to {made}")
    return 0


def cmd_set_password(args) -> int:
    from werkzeug.security import generate_password_hash

    paths, db = open_database(args)
    password = os.environ.get("SHACKLOG_PASSWORD") or getpass.getpass("New password: ")
    if len(password) < 8:
        print("Use at least 8 characters.")
        return 2
    if not os.environ.get("SHACKLOG_PASSWORD") and getpass.getpass("Repeat: ") != password:
        print("The two passwords don't match.")
        return 2
    with db.transaction() as s:
        set_setting(s, "password_hash", generate_password_hash(password))
    print("Password set.")
    return 0


def cmd_where(args) -> int:
    """Print the data folder; ``--open`` also shows it in Explorer/Finder."""
    from shacklog.services import system

    paths = Paths(resolve_data_dir(getattr(args, "data_dir", None)))
    print(paths.data_dir)
    if getattr(args, "open", False):
        system.open_folder(paths.data_dir)
    return 0


def cmd_info(args) -> int:
    from shacklog import migrations
    from shacklog.models import SYNC_ORDER

    paths, db = open_database(args)
    with db.engine.connect() as conn:
        version = migrations.get_version(conn)
    from shacklog.services import system

    print(f"{APP_NAME} {__version__}")
    print(f"Built from      {system.build_commit()[:7] or 'source (not a built executable)'}")
    print(f"Data folder     {paths.data_dir}")
    print(f"Database        {paths.database}")
    print(f"Instance        {db.instance_name} ({db.instance_id})")
    print(f"Schema version  {version}")
    with db.transaction() as s:
        for model in SYNC_ORDER:
            count = s.query(model).filter(model.deleted_at.is_(None)).count()
            print(f"  {model.__tablename__:16s} {count}")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME.lower(),
        description=f"{APP_NAME} — amateur radio logbook. Without a command it starts on "
                    "this computer and opens the browser.")
    parser.add_argument("--data-dir", help="data folder (default: portable folder next to "
                        "the program, else the per-user data folder)")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    desktop = sub.add_parser("run", help="desktop mode (the default)")
    for p in (parser, desktop):
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help=argparse.SUPPRESS)
        p.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
        p.add_argument("--no-sync", action="store_true", help=argparse.SUPPRESS)

    server = sub.add_parser("serve", help="server mode for the hub (Proxmox)")
    server.add_argument("--host", default="0.0.0.0", help="address to listen on (0.0.0.0)")
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    server.add_argument("--behind-proxy", action="store_true",
                        help="trust X-Forwarded-* headers from a reverse proxy")
    server.add_argument("--no-sync", action="store_true",
                        help="don't run the sync client on this instance")

    sub.add_parser("sync", help="sync once with the configured hub")
    imp = sub.add_parser("import", help="import an ADIF file, bundle or database file")
    imp.add_argument("file")
    imp.add_argument("--station", help="station name to assign to imported QSOs")
    exp = sub.add_parser("export-bundle", help="write database + files to one zip")
    exp.add_argument("out", nargs="?")
    sub.add_parser("backup", help="back up the database now")
    sub.add_parser("set-password", help="set the web login password "
                   "(or use SHACKLOG_PASSWORD)")
    sub.add_parser("info", help="paths, identity and record counts")
    where = sub.add_parser("where", help="print the data folder (--open shows it)")
    where.add_argument("--open", action="store_true")

    # Subcommands accept the global options too (``shacklog serve --data-dir X``).
    # default=SUPPRESS so a subcommand doesn't overwrite a value given before it.
    for p in sub.choices.values():
        if not any(a.dest == "data_dir" for a in p._actions):
            p.add_argument("--data-dir", help=argparse.SUPPRESS, default=argparse.SUPPRESS)
        if not any(a.dest == "verbose" for a in p._actions):
            p.add_argument("-v", "--verbose", action="store_true", help=argparse.SUPPRESS,
                           default=argparse.SUPPRESS)
    return parser


COMMANDS = {
    None: cmd_desktop, "run": cmd_desktop, "serve": cmd_serve, "sync": cmd_sync,
    "import": cmd_import, "export-bundle": cmd_export_bundle, "backup": cmd_backup,
    "set-password": cmd_set_password, "info": cmd_info, "where": cmd_where,
}


#: Arguments that print something for a person at a terminal (see main()).
CONSOLE_WORDS = {name for name in COMMANDS if name not in (None, "run")} | {
    "-h", "--help", "--version"}


def main(argv: list[str] | None = None) -> int:
    from shacklog import tray

    no_console = tray.windowed()
    # Windowed .exe: print() and logging need somewhere to go. Only a maintenance command
    # typed in a terminal borrows that terminal; desktop mode must not, or Ctrl+C or
    # closing the terminal later would stop the ShackLog running in the tray.
    words = set(sys.argv[1:] if argv is None else argv)
    tray.fix_std_streams(attach=bool(words & CONSOLE_WORDS))
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args) or 0
    except RuntimeError as exc:  # e.g. database newer than this program
        if no_console and args.command in (None, "run"):
            tray.message_box(str(exc))
        print(f"\n{exc}\n")
        if getattr(sys, "frozen", False) and os.name == "nt" and not no_console:
            time.sleep(10)  # keep the console window readable on Windows
        return 1
