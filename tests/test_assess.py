from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from proofline.assess import (
    CONFIRMED_MARKER,
    AssessContext,
    Criterion,
    assess,
    compute_level,
    parse_features,
    save_snapshot,
)
from proofline.cli import app
from proofline.config import ProoflineConfig
from proofline.proc import AppCheckResult, CommandResult
from proofline.state import init_db

INVENTORY = f"""# Tính năng
{CONFIRMED_MARKER}

### F-1: Đăng nhập
journey: `tests/journeys/login.spec.ts`

### F-2: Tạo agent
journey: tests/journeys/create-agent.spec.ts

### F-3: Xóa agent
(chưa có journey)
"""


ORIGINAL_DOC = "# Danh mục tính năng gốc của repo\n| Agent Studio | Tạo agent |\n"


def ok(command: str, cwd: Path, timeout: float) -> CommandResult:
    return CommandResult(command, 0, "", 1.0)


def healthy(start: str, url: str, cwd: Path, timeout: float) -> AppCheckResult:
    return AppCheckResult(True, f"healthcheck {url} OK", "")


def make_repo(root: Path, *, journeys: int = 2) -> Path:
    repo = root / "svc"
    (repo / "docs").mkdir(parents=True)
    (repo / "package.json").write_text("{}", encoding="utf-8")
    (repo / "Dockerfile").write_text("FROM node:22\n", encoding="utf-8")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/svc.git\n', encoding="utf-8"
    )
    (repo / "docs" / "FEATURES.md").write_text(ORIGINAL_DOC, encoding="utf-8")
    (repo / ".proofline").mkdir()
    (repo / ".proofline" / "features.md").write_text(INVENTORY, encoding="utf-8")
    (repo / "tests" / "journeys").mkdir(parents=True)
    for name in ["login.spec.ts", "create-agent.spec.ts"][:journeys]:
        (repo / "tests" / "journeys" / name).write_text("//", encoding="utf-8")
    return repo


@pytest.fixture
def cfg(config_dict) -> ProoflineConfig:
    config_dict["target_repos"][0].update(
        {
            "commands": {"install": "pnpm install", "test": "pnpm test"},
            "app_runtime": {
                "enabled": True,
                "start_command": "pnpm dev",
                "healthcheck_url": "http://localhost:3200/",
            },
        }
    )
    config_dict["readiness"] = {"levels": {"R2": {"min_features_with_journey_ratio": 0.6}}}
    return ProoflineConfig.model_validate(config_dict)


def make_ctx(cfg: ProoflineConfig, tmp_path: Path, repo: Path, **kwargs) -> AssessContext:
    return AssessContext(
        config=cfg,
        repo=cfg.target_repo("svc"),
        repo_path=repo,
        workspace=tmp_path,
        **kwargs,
    )


def by_key(result, key: str) -> Criterion:
    return next(c for c in result.criteria if c.key == key)


def test_compute_level_stops_at_first_gap():
    crit = [
        Criterion("a", "R1", "pass", ""),
        Criterion("b", "R2", "unknown", ""),
        Criterion("c", "R3", "pass", ""),
    ]
    assert compute_level(crit) == "R1"
    assert compute_level([Criterion("a", "R1", "fail", "")]) == "R0"


def test_missing_repo_is_r0(cfg, tmp_path):
    result = assess(make_ctx(cfg, tmp_path, tmp_path / "nope"))
    assert result.level == "R0"
    assert result.criteria[0].key == "repo_exists"


def test_empty_repo_points_to_idea_track(cfg, tmp_path):
    (tmp_path / "svc").mkdir()
    result = assess(make_ctx(cfg, tmp_path, tmp_path / "svc"))
    assert result.level == "R0"
    assert "luồng A" in by_key(result, "has_runnable_code").action


def test_static_run_leaves_execution_unknown(cfg, tmp_path):
    result = assess(make_ctx(cfg, tmp_path, make_repo(tmp_path)))
    assert result.level == "R0"
    for key in ("container_builds", "test_command_runs", "app_starts_and_healthy"):
        assert by_key(result, key).status == "unknown"
    assert by_key(result, "feature_inventory_confirmed").status == "pass"
    assert by_key(result, "ci_green_on_default_branch").status == "unknown"


def test_full_run_reaches_r2(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("proofline.assess.shutil.which", lambda _: "docker")
    ctx = make_ctx(
        cfg,
        tmp_path,
        make_repo(tmp_path),
        run_commands=True,
        runner=ok,
        app_checker=healthy,
        ci_status=lambda slug, branch: "pass" if slug == "acme/svc" else "fail",
    )
    result = assess(ctx)
    assert result.level == "R2"
    assert by_key(result, "features_with_journey").status == "pass"  # 2/3 ≥ 60%
    assert {c.level for c in result.missing_for_next_level()} == {"R3"}
    assert [c.command for c in ctx.command_log] == [
        "docker build -q -t proofline-assess-svc .",
        "pnpm install",
        "pnpm test",
    ]


def test_install_failure_fails_tests(cfg, tmp_path):
    def runner(command: str, cwd: Path, timeout: float) -> CommandResult:
        code = 1 if command == "pnpm install" else 0
        return CommandResult(command, code, "line1\nERR_PNPM boom", 1.0)

    ctx = make_ctx(cfg, tmp_path, make_repo(tmp_path), run_commands=True, runner=runner)
    criterion = by_key(assess(ctx), "test_command_runs")
    assert criterion.status == "fail"
    assert "ERR_PNPM boom" in criterion.detail
    assert "pnpm test" not in [c.command for c in ctx.command_log]


def test_journey_coverage_below_threshold(cfg, tmp_path):
    result = assess(make_ctx(cfg, tmp_path, make_repo(tmp_path, journeys=1)))
    criterion = by_key(result, "features_with_journey")
    assert criterion.status == "fail"
    assert "F-2" in criterion.action and "F-3" in criterion.action
    assert "tests/journeys/" in criterion.action


def test_unconfirmed_inventory(cfg, tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".proofline" / "features.md").write_text("# Tính năng\n", encoding="utf-8")
    criterion = by_key(assess(make_ctx(cfg, tmp_path, repo)), "feature_inventory_confirmed")
    assert criterion.status == "fail"
    assert ".proofline/features.md" in criterion.detail
    assert CONFIRMED_MARKER in criterion.action


def test_original_doc_is_only_a_source(cfg, tmp_path):
    # Tài liệu gốc trùng tên (khác hoa thường), thậm chí đúng định dạng Proofline,
    # vẫn không được coi là danh sách tính năng của Proofline và không bị sửa.
    repo = make_repo(tmp_path)
    (repo / ".proofline" / "features.md").unlink()
    (repo / "docs" / "FEATURES.md").write_text(INVENTORY, encoding="utf-8")
    result = assess(make_ctx(cfg, tmp_path, repo))
    inventory = by_key(result, "feature_inventory_confirmed")
    assert inventory.status == "fail"
    assert "docs/FEATURES.md" in inventory.detail and "nguồn" in inventory.detail
    assert "proofline features draft" in inventory.action
    assert by_key(result, "features_with_journey").status == "unknown"
    assert (repo / "docs" / "FEATURES.md").read_text(encoding="utf-8") == INVENTORY


def test_workspace_artifacts_location(config_dict, tmp_path):
    config_dict["target_repos"][0]["artifacts_location"] = "workspace"
    cfg = ProoflineConfig.model_validate(config_dict)
    repo = make_repo(tmp_path)
    (repo / ".proofline" / "features.md").unlink()
    target = tmp_path / "targets" / "svc"
    target.mkdir(parents=True)
    (target / "features.md").write_text(INVENTORY, encoding="utf-8")
    criterion = by_key(assess(make_ctx(cfg, tmp_path, repo)), "feature_inventory_confirmed")
    assert criterion.status == "pass"
    assert criterion.detail.startswith("targets/svc/features.md")


def test_no_ci_config(cfg, tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".github" / "workflows" / "ci.yml").unlink()
    criterion = by_key(assess(make_ctx(cfg, tmp_path, repo)), "ci_green_on_default_branch")
    assert criterion.status == "fail"


def test_parse_features():
    assert parse_features(INVENTORY) == {
        "F-1": "tests/journeys/login.spec.ts",
        "F-2": "tests/journeys/create-agent.spec.ts",
        "F-3": None,
    }


def test_golden_set_counts_and_synthetic_ratio(cfg, tmp_path):
    for i, source in enumerate(["history", "synthetic", "synthetic"]):
        case = tmp_path / "evals" / "golden-set" / "svc" / f"c{i}"
        case.mkdir(parents=True)
        (case / "case.yaml").write_text(f"source: {source}\n", encoding="utf-8")
    criterion = by_key(assess(make_ctx(cfg, tmp_path, make_repo(tmp_path))), "golden_set")
    assert criterion.status == "fail"
    assert "3 case" in criterion.detail and "67%" in criterion.detail


def test_save_snapshot(cfg, tmp_path):
    result = assess(make_ctx(cfg, tmp_path, make_repo(tmp_path)))
    db = tmp_path / "state" / "proofline.db"
    snapshot_id = save_snapshot(db, result)
    conn = init_db(db)
    row = conn.execute(
        "SELECT * FROM readiness_snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    conn.close()
    assert row["repo"] == "svc" and row["level"] == "R0"
    assert len(json.loads(row["criteria"])) == len(result.criteria)
    assert json.loads(row["missing_actions"])


def test_cli_assess(config_dict, write_config, tmp_path, monkeypatch):
    make_repo(tmp_path)
    config_dict["target_repos"][0]["local_path"] = "svc"
    path = write_config(config_dict)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["assess", "svc", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "mức sẵn sàng R0" in result.output
    assert "Để lên R1" in result.output
    assert (tmp_path / "state" / "proofline.db").exists()


def test_cli_assess_unknown_repo(config_dict, write_config):
    result = CliRunner().invoke(
        app, ["assess", "ghost", "--config", str(write_config(config_dict))]
    )
    assert result.exit_code == 1
