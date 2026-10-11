"""Card rendering and the two layers that stop a template from silencing alerts.

Layer 1 refuses to store something that cannot render. Layer 2 falls back to
the built-in template when a stored one breaks on a particular alert. Layer 2
is the one that matters: a template can be valid against the sample used at
save time and still fail on an alert with no service.
"""

import datetime as dt

import pytest

from app.models import Alert, AlertStatus
from app.notifiers import render


def make_alert(**kw) -> Alert:
    defaults = dict(
        id=1,
        fingerprint="fp1",
        starts_at=dt.datetime(2026, 10, 7, 8, 30, tzinfo=dt.UTC),
        ends_at=None,
        status=AlertStatus.firing,
        alertname="PostgresqlDown",
        severity="critical",
        summary="Postgres down",
        description=None,
        labels={},
        annotations={},
        generator_url=None,
        external_url=None,
    )
    defaults.update(kw)
    return Alert(**defaults)


def ctx(alert, **kw):
    return render.build_context(alert, **kw)


# ---------------------------------------------------------------------------
# severity banding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("critical", "CRITICAL"), ("P1", "CRITICAL"), ("fatal", "CRITICAL"),
        ("error", "ERROR"), ("high", "ERROR"),
        ("warning", "WARNING"), ("warn", "WARNING"),
        ("info", "INFO"), (None, "INFO"), ("", "INFO"),
        ("weird", "WEIRD"),   # unknown bands pass through, never swallowed
    ],
)
def test_severity_banding(raw, expected):
    assert render.band_severity(raw) == expected


# ---------------------------------------------------------------------------
# the built-in template
# ---------------------------------------------------------------------------

def test_sparse_alert_renders_without_placeholder_rows():
    """An alert carrying only a name and severity must not produce a column
    of dashes."""
    card = render.render_card(ctx(make_alert(summary=None)))
    facts = [f for b in card["body"] if b.get("type") == "FactSet" for f in b["facts"]]
    titles = [f["title"] for f in facts]
    assert titles == ["Bắt đầu"]
    assert all(f["value"].strip() not in ("", "—") for f in facts)
    assert "actions" not in card


def test_severity_is_in_the_title_not_a_fact_row():
    """One less row to read, and the colour already carries the same signal."""
    card = render.render_card(ctx(make_alert()))
    assert "CRITICAL" in card["body"][0]["text"]
    titles = [f["title"] for b in card["body"] if b.get("type") == "FactSet" for f in b["facts"]]
    assert "Severity" not in titles


def test_team_and_owner_are_not_on_the_card():
    """They route the notification and feed the dashboards; the card is for
    whoever has to look at the problem."""
    card = render.render_card(
        ctx(make_alert(), service_name="payments-api", team_name="Payments",
            owner_names=["Nguyen Van A"])
    )
    titles = [f["title"] for b in card["body"] if b.get("type") == "FactSet" for f in b["facts"]]
    assert "Team" not in titles
    assert "Owner" not in titles
    assert "Service" in titles


def test_context_labels_are_surfaced_in_order():
    alert = make_alert(
        labels={"environment": "production", "namespace": "payments",
                "instance": "pg-01", "cluster": "prod-hcm", "job": "postgres"}
    )
    card = render.render_card(ctx(alert, service_name="payments-api"))
    titles = [f["title"] for b in card["body"] if b.get("type") == "FactSet" for f in b["facts"]]
    assert titles == ["Service", "Instance", "Cluster", "Namespace",
                      "Environment", "Bắt đầu"]
    assert "Job" not in titles


# ---------------------------------------------------------------------------
# value and threshold
# ---------------------------------------------------------------------------

def test_value_and_threshold_combine():
    alert = make_alert(annotations={"value": "93%", "threshold": "85%"})
    assert render.value_line(alert) == "93% (ngưỡng 85%)"


def test_value_alone_and_threshold_alone():
    assert render.value_line(make_alert(annotations={"value": "93%"})) == "93%"
    assert render.value_line(make_alert(annotations={"threshold": "85%"})) == "ngưỡng 85%"


def test_no_value_line_when_the_rule_carries_neither():
    """Prometheus does not send the measurement; a rule that was never updated
    must still produce a clean card."""
    alert = make_alert(annotations={"summary": "x"})
    assert render.value_line(alert) is None
    card = render.render_card(ctx(alert))
    texts = [b.get("text", "") for b in card["body"]]
    assert not any("ngưỡng" in t for t in texts)


@pytest.mark.parametrize(
    "keys", [("current_value", "limit"), ("observed", "expected"), ("actual", "target")]
)
def test_synonym_keys_are_accepted(keys):
    vk, tk = keys
    assert render.value_line(make_alert(annotations={vk: "7", tk: "5"})) == "7 (ngưỡng 5)"


def test_annotations_win_over_labels():
    alert = make_alert(
        annotations={"threshold": "85%"}, labels={"threshold": "999%"}
    )
    assert render.value_line(alert) == "ngưỡng 85%"


def test_labels_are_used_when_annotations_have_none():
    assert render.value_line(make_alert(labels={"threshold": "85"})) == "ngưỡng 85"


def test_units_are_never_reformatted():
    """The rule author formatted the string; parsing it back into a number is
    how `1.2 GB` turns into `1.2`."""
    alert = make_alert(annotations={"value": "1.2 GB", "threshold": "1 GB"})
    assert render.value_line(alert) == "1.2 GB (ngưỡng 1 GB)"


def test_blank_annotation_counts_as_absent():
    assert render.value_line(make_alert(annotations={"value": "   "})) is None


# ---------------------------------------------------------------------------
# resolved cards
# ---------------------------------------------------------------------------

def test_resolved_card_leads_with_how_long_it_took():
    alert = make_alert(
        status=AlertStatus.resolved,
        ends_at=dt.datetime(2026, 10, 7, 9, 12, 45, tzinfo=dt.UTC),
    )
    card = render.render_card(ctx(alert))
    assert "RESOLVED" in card["body"][0]["text"]
    assert any("Đã xử lý sau" in b.get("text", "") for b in card["body"])


def test_resolved_card_never_mentions_anyone():
    """The work is done; waking the owner at that point is noise."""
    entities = [{"type": "mention", "text": "<at>A</at>",
                 "mentioned": {"id": "a@c.com", "name": "A"}}]
    alert = make_alert(
        status=AlertStatus.resolved,
        ends_at=dt.datetime(2026, 10, 7, 9, 12, 45, tzinfo=dt.UTC),
    )
    card = render.render_card(
        ctx(alert, mention_entities=entities, mention_text="<at>A</at> — cần xử lý")
    )
    assert "entities" not in card["msteams"]
    assert not any("<at>" in b.get("text", "") for b in card["body"])


def test_resolved_card_drops_the_silence_button():
    """Nothing left to silence."""
    alert = make_alert(
        status=AlertStatus.resolved,
        ends_at=dt.datetime(2026, 10, 7, 9, 12, 45, tzinfo=dt.UTC),
        external_url="http://am.local",
        generator_url="http://prom/graph",
    )
    card = render.render_card(ctx(alert, detail_url="http://ops/alerts/1"))
    titles = [a["title"] for a in card["actions"]]
    assert not any("Silence" in t for t in titles)
    assert not any("Prometheus" in t for t in titles)
    assert titles == ["🗂 OpsTrail"]


def test_no_mention_block_when_there_are_no_owners():
    card = render.render_card(ctx(make_alert()))
    assert "entities" not in card["msteams"]
    assert not any("<at>" in b.get("text", "") for b in card["body"])


def test_mention_block_present_when_entities_given():
    entities = [{"type": "mention", "text": "<at>A</at>",
                 "mentioned": {"id": "a@c.com", "name": "A"}}]
    card = render.render_card(
        ctx(make_alert(), mention_entities=entities, mention_text="<at>A</at> — cần xử lý")
    )
    assert card["msteams"]["entities"][0]["mentioned"]["id"] == "a@c.com"
    assert any("<at>A</at>" in b.get("text", "") for b in card["body"])


def test_silence_url_is_built_and_encoded():
    alert = make_alert(external_url="http://am.local", labels={"instance": "pg-01"})
    card = render.render_card(ctx(alert))
    silence = [a for a in card["actions"] if "Silence" in a["title"]][0]
    assert silence["url"].startswith("http://am.local/#/silences/new?filter=")
    assert "%22" in silence["url"]          # quotes are percent-encoded


def test_runbook_and_dashboard_come_from_annotations():
    alert = make_alert(
        annotations={"runbook_url": "https://wiki/rb", "dashboard_url": "https://graf/d"}
    )
    titles = [a["title"] for a in render.render_card(ctx(alert))["actions"]]
    assert any("Runbook" in t for t in titles)
    assert any("Dashboard" in t for t in titles)


def test_hostile_characters_cannot_break_the_json():
    alert = make_alert(
        alertname='Weird "quoted" \\ name',
        summary='He said "it\'s down"\nline two',
        description="unicode: áầu \U0001f525 brace } and {",
    )
    card = render.render_card(ctx(alert))
    assert 'Weird "quoted" \\ name' in card["body"][0]["text"]


def test_resolved_card_is_styled_as_good():
    alert = make_alert(
        status=AlertStatus.resolved,
        ends_at=dt.datetime(2026, 10, 7, 9, 12, tzinfo=dt.UTC),
    )
    card = render.render_card(ctx(alert))
    assert card["body"][0]["color"] == "Good"
    titles = [f["title"] for b in card["body"] if b.get("type") == "FactSet" for f in b["facts"]]
    assert "Kết thúc" in titles


# ---------------------------------------------------------------------------
# the safety layers
# ---------------------------------------------------------------------------

def test_invalid_json_template_raises_so_it_can_be_refused_at_save():
    with pytest.raises(ValueError, match="valid JSON"):
        render.render_source('{"type":"AdaptiveCard" "body":[]}', ctx(make_alert()))


def test_broken_template_falls_back_to_builtin_at_send_time():
    """The whole point: a template that breaks on THIS alert must not stop
    the alert from reaching Teams."""
    # Valid syntax, but service is None for this alert.
    broken = (
        '{"type":"AdaptiveCard","body":[{"type":"TextBlock",'
        '"text":{{ service.upper() | tojson }}}]}'
    )
    card, reason = render.render_card_safe(ctx(make_alert()), broken)

    assert card["type"] == "AdaptiveCard"
    assert "PostgresqlDown" in card["body"][0]["text"]   # built-in output
    assert reason is not None and "built-in" in reason


def test_working_custom_template_is_used_as_is():
    custom = '{"type":"AdaptiveCard","body":[{"type":"TextBlock","text":"custom"}]}'
    card, reason = render.render_card_safe(ctx(make_alert()), custom)
    assert card["body"][0]["text"] == "custom"
    assert reason is None


def test_no_custom_template_means_builtin_with_no_reason():
    card, reason = render.render_card_safe(ctx(make_alert()), None)
    assert reason is None
    assert card["$schema"].startswith("http://adaptivecards.io/")


def test_every_context_variable_is_documented():
    """The editor's variable table is generated from CONTEXT_VARS. Adding a
    key to build_context() without describing it there would leave operators
    guessing -- which is exactly how the old hand-written table lost
    value_line and duration_line."""
    documented = {name for name, _ in render.CONTEXT_VARS}
    produced = set(ctx(make_alert()).keys())
    assert documented == produced, {
        "undocumented": produced - documented,
        "described but not produced": documented - produced,
    }
