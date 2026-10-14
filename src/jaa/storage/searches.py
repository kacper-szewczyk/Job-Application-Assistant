"""Searching stored jobs and keeping named searches (T11, ADR 0014).

A saved search remembers when its results were last refreshed, so the next run can point out
jobs first seen since then. Logs carry ids and counts, never query text.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from jaa.core.clock import utcnow
from jaa.core.errors import SearchError
from jaa.core.models import FetchRunOutcome, Job, SavedSearch
from jaa.core.search import JobQuery, matches
from jaa.storage.repository import jobs, saved_searches

log = logging.getLogger(__name__)

MAX_NAME_LENGTH = 64


def search_jobs(conn: sqlite3.Connection, query: JobQuery) -> list[Job]:
    """Jobs matching `query`, most recently first seen first."""
    found = [j for j in jobs.list(conn) if matches(j, query)]
    return sorted(found, key=lambda j: (j.first_seen_at, j.id or 0), reverse=True)


def _name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > MAX_NAME_LENGTH or not name.isprintable():
        raise SearchError(f"a search name is 1 to {MAX_NAME_LENGTH} printable characters")
    return name


def find_search(conn: sqlite3.Connection, name: str) -> SavedSearch:
    found = saved_searches.list(conn, name=name.strip())
    if not found:
        raise SearchError(f"no saved search named {name.strip()!r}; see `jaa jobs searches list`")
    return found[0]


def save_search(
    conn: sqlite3.Connection,
    name: str,
    query: JobQuery,
    *,
    replace: bool = False,
    at: datetime | None = None,
) -> SavedSearch:
    """Save `query` as `name`. An existing name is only replaced with `replace=True`."""
    name = _name(name)
    at = at or utcnow()
    with conn:
        existing = saved_searches.list(conn, name=name)
        if existing:
            if not replace:
                raise SearchError(f"a search named {name!r} exists; replace it to change it")
            current = existing[0]
            if current.id is None:
                raise SearchError("saved search has no id")
            saved = saved_searches.update(conn, current.id, query=query.to_json(), updated_at=at)
        else:
            saved = saved_searches.insert(
                conn, SavedSearch(name=name, query=query.to_json(), created_at=at, updated_at=at)
            )
    log.info("saved search id=%s", saved.id)
    return saved


def list_searches(conn: sqlite3.Connection) -> list[SavedSearch]:
    """Saved searches by name."""
    return sorted(saved_searches.list(conn), key=lambda s: s.name.casefold())


def delete_search(conn: sqlite3.Connection, name: str) -> None:
    search = find_search(conn, name)
    with conn:
        conn.execute("DELETE FROM saved_searches WHERE id = ?", (search.id,))
    log.info("deleted saved search id=%s", search.id)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRun:
    search: SavedSearch
    query: JobQuery
    jobs: list[Job]
    # Ids of results first seen after the previous refresh (all of them on the first run).
    new_since_last: frozenset[int]
    previous_refresh: datetime | None
    # When the newest successful fetch of a searched source finished, if any.
    data_as_of: datetime | None


def run_search(conn: sqlite3.Connection, name: str, at: datetime | None = None) -> SearchRun:
    """Run a saved search and record the refresh."""
    search = find_search(conn, name)
    if search.id is None:
        raise SearchError("saved search has no id")
    query = JobQuery.from_json(search.query)
    found = search_jobs(conn, query)
    previous = search.last_refreshed_at
    new = frozenset(
        j.id for j in found if j.id is not None and (previous is None or j.first_seen_at > previous)
    )
    with conn:
        updated = saved_searches.update(
            conn, search.id, last_refreshed_at=at or utcnow(), last_result_count=len(found)
        )
    log.info("ran saved search id=%s results=%s new=%s", search.id, len(found), len(new))
    return SearchRun(
        search=updated,
        query=query,
        jobs=found,
        new_since_last=new,
        previous_refresh=previous,
        data_as_of=last_fetched(conn, query.sources),
    )


def last_fetched(conn: sqlite3.Connection, sources: tuple[str, ...] = ()) -> datetime | None:
    """When the newest successful fetch (of `sources`, or of any source) finished."""
    sql = "SELECT MAX(finished_at) FROM source_fetch_runs WHERE outcome = ?"
    params: list[str] = [FetchRunOutcome.OK.value]
    if sources:
        sql += f" AND source IN ({', '.join('?' for _ in sources)})"
        params += sources
    value = conn.execute(sql, params).fetchone()[0]
    return None if value is None else datetime.fromisoformat(value)
