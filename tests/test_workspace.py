from __future__ import annotations

import pytest
from typer.testing import CliRunner

from proofline.cli import app
from proofline.config import load_config
from proofline.workspace import WorkspaceExistsError, init_workspace


def test_init_creates_workspace_from_templates(tmp_path):
    ws = tmp_path / "ws"
    result = init_workspace(ws)
    assert "proofline.yaml" in result.created
    assert "playbooks/guardrails.md" in result.created
    assert "playbooks/tech-lead-taste.md" in result.created
    assert "playbooks/skills/bug-fix-playbook.md" in result.created
    for rel in ("state", "logs", "targets", "evals/golden-set", "evals/decision-replay"):
        assert (ws / rel).is_dir()
    assert "state/" in (ws / ".gitignore").read_text(encoding="utf-8")
    cfg = load_config(ws / "proofline.yaml")
    assert cfg.target_repos == []
    assert cfg.workspace == ws.resolve()


def test_init_refuses_existing_workspace(tmp_path):
    init_workspace(tmp_path)
    with pytest.raises(WorkspaceExistsError):
        init_workspace(tmp_path)


def test_init_keeps_existing_files(tmp_path):
    (tmp_path / "playbooks").mkdir()
    (tmp_path / "playbooks" / "tech-lead-taste.md").write_text("gu của tôi", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    result = init_workspace(tmp_path)
    assert "playbooks/tech-lead-taste.md" in result.kept
    assert ".gitignore" in result.kept
    assert (tmp_path / "playbooks" / "tech-lead-taste.md").read_text(encoding="utf-8") == (
        "gu của tôi"
    )


def test_cli_workspace_init_then_assess(tmp_path, monkeypatch):
    runner = CliRunner()
    ws = tmp_path / "ws"
    result = runner.invoke(app, ["workspace", "init", str(ws)])
    assert result.exit_code == 0, result.output
    assert "proofline.yaml" in result.output

    again = runner.invoke(app, ["workspace", "init", str(ws)])
    assert again.exit_code == 1

    monkeypatch.delenv("PROOFLINE_CONFIG", raising=False)
    monkeypatch.chdir(ws)
    check = runner.invoke(app, ["config", "check"])
    assert check.exit_code == 0, check.output
    assert "(chưa có)" in check.output

    db = runner.invoke(app, ["db", "init"])
    assert db.exit_code == 0, db.output
    assert (ws / "state" / "proofline.db").exists()
