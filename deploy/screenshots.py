"""Re-capture the admin-UI screenshots used in docs/.

Development tool, run by hand -- not imported by the application and not in
the image. Without it, refreshing the docs after a UI change means
rediscovering which pages, viewport sizes and themes were used.

    pip install playwright          # drives the Chrome already installed
    OPSTRAIL_ADMIN_PASSWORD=... python deploy/screenshots.py

Images land in docs/images/ at 2x, then want downscaling to 1600px wide:

    cd docs/images && for f in *.png; do sips --resampleWidth 1600 "$f"; done

Note `--resampleWidth`, not `-Z`: `-Z` scales the LONGEST side, which squashes
the tall dashboard capture down to an unreadable width.
"""

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = os.getenv("OPSTRAIL_URL", "http://localhost:8000")
USER = os.getenv("OPSTRAIL_ADMIN_USER", "admin")
PWD = os.getenv("OPSTRAIL_ADMIN_PASSWORD", "")
OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "images"

ALERTS = "/alerts?size=25"
# Sorted by severity: the ordering is by RANK, not alphabetically, which is
# the thing worth showing.
BY_SEVERITY = "/alerts?sort=severity&dir=desc&size=25"

# name, path, viewport height, theme, sidebar collapsed, element selector
SHOTS = [
    ("alerts-list",          ALERTS,               1000, "light", False, None),
    ("alerts-sort-severity", BY_SEVERITY,           820, "light", False, None),
    ("alerts-pager",         ALERTS,               1000, "light", False, ".pager"),
    ("alert-detail",         "/alerts/761",        1050, "light", False, None),
    ("unlabelled",           "/unlabelled",         900, "light", False, None),
    ("dashboard-light",      "/dashboard",         2400, "light", False, None),
    ("dashboard-dark",       "/dashboard",         1150, "dark",  False, None),
    ("label-rules",          "/labels/rules",      1000, "light", False, None),
    ("services",             "/labels/services",    900, "light", False, None),
    ("teams",                "/labels/teams",       800, "light", False, None),
    ("owners",               "/labels/owners",      800, "light", False, None),
    ("users",                "/users",              800, "light", False, None),
    ("template-editor",      "/settings/template", 1100, "light", False, None),
    ("sidebar-collapsed",    ALERTS,                800, "light", True,  None),
]


def main() -> int:
    if not PWD:
        print("set OPSTRAIL_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  device_scale_factor=2)
        page = ctx.new_page()

        page.goto(f"{BASE}/login")
        page.fill("input[name=username]", USER)
        page.fill("input[name=password]", PWD)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        if "/login" in page.url:
            print("login failed", file=sys.stderr)
            return 1

        for name, path, height, theme, collapsed, selector in SHOTS:
            page.set_viewport_size({"width": 1280, "height": height})
            # Theme and sidebar state live in localStorage and are applied by
            # an inline script in <head>, so they must be set before the load.
            page.evaluate(
                "([t, c]) => { localStorage.setItem('opstrail-theme', t);"
                " localStorage.setItem('opstrail-sidebar',"
                " c ? 'collapsed' : 'expanded'); }",
                [theme, collapsed],
            )
            resp = page.goto(f"{BASE}{path}")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)  # let Chart.js finish drawing
            if resp and resp.status >= 400:
                print(f"SKIP {name}: HTTP {resp.status} at {path}", file=sys.stderr)
                continue
            target = page.locator(selector).first if selector else page
            try:
                target.screenshot(path=str(OUT / f"{name}.png"))
            except Exception as exc:  # noqa: BLE001
                print(f"SKIP {name}: {exc}", file=sys.stderr)
                continue
            print(f"{name}.png  {path}  {theme}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
