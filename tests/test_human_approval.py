# ruff: noqa: F811  (fixture `setup` lấy từ test_verify)
"""Duyệt chỉ đến từ người, và verify đối chiếu bản duyệt với sổ quyết định (docs/09 mục 6)."""

from __future__ import annotations

from pathlib import Path

from test_acp import new_session, prompt
from test_acp_decisions import answer, decisions, drive, last_text, permission_requests
from test_verify import JOURNEY, approve_check_commands, run_verify, setup, sh  # noqa: F401
from typer.testing import CliRunner

from proofline import cli
from proofline.cli import app
from proofline.verify import JourneyApproval, load_manifest, save_manifest

SPEC = ".proofline/specs/mul.yaml"


def change_journey(repo: Path) -> None:
    (repo / JOURNEY).write_text(
        (repo / JOURNEY).read_text(encoding="utf-8") + "\n# đổi\n", encoding="utf-8"
    )


def test_cli_approval_is_refused_outside_a_terminal(setup, monkeypatch):
    repo, ws, config = setup
    monkeypatch.setattr(cli, "_interactive", lambda: False)  # an agent's shell
    change_journey(repo)
    before = len(decisions(ws, "acceptance_approval"))
    result = CliRunner().invoke(app, ["spec", "approve", str(repo / SPEC), "--config", str(config)])
    assert result.exit_code == 2 and "terminal của bạn" in result.output
    assert len(load_manifest(repo / SPEC).journey_approvals) == 1
    assert len(decisions(ws, "acceptance_approval")) == before
    result = CliRunner().invoke(
        app, ["target", "approve-commands", "calc", "--config", str(config)]
    )
    assert result.exit_code == 2


def test_cli_approval_needs_the_shown_code(setup):
    repo, _, config = setup
    change_journey(repo)
    result = CliRunner().invoke(
        app, ["spec", "approve", str(repo / SPEC), "--config", str(config)], input="y\n"
    )
    assert result.exit_code == 2 and "Không khớp" in result.output
    assert len(load_manifest(repo / SPEC).journey_approvals) == 1


def test_terminal_approval_is_linked_to_a_human_decision(setup):
    repo, ws, config = setup
    entry = load_manifest(repo / SPEC).journey_approvals[-1]
    assert entry.via == "terminal" and entry.decision_id
    row = next(
        r for r in decisions(ws, "acceptance_approval") if r["decision_id"] == entry.decision_id
    )
    assert row["decided_by"] == "human" and "[terminal]" in row["human_reason"]
    _, gates = run_verify(repo, config)
    assert gates["acceptance_unchanged"].status == "pass"
    assert "qua terminal" in gates["acceptance_unchanged"].detail


def forge(repo: Path, decision_id: str) -> None:
    """Agent tự thêm một bản duyệt vào manifest trên nhánh của nó."""
    change_journey(repo)
    manifest = load_manifest(repo / SPEC)
    manifest.journey_approvals.append(
        JourneyApproval(
            blob=sh(repo, "hash-object", JOURNEY),
            approved_at="2026-10-27T00:00:00+00:00",
            decision_id=decision_id,
            via="terminal",
        )
    )
    save_manifest(repo / SPEC, manifest)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "forged approval")


def test_an_approval_written_into_the_manifest_without_a_decision_fails(setup):
    repo, _, config = setup
    forge(repo, "0" * 32)
    report, gates = run_verify(repo, config)
    assert gates["acceptance_unchanged"].status == "fail"
    assert "không khớp quyết định nào của người" in gates["acceptance_unchanged"].detail
    assert report.status == "fail"


def test_reusing_an_old_decision_for_a_new_journey_fails(setup):
    repo, _, config = setup
    old = load_manifest(repo / SPEC).journey_approvals[-1].decision_id
    forge(repo, old)  # real human decision, but for the previous journey
    _, gates = run_verify(repo, config)
    assert gates["acceptance_unchanged"].status == "fail"


def test_an_approval_from_before_this_check_is_not_trusted(setup):
    repo, _, config = setup
    forge(repo, "")
    _, gates = run_verify(repo, config)
    assert gates["acceptance_unchanged"].status == "unknown"
    assert "duyệt lại" in gates["acceptance_unchanged"].detail


def set_test_command(config: Path, command: str) -> None:
    text = config.read_text(encoding="utf-8")
    start = text.index("      test_junit: ")
    end = text.index("\n", start)
    config.write_text(text[:start] + f"      test_junit: '{command}'" + text[end:], "utf-8")


def test_changed_check_commands_stop_verify_before_running_them(setup):
    repo, _, config = setup
    original = config.read_text(encoding="utf-8")
    set_test_command(config, "echo skip-all-tests {junit} {files}")
    report, gates = run_verify(repo, config)
    commands = gates["commands_approved"]
    assert commands.status == "fail" and commands.evidence["changed"] == ["commands.test_junit"]
    assert "commands.test_junit" in commands.detail
    # The unapproved command never ran: only the gates that read git are there.
    assert "no_regression" not in gates and "acceptance_fails_before" not in gates
    assert report.status == "fail"
    # Back to the approved commands: passes again without a new approval.
    config.write_text(original, encoding="utf-8")
    _, gates = run_verify(repo, config)
    assert gates["commands_approved"].status == "pass"


def test_new_commands_pass_once_a_human_approves_them(setup):
    repo, _, config = setup
    set_test_command(
        config,
        (config.read_text(encoding="utf-8").split("test_junit: '")[1].split("'")[0]).replace(
            "-q ", "-q -ra "
        ),
    )
    assert run_verify(repo, config)[1]["commands_approved"].status == "fail"
    approve_check_commands(config)
    assert run_verify(repo, config)[1]["commands_approved"].status == "pass"


def test_commands_never_approved_are_reported(setup):
    repo, ws, config = setup
    (ws / "state/proofline.db").unlink()
    _, gates = run_verify(repo, config)
    assert gates["commands_approved"].status == "unknown"
    assert "approve-commands calc" in gates["commands_approved"].detail


def test_acp_commands_approval_goes_through_the_guard(setup):
    repo, ws, config = setup
    set_test_command(config, "python -m pytest {files} --junitxml={junit}")
    sid = new_session(ws, repo)
    messages = drive(config, prompt(2, sid, "/commands"), answer(1, "approve"), seconds=0.3)
    assert "Chưa áp dụng" in last_text(messages)
    assert run_verify(repo, config)[1]["commands_approved"].status == "fail"
    messages = drive(config, prompt(3, sid, "/commands"), answer(1, "approve"), seconds=6.0)
    (request,) = permission_requests(messages)
    assert [o["kind"] for o in request["params"]["options"]] == [
        "allow_once",
        "allow_once",
        "reject_once",
    ]
    assert "Đã duyệt lệnh kiểm tra" in last_text(messages)
    row = decisions(ws, "commands_approval")[-1]
    assert row["decided_by"] == "human" and "[acp]" in row["human_reason"]
    assert run_verify(repo, config)[1]["commands_approved"].status == "pass"


def test_acp_journey_approval_is_linked_to_the_decision(setup):
    repo, ws, config = setup
    change_journey(repo)
    sid = new_session(ws, repo)
    drive(config, prompt(2, sid, "/spec mul"), answer(1, "approve"), seconds=6.0)
    entry = load_manifest(repo / SPEC).journey_approvals[-1]
    assert entry.via == "acp"
    row = next(
        r for r in decisions(ws, "acceptance_approval") if r["decision_id"] == entry.decision_id
    )
    assert row["decided_by"] == "human"
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "journey v2")
    assert run_verify(repo, config)[1]["acceptance_unchanged"].status == "pass"
