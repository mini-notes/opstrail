"""Shared Jinja2 environment for the server-rendered admin UI.

Separate from app/notifiers/render.py: that one emits JSON for Adaptive Cards
with autoescaping OFF, while this one emits HTML and MUST autoescape. Keeping
one Environment for both would make an alert annotation an XSS vector.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.autoescape = True


def _asset_version() -> str:
    """A token that changes whenever a static file changes.

    Nothing sets Cache-Control on /static, so a browser holds on to app.css
    and the vendored scripts indefinitely. After an upgrade that means stale
    CSS against new markup -- a layout that is broken for exactly the people
    who were already using the tool, and fixed by a hard refresh nobody
    thinks to do. The newest mtime under static/ is enough: it moves on every
    deploy that changed an asset, and not otherwise.
    """
    static = Path(__file__).resolve().parent / "static"
    try:
        newest = max(f.stat().st_mtime for f in static.rglob("*") if f.is_file())
    except ValueError:  # no static files at all
        return "0"
    return f"{int(newest):x}"


# Computed once at import: the files cannot change under a running process
# without a restart, and stat-ing the tree per request would be wasteful.
templates.env.globals["asset_v"] = _asset_version()
