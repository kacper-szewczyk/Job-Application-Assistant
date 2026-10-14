"""Generic mapping between frozen domain dataclasses and table rows.

Encoding rules: aware datetimes as ISO-8601 text, enums as their value, bools as 0/1,
JSON-typed fields as JSON text. Logging here records table names and ids only, never values.
"""

import json
import logging
import sqlite3
import types
from dataclasses import fields, replace
from datetime import datetime
from enum import Enum
from typing import Any, Final, Union, cast, get_args, get_origin, get_type_hints

from jaa.core import models
from jaa.core.errors import NotFoundError, StorageError

log = logging.getLogger(__name__)

# Domain types whose fields hold JSON in the database.
_JSON_ALIASES: Final = {models.JsonValue, models.JsonObject, models.JsonArray}


def _unwrap_optional(tp: object) -> object:
    if get_origin(tp) in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def encode(value: object, field_type: object) -> object:
    """Convert a Python value to an SQLite value."""
    # JSON fields store None as the JSON text 'null'; the columns are NOT NULL.
    if field_type in _JSON_ALIASES:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise StorageError("refusing to store a naive datetime; use timezone-aware UTC")
        return value.isoformat()
    if isinstance(value, Enum):
        return cast(object, value.value)
    if isinstance(value, bool):
        return int(value)
    return value


def decode(value: object, field_type: object) -> object:
    """Convert an SQLite value back to the dataclass field type."""
    if field_type in _JSON_ALIASES:
        return json.loads(cast(str, value))
    if value is None:
        return None
    target = _unwrap_optional(field_type)
    if target is datetime:
        return datetime.fromisoformat(cast(str, value))
    if isinstance(target, type) and issubclass(target, Enum):
        return target(value)
    if target is bool:
        return bool(value)
    return value


class Table[T]:
    """Insert, fetch, list and update one dataclass type in one table with an `id` key."""

    def __init__(self, model: type[T], table: str) -> None:
        self.model = model
        self.table = table
        self._types: dict[str, object] = dict(get_type_hints(model))
        self._columns: list[str] = [f.name for f in fields(cast(Any, model))]  # dataclass type

    def _to_row(self, obj: T) -> dict[str, object]:
        return {c: encode(getattr(obj, c), self._types[c]) for c in self._columns}

    def from_row(self, row: sqlite3.Row) -> T:
        values = {c: decode(row[c], self._types[c]) for c in self._columns}
        return self.model(**values)

    def insert(self, conn: sqlite3.Connection, obj: T) -> T:
        """Insert a new record (its id must be None) and return it with the assigned id."""
        if getattr(obj, "id", None) is not None:
            raise StorageError(f"{self.table}: insert requires id=None")
        row = self._to_row(obj)
        del row["id"]
        cols = ", ".join(row)
        params = ", ".join(f":{c}" for c in row)
        try:
            cur = conn.execute(f"INSERT INTO {self.table} ({cols}) VALUES ({params})", row)  # noqa: S608 - identifiers come from the dataclass, values are bound
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"{self.table}: constraint violated: {exc}") from exc
        log.debug("inserted %s id=%s", self.table, cur.lastrowid)
        return cast(T, replace(cast(Any, obj), id=cur.lastrowid))  # dataclass type

    def get(self, conn: sqlite3.Connection, id_: int) -> T:
        row = conn.execute(f"SELECT * FROM {self.table} WHERE id = ?", (id_,)).fetchone()  # noqa: S608 - fixed identifier
        if row is None:
            raise NotFoundError(f"{self.table} id={id_} not found")
        return self.from_row(row)

    def list(self, conn: sqlite3.Connection, **where: object) -> list[T]:
        """List records matching all equality filters, ordered by id."""
        unknown = set(where) - set(self._columns)
        if unknown:
            raise StorageError(f"{self.table}: unknown filter columns {sorted(unknown)}")
        clause = " AND ".join(f"{c} = :{c}" for c in where) or "1"
        params = {c: encode(v, self._types[c]) for c, v in where.items()}
        rows = conn.execute(
            f"SELECT * FROM {self.table} WHERE {clause} ORDER BY id",  # noqa: S608 - columns validated above, values bound
            params,
        ).fetchall()
        return [self.from_row(r) for r in rows]

    def update(self, conn: sqlite3.Connection, id_: int, **changes: object) -> T:
        """Update the given columns only, so immutability triggers on other columns don't fire."""
        unknown = set(changes) - (set(self._columns) - {"id"})
        if unknown:
            raise StorageError(f"{self.table}: cannot update columns {sorted(unknown)}")
        if not changes:
            return self.get(conn, id_)
        assignments = ", ".join(f"{c} = :{c}" for c in changes)
        params = {c: encode(v, self._types[c]) for c, v in changes.items()}
        params["id"] = id_
        try:
            cur = conn.execute(
                f"UPDATE {self.table} SET {assignments} WHERE id = :id",  # noqa: S608 - columns validated above, values bound
                params,
            )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"{self.table}: constraint violated: {exc}") from exc
        if cur.rowcount == 0:
            raise NotFoundError(f"{self.table} id={id_} not found")
        log.debug("updated %s id=%s columns=%s", self.table, id_, sorted(changes))
        return self.get(conn, id_)


profiles: Final = Table(models.Profile, "profile")
import_runs: Final = Table(models.ImportRun, "import_runs")
profile_facts: Final = Table(models.ProfileFact, "profile_facts")
profile_versions: Final = Table(models.ProfileVersion, "profile_versions")
profile_fact_events: Final = Table(models.ProfileFactEvent, "profile_fact_events")
preferences: Final = Table(models.Preference, "preferences")
answers: Final = Table(models.Answer, "answers")
jobs: Final = Table(models.Job, "jobs")
job_status_events: Final = Table(models.JobStatusEvent, "job_status_events")
job_matches: Final = Table(models.JobMatch, "job_matches")
applications: Final = Table(models.Application, "applications")
document_artifacts: Final = Table(models.DocumentArtifact, "document_artifacts")
