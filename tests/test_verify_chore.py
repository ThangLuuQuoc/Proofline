"""Chế độ nhẹ `chore` (docs/09-lessons-macroeconomics.md mục 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_verify import PY, approve_check_commands, as_human_in_terminal, commit, sh, write
from typer.testing import CliRunner

from proofline import knowledge
from proofline.cli import app
from proofline.config import load_config
from proofline.verify import VerifyContext, chore_manifest, verify
from proofline.workspace import init_workspace

CALC = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def chore_repo(tmp_path, monkeypatch):
    """Repo Python nhỏ trên main, đang ở nhánh chore/tidy chưa có thay đổi."""
    repo = tmp_path / "calc"
    write(repo / "pytest.ini", "[pytest]\npythonpath = .\n")
    write(repo / "calc.py", CALC)
    write(
        repo / "tests/test_add.py",
        "import calc\n\n\ndef test_add():\n    assert calc.add(1, 2) == 3\n",
    )
    sh(repo.parent, "init", "-q", "-b", "main", str(repo))
    commit(repo, "base")
    sh(repo, "checkout", "-q", "-b", "chore/tidy")

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
    as_human_in_terminal(monkeypatch)
    approve_check_commands(config)
    return repo, config


def run_chore(repo: Path, config: Path, runs: int = 1, cache=None):
    cfg = load_config(config)
    ctx = VerifyContext(
        config=cfg,
        target=cfg.target_repo("calc"),
        candidate=repo,
        manifest=chore_manifest(repo, "main"),
        runs=runs,
    )
    if cache is not None:
        ctx.base_cache = cache
    report = verify(ctx)
    return report, {g.key: g for g in report.gates}


def test_small_fix_passes_with_the_light_gates(chore_repo):
    repo, config = chore_repo
    write(repo / "calc.py", 'def add(a, b):\n    """Tổng hai số."""\n    return a + b\n')
    commit(repo, "docstring")
    report, gates = run_chore(repo, config, runs=2)
    assert report.status == "pass", report.to_markdown()
    assert report.spec == "chore-chore-tidy" and report.task_type == "chore"
    assert list(gates) == ["commands_approved", "chore_scope", "test_integrity", "no_regression"]
    assert gates["chore_scope"].evidence["code_lines"] == 1
    assert report.to_markdown().startswith("## Proofline verify: chore-chore-tidy (chore)")


def test_a_regression_still_fails(chore_repo):
    repo, config = chore_repo
    write(repo / "calc.py", CALC.replace("a + b", "a - b"))
    commit(repo, "oops")
    report, gates = run_chore(repo, config)
    assert report.status == "fail"
    assert gates["no_regression"].evidence["consistent"] == ["tests.test_add::test_add"]


def test_touching_an_old_test_fails_integrity(chore_repo):
    repo, config = chore_repo
    write(
        repo / "tests/test_add.py",
        "import calc\n\n\ndef test_add():\n    assert calc.add(1, 2) >= 0\n",
    )
    commit(repo, "loosen")
    _, gates = run_chore(repo, config)
    assert gates["test_integrity"].status == "fail"


@pytest.mark.parametrize(
    ("path", "text", "reason"),
    [
        ("calc.py", CALC + "".join(f"\nX{i} = {i}" for i in range(70)) + "\n", "quá 60"),
        ("db/migrations/001.sql", "ALTER TABLE t ADD c int;\n", "vùng nhạy cảm"),
        (".proofline/specs/x.yaml", "name: x\nbase: main\njourney: j.py\n", "vùng nhạy cảm"),
        (
            "api.py",
            "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/x')\n"
            "def x():\n    return 1\n",
            "lối vào mới",
        ),
        ("web/app/reports/page.tsx", "export default function P() { return null }\n", "lối vào"),
    ],
)
def test_changes_that_are_not_chores_are_refused_before_running_tests(
    chore_repo, path, text, reason
):
    repo, config = chore_repo
    write(repo / path, text)
    commit(repo, "too much")
    report, gates = run_chore(repo, config)
    scope = gates["chore_scope"]
    assert scope.status == "fail" and reason in scope.detail, scope.detail
    assert "cần spec" in scope.detail
    assert "no_regression" not in gates  # no test run once the scope is refused
    assert report.status == "fail"


def test_large_docs_only_change_is_a_chore(chore_repo):
    repo, config = chore_repo
    write(repo / "docs/guide.md", "".join(f"Dòng {i}\n" for i in range(300)))
    # Proofline's own verify reports count as documents too.
    write(repo / ".proofline/verify/x.json", "".join(f'"{i}"\n' for i in range(200)))
    commit(repo, "docs")
    _, gates = run_chore(repo, config)
    assert gates["chore_scope"].status == "pass"
    assert "chỉ tài liệu" in gates["chore_scope"].detail


def test_limit_comes_from_config(chore_repo):
    repo, config = chore_repo
    text = config.read_text(encoding="utf-8")
    assert "    max_diff_lines: 60" in text  # the workspace template declares the default
    config.write_text(text.replace("    max_diff_lines: 60", "    max_diff_lines: 2", 1), "utf-8")
    write(repo / "calc.py", CALC + "\n\nA = 1\nB = 2\n")
    commit(repo, "three lines")
    _, gates = run_chore(repo, config)
    assert gates["chore_scope"].status == "fail" and "quá 2" in gates["chore_scope"].detail


def test_measured_base_results_are_reused_instead_of_rerunning_base(chore_repo):
    repo, config = chore_repo
    base_sha = sh(repo, "rev-parse", "main")
    # The cache claims test_add already fails on base: if base were run again it would pass,
    # so a "pre-existing" verdict proves the cache was used.
    seen = {
        "commit": base_sha,
        "failed_per_run": [1, 1],
        "always_failing": ["tests.test_add::test_add"],
        "unstable": [],
    }
    write(repo / "calc.py", CALC.replace("a + b", "a - b"))
    commit(repo, "break")
    _, gates = run_chore(repo, config, cache=lambda ref, sha: seen if sha == base_sha else None)
    regression = gates["no_regression"]
    assert regression.status == "pass", regression.detail
    assert regression.evidence["pre_existing"] == ["tests.test_add::test_add"]
    assert "base" not in regression.evidence  # not counted again in the knowledge library
    assert "không chạy lại" in regression.detail
    # A single measured run is not enough to know which tests are unstable.
    seen["failed_per_run"] = [1]
    _, gates = run_chore(repo, config, cache=lambda ref, sha: seen)
    assert gates["no_regression"].status == "fail"


def test_cli_chore_runs_base_once_then_reuses_it(chore_repo):
    repo, config = chore_repo
    write(repo / "README.md", "# calc\n")
    commit(repo, "readme")
    runner = CliRunner()
    args = ["verify", "--chore", "--path", str(repo), "--config", str(config)]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "(base)" in first.output and "không chạy lại" not in first.output
    report = json.loads((repo / ".proofline/verify/chore-chore-tidy.json").read_text("utf-8"))
    assert report["status"] == "pass" and report["task_type"] == "chore" and report["runs"] == 2
    cfg = load_config(config)
    assert knowledge.base_test_cache(cfg, "calc")("main", sh(repo, "rev-parse", "main"))

    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.output
    assert "(base)" not in second.output and "không chạy lại" in second.output
    report = json.loads((repo / ".proofline/verify/chore-chore-tidy.json").read_text("utf-8"))
    assert report["runs"] == 1
    assert "nhánh 1 lượt, base dùng lại số đo trước" in second.output


def test_cli_needs_a_spec_or_chore(chore_repo):
    _, config = chore_repo
    result = CliRunner().invoke(app, ["verify", "--config", str(config)])
    assert result.exit_code == 2
    assert "--chore" in result.output


def test_vietnamese_text_in_the_diff_is_read_as_utf8(chore_repo):
    """git output was decoded with the Windows code page and broke on Vietnamese text."""
    repo, config = chore_repo
    write(repo / "calc.py", CALC + '\n\nLABEL = "Tổng hai số: ước lượng, kiểm chứng"\n')
    commit(repo, "nhãn tiếng Việt")
    _, gates = run_chore(repo, config)
    assert gates["chore_scope"].status == "pass", gates["chore_scope"].detail
