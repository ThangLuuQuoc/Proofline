# ruff: noqa: F811  (fixture `setup` lấy từ test_verify)
"""Bước 3 của docs/07: luồng có duyệt qua ACP và `/verify` (repo "calc" của test_verify)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from test_acp import new_session, prompt, rpc, updates
from test_verify import GOOD_JOURNEY, JOURNEY, setup, sh  # noqa: F401

from proofline.acp import AcpServer
from proofline.config import load_config
from proofline.state import connect
from proofline.verify import load_manifest

CHANGED_JOURNEY = (
    GOOD_JOURNEY + "\n\ndef test_user_can_multiply_by_zero():\n    assert calc.mul(5, 0) == 0\n"
)


def fake_clock(*times: float):
    """Đồng hồ giả: mỗi lần gọi trả giá trị kế tiếp (giữ giá trị cuối)."""
    remaining = list(times)
    last = [0.0]

    def clock() -> float:
        if remaining:
            last[0] = remaining.pop(0)
        return last[0]

    return clock


def answer(n: int, option: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": f"proofline-{n}",
            "result": {"outcome": {"outcome": "selected", "optionId": option}},
        }
    )


def drive(config: Path, *lines: str, seconds: float = 5.0) -> list[dict]:
    """Một lượt của client; `seconds` là thời gian từ lúc hỏi tới lúc có câu trả lời."""
    out = io.StringIO()
    AcpServer(
        io.StringIO("\n".join(lines) + "\n"),
        out,
        lambda: load_config(config),
        clock=fake_clock(0.0, seconds),
    ).serve()
    return [json.loads(line) for line in out.getvalue().splitlines()]


def last_text(messages: list[dict]) -> str:
    return updates(messages, "agent_message_chunk")[-1]["content"]["text"]


def permission_requests(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("method") == "session/request_permission"]


def decisions(ws: Path, decision_type: str) -> list[dict]:
    conn = connect(ws / "state/proofline.db")
    rows = conn.execute(
        "SELECT * FROM decisions WHERE decision_type = ? ORDER BY rowid", (decision_type,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approvals(repo: Path) -> int:
    return len(load_manifest(repo / ".proofline/specs/mul.yaml").journey_approvals)


@pytest.fixture
def changed(setup):
    """Journey bị sửa sau lần duyệt: cần duyệt lại."""
    repo, ws, config = setup
    (repo / JOURNEY).write_text(CHANGED_JOURNEY, encoding="utf-8")
    return repo, ws, config, new_session(ws, repo)


def test_spec_asks_with_two_allow_once_and_a_human_answer_approves(changed):
    repo, ws, config, sid = changed
    messages = drive(config, prompt(2, sid, "/spec mul"), answer(1, "approve"), seconds=6.0)
    (request,) = permission_requests(messages)
    kinds = [o["kind"] for o in request["params"]["options"]]
    # Two allow_once: PandaCode's Full access only auto-picks when there is exactly one.
    assert kinds == ["allow_once", "allow_once", "reject_once"]
    diff = updates(messages, "tool_call")[0]["content"][1]
    assert diff["type"] == "diff"
    assert diff["oldText"] == GOOD_JOURNEY and diff["newText"] == CHANGED_JOURNEY
    assert "Đã duyệt journey" in last_text(messages)
    assert approvals(repo) == 2
    manifest = load_manifest(repo / ".proofline/specs/mul.yaml")
    assert manifest.approved_blob == sh(repo, "hash-object", JOURNEY)
    row = decisions(ws, "acceptance_approval")[-1]
    assert row["decided_by"] == "human" and row["human_choice"].startswith("approved mul")


def test_an_answer_faster_than_the_guard_is_not_applied(changed):
    repo, ws, config, sid = changed
    messages = drive(config, prompt(2, sid, "/spec mul"), answer(1, "approve"), seconds=0.3)
    text = last_text(messages)
    assert "Chưa áp dụng" in text and "/approve mul" in text
    assert approvals(repo) == 1
    row = decisions(ws, "acceptance_approval")[-1]
    assert row["decided_by"] == "agent" and row["agent_choice"] == "client_auto_suspected"
    # Typing the command is a human act: no guard on the text path.
    messages = drive(config, prompt(3, sid, "/approve mul trông ổn"))
    assert not permission_requests(messages)
    manifest = load_manifest(repo / ".proofline/specs/mul.yaml")
    assert manifest.approved_blob == sh(repo, "hash-object", JOURNEY)
    row = decisions(ws, "acceptance_approval")[-1]
    assert row["decided_by"] == "human" and "trông ổn" in row["human_reason"]


def test_reject_asks_why_and_the_next_message_is_the_reason(changed):
    repo, ws, config, sid = changed
    assert "Vì sao?" in last_text(drive(config, prompt(2, sid, "/spec"), answer(1, "reject")))
    # PandaCode opens a new process for the next turn: the question waits in the DB.
    messages = drive(config, prompt(3, sid, "quá rộng, chỉ cần nhân số dương"))
    assert "Đã ghi lý do từ chối" in last_text(messages)
    row = decisions(ws, "acceptance_approval")[-1]
    assert row["human_choice"].startswith("rejected mul") and "quá rộng" in row["human_reason"]
    assert approvals(repo) == 1
    # Answered once only: the next free text is no longer taken as a reason.
    assert "bắt đầu bằng `/`" in last_text(drive(config, prompt(4, sid, "cảm ơn")))


def test_approve_with_a_note(changed):
    repo, ws, config, sid = changed
    drive(config, prompt(2, sid, "/spec mul"), answer(1, "approve_note"))
    assert approvals(repo) == 2
    drive(config, prompt(3, sid, "nên thêm số âm sau"))
    assert "nên thêm số âm sau" in decisions(ws, "acceptance_approval")[-1]["human_reason"]


def test_cancel_while_waiting_decides_nothing(changed):
    repo, ws, config, sid = changed
    cancel = json.dumps(
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
    )
    messages = drive(config, prompt(2, sid, "/spec mul"), cancel)
    assert messages[-1]["result"] == {"stopReason": "cancelled"}
    assert approvals(repo) == 1
    assert len(decisions(ws, "acceptance_approval")) == 1  # only the CLI approval from setup


def test_status_flags_a_journey_changed_after_approval(changed):
    _, _, config, sid = changed
    assert "đã đổi sau lần duyệt" in last_text(drive(config, prompt(2, sid, "/status")))


def test_verify_over_acp_streams_gates_and_writes_the_report(setup):
    repo, ws, config = setup
    sid = new_session(ws, repo)
    messages = drive(config, prompt(2, sid, "/verify mul"), answer(1, "run_quick"))
    (request,) = permission_requests(messages)
    kinds = [o["kind"] for o in request["params"]["options"]]
    assert kinds == ["allow_once", "allow_once", "reject_once"]
    gates = {
        c["title"].removeprefix("Gate: "): c["status"]
        for c in updates(messages, "tool_call")
        if c["title"].startswith("Gate: ")
    }
    assert gates["acceptance_fails_before"] == "completed"
    assert gates["no_regression"] == "completed"
    assert "Proofline verify: mul" in last_text(messages)
    report = json.loads((repo / ".proofline/verify/mul.json").read_text(encoding="utf-8"))
    assert report["status"] == "pass" and report["runs"] == 1
    assert decisions(ws, "run_repo_commands")[-1]["decided_by"] == "human"
    assert "verify ✅ pass" in last_text(drive(config, prompt(3, sid, "/status")))


def test_verify_is_refused_in_plan_mode_and_guarded_otherwise(setup):
    repo, ws, config = setup
    sid = new_session(ws, repo)
    messages = drive(
        config,
        rpc(1, "session/set_mode", {"sessionId": sid, "modeId": "plan"}),
        prompt(2, sid, "/verify mul"),
    )
    assert "Chế độ Plan" in last_text(messages) and not permission_requests(messages)
    drive(config, rpc(1, "session/set_mode", {"sessionId": sid, "modeId": "default"}))
    messages = drive(config, prompt(2, sid, "/verify mul"), answer(1, "run"), seconds=0.2)
    assert "/verify mul --yes" in last_text(messages)
    assert not (repo / ".proofline/verify/mul.json").exists()
    assert decisions(ws, "run_repo_commands")[-1]["decided_by"] == "agent"


def test_a_pandacode_turn_check_is_not_taken_as_the_reject_reason(changed):
    _, ws, config, sid = changed
    drive(config, prompt(2, sid, "/spec mul"), answer(1, "reject"))
    check = "Project checks failed after your last turn (npm test). Fix the failures first."
    assert "turn checks" in last_text(drive(config, prompt(3, sid, check)))
    assert "npm test" not in (decisions(ws, "acceptance_approval")[-1]["human_reason"] or "")
    # The question is still waiting for the person.
    drive(config, prompt(4, sid, "sai mục tiêu"))
    assert "sai mục tiêu" in decisions(ws, "acceptance_approval")[-1]["human_reason"]
