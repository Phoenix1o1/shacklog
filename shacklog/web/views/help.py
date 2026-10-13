"""Help: the user guide and the credits, shown inside the program.

The pages are Markdown files in ``shacklog/help/`` (bundled into the executable), so the
same text reads well on GitHub and in the app. Add a page: drop a ``.md`` file there and
list it in :data:`PAGES`.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, render_template

from shacklog.services import markdown_render

bp = Blueprint("help", __name__, url_prefix="/help")

HELP_DIR = Path(__file__).resolve().parents[2] / "help"

#: slug -> (file, menu label)
PAGES = {
    "guide": ("user-guide.md", "User guide"),
    "credits": ("credits.md", "Credits & licences"),
}


@bp.route("/")
@bp.route("/<page>")
def index(page: str = "guide"):
    if page not in PAGES:
        abort(404)
    filename, label = PAGES[page]
    try:
        text = (HELP_DIR / filename).read_text(encoding="utf-8")
    except OSError:
        abort(404)
    return render_template("help/page.html", body=markdown_render.render(text),
                           label=label, page=page, pages=PAGES)
