"""`/init` qua ACP: đăng ký thư mục đang mở làm repo đích, thư mục trống thì hỏi `git init`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from test_acp import git, new_session, prompt, rpc
from test_acp_decisions import answer, decisions, drive, last_text, permission_requests

from proofline.config import load_config
from proofline.workspace import init_workspace


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Workspace chưa có repo đích nào; git có danh tính để thử commit đầu tiên."""
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "t")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "t@example.com")
    workspace = tmp_path / "ws"
    init_workspace(workspace)
    return workspace, workspace / "proofline.yaml"


def make_repo(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    git(path.parent, "init", "-q", "-b", "main", str(path))
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def status(repo: Path) -> str:
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def target_file(workspace: Path, name: str) -> dict:
    return yaml.safe_load(
        (workspace / "targets" / name / "target.yaml").read_text(encoding="utf-8")
    )


def texts(messages: list[dict]) -> str:
    return "\n".join(
        m["params"]["update"]["content"]["text"]
        for m in messages
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    )


def test_init_registers_an_existing_repo_without_touching_it(ws, tmp_path):
    workspace, config = ws
    repo = make_repo(tmp_path / "shop")
    sid = new_session(workspace, repo)
    assert "Gõ `/init`" in last_text(drive(config, prompt(2, sid, "/assess")))

    messages = drive(config, prompt(3, sid, "/init"))
    assert not permission_requests(messages)  # typing /init is the human act
    said = texts(messages)
    assert "Đã đăng ký repo đích `shop` (stack node" in said
    assert "mức sẵn sàng R" in last_text(messages)  # assess ran right after
    assert target_file(workspace, "shop")["default_branch"] == "main"
    assert load_config(config).target_repo("shop")
    assert status(repo) == ""  # nothing written into the repo
    assert decisions(workspace, "register_target")[-1]["decided_by"] == "human"
    # A new process (next turn) knows the repo; /init again changes nothing.
    assert "Repo `shop`" in last_text(drive(config, prompt(4, sid, "/status")))
    assert "đã là repo đích `shop`" in last_text(drive(config, prompt(5, sid, "/init")))


def test_init_from_a_subfolder_registers_the_repo_root_under_a_chosen_name(ws, tmp_path):
    workspace, config = ws
    repo = make_repo(tmp_path / "shop")
    sid = new_session(workspace, repo / "src")
    drive(config, prompt(2, sid, "/init cua-hang"))
    entry = target_file(workspace, "cua-hang")
    assert (workspace / entry["local_path"]).resolve() == repo.resolve()


def test_empty_folder_asks_for_git_init_then_registers(ws, tmp_path):
    workspace, config = ws
    folder = tmp_path / "y-tuong"
    folder.mkdir()
    sid = new_session(workspace, folder)
    messages = drive(config, prompt(2, sid, "/init"), answer(1, "git_init_commit"))
    (request,) = permission_requests(messages)
    kinds = [o["kind"] for o in request["params"]["options"]]
    assert kinds == ["allow_once", "allow_once", "reject_once"]
    said = texts(messages)
    assert "Đã `git init`" in said and "commit rỗng đầu tiên" in said
    assert "Đã đăng ký repo đích `y-tuong`" in said
    assert (folder / ".git").is_dir()
    assert [p.name for p in folder.iterdir()] == [".git"]  # no other file created
    assert target_file(workspace, "y-tuong")["default_branch"] == "main"
    assert decisions(workspace, "git_init")[-1]["decided_by"] == "human"


def test_a_fast_answer_does_not_init_and_the_typed_flag_does(ws, tmp_path):
    workspace, config = ws
    folder = tmp_path / "y-tuong"
    folder.mkdir()
    sid = new_session(workspace, folder)
    messages = drive(config, prompt(2, sid, "/init"), answer(1, "git_init"), seconds=0.2)
    assert "/init --git-init" in last_text(messages)
    assert not (folder / ".git").exists()
    assert not (workspace / "targets" / "y-tuong").exists()
    assert decisions(workspace, "git_init")[-1]["decided_by"] == "agent"

    messages = drive(config, prompt(3, sid, "/init --git-init"))
    assert not permission_requests(messages)
    assert (folder / ".git").is_dir()
    assert target_file(workspace, "y-tuong")["name"] == "y-tuong"


def test_a_folder_with_files_but_no_git_is_left_alone(ws, tmp_path):
    workspace, config = ws
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "y-tuong.md").write_text("# Ý tưởng\n", encoding="utf-8")
    sid = new_session(workspace, folder)
    messages = drive(config, prompt(2, sid, "/init"))
    assert not permission_requests(messages)
    assert "không tự khởi tạo git" in last_text(messages)
    assert not (folder / ".git").exists()


def test_plan_mode_does_not_git_init(ws, tmp_path):
    workspace, config = ws
    folder = tmp_path / "y-tuong"
    folder.mkdir()
    sid = new_session(workspace, folder)
    messages = drive(
        config,
        rpc(1, "session/set_mode", {"sessionId": sid, "modeId": "plan"}),
        prompt(2, sid, "/init --git-init"),
    )
    assert "Chế độ Plan" in last_text(messages) and not (folder / ".git").exists()


def test_a_name_already_in_the_workspace_is_refused(ws, tmp_path):
    workspace, config = ws
    first = make_repo(tmp_path / "a" / "shop")
    second = make_repo(tmp_path / "b" / "shop")
    drive(config, prompt(2, new_session(workspace, first), "/init"))
    messages = drive(config, prompt(2, new_session(workspace, second), "/init"))
    assert "đã có trong workspace" in last_text(messages) and "/init <tên>" in last_text(messages)
    messages = drive(config, prompt(3, new_session(workspace, second), "/init shop-b"))
    assert "Đã đăng ký repo đích `shop-b`" in texts(messages)
