from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from proofline.acp import COMMANDS, AcpServer
from proofline.config import load_config
from proofline.state import connect
from proofline.targets import add_target
from proofline.workspace import init_workspace


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def ws(tmp_path):
    """Workspace với một repo đích Node nhỏ (có tài liệu tính năng gốc, chưa có CI)."""
    repo = tmp_path / "shop"
    (repo / "docs").mkdir(parents=True)
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    (repo / "docs" / "FEATURES.md").write_text("# Tính năng\n", encoding="utf-8")
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    workspace = tmp_path / "ws"
    init_workspace(workspace)
    add_target(load_config(workspace / "proofline.yaml"), str(repo))
    return workspace, repo


def rpc(msg_id, method, params=None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def run_server(workspace: Path, *lines: str) -> list[dict]:
    out = io.StringIO()
    AcpServer(
        io.StringIO("\n".join(lines) + "\n"), out, lambda: load_config(workspace / "proofline.yaml")
    ).serve()
    return [json.loads(line) for line in out.getvalue().splitlines()]


def prompt(msg_id, session_id, text) -> str:
    return rpc(
        msg_id,
        "session/prompt",
        {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
    )


def new_session(workspace: Path, cwd: Path) -> str:
    messages = run_server(workspace, rpc(1, "session/new", {"cwd": str(cwd), "mcpServers": []}))
    return messages[0]["result"]["sessionId"]


def updates(messages: list[dict], kind: str) -> list[dict]:
    return [
        m["params"]["update"]
        for m in messages
        if m.get("method") == "session/update" and m["params"]["update"]["sessionUpdate"] == kind
    ]


def test_initialize_and_new_session_announce_commands(ws):
    workspace, repo = ws
    messages = run_server(
        workspace,
        rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}),
        rpc(2, "session/new", {"cwd": str(repo), "mcpServers": []}),
    )
    init, new, announce = messages
    assert init["result"]["protocolVersion"] == 1
    assert init["result"]["agentCapabilities"]["loadSession"] is True
    assert new["result"]["modes"]["currentModeId"] == "default"
    # No mode that skips human decisions is advertised.
    assert {m["id"] for m in new["result"]["modes"]["availableModes"]} == {"default", "plan"}
    # Commands come after the response, so the client already knows the session id.
    assert announce["params"]["sessionId"] == new["result"]["sessionId"]
    assert announce["params"]["update"]["availableCommands"] == COMMANDS


def test_assess_streams_each_criterion_and_saves_a_snapshot(ws):
    workspace, repo = ws
    sid = new_session(workspace, repo)
    messages = run_server(workspace, prompt(5, sid, "/assess"))
    calls = updates(messages, "tool_call")
    assert calls[0]["title"] == "Assess shop" and calls[0]["status"] == "in_progress"
    by_title = {c["title"]: c for c in calls[1:]}
    assert by_title["R1 · has_runnable_code"]["status"] == "completed"
    assert by_title["R2 · ci_green_on_default_branch"]["status"] == "failed"
    assert (
        "docs/FEATURES.md"
        in by_title["R2 · feature_inventory_confirmed"]["content"][0]["content"]["text"]
    )
    assert updates(messages, "tool_call_update")[0]["status"] == "completed"
    text = updates(messages, "agent_message_chunk")[-1]["content"]["text"]
    assert "mức sẵn sàng R0" in text and "Để lên R1" in text
    assert messages[-1] == {"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "end_turn"}}
    conn = connect(workspace / "state/proofline.db")
    assert (
        conn.execute("SELECT count(*) FROM readiness_snapshots WHERE repo = 'shop'").fetchone()[0]
        == 1
    )
    conn.close()


def test_a_new_process_resumes_the_session(ws):
    workspace, repo = ws
    sid = new_session(workspace, repo)
    run_server(workspace, prompt(2, sid, "/assess"))
    # PandaCode starts a new agent process for each turn: state must come from the DB.
    messages = run_server(
        workspace,
        rpc(1, "session/load", {"sessionId": sid, "cwd": str(repo), "mcpServers": []}),
        rpc(2, "session/set_mode", {"sessionId": sid, "modeId": "plan"}),
        prompt(3, sid, "/status"),
    )
    assert messages[0] == {"jsonrpc": "2.0", "id": 1, "result": None}
    text = updates(messages, "agent_message_chunk")[-1]["content"]["text"]
    assert "Repo `shop`" in text and "Mức sẵn sàng gần nhất: R0" in text and "Chưa có spec" in text
    conn = connect(workspace / "state/proofline.db")
    assert (
        conn.execute("SELECT mode FROM acp_sessions WHERE session_id = ?", (sid,)).fetchone()[0]
        == "plan"
    )
    conn.close()


def test_unknown_folder_points_to_target_add(ws, tmp_path):
    workspace, _ = ws
    other = tmp_path / "elsewhere"
    other.mkdir()
    sid = new_session(workspace, other)
    text = updates(run_server(workspace, prompt(2, sid, "/assess")), "agent_message_chunk")[-1][
        "content"
    ]["text"]
    assert "proofline target add" in text


def test_errors_never_stop_the_agent(ws):
    workspace, repo = ws
    messages = run_server(
        workspace,
        "{not json",
        rpc(1, "fs/whatever"),
        rpc(2, "session/prompt", {"sessionId": "nope", "prompt": []}),
        rpc(3, "session/set_mode", {"sessionId": new_session(workspace, repo), "modeId": "bypass"}),
        rpc(4, "initialize", {"protocolVersion": 1}),
    )
    assert messages[0]["error"]["code"] == -32700
    assert messages[1]["error"]["code"] == -32601
    assert messages[2]["error"]["code"] == -32002
    assert messages[3]["error"]["code"] == -32602  # no "bypass" mode (Full access) to switch into
    assert messages[4]["result"]["protocolVersion"] == 1


def test_stdio_process_speaks_clean_json_lines(ws):
    workspace, repo = ws
    lines = [
        rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}),
        rpc(2, "session/new", {"cwd": str(repo), "mcpServers": []}),
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "proofline", "acp", "--workspace", str(workspace)],
        input=("\n".join(lines) + "\n").encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    raw = proc.stdout
    assert b"\r" not in raw
    messages = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert [m.get("id") for m in messages] == [1, 2, None]
    assert (
        "Đo mức sẵn sàng" in messages[2]["params"]["update"]["availableCommands"][0]["description"]
    )
