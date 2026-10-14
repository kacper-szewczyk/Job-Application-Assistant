import stat
from pathlib import Path

import pytest

from jaa.config import DATA_DIR_ENV, LOG_LEVEL_ENV, ensure_data_dir, load_settings
from jaa.core.errors import ConfigError


def test_default_data_dir_is_platform_dir_outside_repo() -> None:
    settings = load_settings({})
    assert settings.data_dir_source == "default"
    assert settings.data_dir.name == "jaa"
    assert Path.cwd() not in settings.data_dir.parents


def test_env_override(tmp_path: Path) -> None:
    settings = load_settings({DATA_DIR_ENV: str(tmp_path / "data")})
    assert settings.data_dir == (tmp_path / "data").resolve()
    assert settings.data_dir_source == "env"
    assert settings.database_path == settings.data_dir / "jaa.sqlite3"


def test_env_override_expands_user() -> None:
    settings = load_settings({DATA_DIR_ENV: "~/jaa-data"})
    assert settings.data_dir == (Path.home() / "jaa-data").resolve()


def test_blank_override_uses_default() -> None:
    assert load_settings({DATA_DIR_ENV: "  "}).data_dir_source == "default"


def test_relative_override_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings({DATA_DIR_ENV: "relative/data"})


def test_log_level_default_and_override() -> None:
    assert load_settings({}).log_level == "WARNING"
    assert load_settings({LOG_LEVEL_ENV: "debug"}).log_level == "DEBUG"


def test_unknown_log_level_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings({LOG_LEVEL_ENV: "chatty"})


def test_ensure_data_dir_creates_owner_only_dir(tmp_path: Path) -> None:
    settings = load_settings({DATA_DIR_ENV: str(tmp_path / "a" / "b")})
    path = ensure_data_dir(settings)
    assert path.is_dir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_ensure_data_dir_tightens_existing_permissions(tmp_path: Path) -> None:
    target = tmp_path / "data"
    target.mkdir(mode=0o755)
    ensure_data_dir(load_settings({DATA_DIR_ENV: str(target)}))
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_data_dir_rejects_file(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x")
    with pytest.raises(ConfigError):
        ensure_data_dir(load_settings({DATA_DIR_ENV: str(target)}))
