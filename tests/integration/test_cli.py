import stat
from pathlib import Path

from typer.testing import CliRunner

from jaa import __version__
from jaa.cli import app

runner = CliRunner()


def _env(data_dir: Path) -> dict[str, str]:
    return {"JAA_DATA_DIR": str(data_dir), "JAA_LOG_LEVEL": "DEBUG"}


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_info_reports_data_dir_before_init(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = runner.invoke(app, ["info"], env=_env(data_dir))
    assert result.exit_code == 0
    assert str(data_dir) in result.output
    assert "not created yet" in result.output
    assert not data_dir.exists()


def test_init_creates_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = runner.invoke(app, ["init"], env=_env(data_dir))
    assert result.exit_code == 0
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700

    info = runner.invoke(app, ["info"], env=_env(data_dir))
    assert "(env, exists)" in info.output


def test_bad_config_exits_with_message() -> None:
    result = runner.invoke(app, ["info"], env={"JAA_DATA_DIR": "relative/path"})
    assert result.exit_code == 2
    assert "Configuration error" in result.output


def test_init_creates_database_and_info_reports_schema(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = runner.invoke(app, ["init"], env=_env(data_dir))
    assert result.exit_code == 0, result.output
    db = data_dir / "jaa.sqlite3"
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert "schema version 11" in result.output

    # Running init again is safe.
    assert runner.invoke(app, ["init"], env=_env(data_dir)).exit_code == 0
    info = runner.invoke(app, ["info"], env=_env(data_dir))
    assert "schema version 11" in info.output


def test_export_and_backup_commands(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    runner.invoke(app, ["init"], env=_env(data_dir))

    export_path = tmp_path / "export.json"
    result = runner.invoke(app, ["export", str(export_path)], env=_env(data_dir))
    assert result.exit_code == 0, result.output
    assert '"format": "jaa-export"' in export_path.read_text()
    assert runner.invoke(app, ["export", str(export_path)], env=_env(data_dir)).exit_code == 1
    assert (
        runner.invoke(app, ["export", str(export_path), "--force"], env=_env(data_dir)).exit_code
        == 0
    )

    backup_path = tmp_path / "backup.sqlite3"
    result = runner.invoke(app, ["backup", str(backup_path)], env=_env(data_dir))
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600


def test_export_without_database_explains(tmp_path: Path) -> None:
    result = runner.invoke(app, ["export", str(tmp_path / "x.json")], env=_env(tmp_path / "none"))
    assert result.exit_code == 1
    assert "jaa init" in result.output
