"""Label-rule matching, exercised without a database.

`resolve()` needs a session, so these tests target the pure matching helpers
that carry the actual logic; end-to-end attribution is covered by the live
Postgres run documented in README.
"""

from app.services.labeling import (
    SERVICE_LABEL_KEYS,
    TEAM_LABEL_KEYS,
    _lookup,
    _rule_matches,
)


def test_lookup_prefers_earlier_keys():
    labels = {"job": "node-exporter", "service": "payments-api", "app": "pay"}
    assert _lookup(labels, SERVICE_LABEL_KEYS) == "payments-api"


def test_lookup_is_case_insensitive():
    assert _lookup({"Service": "pay"}, SERVICE_LABEL_KEYS) == "pay"
    assert _lookup({"TEAM": "core"}, TEAM_LABEL_KEYS) == "core"


def test_lookup_falls_through_to_job_last():
    """`job` names a scrape target more often than a service, so it is the
    last resort rather than a peer of `service`."""
    assert _lookup({"job": "node-exporter"}, SERVICE_LABEL_KEYS) == "node-exporter"


def test_lookup_skips_empty_values():
    assert _lookup({"service": "", "app": "pay"}, SERVICE_LABEL_KEYS) == "pay"


def test_lookup_returns_none_when_nothing_matches():
    assert _lookup({"instance": "pg-01"}, SERVICE_LABEL_KEYS) is None


def test_rule_requires_every_match_entry():
    labels = {"alertname": "KubePodCrashLooping", "namespace": "payments"}
    assert _rule_matches({"alertname": "KubePodCrashLooping"}, labels)
    assert _rule_matches(
        {"alertname": "KubePodCrashLooping", "namespace": "payments"}, labels
    )
    # one alertname spans many namespaces -- a rule naming the wrong one
    # must not claim the alert
    assert not _rule_matches(
        {"alertname": "KubePodCrashLooping", "namespace": "billing"}, labels
    )


def test_rule_matching_is_case_insensitive():
    assert _rule_matches({"AlertName": "KubePodCrashLooping"},
                         {"alertname": "kubepodcrashlooping"})


def test_empty_match_never_matches():
    """An empty match dict would silently claim every alert; treat it as a
    misconfiguration instead of a catch-all."""
    assert not _rule_matches({}, {"alertname": "Anything"})


def test_rule_does_not_match_missing_label():
    assert not _rule_matches({"namespace": "payments"}, {"alertname": "X"})
