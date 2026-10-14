"""Settings and data directory resolution (ADR 0003)."""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from jaa.core.errors import ConfigError

APP_NAME = "jaa"
DATA_DIR_ENV = "JAA_DATA_DIR"
LOG_LEVEL_ENV = "JAA_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "WARNING"
DATABASE_FILENAME = "jaa.sqlite3"

# The data dir holds CVs and personal answers, so only the owner may read it.
DATA_DIR_MODE = 0o700


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    data_dir_source: str
    log_level: str

    @property
    def database_path(self) -> Path:
        return self.data_dir / DATABASE_FILENAME


def resolve_data_dir(env: Mapping[str, str]) -> tuple[Path, str]:
    """Return the data directory and where it came from ("env" or "default")."""
    override = env.get(DATA_DIR_ENV, "").strip()
    if not override:
        return user_data_path(APP_NAME, appauthor=False), "default"

    path = Path(override).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{DATA_DIR_ENV} must be an absolute path, got a relative path")
    return path.resolve(), "env"


def resolve_log_level(env: Mapping[str, str]) -> str:
    level = env.get(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL).strip().upper()
    if level not in logging.getLevelNamesMapping():
        raise ConfigError(f"{LOG_LEVEL_ENV} has unknown level {level!r}")
    return level


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    data_dir, source = resolve_data_dir(env)
    return Settings(data_dir=data_dir, data_dir_source=source, log_level=resolve_log_level(env))


def ensure_data_dir(settings: Settings) -> Path:
    """Create the data directory with owner-only permissions if it does not exist."""
    path = settings.data_dir
    if path.exists() and not path.is_dir():
        raise ConfigError(f"data directory path exists and is not a directory: {path}")
    path.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
    path.chmod(DATA_DIR_MODE)
    return path
