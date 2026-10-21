"""`proofline assess`: đo mức sẵn sàng R0–R4 của một repo đích.

Tiêu chí theo docs/05-starting-points.md mục 2. Mặc định chỉ kiểm tra tĩnh; các tiêu chí
cần chạy lệnh (build container, chạy test, khởi chạy app, hỏi trạng thái CI) chỉ được
kiểm tra khi `run_commands=True`, nếu không sẽ là `unknown`. Mức đạt được là mức cao nhất
mà mọi tiêu chí của nó và của các mức thấp hơn đều `pass`.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from proofline.config import ProoflineConfig, TargetRepo
from proofline.levels import READINESS_LEVELS, ReadinessLevel, autonomy_rank
from proofline.proc import AppCheckResult, CommandResult, check_app_starts, run_command
from proofline.state import init_db

Status = Literal["pass", "fail", "unknown"]

MANIFESTS = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
)
CI_PATHS = (".github/workflows", ".gitlab-ci.yml", "azure-pipelines.yml", ".circleci")
CONFIRMED_MARKER = "<!-- proofline:confirmed -->"
FEATURE_HEADING = re.compile(r"^#{2,4}\s+(F-\d+)\b", re.MULTILINE)
JOURNEY_REF = re.compile(r"journey:\s*`?([^`\s]+)`?")
COMMAND_TIMEOUT = 900.0


@dataclass(frozen=True)
class Criterion:
    key: str
    level: ReadinessLevel
    status: Status
    detail: str
    action: str = ""


@dataclass(frozen=True)
class Assessment:
    repo: str
    level: ReadinessLevel
    criteria: tuple[Criterion, ...]
    assessed_at: str

    def next_level(self) -> ReadinessLevel | None:
        index = READINESS_LEVELS.index(self.level)
        return READINESS_LEVELS[index + 1] if index + 1 < len(READINESS_LEVELS) else None

    def missing_for_next_level(self) -> list[Criterion]:
        target = self.next_level()
        return [c for c in self.criteria if c.level == target and c.status != "pass"]


def compute_level(criteria: list[Criterion] | tuple[Criterion, ...]) -> ReadinessLevel:
    level: ReadinessLevel = "R0"
    for candidate in READINESS_LEVELS[1:]:
        items = [c for c in criteria if c.level == candidate]
        if not items or any(c.status != "pass" for c in items):
            break
        level = candidate
    return level


Runner = Callable[[str, Path, float], CommandResult]
AppChecker = Callable[[str, str, Path, float], AppCheckResult]
CiStatusFetcher = Callable[[str, str], Status]


@dataclass
class AssessContext:
    config: ProoflineConfig
    repo: TargetRepo
    repo_path: Path
    workspace: Path
    run_commands: bool = False
    runner: Runner = run_command
    app_checker: AppChecker = check_app_starts
    ci_status: CiStatusFetcher | None = None
    command_log: list[CommandResult] = field(default_factory=list)

    def run(self, command: str) -> CommandResult:
        result = self.runner(command, self.repo_path, COMMAND_TIMEOUT)
        self.command_log.append(result)
        return result


# ---- R1 ---------------------------------------------------------------------------------


def check_runnable_code(ctx: AssessContext) -> Criterion:
    found = [m for m in MANIFESTS if (ctx.repo_path / m).exists()]
    if found:
        return Criterion("has_runnable_code", "R1", "pass", f"có {', '.join(found)}")
    return Criterion(
        "has_runnable_code",
        "R1",
        "fail",
        "không thấy file khai báo dự án (package.json, pyproject.toml, ...)",
        "Repo chưa có code chạy được: đi theo luồng A (từ ý tưởng).",
    )


def check_container(ctx: AssessContext) -> Criterion:
    dockerfile = ctx.repo_path / "Dockerfile"
    devcontainer = ctx.repo_path / ".devcontainer" / "devcontainer.json"
    if not dockerfile.exists() and not devcontainer.exists():
        return Criterion(
            "container_builds",
            "R1",
            "fail",
            "không có Dockerfile hay .devcontainer/devcontainer.json ở gốc repo",
            "Thêm Dockerfile (hoặc devcontainer) cho môi trường dev/test của app.",
        )
    if not dockerfile.exists():
        return Criterion(
            "container_builds",
            "R1",
            "unknown",
            "có devcontainer; assess chưa hỗ trợ build devcontainer",
            "Thêm Dockerfile hoặc build devcontainer thủ công để xác nhận.",
        )
    if not ctx.run_commands:
        return Criterion(
            "container_builds", "R1", "unknown", "có Dockerfile; chạy với --run để build"
        )
    if shutil.which("docker") is None:
        return Criterion("container_builds", "R1", "unknown", "máy không có docker")
    tag = f"proofline-assess-{re.sub(r'[^a-z0-9-]', '-', ctx.repo.name.lower())}"
    result = ctx.run(f"docker build -q -t {tag} .")
    if result.ok:
        return Criterion(
            "container_builds", "R1", "pass", f"docker build OK ({result.seconds:.0f}s)"
        )
    return Criterion(
        "container_builds", "R1", "fail", _failure("docker build", result), "Sửa Dockerfile."
    )


def check_tests_run(ctx: AssessContext) -> Criterion:
    test_cmd = ctx.repo.commands.get("test")
    if not test_cmd:
        return Criterion(
            "test_command_runs",
            "R1",
            "fail",
            "chưa khai báo commands.test",
            "Khai báo commands.test (và commands.install) cho repo trong config.",
        )
    if not ctx.run_commands:
        return Criterion("test_command_runs", "R1", "unknown", f"'{test_cmd}'; chạy với --run")
    install_cmd = ctx.repo.commands.get("install")
    if install_cmd:
        installed = ctx.run(install_cmd)
        if not installed.ok:
            return Criterion(
                "test_command_runs",
                "R1",
                "fail",
                _failure(install_cmd, installed),
                "Sửa lệnh cài đặt.",
            )
    result = ctx.run(test_cmd)
    if result.ok:
        return Criterion(
            "test_command_runs", "R1", "pass", f"'{test_cmd}' OK ({result.seconds:.0f}s)"
        )
    return Criterion(
        "test_command_runs",
        "R1",
        "fail",
        _failure(test_cmd, result),
        "Làm cho bộ test chạy xanh trên nhánh mặc định.",
    )


def check_app_runtime(ctx: AssessContext) -> Criterion:
    runtime = ctx.repo.app_runtime
    if not runtime.enabled or not runtime.start_command or not runtime.healthcheck_url:
        return Criterion(
            "app_starts_and_healthy",
            "R1",
            "fail",
            "chưa khai báo app_runtime (start_command, healthcheck_url)",
            "Khai báo app_runtime để Proofline khởi chạy app và chạy kịch bản người dùng.",
        )
    if not ctx.run_commands:
        return Criterion(
            "app_starts_and_healthy", "R1", "unknown", "đã khai báo app_runtime; chạy với --run"
        )
    if runtime.seed_command:
        seeded = ctx.run(runtime.seed_command)
        if not seeded.ok:
            return Criterion(
                "app_starts_and_healthy", "R1", "fail", _failure(runtime.seed_command, seeded)
            )
    result = ctx.app_checker(
        runtime.start_command,
        runtime.healthcheck_url,
        ctx.repo_path,
        float(runtime.startup_timeout_seconds),
    )
    if result.healthy:
        return Criterion("app_starts_and_healthy", "R1", "pass", result.detail)
    tail = _last_lines(result.output_tail)
    return Criterion(
        "app_starts_and_healthy",
        "R1",
        "fail",
        f"{result.detail}{tail}",
        "Sửa app_runtime hoặc biến môi trường cần để app khởi chạy.",
    )


# ---- R2 ---------------------------------------------------------------------------------


def find_feature_inventory(ctx: AssessContext) -> Path | None:
    configured = ctx.config.extra_value(
        "onboarding", "existing_repo_track", "feature_inventory_path", default="docs/features.md"
    )
    # Tìm theo tên không phân biệt hoa thường nhưng trả về tên thật trên đĩa
    # (trên Windows, `exists()` của docs/features.md cũng đúng với docs/FEATURES.md).
    target = ctx.repo_path / configured
    if not target.parent.is_dir():
        return None
    for candidate in target.parent.iterdir():
        if candidate.name.lower() == target.name.lower() and candidate.is_file():
            return candidate
    return None


def check_feature_inventory(ctx: AssessContext) -> Criterion:
    inventory = find_feature_inventory(ctx)
    if inventory is None:
        return Criterion(
            "feature_inventory_confirmed",
            "R2",
            "fail",
            "chưa có danh sách tính năng",
            "Kiểm kê tính năng thành docs/features.md và xác nhận.",
        )
    rel = inventory.relative_to(ctx.repo_path).as_posix()
    if CONFIRMED_MARKER not in inventory.read_text(encoding="utf-8", errors="replace"):
        return Criterion(
            "feature_inventory_confirmed",
            "R2",
            "fail",
            f"có {rel} nhưng chưa được xác nhận",
            f"Rà {rel} rồi thêm dòng {CONFIRMED_MARKER} để xác nhận.",
        )
    return Criterion("feature_inventory_confirmed", "R2", "pass", f"{rel} đã xác nhận")


def parse_features(text: str) -> dict[str, str | None]:
    """Trả về {mã tính năng: đường dẫn journey hoặc None} theo định dạng `### F-<số>`."""
    matches = list(FEATURE_HEADING.finditer(text))
    features: dict[str, str | None] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        journey = JOURNEY_REF.search(text, match.end(), end)
        features[match.group(1)] = journey.group(1) if journey else None
    return features


def check_journey_coverage(ctx: AssessContext) -> Criterion:
    needed = float(ctx.config.readiness.threshold("R2", "min_features_with_journey_ratio", 0.8))
    inventory = find_feature_inventory(ctx)
    features = (
        parse_features(inventory.read_text(encoding="utf-8", errors="replace")) if inventory else {}
    )
    if not features:
        return Criterion(
            "features_with_journey",
            "R2",
            "unknown",
            "danh sách tính năng chưa theo định dạng Proofline (### F-<số> ... journey: <file>)",
            "Viết danh sách tính năng theo định dạng Proofline, mỗi tính năng một dòng journey.",
        )
    covered = [fid for fid, path in features.items() if path and (ctx.repo_path / path).exists()]
    ratio = len(covered) / len(features)
    detail = (
        f"{len(covered)}/{len(features)} tính năng có kịch bản người dùng"
        f" ({ratio:.0%}, cần {needed:.0%})"
    )
    if ratio >= needed:
        return Criterion("features_with_journey", "R2", "pass", detail)
    missing = [fid for fid in features if fid not in covered]
    listed = ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else "")
    return Criterion(
        "features_with_journey", "R2", "fail", detail, f"Viết kịch bản người dùng cho: {listed}"
    )


def detect_ci(repo_path: Path) -> list[str]:
    found = []
    for rel in CI_PATHS:
        path = repo_path / rel
        if path.is_file() or (path.is_dir() and any(path.iterdir())):
            found.append(rel)
    return found


def github_slug(repo_path: Path) -> str | None:
    config = repo_path / ".git" / "config"
    if not config.exists():
        return None
    match = re.search(
        r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?\s*$", config.read_text(), re.M
    )
    return f"{match.group(1)}/{match.group(2)}" if match else None


def github_ci_status(slug: str, branch: str) -> Status:
    url = f"https://api.github.com/repos/{slug}/actions/runs?branch={branch}&per_page=1"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310
            runs = json.load(response).get("workflow_runs", [])
    except (urllib.error.URLError, OSError, ValueError):
        return "unknown"
    if not runs:
        return "unknown"
    return "pass" if runs[0].get("conclusion") == "success" else "fail"


def check_ci(ctx: AssessContext) -> Criterion:
    found = detect_ci(ctx.repo_path)
    branch = ctx.repo.default_branch
    if not found:
        return Criterion(
            "ci_green_on_default_branch",
            "R2",
            "fail",
            "chưa có cấu hình CI",
            f"Thêm CI chạy lint + test trên nhánh {branch} (task type ci_setup).",
        )
    slug = github_slug(ctx.repo_path)
    if not ctx.run_commands or slug is None:
        reason = "chạy với --run để hỏi trạng thái" if slug else "không phải repo GitHub"
        return Criterion(
            "ci_green_on_default_branch", "R2", "unknown", f"có {', '.join(found)}; {reason}"
        )
    fetch = ctx.ci_status or github_ci_status
    status = fetch(slug, branch)
    detail = {
        "pass": f"lần chạy CI gần nhất trên {branch} xanh",
        "fail": f"lần chạy CI gần nhất trên {branch} không xanh",
        "unknown": f"không lấy được trạng thái CI của {branch}",
    }[status]
    return Criterion("ci_green_on_default_branch", "R2", status, detail)


# ---- R3 / R4 ----------------------------------------------------------------------------


def _golden_cases(ctx: AssessContext) -> list[dict]:
    root = ctx.workspace / ctx.config.extra_value(
        "evals", "golden_set_path", default="evals/golden-set"
    )
    cases = []
    for case_file in sorted((root / ctx.repo.name).glob("*/case.yaml")):
        data = yaml.safe_load(case_file.read_text(encoding="utf-8")) or {}
        cases.append(data if isinstance(data, dict) else {})
    return cases


def check_golden_set(ctx: AssessContext) -> Criterion:
    needed = int(ctx.config.readiness.threshold("R3", "min_golden_set_cases", 30))
    max_synthetic = float(ctx.config.readiness.threshold("R3", "max_synthetic_ratio", 0.7))
    cases = _golden_cases(ctx)
    synthetic = sum(1 for c in cases if c.get("source") == "synthetic")
    ratio = synthetic / len(cases) if cases else 0.0
    detail = f"{len(cases)} case (cần {needed}), synthetic {ratio:.0%} (tối đa {max_synthetic:.0%})"
    if len(cases) >= needed and ratio <= max_synthetic:
        return Criterion("golden_set", "R3", "pass", detail)
    return Criterion(
        "golden_set", "R3", "fail", detail, "Dựng Golden Set: proofline golden mine / golden synth."
    )


def _query(ctx: AssessContext, sql: str, params: tuple) -> list[sqlite3.Row]:
    db_path = ctx.workspace / ctx.config.factory.state_db
    if not db_path.exists():
        return []
    conn = init_db(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def check_baseline(ctx: AssessContext) -> Criterion:
    rows = _query(
        ctx,
        "SELECT count(*) AS n FROM tasks t JOIN runs r ON r.run_id = t.run_id"
        " WHERE r.kind = 'eval' AND t.repo = ? AND t.case_id IS NOT NULL",
        (ctx.repo.name,),
    )
    count = rows[0]["n"] if rows else 0
    if count:
        return Criterion("baseline_measured", "R3", "pass", f"{count} lần chạy eval đã ghi nhận")
    return Criterion(
        "baseline_measured", "R3", "fail", "chưa có lần chạy eval nào", "Chạy baseline Golden Set."
    )


def check_regression_tests(ctx: AssessContext) -> Criterion:
    return Criterion(
        "every_feature_has_regression_tests",
        "R3",
        "unknown",
        "chưa đo tự động được",
        "Xác nhận thủ công mỗi tính năng trong danh sách đều có test hồi quy.",
    )


def check_decision_replay(ctx: AssessContext) -> Criterion:
    needed = int(ctx.config.readiness.threshold("R4", "min_decision_replay_prs", 30))
    root = ctx.workspace / ctx.config.extra_value(
        "evals", "decision_replay_path", default="evals/decision-replay"
    )
    repo_dir = root / ctx.repo.name
    count = sum(1 for p in repo_dir.iterdir() if p.is_dir()) if repo_dir.is_dir() else 0
    detail = f"{count} PR (cần {needed})"
    if count >= needed:
        return Criterion("decision_replay_set", "R4", "pass", detail)
    return Criterion(
        "decision_replay_set", "R4", "fail", detail, "Thu thập PR lịch sử kèm quyết định của người."
    )


def check_human_decisions(ctx: AssessContext) -> Criterion:
    needed = int(ctx.config.readiness.threshold("R4", "min_human_decisions_per_type", 50))
    # Chỉ xét các quyết định có thể vượt L1; quyết định luôn do người thì không cần dữ liệu.
    types = [
        name
        for name, d in ctx.config.autonomy.decisions.items()
        if autonomy_rank(d.max) > autonomy_rank("L1")
    ]
    rows = _query(
        ctx,
        "SELECT d.decision_type AS t, count(*) AS n FROM decisions d"
        " LEFT JOIN tasks k ON k.task_id = d.task_id"
        " WHERE d.human_choice IS NOT NULL AND (k.repo = ? OR k.repo IS NULL)"
        " GROUP BY d.decision_type",
        (ctx.repo.name,),
    )
    counts = {row["t"]: row["n"] for row in rows}
    short = sorted((counts.get(t, 0), t) for t in types if counts.get(t, 0) < needed)
    if not short:
        return Criterion("human_decisions", "R4", "pass", f"mọi loại quyết định có ≥ {needed} mẫu")
    lowest = ", ".join(f"{t}={n}" for n, t in short[:5])
    return Criterion(
        "human_decisions",
        "R4",
        "fail",
        f"{len(short)}/{len(types)} loại quyết định chưa đủ {needed} mẫu (thấp nhất: {lowest})",
        "Tiếp tục vận hành để tích lũy quyết định của người.",
    )


CHECKS = (
    check_runnable_code,
    check_container,
    check_tests_run,
    check_app_runtime,
    check_feature_inventory,
    check_journey_coverage,
    check_ci,
    check_golden_set,
    check_baseline,
    check_regression_tests,
    check_decision_replay,
    check_human_decisions,
)


def assess(ctx: AssessContext) -> Assessment:
    if not ctx.repo_path.is_dir():
        criterion = Criterion(
            "repo_exists", "R1", "fail", f"không tìm thấy {ctx.repo_path}", "Kiểm tra local_path."
        )
        return Assessment(ctx.repo.name, "R0", (criterion,), _now())
    criteria = tuple(check(ctx) for check in CHECKS)
    return Assessment(ctx.repo.name, compute_level(criteria), criteria, _now())


def save_snapshot(db_path: Path, assessment: Assessment) -> str:
    snapshot_id = uuid.uuid4().hex
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO readiness_snapshots"
                " (snapshot_id, repo, assessed_at, level, criteria, missing_actions)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    assessment.repo,
                    assessment.assessed_at,
                    assessment.level,
                    json.dumps([asdict(c) for c in assessment.criteria], ensure_ascii=False),
                    json.dumps(
                        [c.action or c.detail for c in assessment.missing_for_next_level()],
                        ensure_ascii=False,
                    ),
                ),
            )
    finally:
        conn.close()
    return snapshot_id


def _failure(command: str, result: CommandResult) -> str:
    if result.exit_code is None:
        return f"'{command}' quá thời gian"
    return f"'{command}' exit {result.exit_code}{_last_lines(result.output_tail)}"


def _last_lines(output: str, count: int = 3) -> str:
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return (": " + " | ".join(lines[-count:]))[:400] if lines else ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
