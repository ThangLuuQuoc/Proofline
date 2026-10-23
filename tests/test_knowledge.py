# ruff: noqa: F811  (fixture `setup` lấy từ test_verify)
"""Kho tri thức bước K0: sự thật đo được từ verify / assess, hết hạn khi base đổi."""

from __future__ import annotations

import yaml
from test_acp import new_session, prompt
from test_acp_decisions import drive, last_text
from test_verify import run_verify, setup, sh  # noqa: F401
from typer.testing import CliRunner

from proofline import knowledge
from proofline.assess import Assessment, Criterion
from proofline.cli import app
from proofline.config import load_config
from proofline.verify import Gate, VerifyReport


def base_report(repo, *, always=(), unstable=(), per_run=(0, 0), lint=None, install_fails=False):
    """Báo cáo verify giả với phần "base" như verify thật ghi vào evidence."""
    base_sha = sh(repo, "rev-parse", "main")
    gates = [
        Gate(
            "no_regression",
            "pass",
            "",
            {
                "base": {
                    "failed_per_run": list(per_run),
                    "always_failing": list(always),
                    "unstable": list(unstable),
                }
            },
        )
    ]
    if lint is not None:
        gates.append(Gate("static_lint", "unknown", "", {"command": "pnpm lint", "base": lint}))
    if install_fails:
        gates.append(
            Gate("install_base", "unknown", "", {"command": "pnpm i", "output": "ERR_PNPM"})
        )
    return VerifyReport(
        spec="mul",
        repo="calc",
        base_ref="main",
        base_sha=base_sha,
        head_sha=sh(repo, "rev-parse", "HEAD"),
        runs=len(per_run),
        gates=gates,
        verified_at="2026-10-23T00:00:00+00:00",
        installed=True,
    )


def facts_of(config, repo_name="calc"):
    cfg = load_config(config)
    return {f.id: f for f in knowledge.current_facts(cfg, cfg.target_repo(repo_name)).facts}


def test_verify_facts_are_recorded_confirmed_and_retired(setup):
    repo, ws, config = setup
    cfg = load_config(config)
    target = cfg.target_repo("calc")
    report = base_report(
        repo,
        always=["t::x"],
        unstable=["t::y"],
        per_run=(1, 2),
        lint={"broken": True, "cause": "eslint: command not found"},
        install_fails=True,
    )
    changes = knowledge.record_verify(cfg, target, report, ws / "x.json")
    assert {c.action for c in changes} == {"added"}
    path = ws / "knowledge/repo/calc/facts.yaml"
    assert path.is_file() and not (repo / "knowledge").exists()  # never in the target repo
    facts = facts_of(config)
    assert set(facts) == {
        "verify.tests.preexisting_failures@main",
        "verify.tests.unstable_on_base@main",
        "verify.static.lint.broken@main",
        "verify.install.broken@main",
    }
    unstable = facts["verify.tests.unstable_on_base@main"]
    assert "1 test fail không ổn định" in unstable.statement and "1, 2" in unstable.statement
    assert unstable.scope.commit == sh(repo, "rev-parse", "main")
    assert facts["verify.static.lint.broken@main"].evidence == "x.json"

    # Measured again on the same base commit: results add up instead of replacing each other.
    again = base_report(repo, always=["t::x"], per_run=(1, 1))
    actions = {c.fact_id: c.action for c in knowledge.record_verify(cfg, target, again)}
    assert actions["verify.tests.preexisting_failures@main"] == "confirmed"
    assert actions["verify.tests.unstable_on_base@main"] == "updated"  # t::y was seen before
    assert actions["verify.install.broken@main"] == "retired"  # install worked this time
    # Not measured this time (the lint gate did not run on base): left as it was.
    assert "verify.static.lint.broken@main" not in actions
    facts = facts_of(config)
    assert facts["verify.tests.preexisting_failures@main"].measurements == 2
    unstable = facts["verify.tests.unstable_on_base@main"]
    assert unstable.status == "active" and "1, 2, 1, 1, qua 2 lần đo" in unstable.statement
    assert facts["verify.static.lint.broken@main"].status == "active"

    # A test failing every run now but passing earlier on the same commit is unstable, not
    # pre-existing (tiniagent: 0 then 23 failures, later 1 then 2).
    knowledge.record_verify(cfg, target, base_report(repo, always=["t::x", "t::z"], per_run=(2, 2)))
    facts = facts_of(config)
    assert facts["verify.tests.unstable_on_base@main"].data["sample"] == ["t::y", "t::z"]
    assert facts["verify.tests.preexisting_failures@main"].data["sample"] == ["t::x"]

    # A new base commit starts over; what no longer holds there is retired.
    moved = sh(repo, "commit-tree", "main^{tree}", "-p", "main", "-m", "move main")
    sh(repo, "update-ref", "refs/heads/main", moved)
    knowledge.record_verify(cfg, target, base_report(repo, always=["t::q"], per_run=(1,)))
    facts = facts_of(config)
    assert facts["verify.tests.preexisting_failures@main"].status == "active"
    # One run cannot tell unstable from stable: not measured, so not retired either.
    assert facts["verify.tests.unstable_on_base@main"].status == "expired"
    # A second single run on the same commit makes two: t::q failed once, passed once.
    knowledge.record_verify(cfg, target, base_report(repo, per_run=(0,)))
    facts = facts_of(config)
    assert facts["verify.tests.preexisting_failures@main"].status == "retired"
    assert facts["verify.tests.unstable_on_base@main"].status == "active"
    assert facts["verify.tests.unstable_on_base@main"].data["sample"] == ["t::q"]


def test_facts_expire_when_the_base_moves(setup):
    repo, ws, config = setup
    cfg = load_config(config)
    target = cfg.target_repo("calc")
    knowledge.record_verify(cfg, target, base_report(repo, always=["t::x"], per_run=(1, 1)))
    old = sh(repo, "rev-parse", "main")
    # Someone moves main (without touching this checkout).
    moved = sh(repo, "commit-tree", "main^{tree}", "-p", "main", "-m", "move main")
    sh(repo, "update-ref", "refs/heads/main", moved)
    fact = facts_of(config)["verify.tests.preexisting_failures@main"]
    assert fact.status == "expired"
    assert old[:7] in fact.status_reason and "cần đo lại" in fact.status_reason
    # Measuring the new base brings it back.
    knowledge.record_verify(cfg, target, base_report(repo, always=["t::x"], per_run=(1, 1)))
    fact = facts_of(config)["verify.tests.preexisting_failures@main"]
    assert fact.status == "active" and fact.scope.commit == sh(repo, "rev-parse", "main")


def test_a_clean_base_records_nothing(setup):
    repo, ws, config = setup
    report, gates = run_verify(repo, config)  # real verify: nothing fails on the calc base
    base = gates["no_regression"].evidence["base"]
    assert base == {"failed_per_run": [0], "always_failing": [], "unstable": []}
    cfg = load_config(config)
    assert knowledge.record_verify(cfg, cfg.target_repo("calc"), report) == []
    assert facts_of(config) == {}


def test_assess_records_only_measured_criteria(setup):
    repo, ws, config = setup
    cfg = load_config(config)
    target = cfg.target_repo("calc")

    def assessment(app_status):
        criteria = (
            Criterion("has_runnable_code", "R1", "pass", "có pytest.ini"),
            Criterion("test_command_runs", "R1", "unknown", "chạy với --run"),
            Criterion("app_starts_and_healthy", "R1", app_status, "healthcheck :3200"),
        )
        return Assessment("calc", "R0", criteria, "2026-10-23T00:00:00+00:00")

    knowledge.record_assess(cfg, target, assessment("fail"), repo, evidence="snapshot 1")
    facts = facts_of(config)
    assert list(facts) == ["assess.app_starts_and_healthy.fails@feature"]
    assert (
        "App khởi chạy: không đạt" in facts["assess.app_starts_and_healthy.fails@feature"].statement
    )
    knowledge.record_assess(cfg, target, assessment("pass"), repo)
    facts = facts_of(config)
    assert facts["assess.app_starts_and_healthy.fails@feature"].status == "retired"
    assert facts["assess.app_starts_and_healthy.ok@feature"].status == "active"


def test_knowledge_show_and_acp_status(setup):
    repo, ws, config = setup
    cfg = load_config(config)
    target = cfg.target_repo("calc")
    knowledge.record_verify(
        cfg,
        target,
        base_report(repo, always=["t::x"], unstable=["t::y"], per_run=(1, 2), install_fails=True),
    )
    knowledge.record_verify(cfg, target, base_report(repo, always=["t::x"], per_run=(1, 1)))
    runner = CliRunner()
    out = runner.invoke(app, ["knowledge", "show", "calc", "--config", str(config)]).output
    assert "calc: 2 sự thật (1 đã hết hạn/rút" in out
    assert "1 test fail sẵn trên `main`" in out and "`pnpm i` lỗi" not in out
    out = runner.invoke(app, ["knowledge", "show", "--all", "--config", str(config)]).output
    assert "retired: đo lại" in out
    sid = new_session(ws, repo)
    text = last_text(drive(config, prompt(2, sid, "/status")))
    assert "Kho tri thức: 2 sự thật đang dùng, 1 đã hết hạn/rút" in text
    assert "1 test fail sẵn trên `main`" in text
    data = yaml.safe_load((ws / "knowledge/repo/calc/facts.yaml").read_text(encoding="utf-8"))
    assert (
        len(data["facts"]) == 3 and data["observations"]["verify.tests@main"]["measurements"] == 2
    )


def journey_report(repo, outcomes, cause="Error: Hook timed out in 10000ms"):
    report = base_report(repo)
    attempts = [{"outcome": o, "detail": cause if o == "wrong_reason" else "1/1"} for o in outcomes]
    report.gates.append(
        Gate("acceptance_fails_before", "pass", "", {"attempts": attempts, "failed": []})
    )
    return report


def test_journey_retries_on_base_become_a_fact(setup):
    repo, ws, config = setup
    cfg = load_config(config)
    target = cfg.target_repo("calc")
    key = "verify.journey.inconclusive_on_base@main"
    knowledge.record_verify(cfg, target, journey_report(repo, ["assertion"]))
    assert key not in facts_of(config)  # conclusive at once: nothing to say
    knowledge.record_verify(cfg, target, journey_report(repo, ["wrong_reason", "assertion"]))
    fact = facts_of(config)[key]
    assert "1/3 lần" in fact.statement and "chạy lại 1 lần" in fact.statement
    assert "Hook timed out" in fact.statement
    # Same commit: counts add up, a clean run does not erase what was seen.
    knowledge.record_verify(cfg, target, journey_report(repo, ["assertion"]))
    fact = facts_of(config)[key]
    assert fact.status == "active" and fact.data["runs"] == 4 and fact.data["retries"] == 1
    # New base commit, clean run: retired.
    moved = sh(repo, "commit-tree", "main^{tree}", "-p", "main", "-m", "move main")
    sh(repo, "update-ref", "refs/heads/main", moved)
    knowledge.record_verify(cfg, target, journey_report(repo, ["assertion"]))
    assert facts_of(config)[key].status == "retired"
