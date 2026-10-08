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
