"""Safe, stable ordering for list pages.

Two things go wrong with table sorting and both are silent:

1. **Column names from the URL.** `order_by(text(request.query_params["sort"]))`
   is SQL injection. Every sortable column is therefore declared up front per
   page and looked up by a short key; anything unrecognised falls back to the
   page's default instead of erroring or reaching the database.

2. **Ties plus LIMIT/OFFSET.** Sorting alerts by `severity` groups hundreds of
   rows under the same value. SQL does not promise an order among equal rows,
   so page 1 and page 2 can both contain the same record while another never
   appears. Every ordering produced here therefore ends with the table's
   primary key, which is unique and makes the sequence total.
"""

from dataclasses import dataclass

from app.services import severity

ASC = "asc"
DESC = "desc"


@dataclass(frozen=True, slots=True)
class SortSpec:
    """The sortable columns of one page.

    `columns` maps the key that appears in the URL to a SQLAlchemy expression.
    `tiebreaker` is the unique column appended to every ordering.
    """

    columns: dict
    default: str
    tiebreaker: object
    default_desc: bool = True

    def resolve(self, key: str | None, direction: str | None) -> "Sort":
        if not key or key not in self.columns:
            return Sort(self.default, self.default_desc, self)
        if direction == ASC:
            descending = False
        elif direction == DESC:
            descending = True
        else:
            descending = self.default_desc
        return Sort(key, descending, self)


@dataclass(frozen=True, slots=True)
class Sort:
    key: str
    desc: bool
    spec: SortSpec

    @property
    def direction(self) -> str:
        return DESC if self.desc else ASC

    def next_direction(self, column_key: str) -> str:
        """Clicking the active column flips it; clicking another starts
        descending, which is what people expect from 'newest/biggest first'."""
        if column_key == self.key:
            return ASC if self.desc else DESC
        return DESC

    def clauses(self) -> list:
        """ORDER BY terms, primary key last.

        NULLs sort last in both directions: an alert still firing has no
        duration, and burying those at the top of a 'longest first' list is
        not useful.
        """
        col = self.spec.columns[self.key]
        primary = col.desc() if self.desc else col.asc()
        primary = primary.nullslast()
        return [primary, self.spec.tiebreaker.desc()]


def severity_rank(column):
    """Order severities by how bad they are, not alphabetically.

    Alphabetical puts `critical` below `warning`, which is useless to whoever
    is on call. Unknown values sort last rather than being dropped.

    The CASE is built from app/services/severity.py, not written out here.
    The hand-written copy this replaces knew six spellings out of
    twenty-eight, so `p1` sorted below `info` while the card called it
    CRITICAL.
    """
    return severity.sql_rank(column)
