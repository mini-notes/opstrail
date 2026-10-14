"""One table of severity names, and the three ways the app reads it.

Until this module existed there were three independent tables -- the mention
threshold, the card banding, and the SQL sort order -- and they had already
drifted. Measured on the code this replaces, 8 of 18 aliases disagreed:

    severity=sev1   ->  CRITICAL card,  rank 5  ->  nobody was mentioned
    severity=p1     ->  CRITICAL card,  rank 1  ->  sorted BELOW info
    severity=alarm  ->  mentioned,      "ALARM" printed raw on the card

An alerting rule that said `sev1` produced a red card and woke nobody. That
is the failure mode this file exists to make impossible: one table, and every
reader derived from it -- including the SQL one, which is where the drift
started, because it was hand-written a second time.
"""

from sqlalchemy import case, func

# Worst first. The index in this tuple IS the rank, so there is nothing to
# keep in step.
BANDS: tuple[str, ...] = ("CRITICAL", "ERROR", "WARNING", "INFO")
UNKNOWN_RANK = len(BANDS) + 1

# Everything an alerting rule might plausibly write, mapped to a band. The
# union of the three tables this replaces, plus the syslog and SEVn spellings
# that only one of them knew about.
ALIASES: dict[str, str] = {
    # worst: wakes someone up
    "critical": "CRITICAL", "crit": "CRITICAL", "fatal": "CRITICAL",
    "emerg": "CRITICAL", "emergency": "CRITICAL", "disaster": "CRITICAL",
    "alarm": "CRITICAL", "page": "CRITICAL",
    "p1": "CRITICAL", "sev1": "CRITICAL",
    # broken, but not necessarily out of hours
    "error": "ERROR", "err": "ERROR", "major": "ERROR", "high": "ERROR",
    "p2": "ERROR", "sev2": "ERROR",
    # needs attention this shift
    "warning": "WARNING", "warn": "WARNING", "minor": "WARNING",
    "medium": "WARNING", "p3": "WARNING", "sev3": "WARNING",
    # informational
    "info": "INFO", "information": "INFO", "informational": "INFO",
    "notice": "INFO", "low": "INFO", "debug": "INFO",
    "p4": "INFO", "sev4": "INFO",
}


def band(value: str | None) -> str:
    """The canonical band for a severity label.

    An unrecognised value is upper-cased and passed through rather than
    forced into a band: inventing a severity for a label nobody mapped would
    be worse than showing the operator the word their rule actually wrote.
    """
    if not value:
        return ""
    raw = value.strip()
    return ALIASES.get(raw.lower(), raw.upper())


def rank(value: str | None) -> int:
    """Lower is worse. Unrecognised and missing values rank last."""
    if not value:
        return UNKNOWN_RANK
    resolved = ALIASES.get(value.strip().lower())
    return BANDS.index(resolved) + 1 if resolved else UNKNOWN_RANK


def meets_threshold(value: str | None, threshold: str | None) -> bool:
    """True when `value` is at least as bad as `threshold`.

    Compared by rank, never as strings: `"critical" >= "warning"` is False
    alphabetically, which is the opposite of what an operator means.
    """
    if not threshold:
        return False
    return rank(value) <= rank(threshold)


def shift(value: str | None, steps: int) -> str | None:
    """Move a severity `steps` bands MORE severe. Negative steps are milder.

    The sign follows how operators say it -- "raise by one band" is
    `shift(v, 1)` -- so no caller has to flip it, which is where a quieting
    rule would turn into an escalating one.

    Clamped at both ends. A rule asking for three bands worse than CRITICAL
    gets CRITICAL, not an invented level, and -- the one that matters -- a
    rule quieting an alert can never push it past INFO into nothing at all.
    Returns None for a value with no band, because there is nothing to move.
    """
    current = rank(value)
    if current >= UNKNOWN_RANK:
        return None
    target = min(max(current - steps, 1), len(BANDS))
    return BANDS[target - 1]


def sql_rank(column):
    """The same ordering, in SQL.

    Built from ALIASES rather than written out again. The hand-written copy
    this replaces knew six spellings out of the twenty-eight above, so `p1`
    sorted below `info` in the alerts table while the card called it
    CRITICAL.
    """
    folded = func.lower(func.trim(column))
    return case(
        *[(folded == alias, BANDS.index(b) + 1) for alias, b in ALIASES.items()],
        else_=UNKNOWN_RANK,
    )
