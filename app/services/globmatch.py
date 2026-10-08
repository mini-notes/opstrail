"""Glob patterns for label-rule values, in Python AND in SQL.

A rule written in the UI has to behave identically in two places:

* `labeling.resolve()` matches in Python when an alert arrives;
* the backfill preview matches in SQL over stored alerts.

If the two drift, the preview promises one thing and ingest does another, so
both translations live here and are tested against each other.

Why glob and not regex: operators write these in a web form. `pg-prod-*` is
readable and cannot accidentally be catastrophic; a mistyped regex can be both.
"""

from fnmatch import fnmatchcase

WILDCARDS = ("*", "?")

# LIKE's own metacharacters. A literal `%` in an alert label (`disk=100%`)
# must stay literal, or the pattern silently widens to match anything.
_LIKE_SPECIAL = ("\\", "%", "_")


def has_wildcard(pattern: str) -> bool:
    return any(w in pattern for w in WILDCARDS)


def matches(pattern: str, value: str | None) -> bool:
    """Case-insensitive glob match. An empty pattern matches nothing.

    Treating "" as "match everything" would turn a half-filled form into a
    rule that claims every alert.
    """
    if not pattern:
        return False
    if value is None:
        return False
    return fnmatchcase(value.lower(), pattern.lower())


def to_like(pattern: str) -> str:
    r"""Translate a glob to a SQL LIKE pattern, escaping literals first.

        pg-prod-*  ->  pg-prod-%
        node-?     ->  node-_
        100%       ->  100\%      (literal, NOT a wildcard)

    Order matters: escape LIKE's metacharacters BEFORE turning `*`/`?` into
    them, otherwise the escaping would also neuter the wildcards we just
    created. Pair this with `ESCAPE '\'` on the SQL side.
    """
    out = []
    for ch in pattern:
        if ch in _LIKE_SPECIAL:
            out.append("\\" + ch)
        elif ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        else:
            out.append(ch)
    return "".join(out)
