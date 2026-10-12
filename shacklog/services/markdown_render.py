"""Markdown → safe HTML for the knowledge base (notes, guides, CONTEXT.md…).

Notes are Andrea's own files, but they also arrive by sync and import, so the output is
made safe anyway: raw HTML in the source is escaped before conversion (``<`` becomes
``&lt;``), links only keep http(s)/mailto/relative targets, and every external link opens
in a new tab without referrer.

Uses Python-Markdown (pure Python) when installed; falls back to escaped text with
paragraphs if it isn't, so a missing package never breaks a page.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_SAFE_HREF = re.compile(r'href="(?!(?:https?:|mailto:|#|/))[^"]*"', re.IGNORECASE)
_EXTERNAL = re.compile(r'<a href="(https?://[^"]+)"')


def render(text: str | None) -> Markup:
    if not text:
        return Markup("")
    source = text.replace("\r\n", "\n").replace("<", "&lt;")
    try:
        import markdown
    except ImportError:
        paragraphs = re.split(r"\n\s*\n", text.strip())
        return Markup("\n".join(f"<p>{escape(p)}</p>" for p in paragraphs))
    html = markdown.markdown(source, extensions=["tables", "fenced_code", "sane_lists", "toc"],
                             output_format="html")
    html = _SAFE_HREF.sub('href="#"', html)
    html = _EXTERNAL.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer"', html)
    return Markup(html)


def title_of(text: str | None) -> str | None:
    """First Markdown heading, used as a title when importing a note."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None
