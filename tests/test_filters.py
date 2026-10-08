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
):
    return {
        "page": page, "service_id": service_id, "team_id": team_id,
        "days": days, "q": q, "severity": severity,
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
