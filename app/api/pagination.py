"""One pagination helper for every list page.

`app/api/ui.py` grew its own page arithmetic for the alert table; copying it
into each new admin page is how the pager on one of them ends up off by one.

Ordering is applied here rather than by the caller so that no page can forget
the unique tiebreaker that keeps LIMIT/OFFSET stable -- see app/api/sorting.py.
"""

from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sorting import Sort, SortSpec

# Offered in the UI. Anything else in `?size=` falls back to the page default
# rather than letting a URL ask for 100000 rows.
PAGE_SIZES = (25, 50, 100)
ALERT_PAGE_SIZE = 50
ADMIN_PAGE_SIZE = 25


def resolve_size(requested: int | None, default: int) -> int:
    return requested if requested in PAGE_SIZES else default


@dataclass(slots=True)
class Page:
    items: list
    total: int
    page: int
    pages: int
    size: int
    sort: Sort | None = None

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_index(self) -> int:
        """1-based index of the first row on this page, for 'showing X–Y'."""
        return 0 if self.total == 0 else (self.page - 1) * self.size + 1

    @property
    def last_index(self) -> int:
        return min(self.page * self.size, self.total)

    @property
    def sizes(self) -> tuple:
        return PAGE_SIZES


async def paginate(
    session: AsyncSession,
    stmt: Select,
    *,
    page: int,
    size: int = ADMIN_PAGE_SIZE,
    sort: Sort | None = None,
    spec: SortSpec | None = None,
    sort_key: str | None = None,
    sort_dir: str | None = None,
) -> Page:
    """Count, order, then fetch one page.

    `page` is clamped so a stale `?page=99` link shows the last page instead
    of an empty table. Pass either a resolved `sort`, or a `spec` plus the raw
    query values to resolve here.
    """
    if sort is None and spec is not None:
        sort = spec.resolve(sort_key, sort_dir)
    if sort is not None:
        stmt = stmt.order_by(None).order_by(*sort.clauses())

    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    pages = max((total + size - 1) // size, 1)
    page = max(1, min(page, pages))
    rows = (
        (await session.execute(stmt.offset((page - 1) * size).limit(size)))
        .scalars()
        .all()
    )
    return Page(
        items=list(rows), total=total, page=page, pages=pages, size=size, sort=sort
    )
