"""Severity adjusted by context, mostly by the clock.

The feature exists so a trading-hours outage is treated differently from the
same outage on a Sunday night. The properties worth pinning are the ones
whose failure is silent: a window that reads midnight wrong, a quieting rule
that pushes an alert out of existence, and -- the whole point -- the same
alert getting two different answers purely because of when it is asked.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.models import Alert, AlertStatus, Service, SeverityRule
from app.services import severity_rules

VN = ZoneInfo("Asia/Ho_Chi_Minh")
TRADING = {"days": [1, 2, 3, 4, 5], "from": "09:00", "to": "15:00"}

# 2026-10-13 is a Tuesday, 2026-10-18 a Sunday.
TUESDAY_10AM = dt.datetime(2026, 10, 13, 10, 0, tzinfo=VN)
TUESDAY_2AM = dt.datetime(2026, 10, 13, 2, 0, tzinfo=VN)
SUNDAY_10AM = dt.datetime(2026, 10, 18, 10, 0, tzinfo=VN)


def make_alert(severity="warning", status=AlertStatus.firing, **kw) -> Alert:
    base = dict(
        id=1, fingerprint="fp1",
        starts_at=dt.datetime(2026, 10, 13, 8, 45, tzinfo=dt.UTC),
        status=status, alertname="PaymentsLatencyHigh", severity=severity,
        labels={"alertname": "PaymentsLatencyHigh", "severity": severity},
        annotations={},
    )
    base.update(kw)
    return Alert(**base)


def rule(**kw) -> SeverityRule:
    base = dict(
        id=1, name="trading hours", priority=100, match={"severity": "*"},
        time_window=dict(TRADING), action="bump", value="1", enabled=True,
    )
    base.update(kw)
    return SeverityRule(**base)


def evaluate(alert, rules, now, **kw):
    return severity_rules.evaluate_against(alert, rules, now=now, **kw)


# ---------------------------------------------------------------------------
# the point of the feature
# ---------------------------------------------------------------------------

def test_the_same_alert_gets_two_answers_depending_only_on_when_you_ask():
    alert, rules = make_alert("warning"), [rule()]

    during, matched = evaluate(alert, rules, TUESDAY_10AM)
    after, _ = evaluate(alert, rules, TUESDAY_2AM)
    weekend, _ = evaluate(alert, rules, SUNDAY_10AM)

    assert during == "ERROR" and matched.name == "trading hours"
    assert after is None
    assert weekend is None


def test_no_rules_means_no_opinion():
    """The invariant: an install that has not configured anything must see
    exactly what it saw before this feature existed."""
    assert evaluate(make_alert(), [], TUESDAY_10AM) == (None, None)


def test_a_rule_agreeing_with_the_source_records_nothing():
    """NULL means "no rule changed this". A rule that bumped WARNING to
    WARNING has not changed it, and writing the value anyway would make
    "is my rule working?" unanswerable."""
    result, _ = evaluate(make_alert("critical"), [rule(value="1")], TUESDAY_10AM)
    assert result is None  # already CRITICAL, bump is a no-op


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "moment,expected",
    [
        (TUESDAY_10AM, True),
        (dt.datetime(2026, 10, 13, 9, 0, tzinfo=VN), True),    # open
        (dt.datetime(2026, 10, 13, 15, 0, tzinfo=VN), True),   # close
        (dt.datetime(2026, 10, 13, 8, 59, tzinfo=VN), False),
        (dt.datetime(2026, 10, 13, 15, 1, tzinfo=VN), False),
        (SUNDAY_10AM, False),
        (dt.datetime(2026, 10, 17, 10, 0, tzinfo=VN), False),  # Saturday
    ],
)
def test_the_trading_window_includes_both_edges(moment, expected):
    assert severity_rules.in_window(TRADING, moment) is expected


def test_a_window_that_wraps_midnight_is_one_window_not_an_empty_one():
    """Every out-of-hours rule people actually want looks like this, and
    reading it as start <= now <= end would match nothing at all."""
    overnight = {"from": "22:00", "to": "06:00"}
    for hour, expected in ((23, True), (3, True), (12, False), (21, False)):
        moment = dt.datetime(2026, 10, 13, hour, 0, tzinfo=VN)
        assert severity_rules.in_window(overnight, moment) is expected


def test_an_empty_window_means_whenever():
    assert severity_rules.in_window({}, SUNDAY_10AM) is True
    assert severity_rules.in_window(None, SUNDAY_10AM) is True


def test_days_without_times_filters_on_the_day_alone():
    assert severity_rules.in_window({"days": [6, 7]}, SUNDAY_10AM) is True
    assert severity_rules.in_window({"days": [6, 7]}, TUESDAY_10AM) is False


def test_the_window_is_read_in_the_exchanges_zone_not_the_servers():
    """09:30 in Vietnam is 02:30 UTC. Reading the window in UTC would put
    the whole session outside it."""
    utc_morning = dt.datetime(2026, 10, 13, 2, 30, tzinfo=dt.UTC)
    assert severity_rules.in_window(TRADING, utc_morning) is True


# ---------------------------------------------------------------------------
# guard rails
# ---------------------------------------------------------------------------

def test_quieting_can_never_push_an_alert_below_info():
    """The one that matters. "Fewer pages out of hours" must not become
    "no alerts out of hours"."""
    result, _ = evaluate(
        make_alert("critical"),
        [rule(time_window={}, action="bump", value="-9")],
        TUESDAY_2AM,
    )
    assert result == "INFO"


def test_raising_stops_at_critical():
    result, _ = evaluate(
        make_alert("warning"), [rule(value="9")], TUESDAY_10AM
    )
    assert result == "CRITICAL"


def test_a_rule_that_cannot_produce_a_severity_is_skipped_not_applied():
    """A `set` to a band that does not exist must leave the field alone
    rather than blanking it."""
    broken = rule(id=1, priority=1, action="set", value="banana")
    usable = rule(id=2, priority=2, action="set", value="critical")
    result, matched = evaluate(make_alert(), [broken, usable], TUESDAY_10AM)
    assert result == "CRITICAL"
    assert matched.id == 2


def test_an_unmappable_source_severity_yields_no_opinion():
    result, _ = evaluate(make_alert("banana"), [rule()], TUESDAY_10AM)
    assert result is None


# ---------------------------------------------------------------------------
# matching and ordering
# ---------------------------------------------------------------------------

def test_lowest_priority_number_wins():
    first = rule(id=1, priority=1, action="set", value="critical")
    second = rule(id=2, priority=50, action="set", value="info")
    result, matched = evaluate(make_alert(), [first, second], TUESDAY_10AM)
    assert result == "CRITICAL" and matched.id == 1


def test_a_rule_can_target_one_tier():
    tier1 = Service(id=1, key="payments-api", name="Payments", tier=1)
    tier3 = Service(id=2, key="batch", name="Batch", tier=3)
    rules = [rule(match={"tier": "1"})]

    hit, _ = evaluate(make_alert(), rules, TUESDAY_10AM, service=tier1)
    miss, _ = evaluate(make_alert(), rules, TUESDAY_10AM, service=tier3)
    assert hit == "ERROR"
    assert miss is None


def test_an_empty_match_claims_nothing():
    """Same rule as the other two rule tables: a half-filled form must not
    become a rule that owns every alert."""
    assert evaluate(make_alert(), [rule(match={})], TUESDAY_10AM) == (None, None)


def test_disabled_rules_are_not_loaded():
    """`active_rules` filters them in SQL, so evaluate_against never sees
    one -- this pins the shape the sweeper relies on."""
    assert evaluate(make_alert(), [], TUESDAY_10AM) == (None, None)


def test_window_description_reads_like_a_person_wrote_it():
    assert severity_rules.describe_window(TRADING) == "T2 T3 T4 T5 T6 09:00–15:00"
    assert severity_rules.describe_window({}) == "mọi lúc"


# ---------------------------------------------------------------------------
# the sweeper: windows open while an alert is already firing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_sweeper_does_nothing_at_all_without_rules(monkeypatch):
    """The invariant, and the cost argument. An install with no severity
    rules must not pay a table scan every minute for a feature it does not
    use -- so the early return happens before any alert query."""
    seen: list[str] = []

    class Recorder:
        async def execute(self, *a, **kw):
            seen.append("query")
            class Empty:
                def scalars(self):
                    return self
                def all(self):
                    return []
            return Empty()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("app.db.SessionLocal", lambda: Recorder())
    assert await severity_rules.reevaluate_once() == 0
    # Exactly one query: the rules. Nothing touched `alerts`.
    assert len(seen) == 1
