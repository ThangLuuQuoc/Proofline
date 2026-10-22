"""`proofline verify`: chạy các gate của một task trên nhánh ứng viên, so với base.

Gate (docs/04-execution-plan.md Phase 2, docs/05-starting-points.md mục 7):
- acceptance_unchanged: journey đúng bản người đã duyệt (git blob).
- acceptance_fails_before / acceptance_passes_after: journey fail ở một khẳng định trên base,
  pass trên nhánh.
- test_integrity: không sửa/xóa test cũ, không thêm skip; new_tests_added: có test mới.
- no_regression: cả bộ test chạy N lượt mỗi bên; so **danh sách test fail**, không chỉ số lượng.
  Test fail ở mọi lượt của nhánh mà không fail lượt nào của base là regression; fail chỉ ở
  vài lượt là "không ổn định" (không kết luận); fail cả trên base là có sẵn.
- static_<lệnh>: typecheck/lint không xấu hơn base; công cụ hỏng sẵn thì "không đo được".

Base chạy trên bản clone riêng trong workspace (không thêm worktree vào .git của người dùng).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from proofline.config import ConfigError, ProoflineConfig, TargetRepo
from proofline.junit import TestRun, parse_junit
from proofline.proc import CommandResult, run_command

Status = Literal["pass", "fail", "unknown"]
COMMAND_TIMEOUT = 1800.0
VERIFY_DIR = ".verify"
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|e2e|spec)/|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.(py|go)$"
)
DEFAULT_SKIP_PATTERNS = [
    "@pytest.mark.skip",
    "@pytest.mark.xfail",
    "it.skip",
    "describe.skip",
    "test.skip",
    "xit(",
]
TOOL_BROKEN = re.compile(
    r"ERR_MODULE_NOT_FOUND|Cannot find module|command not found"
    r"|is not recognized as an internal|No such file or directory"
)
DIAGNOSTIC = re.compile(
    r"error TS\d+|\b(error|warning)\b|✖ \d+ problem|Found \d+ error", re.IGNORECASE
)


# ---- Manifest --------------------------------------------------------------------------


class JourneyApproval(BaseModel):
    blob: str
    approved_at: str
    note: str = ""


class SpecManifest(BaseModel):
    """`.proofline/specs/<tên>.yaml`: phần máy đọc được của một spec."""

    name: str
    task_type: Literal["feature", "bug"] = "feature"
    spec: str | None = None
    base: str
    journey: str
    journey_approvals: list[JourneyApproval] = Field(default_factory=list)

    @property
    def approved_blob(self) -> str | None:
        return self.journey_approvals[-1].blob if self.journey_approvals else None


def load_manifest(path: Path) -> SpecManifest:
    try:
        return SpecManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except FileNotFoundError as exc:
        raise ConfigError(f"không tìm thấy manifest: {path}") from exc
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"manifest không hợp lệ ({path}):\n{exc}") from exc


def save_manifest(path: Path, manifest: SpecManifest) -> None:
    data = manifest.model_dump(exclude_none=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


# ---- Git -------------------------------------------------------------------------------


def git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise ConfigError(f"git {' '.join(args)} lỗi: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def repo_root(path: Path) -> Path:
    return Path(git(["rev-parse", "--show-toplevel"], path))


def blob_of(repo: Path, rel: str) -> str:
    return git(["hash-object", "--", rel], repo)


# ---- Kết quả ---------------------------------------------------------------------------


@dataclass
class Gate:
    key: str
    status: Status
    detail: str
    evidence: dict = field(default_factory=dict)


@dataclass
class VerifyReport:
    spec: str
    repo: str
    base_ref: str
    base_sha: str
    head_sha: str
    runs: int
    gates: list[Gate]
    verified_at: str

    @property
    def status(self) -> Status:
        if any(g.status == "fail" for g in self.gates):
            return "fail"
        if any(g.status == "unknown" for g in self.gates):
            return "unknown"
        return "pass"

    def to_markdown(self) -> str:
        mark = {"pass": "✅", "fail": "❌", "unknown": "⚠️"}
        lines = [
            f"## Proofline verify: {self.spec}",
            "",
            f"Base `{self.base_ref}` @ `{self.base_sha[:7]}` · nhánh @ `{self.head_sha[:7]}`"
            f" · {self.runs} lượt mỗi bên · {self.verified_at}",
            "",
            "| Gate | Kết quả | Chi tiết |",
            "| :--- | :--- | :--- |",
        ]
        for g in self.gates:
            lines.append(f"| `{g.key}` | {mark[g.status]} | {g.detail.replace('|', '/')} |")
        lines += ["", f"**Tổng: {mark[self.status]} {self.status}**"]
        return "\n".join(lines) + "\n"


# ---- Ngữ cảnh --------------------------------------------------------------------------

Runner = Callable[[str, Path, float], CommandResult]


@dataclass
class VerifyContext:
    config: ProoflineConfig
    target: TargetRepo
    candidate: Path
    manifest: SpecManifest
    runs: int = 2
    install: bool = True
    runner: Runner = run_command
    log: Callable[[str], None] = lambda _msg: None

    @property
    def work_dir(self) -> Path:
        return self.config.workspace / VERIFY_DIR / self.target.name

    def skip_patterns(self) -> list[str]:
        return (
            self.config.extra_value("verification", "forbid_test_skip_patterns", default=None)
            or DEFAULT_SKIP_PATTERNS
        )


def _quote(path: Path | str) -> str:
    return f'"{path}"'


def run_tests(
    ctx: VerifyContext, cwd: Path, label: str, files: list[str] | None = None
) -> tuple[TestRun | None, CommandResult]:
    """Chạy lệnh `test_junit` của repo (placeholder {junit}, {files}) và đọc kết quả."""
    template = ctx.target.commands.get("test_junit")
    if not template:
        raise ConfigError(
            "chưa khai báo commands.test_junit"
            " (vd: pnpm exec vitest run {files} --reporter=junit --outputFile={junit})"
        )
    junit = ctx.work_dir / "reports" / f"{label}.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.unlink(missing_ok=True)
    command = template.replace("{junit}", _quote(junit)).replace(
        "{files}", " ".join(_quote(f) for f in files or [])
    )
    result = ctx.runner(command, cwd, COMMAND_TIMEOUT)
    if not junit.is_file():
        return None, result
    return parse_junit(junit), result


def prepare_base(ctx: VerifyContext, base_sha: str) -> Path:
    """Bản clone riêng của repo ở commit base, trong workspace.

    Giữ node_modules/.venv giữa các lần để không cài lại từ đầu."""
    base = ctx.work_dir / "base"
    source = repo_root(ctx.candidate)
    if not (base / ".git").is_dir():
        ctx.log(f"Tạo bản clone baseline tại {base}")
        base.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", "--quiet", "--no-checkout", str(source), str(base)], ctx.config.workspace)
    else:
        git(["fetch", "--quiet", str(source), "+refs/heads/*:refs/remotes/source/*"], base)
    git(["checkout", "--quiet", "--force", "--detach", base_sha], base)
    git(["clean", "-fdxq", "-e", "node_modules", "-e", ".venv"], base)
    return base


def _install(ctx: VerifyContext, cwd: Path, label: str) -> Gate | None:
    command = ctx.target.commands.get("install")
    if not ctx.install or not command:
        return None
    ctx.log(f"Cài đặt ({label}): {command}")
    result = ctx.runner(command, cwd, COMMAND_TIMEOUT)
    if result.ok:
        return None
    return Gate(
        f"install_{label}",
        "unknown",
        f"'{command}' lỗi trên {label}: {result.output_tail.strip()[-200:]}",
    )


# ---- Gate ------------------------------------------------------------------------------


def gate_acceptance_unchanged(ctx: VerifyContext) -> Gate:
    approved = ctx.manifest.approved_blob
    if not approved:
        return Gate(
            "acceptance_unchanged", "fail", "journey chưa được duyệt (chạy proofline spec approve)"
        )
    current = blob_of(ctx.candidate, ctx.manifest.journey)
    if current == approved:
        return Gate(
            "acceptance_unchanged",
            "pass",
            f"journey đúng bản đã duyệt ({approved[:7]})",
            {"blob": current},
        )
    return Gate(
        "acceptance_unchanged",
        "fail",
        f"journey đã đổi so với bản duyệt ({current[:7]} ≠ {approved[:7]});"
        " đề xuất thay đổi cho người duyệt",
        {"blob": current, "approved": approved},
    )


def gate_test_changes(ctx: VerifyContext, base_sha: str) -> list[Gate]:
    rows = git(["diff", "--name-status", "--no-renames", base_sha], ctx.candidate).splitlines()
    changed = [
        (r.split("\t")[0][0], r.split("\t")[-1].replace("\\", "/")) for r in rows if "\t" in r
    ]
    tests = [(s, p) for s, p in changed if TEST_PATH.search(p)]
    touched = sorted(p for s, p in tests if s in ("M", "D") and p != ctx.manifest.journey)
    added = sorted(p for s, p in tests if s == "A")
    skipped: list[str] = []
    if tests:
        diff = git(["diff", base_sha, "--", *[p for _, p in tests]], ctx.candidate)
        current = ""
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
            elif line.startswith("+") and not line.startswith("+++"):
                hits = [pat for pat in ctx.skip_patterns() if pat in line]
                if hits:
                    skipped.append(f"{current}: {hits[0]}")
    gates = []
    if touched or skipped:
        detail = "; ".join(
            filter(
                None,
                [
                    f"sửa/xóa test cũ: {', '.join(touched)}" if touched else "",
                    f"thêm skip: {', '.join(skipped)}" if skipped else "",
                ],
            )
        )
        gates.append(Gate("test_integrity", "fail", detail, {"touched": touched, "skips": skipped}))
    else:
        gates.append(Gate("test_integrity", "pass", "không sửa/xóa test cũ, không thêm skip"))
    others = [p for p in added if p != ctx.manifest.journey]
    if others:
        gates.append(
            Gate(
                "new_tests_added",
                "pass",
                f"{len(others)} file test mới: {', '.join(others)}",
                {"added": others},
            )
        )
    else:
        gates.append(Gate("new_tests_added", "fail", "không có file test mới ngoài journey"))
    return gates


def gate_fails_before(ctx: VerifyContext, base: Path) -> Gate:
    journey = ctx.manifest.journey
    target = base / journey
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ctx.candidate / journey, target)
    try:
        run, result = run_tests(ctx, base, "journey-base", [journey])
    finally:
        if existed:
            git(["checkout", "--quiet", "--", journey], base)
        else:
            target.unlink(missing_ok=True)
    if run is None:
        return Gate(
            "acceptance_fails_before",
            "unknown",
            f"không có kết quả JUnit trên base: {result.output_tail.strip()[-200:]}",
        )
    failed = [o for o in run.outcomes if o.failed]
    if not failed:
        return Gate(
            "acceptance_fails_before",
            "fail",
            "journey PASS trên base: không chứng minh được tính năng/bug",
        )
    wrong = [o for o in failed if not o.is_assertion]
    if wrong:
        return Gate(
            "acceptance_fails_before",
            "fail",
            f"journey fail sai lý do trên base"
            f" ({wrong[0].error_type or wrong[0].status}: {wrong[0].message[:120]})",
            {"failed": [asdict(o) for o in failed]},
        )
    return Gate(
        "acceptance_fails_before",
        "pass",
        f"{len(failed)}/{len(run.outcomes)} test của journey fail ở khẳng định trên base",
        {"failed": [o.id for o in failed]},
    )


def gate_passes_after(ctx: VerifyContext) -> Gate:
    run, result = run_tests(ctx, ctx.candidate, "journey-head", [ctx.manifest.journey])
    if run is None:
        return Gate(
            "acceptance_passes_after",
            "unknown",
            f"không có kết quả JUnit: {result.output_tail.strip()[-200:]}",
        )
    ran = [o for o in run.outcomes if o.status != "skipped"]
    failed = [o for o in run.outcomes if o.failed]
    if not ran:
        return Gate("acceptance_passes_after", "fail", "journey không chạy test nào")
    if failed:
        return Gate(
            "acceptance_passes_after",
            "fail",
            f"{len(failed)} test của journey fail: {failed[0].id}",
            {"failed": [o.id for o in failed]},
        )
    return Gate("acceptance_passes_after", "pass", f"{len(ran)}/{len(ran)} test của journey pass")


def classify_regressions(
    base_runs: list[TestRun], head_runs: list[TestRun]
) -> dict[str, list[str]]:
    base_failed = frozenset().union(*(r.failed_ids for r in base_runs))
    base_ids = frozenset().union(*(r.ids for r in base_runs))
    head_ids = frozenset().union(*(r.ids for r in head_runs))
    new_tests = head_ids - base_ids
    always = (
        frozenset.intersection(*(r.failed_ids for r in head_runs)) if head_runs else frozenset()
    )
    some = frozenset().union(*(r.failed_ids for r in head_runs))
    consistent = always - base_failed - new_tests
    unstable = some - always - base_failed - new_tests
    return {
        "consistent": sorted(consistent),
        "unstable": sorted(unstable),
        "pre_existing": sorted(some & base_failed),
        "new_tests_failing": sorted(some & new_tests),
    }


def gate_no_regression(ctx: VerifyContext, base: Path) -> Gate:
    base_runs: list[TestRun] = []
    head_runs: list[TestRun] = []
    for i in range(1, ctx.runs + 1):
        for side, cwd, bucket in (("base", base, base_runs), ("head", ctx.candidate, head_runs)):
            ctx.log(f"Cả bộ test, lượt {i}/{ctx.runs} ({side})")
            run, result = run_tests(ctx, cwd, f"suite-{side}-{i}")
            if run is None:
                return Gate(
                    "no_regression",
                    "unknown",
                    f"không có kết quả JUnit ({side}, lượt {i}):"
                    f" {result.output_tail.strip()[-200:]}",
                )
            bucket.append(run)
    groups = classify_regressions(base_runs, head_runs)
    counts = (
        f"base: {', '.join(str(len(r.failed_ids)) for r in base_runs)} fail · "
        f"nhánh: {', '.join(str(len(r.failed_ids)) for r in head_runs)} fail (theo lượt)"
    )
    if groups["consistent"]:
        return Gate(
            "no_regression",
            "fail",
            f"{len(groups['consistent'])} test fail mọi lượt trên nhánh, không fail trên base:"
            f" {groups['consistent'][0]} · {counts}",
            groups,
        )
    if groups["unstable"] or groups["new_tests_failing"]:
        unstable = groups["unstable"] + groups["new_tests_failing"]
        return Gate(
            "no_regression",
            "unknown",
            f"{len(unstable)} test fail không ổn định chỉ trên nhánh (vd {unstable[0]}) · {counts}",
            groups,
        )
    extra = (
        f"; {len(groups['pre_existing'])} test fail có sẵn trên base"
        if groups["pre_existing"]
        else ""
    )
    return Gate(
        "no_regression", "pass", f"không test nào xấu đi so với base{extra} · {counts}", groups
    )


def diagnostics(result: CommandResult) -> int:
    return sum(1 for line in result.output_tail.splitlines() if DIAGNOSTIC.search(line))


def gate_static(ctx: VerifyContext, base: Path, name: str) -> Gate | None:
    command = ctx.target.commands.get(name)
    if not command:
        return None
    ctx.log(f"{name}: {command}")
    head = ctx.runner(command, ctx.candidate, COMMAND_TIMEOUT)
    if head.ok:
        return Gate(f"static_{name}", "pass", f"'{command}' sạch trên nhánh")
    before = ctx.runner(command, base, COMMAND_TIMEOUT)
    if TOOL_BROKEN.search(head.output_tail) or TOOL_BROKEN.search(before.output_tail):
        both = not before.ok
        lines = head.output_tail.strip().splitlines()
        last = lines[-1][:160] if lines else ""
        return Gate(
            f"static_{name}",
            "unknown",
            f"'{command}' không chạy được{' (hỏng sẵn trên base)' if both else ''}: {last}",
        )
    if before.ok:
        return Gate(
            f"static_{name}",
            "fail",
            f"'{command}' sạch trên base nhưng lỗi trên nhánh ({diagnostics(head)} dòng)",
        )
    h, b = diagnostics(head), diagnostics(before)
    status: Status = "pass" if h <= b else "fail"
    return Gate(f"static_{name}", status, f"{h} dòng lỗi/cảnh báo trên nhánh, {b} trên base")


# ---- Chạy ------------------------------------------------------------------------------


def verify(ctx: VerifyContext) -> VerifyReport:
    base_sha = git(["rev-parse", f"{ctx.manifest.base}^{{commit}}"], ctx.candidate)
    head_sha = git(["rev-parse", "HEAD"], ctx.candidate)
    if not (ctx.candidate / ctx.manifest.journey).is_file():
        raise ConfigError(f"không thấy journey {ctx.manifest.journey} trên nhánh")
    gates: list[Gate] = [gate_acceptance_unchanged(ctx), *gate_test_changes(ctx, base_sha)]

    base = prepare_base(ctx, base_sha)
    for label, cwd in (("base", base), ("head", ctx.candidate)):
        failed_install = _install(ctx, cwd, label)
        if failed_install:
            gates.append(failed_install)
    ctx.log("Journey trên base (phải fail ở khẳng định)")
    gates.append(gate_fails_before(ctx, base))
    ctx.log("Journey trên nhánh (phải pass)")
    gates.append(gate_passes_after(ctx))
    gates.append(gate_no_regression(ctx, base))
    for name in ("typecheck", "lint"):
        gate = gate_static(ctx, base, name)
        if gate:
            gates.append(gate)

    return VerifyReport(
        spec=ctx.manifest.name,
        repo=ctx.target.name,
        base_ref=ctx.manifest.base,
        base_sha=base_sha,
        head_sha=head_sha,
        runs=ctx.runs,
        gates=gates,
        verified_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def write_report(report: VerifyReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"{report.spec}.md"
    js = out_dir / f"{report.spec}.json"
    md.write_text(report.to_markdown(), encoding="utf-8")
    payload = {**asdict(report), "status": report.status}
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js
