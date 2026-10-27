"""Render an Alert into an MS Teams Adaptive Card.

Jinja2 emits the card JSON directly rather than producing markdown that a
converter then turns into a card. A round trip through markdown cannot express
a FactSet or an Action, and when it goes wrong the failure surfaces at Power
Automate rather than here. Emitting JSON means `json.loads()` catches a
malformed template immediately, with the offending text in the error.

Templates come from the database when an operator has saved one, and from the
file in `app/templates/msteams/` otherwise. `render_card_safe()` is what the
sender uses: if a saved template fails on a particular alert it falls back to
the built-in one, because an alert that never arrives is worse than an ugly
card.
"""

import json
import logging
from pathlib import Path
from urllib.parse import quote

from jinja2 import FileSystemLoader, StrictUndefined, nodes, select_autoescape
from jinja2.sandbox import ImmutableSandboxedEnvironment

from app.models import Alert
from app.services import severity as severity_mod

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
BUILTIN_TEMPLATE = "msteams/default.json.j2"


class CardEnvironment(ImmutableSandboxedEnvironment):
    intercepted_binops = frozenset({"*", "**"})

    def call_binop(self, context, operator, left, right):
        if operator == "*":
            for value, times in ((left, right), (right, left)):
                if isinstance(value, (str, list, tuple)) and isinstance(times, int):
                    if len(value) * max(times, 0) > 262_144:
                        raise ValueError("template result limit exceeded")
        if operator == "**":
            raise ValueError("exponentiation is disabled in card templates")
        if isinstance(left, (str, list, tuple)) and isinstance(right, int) and right > 100:
            raise ValueError("template repetition limit exceeded")
        if isinstance(right, (str, list, tuple)) and isinstance(left, int) and left > 100:
            raise ValueError("template repetition limit exceeded")
        return super().call_binop(context, operator, left, right)

    def is_safe_callable(self, obj):
        owner = getattr(obj, "__self__", None)
        name = getattr(obj, "__name__", "")
        return isinstance(owner, dict) and name in {"get", "items", "keys", "values"}


_env = CardEnvironment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    undefined=StrictUndefined,
    # autoescape is for HTML; these templates emit JSON and escape via
    # the `tojson` filter instead.
    autoescape=select_autoescape(enabled_extensions=("html",), default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)

_env.globals.clear()
_env.filters.pop("format", None)

# Severity vocabulary seen in the wild, folded into four bands.
#
# Prometheus itself has no severity concept -- `severity` is only a label by
# convention, so every shop writes it differently. The aliases below cover the
# three families that actually turn up: the Alertmanager example-rule words
# (critical/warning/info), syslog-derived words (emerg/alert/err/notice), and
# the incident grades operations teams use (P1-P4, SEV1-SEV4). Anything
# unrecognised is passed through upper-cased rather than silently relabelled,
# because guessing wrong about how bad something is, is worse than admitting
# we do not know.
# Banding moved to app/services/severity.py so the card, the mention
# threshold and the SQL sort order cannot disagree again.

# Traffic-light colours: the convention a reader already knows, so the band is
# legible before the word is read. Teams renders these inline in the FactSet.
_SEVERITY_ICONS = {
    "CRITICAL": "\U0001f534",  # red circle
    "ERROR": "\U0001f7e0",  # orange circle
    "WARNING": "\U0001f7e1",  # yellow circle
    "INFO": "\U0001f535",  # blue circle
}
_STATUS_ICONS = {
    "FIRING": "\U0001f525",  # fire
    "RESOLVED": "\u2705",  # check mark
}

# Labels worth putting on the card, in the order an operator reads them:
# what is broken, then where. `pod` and `job` are deliberately absent -- they
# are in the OpsTrail detail page, and a card that lists everything is a card
# nobody reads.
_CONTEXT_LABELS = (
    ("instance", "Instance"),
    ("cluster", "Cluster"),
    ("namespace", "Namespace"),
    ("environment", "Environment"),
    ("env", "Environment"),
)

# Prometheus does NOT send the observed value or the threshold: the webhook
# payload carries only status/labels/annotations/timestamps. Both only reach
# OpsTrail when the alerting rule puts them in an annotation, typically with
# `{{ $value }}`. These are the key names we accept, annotations first.
_VALUE_KEYS = ("value", "current_value", "current", "observed", "actual", "metric_value")
_THRESHOLD_KEYS = ("threshold", "limit", "expected", "target")


def band_severity(raw: str | None) -> str:
    """Map a free-form severity label onto one of four bands."""
    if not raw:
        return "INFO"
    return severity_mod.band(raw)


def _lookup(alert: Alert, keys: tuple[str, ...]) -> str | None:
    """Annotations first, then labels. Case-insensitive, blank counts as absent."""
    for source in (alert.annotations or {}, alert.labels or {}):
        folded = {str(k).lower(): v for k, v in source.items()}
        for key in keys:
            value = folded.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def value_line(alert: Alert) -> str | None:
    """The headline measurement: `93% (ngưỡng 85%)`.

    Values are shown exactly as the rule author wrote them. Parsing `1.2 GB`
    into a number to reformat it is how units get mangled, so the string is
    passed through untouched.

    Returns None when the rule carries neither -- the card then simply has no
    such line rather than a row of blanks.
    """
    value = _lookup(alert, _VALUE_KEYS)
    threshold = _lookup(alert, _THRESHOLD_KEYS)
    if value and threshold:
        return f"{value} (ngưỡng {threshold})"
    if value:
        return value
    if threshold:
        return f"ngưỡng {threshold}"
    return None


def duration_line(alert: Alert) -> str | None:
    """How long the alert was firing -- the headline on a RESOLVED card."""
    if not alert.ends_at or not alert.starts_at:
        return None
    return f"Đã xử lý sau {str(alert.ends_at - alert.starts_at).split('.')[0]}"


def _silence_url(alert: Alert) -> str | None:
    """Deep link into Alertmanager's 'new silence' form, pre-filtered.

    Built from `external_url`, which Alertmanager includes in its payload.
    Saves the person on call from retyping matchers at 3am.
    """
    if not alert.external_url or not alert.alertname:
        return None
    matchers = [f'alertname="{alert.alertname}"']
    instance = (alert.labels or {}).get("instance")
    if instance:
        matchers.append(f'instance="{instance}"')
    f = quote("{" + ", ".join(matchers) + "}", safe="")
    return f"{alert.external_url.rstrip('/')}/#/silences/new?filter={f}"


def _facts(alert: Alert, service_name: str | None) -> list:
    """Ordered (title, value) pairs for the card's FactSet.

    Deliberately short: what is broken and where, nothing else. Severity moved
    to the title, and Team/Owner left the card entirely -- they drive channel
    routing and the dashboards, which is a different job from telling the
    person reading the card what to look at.

    Only non-empty entries are included so a sparse alert does not render a
    column of dashes.
    """
    out: list[tuple[str, str]] = []
    if service_name:
        out.append(("Service", service_name))

    labels = alert.labels or {}
    seen: set[str] = set()
    for key, title in _CONTEXT_LABELS:
        if title in seen:
            continue
        value = labels.get(key)
        if value:
            out.append((title, value))
            seen.add(title)

    out.append(("Bắt đầu", alert.starts_at.strftime("%Y-%m-%d %H:%M:%S UTC")))
    if alert.ends_at:
        out.append(("Kết thúc", alert.ends_at.strftime("%Y-%m-%d %H:%M:%S UTC")))
    return out


# What a template may reference, documented next to the function that
# produces it. The editor renders its variable table from this list.
#
# The previous table was hand-written in template_editor.html and had already
# drifted: it never mentioned value_line or duration_line, the two variables
# an operator most needs to know about, while the built-in template used
# both. A test asserts this list and build_context() agree, so adding a key
# without documenting it fails the suite rather than quietly going missing.
CONTEXT_VARS: tuple[tuple[str, str], ...] = (
    (
        "alert",
        "Toàn bộ bản ghi: alertname, severity, summary, description, "
        "labels, annotations, starts_at, ends_at…",
    ),
    ("service", "Tên hiển thị của service. Rỗng nếu alert chưa gán nhãn"),
    ("team", "Tên hiển thị của team. Rỗng nếu alert chưa gán nhãn"),
    (
        "severity",
        "Đã chuẩn hoá: CRITICAL / ERROR / WARNING / INFO. Là mức "
        "**sau** khi severity rule điều chỉnh, nếu có",
    ),
    (
        "severity_source",
        "Mức gốc Alertmanager gửi, **chỉ** có khi rule đã đổi nó. None nghĩa là không ai đụng tới",
    ),
    ("status", "FIRING hoặc RESOLVED"),
    ("icon", "Emoji theo severity"),
    ("status_icon", "Emoji theo trạng thái"),
    ("facts", "Danh sách (tiêu đề, giá trị) đã lọc bỏ trường rỗng"),
    (
        "value_line",
        "Ví dụ `93% (ngưỡng 85%)`. **None** nếu alerting rule không gửi annotation value/threshold",
    ),
    ("duration_line", "Ví dụ `Đã xử lý sau 0:42:15`. None khi alert chưa kết thúc"),
    ("owners", "Tên người phụ trách (luôn có nếu đã gán, kể cả khi không mention)"),
    (
        "mention_entities",
        "Khối msteams.entities. Rỗng nếu chưa đạt ngưỡng severity hoặc chưa có owner",
    ),
    ("mention_text", "Chuỗi <at>Tên</at> khớp với entities. Rỗng khi không mention"),
    ("generator_url", "Link đồ thị Prometheus"),
    ("detail_url", "Link trang chi tiết trong OpsTrail"),
    ("runbook_url", "Từ annotation runbook_url / runbook"),
    ("dashboard_url", "Từ annotation dashboard_url / dashboard"),
    ("silence_url", "Link tạo silence trong Alertmanager, dựng từ external_url"),
)


def build_context(
    alert: Alert,
    *,
    service_name: str | None = None,
    team_name: str | None = None,
    detail_url: str | None = None,
    mention_entities: list[dict] | None = None,
    mention_text: str | None = None,
    owner_names: list[str] | None = None,
) -> dict:
    """Everything a template may reference. One place, so the editor's preview
    and the real send see identical variables."""
    status = "RESOLVED" if alert.status.value == "resolved" else "FIRING"
    severity = band_severity(alert.acting_severity)
    source_severity = band_severity(alert.severity)
    annotations = alert.annotations or {}
    return {
        "alert": alert,
        "service": service_name,
        "team": team_name,
        "severity": severity,
        # Only set when a rule moved it. Whoever is woken at 3am is entitled
        # to know that the decision was OpsTrail's and not Prometheus's.
        "severity_source": source_severity if alert.effective_severity else None,
        "status": status,
        "icon": _SEVERITY_ICONS.get(severity, ""),
        "status_icon": _STATUS_ICONS[status],
        "facts": _facts(alert, service_name),
        "value_line": value_line(alert),
        "duration_line": duration_line(alert),
        "owners": owner_names or [],
        "mention_entities": mention_entities or [],
        "mention_text": mention_text or "",
        "generator_url": alert.generator_url or None,
        "detail_url": detail_url,
        "runbook_url": annotations.get("runbook_url") or annotations.get("runbook"),
        "dashboard_url": annotations.get("dashboard_url") or annotations.get("dashboard"),
        "silence_url": _silence_url(alert),
    }


def render_source(source: str, context: dict) -> dict:
    """Render template text and parse it. Raises ValueError on bad JSON."""
    if len(source.encode()) > 32_768:
        raise ValueError("template exceeds 32 KiB")
    # Never expose SQLAlchemy objects (and their methods/session) to templates.
    context = dict(context)
    alert = context.get("alert")
    if isinstance(alert, Alert):
        context["alert"] = {
            column.key: getattr(alert, column.key) for column in Alert.__table__.columns
        }
    tree = _env.parse(source)
    for loop in tree.find_all(nodes.For):
        if loop.recursive or any(loop.find_all(nodes.For)):
            raise ValueError("nested or recursive loops are disabled")
    if any(
        tree.find_all(
            (
                nodes.Macro,
                nodes.Import,
                nodes.Include,
                nodes.Extends,
                nodes.FromImport,
                nodes.CallBlock,
            )
        )
    ):
        raise ValueError("template composition is disabled")
    if len(json.dumps(context, default=str).encode()) > 262_144:
        raise ValueError("template context exceeds 256 KiB")
    parts = []
    total = 0
    for part in _env.from_string(source).generate(**context):
        total += len(part.encode())
        if total > 262_144:
            raise ValueError("rendered card exceeds 256 KiB")
        parts.append(part)
    rendered = "".join(parts)
    if len(rendered.encode()) > 262_144:
        raise ValueError("rendered card exceeds 256 KiB")
    try:
        card = json.loads(rendered)
        if not isinstance(card, dict):
            raise ValueError("card template must produce a JSON object")
        return card
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"template did not produce valid JSON: {exc}\n--- output ---\n{rendered}"
        ) from exc


def builtin_source() -> str:
    return (TEMPLATE_DIR / BUILTIN_TEMPLATE).read_text()


def render_card(context: dict, source: str | None = None) -> dict:
    """Render with `source`, or the built-in template when None."""
    return render_source(source if source is not None else builtin_source(), context)


def render_card_safe(context: dict, source: str | None) -> tuple[dict, str | None]:
    """Render, falling back to the built-in template if a custom one fails.

    Returns (card, fallback_reason). A saved template can be valid against the
    sample alert used at save time and still break on a real one -- an alert
    with no service, an annotation containing an unexpected type. When that
    happens the alert must still reach Teams, so the built-in template takes
    over and the reason is recorded against the notification.
    """
    if source is None:
        return render_card(context), None
    try:
        return render_source(source, context), None
    except Exception as exc:  # noqa: BLE001 - any template error must degrade
        reason = f"custom template failed, used built-in: {type(exc).__name__}: {exc}"
        log.error("msteams: %s", reason)
        return render_card(context), reason
