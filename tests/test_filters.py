"""Query parameters as an HTML form actually submits them.

Regression guard for a shipped bug: `<form method="get">` sends every control
including the ones left on their placeholder option, so clicking Filter with
nothing selected produced `?service_id=&team_id=&days=` and FastAPI answered
422. The original tests only ever passed parameters WITH values, which is why
they stayed green while the button was broken.
"""

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from app.api.params import OptInt, OptStr

app = FastAPI()


@app.get("/probe")
def probe(
    page: int = Query(1, ge=1),
    service_id: OptInt = None,
    team_id: OptInt = None,
    days: OptInt = None,
    q: OptStr = None,
    severity: OptStr = None,
    channel: OptStr = None,
    delivery_status: OptStr = None,
):
    return {
        "page": page, "service_id": service_id, "team_id": team_id,
        "days": days, "q": q, "severity": severity,
        "channel": channel, "delivery_status": delivery_status,
    }


client = TestClient(app)

# Exactly what the browser sends when every control is left at its default.
EMPTY_FORM = "?q=&service_id=&team_id=&severity=&alert_status=&label_source=&days="


def test_empty_form_submission_is_accepted():
    res = client.get("/probe" + EMPTY_FORM)
    assert res.status_code == 200
    body = res.json()
    assert body["service_id"] is None
    assert body["team_id"] is None
    assert body["days"] is None
    assert body["q"] is None


@pytest.mark.parametrize("param", ["service_id=", "team_id=", "days=", "q=", "severity="])
def test_each_blank_parameter_alone(param):
    assert client.get(f"/probe?{param}").status_code == 200


def test_whitespace_only_is_also_treated_as_absent():
    """A user who types spaces into the search box has not filtered anything."""
    assert client.get("/probe?q=%20%20").json()["q"] is None


def test_real_values_still_bind():
    body = client.get("/probe?service_id=3&team_id=5&days=7&q=disk&severity=critical").json()
    assert body == {
        "page": 1, "service_id": 3, "team_id": 5,
        "days": 7, "q": "disk", "severity": "critical",
        "channel": None, "delivery_status": None,
    }


@pytest.mark.parametrize("bad", ["service_id=abc", "team_id=1.5", "days=xyz"])
def test_garbage_is_still_rejected(bad):
    """Coercing blanks must not become 'accept anything' -- a typo in a URL
    should still be a 422 rather than a silently ignored filter."""
    assert client.get(f"/probe?{bad}").status_code == 422


def test_other_constraints_survive():
    assert client.get("/probe?page=0").status_code == 422
    assert client.get("/probe?page=2").json()["page"] == 2


def test_missing_parameters_behave_like_blank_ones():
    assert client.get("/probe").json() == client.get("/probe" + EMPTY_FORM).json()


# The delivery page's own controls, all left on their placeholder option.
EMPTY_DELIVERY_FORM = (
    "?size=50&q=&channel=&delivery_status=&service_id=&team_id=&days="
)


def test_empty_delivery_form_submission_is_accepted():
    """Same shipped bug, one page later: the delivery filters are a GET form
    too, so they submit blanks the moment anyone clicks Reset."""
    res = client.get("/probe" + EMPTY_DELIVERY_FORM)
    assert res.status_code == 200
    body = res.json()
    assert body["channel"] is None
    assert body["delivery_status"] is None
    assert body["service_id"] is None


def test_delivery_filters_with_values_still_arrive():
    res = client.get("/probe?channel=msteams&delivery_status=failed&days=7")
    assert res.json() == {
        "page": 1, "service_id": None, "team_id": None, "days": 7,
        "q": None, "severity": None,
        "channel": "msteams", "delivery_status": "failed",
    }


# ---------------------------------------------------------------------------
# the time picker's own parameters
# ---------------------------------------------------------------------------

from app.api.params import parse_window  # noqa: E402

EMPTY_WINDOW = "?days=&from=&to=&tz_offset="


def test_an_empty_time_picker_is_not_a_window():
    """Reset submits every field blank, including the absolute pair."""
    start, end, offset = parse_window("", "", None)
    assert (start, end, offset) == (None, None, 0)


def test_a_local_reading_becomes_an_instant():
    start, end, offset = parse_window("2026-10-12T14:00", "2026-10-12T16:00", 420)
    assert start.isoformat() == "2026-10-12T07:00:00+00:00"
    assert end.isoformat() == "2026-10-12T09:00:00+00:00"
    assert offset == 420


def test_a_reversed_range_is_swapped_rather_than_refused():
    """Dragging a date picker backwards is a normal mistake, and an empty
    dashboard is a worse answer than the range they clearly meant."""
    start, end, _ = parse_window("2026-10-12T16:00", "2026-10-12T14:00", 0)
    assert start < end


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-45T99:99", "???"])
def test_an_unparseable_range_falls_back_instead_of_failing(bad):
    """A mangled `?from=` in a pasted URL should draw the default view, not
    refuse to draw anything."""
    assert parse_window(bad, "2026-10-12T16:00", 0) == (None, None, 0)


def test_an_absurd_offset_is_ignored():
    """Nothing on earth is more than 14 hours from UTC; a larger value is a
    broken client, and trusting it would shift every day boundary."""
    _s, _e, offset = parse_window("2026-10-12T14:00", "2026-10-12T16:00", 99999)
    assert offset == 0


def test_a_value_that_already_carries_an_offset_is_taken_as_written():
    start, _e, _o = parse_window("2026-10-12T14:00:00+07:00", "2026-10-12T16:00:00+07:00", 0)
    assert start.isoformat() == "2026-10-12T07:00:00+00:00"
