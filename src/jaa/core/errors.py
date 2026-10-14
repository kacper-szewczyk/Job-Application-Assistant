"""Domain exceptions. CLI and MCP layers translate these into user-facing messages."""


class JaaError(Exception):
    """Base class for all expected application errors."""


class ConfigError(JaaError):
    """Invalid or unusable configuration, e.g. a bad data directory."""
