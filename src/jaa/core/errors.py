"""Domain exceptions. CLI and MCP layers translate these into user-facing messages."""


class JaaError(Exception):
    """Base class for all expected application errors."""


class ConfigError(JaaError):
    """Invalid or unusable configuration, e.g. a bad data directory."""


class StorageError(JaaError):
    """Database problems: missing database, failed migration, constraint violations."""


class MigrationError(StorageError):
    """Migrations cannot be applied, e.g. an applied migration was edited or is unknown."""


class ExportFormatError(StorageError):
    """An export file is malformed or incompatible with this version."""


class NotFoundError(StorageError):
    """A requested record does not exist."""


class ReviewError(JaaError):
    """A profile review action is not allowed, e.g. an invalid state transition."""


class FactConflictError(ReviewError):
    """Confirming a fact would replace another confirmed value for the same field key."""


class PreferenceError(JaaError):
    """An unknown preference key, or a value that does not fit the question."""


class ImportSourceError(JaaError):
    """An import source cannot be used: missing, too large, unsupported or unreadable."""


class UnsafeArchiveError(ImportSourceError):
    """An archive is refused because a member could escape it or exhaust resources."""


class ConnectorError(JaaError):
    """A job source connector could not fetch or understand listings."""


class SourcePausedError(ConnectorError):
    """The source is paused, by its policy or by the user, so nothing may be fetched."""


class SourceAccessDeniedError(ConnectorError):
    """The source refused access (login wall, bot check, 401/403). Never retried or worked around.

    Callers pause the source when they see this (ADR 0011).
    """


class SourceUnavailableError(ConnectorError):
    """The source kept failing (network errors, 5xx, rate limiting) after the allowed retries."""


class SourceDataError(ConnectorError):
    """The source answered, but not with data the connector can use, or broke the policy limits."""
