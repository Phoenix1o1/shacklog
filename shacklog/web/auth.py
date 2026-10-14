"""Login, first-run password, and CSRF protection.

When is a login required?
* **Server mode** (listening beyond localhost): always.
* **Desktop mode** (127.0.0.1): only if a password is set *and* "require login on this
  computer" is ticked in Settings.

Server mode with no password yet: the CLI prints a one-time **setup code**; the login page
asks for it once to set the password. Nobody else on the network can claim the instance.

CSRF: every POST form carries ``_csrf`` (htmx sends it as ``X-CSRFToken``). The sync API
(``/api/sync``) is exempt because it authenticates each request with a bearer token.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session as http_session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from shacklog.web import helpers as h

bp = Blueprint("auth", __name__)

#: Endpoints reachable without logging in.
PUBLIC_ENDPOINTS = {"auth.login", "auth.logout", "static", "health"}
#: Blueprints with their own authentication (bearer token).
TOKEN_BLUEPRINTS = {"api_sync", "api_ha"}

MIN_PASSWORD_LENGTH = 8
_failures: dict[str, list[float]] = defaultdict(list)  # ip -> timestamps of failed logins
LOCKOUT_ATTEMPTS, LOCKOUT_WINDOW_S = 5, 300


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def login_required_now() -> bool:
    if current_app.config["MODE"] == "server":
        return True
    return bool(h.setting("password_hash")) and bool(h.setting("require_login_local"))


def csrf_token() -> str:
    token = http_session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        http_session["_csrf"] = token
    return token


def _check_csrf() -> None:
    sent = request.form.get("_csrf") or request.headers.get("X-CSRFToken") or ""
    expected = http_session.get("_csrf") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description="The form expired or came from another site. "
                               "Reload the page and try again.")


@bp.before_app_request
def guard():
    if request.endpoint in PUBLIC_ENDPOINTS or request.blueprint in TOKEN_BLUEPRINTS:
        if request.method == "POST" and request.endpoint == "auth.login":
            _check_csrf()
        return None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        _check_csrf()
    if login_required_now() and not http_session.get("logged_in"):
        if request.headers.get("HX-Request"):
            return "", 401
        return redirect(url_for("auth.login", next=request.full_path))
    return None


def _locked_out(ip: str) -> bool:
    now = time.monotonic()
    _failures[ip] = [t for t in _failures[ip] if now - t < LOCKOUT_WINDOW_S]
    return len(_failures[ip]) >= LOCKOUT_ATTEMPTS


@bp.route("/login", methods=["GET", "POST"])
def login():
    has_password = bool(h.setting("password_hash"))
    setup_code = current_app.config.get("SETUP_CODE")
    next_url = request.args.get("next") or url_for("main.dashboard")
    # Never redirect off-site: "/x" only, not "//host" or "/\\host" (browsers treat both
    # as another site).
    if not next_url.startswith("/") or next_url.startswith(("//", "/\\")):
        next_url = url_for("main.dashboard")
    ip = request.remote_addr or "?"

    if request.method == "POST":
        if _locked_out(ip):
            flash("Too many failed attempts. Wait five minutes and try again.", "error")
            return render_template("login.html", first_run=not has_password), 429
        if not has_password:
            # First run in server mode: setup code + new password.
            code = request.form.get("setup_code", "").strip()
            pw1, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            if not setup_code or not hmac.compare_digest(code, setup_code):
                _failures[ip].append(time.monotonic())
                flash("The setup code doesn't match the one in the server log.", "error")
            elif len(pw1) < MIN_PASSWORD_LENGTH:
                flash(f"Use at least {MIN_PASSWORD_LENGTH} characters.", "error")
            elif pw1 != pw2:
                flash("The two passwords don't match.", "error")
            else:
                h.save_setting("password_hash", hash_password(pw1))
                h.session().commit()
                current_app.config["SETUP_CODE"] = None
                http_session.clear()
                http_session["logged_in"] = True
                http_session.permanent = True
                flash("Password set. You're signed in.", "ok")
                return redirect(next_url)
        else:
            if check_password_hash(h.setting("password_hash"), request.form.get("password", "")):
                _failures.pop(ip, None)
                http_session.clear()  # new session id after login
                http_session["logged_in"] = True
                http_session.permanent = bool(request.form.get("remember"))
                return redirect(next_url)
            _failures[ip].append(time.monotonic())
            time.sleep(0.5)  # slow down guessing
            flash("Wrong password.", "error")
    return render_template("login.html", first_run=not has_password)


@bp.route("/logout", methods=["POST"])
def logout():
    http_session.clear()
    flash("Signed out.", "ok")
    return redirect(url_for("auth.login"))
