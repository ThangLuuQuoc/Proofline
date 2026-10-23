from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from proofline.cli import app
from proofline.config import load_config
from proofline.junit import TestRun, parse_junit
from proofline.proc import CommandResult
from proofline.state import init_db
from proofline.verify import (
    Gate,
    VerifyContext,
    classify_regressions,
    gate_static,
    load_manifest,
    verify,
)
from proofline.workspace import init_workspace

PY = f'"{sys.executable}"'
JOURNEY = "tests/journeys/test_journey.py"
GOOD_JOURNEY = (
    "import calc\n\n\ndef test_user_can_multiply():\n"
    '    assert hasattr(calc, "mul"), "mul missing"\n'
    "    assert calc.mul(2, 3) == 6\n"
)


def sh(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    """Một repo Python nhỏ: base trên main, nhánh feature thêm mul() + test + journey đã duyệt."""
    repo = tmp_path / "calc"
    write(repo / "pytest.ini", "[pytest]\npythonpath = .\n")
    write(repo / "calc.py", "def add(a, b):\n    return a + b\n")
    write(
        repo / "tests/test_add.py",
        "import calc\n\n\ndef test_add():\n    assert calc.add(1, 2) == 3\n",
    )
    sh(repo.parent, "init", "-q", "-b", "main", str(repo))
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "base")
    sh(repo, "checkout", "-q", "-b", "feature")
    write(
        repo / "calc.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    write(
        repo / "tests/test_mul.py",
        "import calc\n\n\ndef test_mul():\n    assert calc.mul(2, 2) == 4\n",
    )
    write(repo / JOURNEY, GOOD_JOURNEY)
    write(
        repo / ".proofline/specs/mul.yaml",
        f"name: mul\ntask_type: feature\nbase: main\njourney: {JOURNEY}\n",
    )
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "feature")

    ws = tmp_path / "ws"
    init_workspace(ws)
    config = ws / "proofline.yaml"
    target = (
        "target_repos:\n"
        "  - name: calc\n"
        f"    local_path: {repo.as_posix()}\n"
        "    commands:\n"
        f"      test_junit: '{PY} -m pytest -q -p no:cacheprovider"
        " --junitxml={junit} {files}'\n"
    )
    config.write_text(
        config.read_text(encoding="utf-8").replace("target_repos: []\n", target), encoding="utf-8"
    )
    runner = CliRunner()
    approved = runner.invoke(
        app, ["spec", "approve", str(repo / ".proofline/specs/mul.yaml"), "--config", str(config)]
    )
    assert approved.exit_code == 0, approved.output
    return repo, ws, config


def run_verify(repo: Path, config: Path, runs: int = 1):
    cfg = load_config(config)
    ctx = VerifyContext(
        config=cfg,
        target=cfg.target_repo("calc"),
        candidate=repo,
        manifest=load_manifest(repo / ".proofline/specs/mul.yaml"),
        runs=runs,
    )
    report = verify(ctx)
    return report, {g.key: g for g in report.gates}


def commit(repo: Path, message: str) -> None:
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", message)


def test_spec_approve_records_blob_and_decision(setup):
    repo, ws, config = setup
    manifest = load_manifest(repo / ".proofline/specs/mul.yaml")
    assert manifest.approved_blob == sh(repo, "hash-object", JOURNEY)
    conn = init_db(ws / "state/proofline.db")
    row = conn.execute(
        "SELECT * FROM decisions WHERE decision_type = 'acceptance_approval'"
    ).fetchone()
    conn.close()
    assert row["decided_by"] == "human" and row["autonomy_level"] == "L1"


def test_all_gates_pass_on_a_good_change(setup):
    repo, _, config = setup
    report, gates = run_verify(repo, config, runs=2)
    assert report.status == "pass", report.to_markdown()
    assert gates["acceptance_fails_before"].evidence["failed"]
    assert gates["new_tests_added"].evidence["added"] == ["tests/test_mul.py"]
    # The base clone lives in the workspace, never as a worktree of the user's repo.
    assert len(sh(repo, "worktree", "list").splitlines()) == 1


def test_changed_journey_and_touched_tests_fail(setup):
    repo, _, config = setup
    write(repo / JOURNEY, GOOD_JOURNEY.replace("== 6", ">= 0"))
    write(
        repo / "tests/test_add.py",
        "import calc\nimport pytest\n\n\n@pytest.mark.skip\n"
        "def test_add():\n    assert calc.add(1, 2) == 3\n",
    )
    commit(repo, "loosen")
    _, gates = run_verify(repo, config)
    assert gates["acceptance_unchanged"].status == "fail"
    integrity = gates["test_integrity"]
    assert integrity.status == "fail"
    assert integrity.evidence["touched"] == ["tests/test_add.py"]
    assert "@pytest.mark.skip" in integrity.evidence["skips"][0]


def test_journey_failing_for_the_wrong_reason_on_base(setup):
    repo, _, config = setup
    write(
        repo / JOURNEY,
        "from calc import mul\n\n\ndef test_user_can_multiply():\n    assert mul(2, 3) == 6\n",
    )
    commit(repo, "journey imports mul")
    runner = CliRunner()
    runner.invoke(
        app, ["spec", "approve", str(repo / ".proofline/specs/mul.yaml"), "--config", str(config)]
    )
    _, gates = run_verify(repo, config)
    assert gates["acceptance_unchanged"].status == "pass"
    assert gates["acceptance_fails_before"].status == "fail"
    assert "sai lý do" in gates["acceptance_fails_before"].detail


def test_a_real_regression_fails(setup):
    repo, _, config = setup
    write(
        repo / "calc.py", "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    commit(repo, "break add")
    report, gates = run_verify(repo, config, runs=2)
    assert report.status == "fail"
    assert gates["no_regression"].status == "fail"
    assert gates["no_regression"].evidence["consistent"] == ["tests.test_add::test_add"]


def test_classify_regressions():
    def run(*failed: str, ids=("a", "b", "c", "d", "e")) -> TestRun:
        from proofline.junit import TestOutcome

        return TestRun(tuple(TestOutcome(i, "failed" if i in failed else "passed") for i in ids))

    base = [run("a"), run("a", "b")]
    head = [
        run("a", "c", "new", ids=("a", "b", "c", "d", "e", "new")),
        run("a", "b", "c", "d", ids=("a", "b", "c", "d", "e", "new")),
    ]
    groups = classify_regressions(base, head)
    assert groups["consistent"] == ["c"]  # fails every head run, never on base
    assert groups["unstable"] == ["d"]  # fails some head runs only
    assert groups["pre_existing"] == ["a", "b"]
    assert groups["new_tests_failing"] == ["new"]


def test_parse_vitest_style_junit(tmp_path):
    xml = tmp_path / "r.xml"
    xml.write_text(
        '<testsuites><testsuite name="test/a.test.ts" tests="3" failures="1" errors="1">'
        '<testcase classname="test/a.test.ts" name="works"/>'
        '<testcase classname="test/a.test.ts" name="checks">'
        '<failure type="AssertionError" message="expected 1 to be 2"/></testcase>'
        '<testcase classname="test/a.test.ts" name="later"><skipped/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    run = parse_junit(xml)
    by_id = run.by_id()
    assert by_id["test/a.test.ts::checks"].is_assertion
    assert by_id["test/a.test.ts::later"].status == "skipped"
    assert "test/a.test.ts::<suite error>" in run.failed_ids  # errors="1" with no erroring case


def test_static_gate(setup, tmp_path):
    repo, _, config = setup
    cfg = load_config(config)
    target = cfg.target_repo("calc")
    target.commands["lint"] = "lint"

    def ctx_with(base_out: tuple[int, str], head_out: tuple[int, str]):
        def runner(command: str, cwd: Path, timeout: float) -> CommandResult:
            code, out = base_out if cwd == tmp_path else head_out
            return CommandResult(command, code, out, 0.1)

        return VerifyContext(
            config=cfg,
            target=target,
            candidate=repo,
            manifest=load_manifest(repo / ".proofline/specs/mul.yaml"),
            runner=runner,
        )

    def status(base_out, head_out) -> Gate:
        return gate_static(ctx_with(base_out, head_out), tmp_path, "lint")

    assert status((1, "a.py:1:1: error x"), (0, "")).status == "pass"
    assert status((0, ""), (1, "a.py:1:1: error x")).status == "fail"
    assert status((1, "error a\nerror b"), (1, "error a")).status == "pass"
    assert status((1, "error a"), (1, "error a\nerror b")).status == "fail"
    broken = status((2, "Error [ERR_MODULE_NOT_FOUND]: x"), (2, "Error [ERR_MODULE_NOT_FOUND]: x"))
    assert broken.status == "unknown" and "hỏng sẵn" in broken.detail
    # The base's cause comes from the base's own output, without its checkout path.
    broken = status(
        (2, f"Error [ERR_MODULE_NOT_FOUND]: {tmp_path}/node_modules/x"),
        (2, f"Error [ERR_MODULE_NOT_FOUND]: {repo}/node_modules/y"),
    )
    assert broken.evidence["base"] == {
        "broken": True,
        "cause": "Error [ERR_MODULE_NOT_FOUND]: <repo>/node_modules/x",
    }
    assert "<repo>/node_modules/y" in broken.detail


def test_cli_verify_writes_report(setup):
    repo, _, config = setup
    result = CliRunner().invoke(
        app,
        ["verify", str(repo / ".proofline/specs/mul.yaml"), "--runs", "1", "--config", str(config)],
    )
    assert result.exit_code == 0, result.output
    assert "acceptance_fails_before" in result.output
    report = json.loads((repo / ".proofline/verify/mul.json").read_text(encoding="utf-8"))
    assert report["status"] == "pass"
    assert (
        (repo / ".proofline/verify/mul.md")
        .read_text(encoding="utf-8")
        .startswith("## Proofline verify: mul")
    )
