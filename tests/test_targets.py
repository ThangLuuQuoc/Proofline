from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from proofline.cli import app
from proofline.config import ConfigError, load_config
from proofline.targets import add_target, is_git_url, name_from_source
from proofline.workspace import init_workspace

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def make_app_repo(path: Path, *, git: bool = False) -> Path:
    path.mkdir(parents=True)
    (path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "next dev -p 3200", "test": "vitest run"}}),
        encoding="utf-8",
    )
    (path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    if git:
        for args in (
            ["git", "init", "-q", "-b", "trunk"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(args, cwd=path, check=True, env=GIT_ENV)
    return path


@pytest.mark.parametrize(
    ("source", "is_url", "name"),
    [
        ("https://github.com/acme/tiny-app.git", True, "tiny-app"),
        ("https://github.com/acme/tiny-app", True, "tiny-app"),
        ("git@github.com:acme/tiny-app.git", True, "tiny-app"),
        ("../tiny-app", False, "tiny-app"),
        ("C:\\work\\tiny-app\\", False, "tiny-app"),
    ],
)
def test_source_parsing(source, is_url, name):
    assert is_git_url(source) is is_url
    assert name_from_source(source) == name


def test_add_local_path_writes_target_file(tmp_path):
    ws = tmp_path / "ws"
    init_workspace(ws)
    make_app_repo(tmp_path / "tiny-app")
    added = add_target(load_config(ws / "proofline.yaml"), str(tmp_path / "tiny-app"))
    assert not added.cloned
    assert added.target_file == ws / "targets" / "tiny-app" / "target.yaml"
    assert "pnpm dev" in added.target_file.read_text(encoding="utf-8")

    cfg = load_config(ws / "proofline.yaml")
    repo = cfg.target_repo("tiny-app")
    assert repo.local_path == "../tiny-app"
    assert repo.commands["test"] == "pnpm test"
    assert repo.app_runtime.healthcheck_url == "http://localhost:3200/"
    # proofline.yaml không bị sửa
    assert "target_repos: []" in (ws / "proofline.yaml").read_text(encoding="utf-8")


def test_add_git_url_clones_into_workspace(tmp_path):
    ws = tmp_path / "ws"
    init_workspace(ws)
    origin = make_app_repo(tmp_path / "origin" / "tiny-app", git=True)
    added = add_target(load_config(ws / "proofline.yaml"), origin.as_uri())
    assert added.cloned
    assert added.repo_path == ws / "repos" / "tiny-app"
    assert (added.repo_path / "package.json").exists()
    repo = load_config(ws / "proofline.yaml").target_repo("tiny-app")
    assert repo.local_path == "repos/tiny-app"
    assert repo.default_branch == "trunk"
    assert repo.model_extra["source_url"] == origin.as_uri()


def test_add_rejects_duplicates_and_bad_input(tmp_path):
    ws = tmp_path / "ws"
    init_workspace(ws)
    make_app_repo(tmp_path / "tiny-app")
    add_target(load_config(ws / "proofline.yaml"), str(tmp_path / "tiny-app"))
    cfg = load_config(ws / "proofline.yaml")
    with pytest.raises(ConfigError, match="đã có"):
        add_target(cfg, str(tmp_path / "tiny-app"))
    with pytest.raises(ConfigError, match="không tìm thấy"):
        add_target(cfg, str(tmp_path / "missing"))
    with pytest.raises(ConfigError, match="không hợp lệ"):
        add_target(cfg, str(tmp_path / "tiny-app"), name="bad name")
    with pytest.raises(ConfigError, match="git clone"):
        add_target(cfg, (tmp_path / "nope").as_uri(), name="nope")


def test_duplicate_name_inline_and_file_is_rejected(tmp_path):
    ws = tmp_path / "ws"
    init_workspace(ws)
    make_app_repo(tmp_path / "tiny-app")
    add_target(load_config(ws / "proofline.yaml"), str(tmp_path / "tiny-app"))
    config = ws / "proofline.yaml"
    text = config.read_text(encoding="utf-8").replace(
        "target_repos: []", 'target_repos:\n  - name: "tiny-app"\n    local_path: "x"'
    )
    config.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="trùng tên"):
        load_config(config)


def test_cli_start_from_git_url(tmp_path):
    origin = make_app_repo(tmp_path / "origin" / "tiny-app", git=True)
    ws = tmp_path / "ws"
    result = CliRunner().invoke(app, ["start", origin.as_uri(), "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "Đã tạo workspace" in result.output
    assert "Đã thêm repo đích 'tiny-app'" in result.output
    assert "Repo tiny-app: mức sẵn sàng" in result.output
    assert (ws / "state" / "proofline.db").exists()

    listed = CliRunner().invoke(app, ["target", "list", "--config", str(ws / "proofline.yaml")])
    assert "tiny-app" in listed.output
