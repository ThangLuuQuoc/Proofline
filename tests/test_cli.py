from __future__ import annotations

import io

from conftest import REPO_CONFIG
from typer.testing import CliRunner

from proofline import __version__
from proofline.cli import app, ensure_utf8

runner = CliRunner()


def test_ensure_utf8_on_legacy_windows_console():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    ensure_utf8(stream)
    stream.write("Config hợp lệ")
    stream.flush()
    assert stream.buffer.getvalue().decode("utf-8") == "Config hợp lệ"


def test_ensure_utf8_leaves_utf8_stream_alone():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    ensure_utf8(stream)
    assert stream.errors == "strict"


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_config_check_repo_config():
    result = runner.invoke(app, ["config", "check", "--config", str(REPO_CONFIG)])
    assert result.exit_code == 0, result.output
    assert "Config hợp lệ" in result.output


def test_config_check_invalid(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: x\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "check", "--config", str(bad)])
    assert result.exit_code == 1


def test_config_model_default_and_env():
    args = ["config", "model", "heavy_coding", "--config", str(REPO_CONFIG)]
    result = runner.invoke(app, args, env={"PROOFLINE_MODEL_HEAVY_CODING": ""})
    assert result.exit_code == 0, result.output
    assert "claude-sonnet-5-5" in result.output and "default" in result.output

    result = runner.invoke(app, args, env={"PROOFLINE_MODEL_HEAVY_CODING": "claude-opus-5-5"})
    assert "claude-opus-5-5" in result.output and "nguồn: env" in result.output


def test_config_model_unknown_role():
    result = runner.invoke(app, ["config", "model", "ghost", "--config", str(REPO_CONFIG)])
    assert result.exit_code == 1


def test_autonomy_show():
    result = runner.invoke(
        app, ["autonomy", "show", "--readiness", "R2", "--config", str(REPO_CONFIG)]
    )
    assert result.exit_code == 0, result.output
    assert "customer_reply" in result.output
    assert "self_sourced_task" in result.output


def test_autonomy_show_rejects_bad_readiness():
    result = runner.invoke(
        app, ["autonomy", "show", "--readiness", "R9", "--config", str(REPO_CONFIG)]
    )
    assert result.exit_code == 1


def test_db_init(tmp_path):
    db = tmp_path / "state" / "proofline.db"
    result = runner.invoke(app, ["db", "init", "--path", str(db)])
    assert result.exit_code == 0, result.output
    assert db.exists()
    assert "decisions" in result.output
