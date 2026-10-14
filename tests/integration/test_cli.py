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
