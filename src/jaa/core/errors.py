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
