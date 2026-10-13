"""Names and defaults of every local setting (stored in the ``settings`` table).

Settings belong to *this* instance and never sync: the laptop can have a different
default station than the desktop, and secrets (password hash, tokens) stay put.
Use :func:`shacklog.db.get_setting` / :func:`shacklog.db.set_setting` with these keys.
"""

from __future__ import annotations

DEFAULTS: dict[str, object] = {
    # --- first run ---
    "setup_done": False,
    # --- logging defaults ---
    "default_station_id": None,  # station preselected in new QSO/SWL entries
    "reference_station_id": None,  # default "distance from" point in filters
    "page_size": 50,
    # --- security ---
    "password_hash": None,  # Werkzeug hash; login is required whenever this is set
    "require_login_local": False,  # also ask for the password on 127.0.0.1
    # --- this instance as hub ---
    "sync_hub_enabled": False,  # accept sync requests from other instances
    "sync_hub_token": None,  # bearer token clients must present
    # --- this instance as client ---
    "sync_hub_url": None,  # e.g. http://192.168.1.20:8573
    "sync_client_token": None,
    "sync_interval_min": 15,  # 0 = only on demand
    "sync_pull_files": True,  # download library files from the hub (else on demand)
    # --- backups ---
    "backup_keep_daily": 14,
    "backup_keep_import": 20,
    "last_backup_date": None,
    "backup_mirror_dir": None,  # second folder for backup copies (OneDrive, NAS share…)
    "backup_mirror_keep": 30,
    # --- updates (desktop; the hub uses the control/auto-update flag file) ---
    "auto_update": True,  # check GitHub at start and update before opening
    # --- country list (services/cty.py) ---
    "cty_auto_update": True,  # download AD1C's list when missing or a month old
    # --- OFFgrid-SDR (desktop) ---
    "offgrid_sdr_path": None,  # full path of OFFgrid-SDR's index.html; None = auto-detect
}


def default(key: str):
    return DEFAULTS.get(key)
