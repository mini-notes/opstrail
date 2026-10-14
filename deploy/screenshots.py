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

The detail pages find their own subject rather than naming a row id: an id
that exists in one database is a 404 in the next, and a 404 captured into the
docs is worse than no capture because nothing looks wrong about it.

Timestamps render in the viewer's timezone, so captures would otherwise
differ by whoever ran this. They are pinned to UTC; set
OPSTRAIL_SHOT_TZ=local to capture in the machine's own zone instead.
"""

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = os.getenv("OPSTRAIL_URL", "http://localhost:8000")
USER = os.getenv("OPSTRAIL_ADMIN_USER", "admin")
PWD = os.getenv("OPSTRAIL_ADMIN_PASSWORD", "")
SHOT_TZ = os.getenv("OPSTRAIL_SHOT_TZ", "utc")
OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "images"

ALERTS = "/alerts?size=25"
# Sorted by severity: the ordering is by RANK, not alphabetically, which is
# the thing worth showing.
BY_SEVERITY = "/alerts?sort=severity&dir=desc&size=25"

# Rows carry their own link in an onclick handler; this is the only place the
# capture script knows about the table markup.
ROW_IDS = """
() => [...document.querySelectorAll('tbody tr[onclick]')]
  .map(r => (/\\/(\\d+)'/.exec(r.getAttribute('onclick') || '') || [])[1])
  .filter(Boolean)
"""

# The delivery detail page is only worth a screenshot when there is an error
# on it -- that blob is the reason the page exists. A `<pre>` is rendered
# only in that branch.
HAS_ERROR_BLOCK = "() => !!document.querySelector('.card pre')"

# Opens the absolute half of the time picker, so the docs show the control in
# the state worth explaining rather than its resting state.
OPEN_ABSOLUTE_RANGE = """
() => {
  const sel = document.querySelector('[data-tr-days]');
  if (!sel) return;
  sel.value = 'abs';
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  const from = document.querySelector('[data-tr-from]');
  const to = document.querySelector('[data-tr-to]');
  if (from) from.value = '2026-10-01T00:00';
  if (to) to.value = '2026-10-05T00:00';
}
"""


def shot(name, path, height=900, theme="light", collapsed=False,
         selector=None, prepare=None):
    return {
        "name": name, "path": path, "height": height, "theme": theme,
        "collapsed": collapsed, "selector": selector, "prepare": prepare,
    }


def row_ids(page, listing: str) -> list[str]:
    page.goto(f"{BASE}{listing}")
    page.wait_for_load_state("networkidle")
    return page.evaluate(ROW_IDS)


def find_subject(page, *listings: str) -> str | None:
    """The id of the first row of the first listing that has any.

    Earlier listings are the more illustrative views; a clean install may
    have none of those, so each falls back to the next.
    """
    for listing in listings:
        found = row_ids(page, listing)
        if found:
            return found[0]
    return None


def find_delivery(page, probe_limit: int = 24) -> str | None:
    """A delivery whose detail page has something to show.

    Picking the newest failure is not enough: a row can be marked failed and
    carry no error text at all, and a screenshot of "no error recorded" on a
    page whose whole point is the error teaches the reader nothing. So the
    candidates are opened until one of them actually has the block.
    """
    # Every listing is read before any probing. Stopping at the first one
    # that filled the quota would hide a newer, better subject sitting in
    # the next -- which is exactly what happened the first time this ran.
    candidates: list[str] = []
    for listing in ("/deliveries?delivery_status=failed&size=25",
                    "/deliveries?delivery_status=sent&size=25",
                    "/deliveries?size=25"):
        for found in row_ids(page, listing):
            if found not in candidates:
                candidates.append(found)

    for candidate in candidates[:probe_limit]:
        page.goto(f"{BASE}/deliveries/{candidate}")
        page.wait_for_load_state("networkidle")
        if page.evaluate(HAS_ERROR_BLOCK):
            return candidate
    # Nothing in this database has an error. Still better than no capture.
    return candidates[0] if candidates else None


def build_shots(page) -> list[dict]:
    # A resolved alert shows a duration; an unresolved one shows a dash.
    alert_id = find_subject(page, "/alerts?alert_status=resolved&size=25", ALERTS)
    delivery_id = find_delivery(page)

    shots = [
        shot("alerts-list", ALERTS, 1000),
        shot("alerts-sort-severity", BY_SEVERITY, 820),
        shot("alerts-pager", ALERTS, 1000, selector=".pager"),
        shot("timerange", ALERTS, 700, selector=".filters",
             prepare=OPEN_ABSOLUTE_RANGE),
        shot("deliveries", "/deliveries?size=25", 1000),
        shot("retention", "/settings/retention", 900),
        shot("unlabelled", "/unlabelled", 900),
        shot("dashboard-light", "/dashboard", 2400),
        shot("dashboard-dark", "/dashboard", 1150, theme="dark"),
        shot("label-rules", "/labels/rules", 1000),
        shot("services", "/labels/services", 900),
        shot("teams", "/labels/teams", 800),
        shot("owners", "/labels/owners", 800),
        shot("users", "/users", 800),
        shot("template-editor", "/settings/template", 2000),
        shot("sidebar-collapsed", ALERTS, 800, collapsed=True),
    ]
    if alert_id:
        shots.insert(4, shot("alert-detail", f"/alerts/{alert_id}", 1050))
    else:
        print("SKIP alert-detail: no alerts in this database", file=sys.stderr)
    if delivery_id:
        shots.insert(6, shot("delivery-detail", f"/deliveries/{delivery_id}", 950))
    else:
        print("SKIP delivery-detail: no deliveries in this database",
              file=sys.stderr)
    return shots


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

        for item in build_shots(page):
            page.set_viewport_size({"width": 1280, "height": item["height"]})
            # Theme, sidebar and timezone all live in localStorage and are
            # applied before first paint, so they must be set before the load.
            page.evaluate(
                "([t, c, z]) => {"
                " localStorage.setItem('opstrail-theme', t);"
                " localStorage.setItem('opstrail-sidebar',"
                " c ? 'collapsed' : 'expanded');"
                " localStorage.setItem('opstrail-tz', z); }",
                [item["theme"], item["collapsed"], SHOT_TZ],
            )
            resp = page.goto(f"{BASE}{item['path']}")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)  # let Chart.js finish drawing
            if resp and resp.status >= 400:
                print(f"SKIP {item['name']}: HTTP {resp.status} at {item['path']}",
                      file=sys.stderr)
                continue
            if item["prepare"]:
                page.evaluate(item["prepare"])
                page.wait_for_timeout(400)
            target = (
                page.locator(item["selector"]).first if item["selector"] else page
            )
            try:
                target.screenshot(path=str(OUT / f"{item['name']}.png"))
            except Exception as exc:  # noqa: BLE001
                print(f"SKIP {item['name']}: {exc}", file=sys.stderr)
                continue
            print(f"{item['name']}.png  {item['path']}  {item['theme']}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
