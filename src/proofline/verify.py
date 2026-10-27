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

Task `chore` (chế độ nhẹ, docs/09-lessons-macroeconomics.md mục 5): không spec, không journey.
chore_scope kiểm bằng diff rằng thay đổi đủ nhỏ, không thêm lối vào mới cho người dùng và không
chạm vùng nhạy cảm; rồi test_integrity, no_regression, static_*. Kết quả test của base đã đo trên
cùng commit (kho tri thức, docs/08) được dùng lại thay vì chạy lại base.

Base chạy trên bản clone riêng trong workspace (không thêm worktree vào .git của người dùng).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from proofline.config import ConfigError, ProoflineConfig, TargetRepo
from proofline.junit import TestRun, parse_junit
from proofline.proc import CommandResult, run_command
from proofline.state import get_decision, latest_human_decision, record_decision

Status = Literal["pass", "fail", "unknown"]
COMMAND_TIMEOUT = 1800.0
VERIFY_DIR = ".verify"
DEFAULT_BASE_RETRIES = 2
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
DEFAULT_CHORE_MAX_DIFF_LINES = 60
# Tài liệu và báo cáo verify của chính Proofline: không tính vào giới hạn dòng code của chore.
DOC_PATH = re.compile(r"\.(md|rst|txt|adoc)$|(^|/)docs?/|(^|/)\.proofline/verify/", re.IGNORECASE)
# Vùng nhạy cảm khi config không khai báo review.risk_score.sensitive_paths.
DEFAULT_SENSITIVE_PATHS = [
    "**/auth/**",
    "**/payment*/**",
    "**/migrations/**",
    "**/.github/workflows/**",
]
# Manifest spec giữ bản duyệt của người: chore không bao giờ được chạm.
ALWAYS_SENSITIVE_PATHS = [".proofline/specs/**"]
# Lối vào mới cho người dùng: route HTTP, lệnh CLI (theo dòng thêm vào), trang/route (theo file).
ENTRY_POINT_LINE = [
    r"@\w+\.(get|post|put|patch|delete|route|api_route|websocket|command)\(",
    r"@click\.(command|group)\b",
    r"\badd_typer\(",
    r"\b(app|router)\.(get|post|put|patch|delete|all)\(\s*['\"`]",
]
ENTRY_POINT_FILE = re.compile(
    r"(^|/)app/(.+/)?(page|route)\.[cm]?[jt]sx?$|(^|/)pages/(.+/)?[^/]+\.[cm]?[jt]sx?$"
)


# ---- Manifest --------------------------------------------------------------------------


class JourneyApproval(BaseModel):
    blob: str
    approved_at: str
    note: str = ""
    # Quyết định của người trong state/proofline.db; verify đối chiếu, không tin riêng manifest.
    decision_id: str = ""
    via: str = ""  # terminal | acp


class SpecManifest(BaseModel):
    """`.proofline/specs/<tên>.yaml`: phần máy đọc được của một spec."""

    name: str
    task_type: Literal["feature", "bug", "chore"] = "feature"
    spec: str | None = None
    base: str
    journey: str = ""  # chore: không có journey
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
    installed: bool = False  # commands.install đã chạy trên cả hai bên
    task_type: str = "feature"

    @property
    def status(self) -> Status:
        if any(g.status == "fail" for g in self.gates):
            return "fail"
        if any(g.status == "unknown" for g in self.gates):
            return "unknown"
        return "pass"

    def _runs_text(self) -> str:
        if any("base_cached" in g.evidence for g in self.gates):
            return f"nhánh {self.runs} lượt, base dùng lại số đo trước"
        return f"{self.runs} lượt mỗi bên"

    def to_markdown(self) -> str:
        mark = {"pass": "✅", "fail": "❌", "unknown": "⚠️"}
        lines = [
            f"## Proofline verify: {self.spec}"
            + (f" ({self.task_type})" if self.task_type == "chore" else ""),
            "",
            f"Base `{self.base_ref}` @ `{self.base_sha[:7]}` · nhánh @ `{self.head_sha[:7]}`"
            f" · {self._runs_text()} · {self.verified_at}",
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
# (ref, commit) -> kết quả test của base đã đo trên đúng commit đó (kho tri thức), hoặc None.
BaseCache = Callable[[str, str], dict[str, Any] | None]


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
    # Lần chạy lại journey trên base khi nó fail không ở khẳng định (None: theo config).
    journey_retries: int | None = None
    base_cache: BaseCache = lambda _ref, _sha: None

    @property
    def base_journey_retries(self) -> int:
        if self.journey_retries is not None:
            return max(0, self.journey_retries)
        value = self.config.extra_value(
            "verification", "user_journey", "base_retries", default=DEFAULT_BASE_RETRIES
        )
        return max(0, int(value))

    @property
    def db_path(self) -> Path:
        return self.config.workspace / self.config.factory.state_db

    @property
    def work_dir(self) -> Path:
        return self.config.workspace / VERIFY_DIR / self.target.name

    def skip_patterns(self) -> list[str]:
        return (
            self.config.extra_value("verification", "forbid_test_skip_patterns", default=None)
            or DEFAULT_SKIP_PATTERNS
        )

    def chore_value(self, key: str, default: Any) -> Any:
        value = self.config.extra_value("verification", "chore", key, default=None)
        return default if value is None else value

    def sensitive_paths(self) -> list[str]:
        configured = self.config.extra_value(
            "review", "risk_score", "sensitive_paths", default=None
        )
        return [*ALWAYS_SENSITIVE_PATHS, *(configured or DEFAULT_SENSITIVE_PATHS)]


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
        {"command": command, "output": result.output_tail.strip()[-200:]},
    )


# ---- Lệnh kiểm tra đã được người duyệt -------------------------------------------------

# Lệnh verify chạy để ra kết luận: đổi chúng (vd bỏ bớt test) là đổi thước đo.
CHECK_COMMANDS = ("install", "test", "test_junit", "typecheck", "lint")


def command_snapshot(config: ProoflineConfig, target: TargetRepo) -> dict[str, Any]:
    """Mọi thứ quyết định verify kết luận gì: lệnh của repo và các ngưỡng/mẫu của verify."""
    return {
        "commands": {k: target.commands[k] for k in CHECK_COMMANDS if k in target.commands},
        "skip_patterns": config.extra_value(
            "verification", "forbid_test_skip_patterns", default=None
        ),
        "base_retries": config.extra_value(
            "verification", "user_journey", "base_retries", default=None
        ),
        "chore": config.extra_value("verification", "chore", default=None),
        "sensitive_paths": config.extra_value(
            "review", "risk_score", "sensitive_paths", default=None
        ),
    }


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def commands_choice_prefix(repo: str) -> str:
    return f"approved commands {repo} "


def approved_snapshot_path(config: ProoflineConfig, repo: str) -> Path:
    return config.workspace / "state" / "approved-commands" / f"{repo}.json"


def approve_commands(config: ProoflineConfig, target: TargetRepo, via: str, note: str = "") -> str:
    """Ghi quyết định của người duyệt bộ lệnh kiểm tra hiện tại. Chỉ gọi từ kênh của người
    (terminal tương tác, ACP); bản chụp lệnh lưu cạnh để verify chỉ ra lệnh nào đã đổi."""
    snapshot = command_snapshot(config, target)
    fingerprint = snapshot_fingerprint(snapshot)
    decision_id = record_decision(
        config.workspace / config.factory.state_db,
        "commands_approval",
        decided_by="human",
        human_choice=commands_choice_prefix(target.name) + fingerprint,
        human_reason=" ".join(filter(None, [note, f"[{via}]"])),
    )
    path = approved_snapshot_path(config, target.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision_id


def gate_commands_approved(ctx: VerifyContext) -> Gate:
    snapshot = command_snapshot(ctx.config, ctx.target)
    current = snapshot_fingerprint(snapshot)
    row = latest_human_decision(
        ctx.db_path, "commands_approval", commands_choice_prefix(ctx.target.name)
    )
    if row is None:
        return Gate(
            "commands_approved",
            "unknown",
            f"lệnh kiểm tra của repo chưa được người duyệt (bản hiện tại {current[:7]}): chạy"
            f" `proofline target approve-commands {ctx.target.name}` trong terminal, hoặc"
            " /commands qua ACP",
            {"fingerprint": current},
        )
    approved = row["human_choice"].rsplit(" ", 1)[-1]
    evidence = {"fingerprint": current, "approved": approved, "decision_id": row["decision_id"]}
    if approved == current:
        return Gate(
            "commands_approved",
            "pass",
            f"lệnh kiểm tra đúng bản người đã duyệt ({current[:7]}, {row['created_at'][:10]})",
            evidence,
        )
    changed = "không đọc được bản đã duyệt để so chi tiết"
    path = approved_snapshot_path(ctx.config, ctx.target.name)
    try:
        before = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = None
    if isinstance(before, dict) and snapshot_fingerprint(before) == approved:
        keys = [
            f"commands.{k}"
            for k in sorted({*before.get("commands", {}), *snapshot["commands"]})
            if before.get("commands", {}).get(k) != snapshot["commands"].get(k)
        ] + [k for k in snapshot if k != "commands" and before.get(k) != snapshot[k]]
        changed = f"đã đổi: {', '.join(keys)}"
        evidence["changed"] = keys
    return Gate(
        "commands_approved",
        "fail",
        f"lệnh kiểm tra khác bản người đã duyệt ({current[:7]} ≠ {approved[:7]}; {changed});"
        " không chạy lệnh chưa được duyệt, cần người duyệt lại",
        evidence,
    )


# ---- Gate ------------------------------------------------------------------------------


def approval_choice(name: str, blob: str) -> str:
    """`human_choice` của quyết định duyệt journey (CLI và ACP ghi giống nhau)."""
    return f"approved {name} {blob[:7]}"


def gate_acceptance_unchanged(ctx: VerifyContext) -> Gate:
    approved = ctx.manifest.approved_blob
    if not approved:
        return Gate(
            "acceptance_unchanged", "fail", "journey chưa được duyệt (chạy proofline spec approve)"
        )
    current = blob_of(ctx.candidate, ctx.manifest.journey)
    if current == approved:
        # The manifest lives on the branch the agent edits: the approval only counts if the
        # human decision it points to is in the decisions table.
        entry = ctx.manifest.journey_approvals[-1]
        evidence = {"blob": current, "decision_id": entry.decision_id, "via": entry.via}
        if not entry.decision_id:
            return Gate(
                "acceptance_unchanged",
                "unknown",
                f"journey đúng bản {approved[:7]} trong manifest, nhưng bản duyệt không gắn quyết"
                " định của người (ghi trước khi có kiểm tra này); người duyệt lại bằng"
                " `proofline spec approve` trong terminal hoặc /approve qua ACP",
                evidence,
            )
        row = get_decision(ctx.db_path, entry.decision_id)
        if not (
            row
            and row["decided_by"] == "human"
            and row["decision_type"] == "acceptance_approval"
            and row["human_choice"] == approval_choice(ctx.manifest.name, current)
        ):
            return Gate(
                "acceptance_unchanged",
                "fail",
                f"bản duyệt {approved[:7]} trong manifest không khớp quyết định nào của người"
                f" trong sổ quyết định ({entry.decision_id[:8]})",
                evidence,
            )
        return Gate(
            "acceptance_unchanged",
            "pass",
            f"journey đúng bản người đã duyệt ({approved[:7]}, qua {entry.via or '?'},"
            f" {row['created_at'][:10]})",
            evidence,
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


def _matches(path: str, pattern: str) -> bool:
    """Glob kiểu `**/auth/**`: `**/` ở đầu cũng khớp ở gốc repo."""
    return fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatchcase(path, pattern[3:])
    )


def gate_chore_scope(ctx: VerifyContext, base_sha: str) -> Gate:
    """Thay đổi có đúng là việc nhẹ không, đo bằng diff (agent không tự khai được)."""
    numstat = git(["diff", "--numstat", "--no-renames", base_sha], ctx.candidate).splitlines()
    status = git(["diff", "--name-status", "--no-renames", base_sha], ctx.candidate).splitlines()
    lines: dict[str, int] = {}
    for row in numstat:
        added, deleted, path = row.split("\t", 2)
        lines[path.replace("\\", "/")] = sum(int(n) for n in (added, deleted) if n.isdigit())
    if not lines:
        return Gate("chore_scope", "fail", "không có thay đổi nào so với base")
    new_files = {
        r.split("\t")[-1].replace("\\", "/") for r in status if "\t" in r and r.startswith("A")
    }
    code = {p: n for p, n in lines.items() if not DOC_PATH.search(p)}
    code_lines = sum(code.values())
    limit = int(ctx.chore_value("max_diff_lines", DEFAULT_CHORE_MAX_DIFF_LINES))

    reasons: list[str] = []
    sensitive = sorted(p for p in lines if any(_matches(p, g) for g in ctx.sensitive_paths()))
    if sensitive:
        reasons.append(f"chạm vùng nhạy cảm: {', '.join(sensitive)}")
    if code_lines > limit:
        reasons.append(f"{code_lines} dòng code thay đổi, quá {limit}")
    entry = sorted(p for p in new_files if ENTRY_POINT_FILE.search(p) and not TEST_PATH.search(p))
    patterns = [*ENTRY_POINT_LINE, *ctx.chore_value("extra_entry_point_patterns", [])]
    line_re = re.compile("|".join(f"(?:{p})" for p in patterns))
    product = [p for p in code if not TEST_PATH.search(p)]
    if product:
        current = ""
        for line in git(["diff", base_sha, "--", *product], ctx.candidate).splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
            elif line.startswith("+") and not line.startswith("+++") and line_re.search(line):
                entry.append(f"{current}: {line[1:].strip()[:60]}")
    if entry:
        reasons.append(f"thêm lối vào mới cho người dùng: {'; '.join(entry[:3])}")

    evidence = {
        "files": len(lines),
        "code_lines": code_lines,
        "limit": limit,
        "sensitive": sensitive,
        "entry_points": entry,
    }
    if reasons:
        return Gate(
            "chore_scope",
            "fail",
            f"không phải việc nhẹ: {' · '.join(reasons)}; chuyển sang task bug hoặc feature"
            " (cần spec)",
            evidence,
        )
    docs_only = " (chỉ tài liệu)" if not code else ""
    return Gate(
        "chore_scope",
        "pass",
        f"{len(lines)} file, {code_lines}/{limit} dòng code{docs_only}; không thêm lối vào mới,"
        " không chạm vùng nhạy cảm",
        evidence,
    )


def _journey_on_base(ctx: VerifyContext, base: Path, label: str):
    journey = ctx.manifest.journey
    target = base / journey
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ctx.candidate / journey, target)
    try:
        return run_tests(ctx, base, label, [journey])
    finally:
        if existed:
            git(["checkout", "--quiet", "--", journey], base)
        else:
            target.unlink(missing_ok=True)


def _attempt(run: TestRun | None, result: CommandResult) -> dict:
    """One run of the journey on base, classified. Only `wrong_reason` and `no_junit` are
    inconclusive (a timeout or a broken environment can be transient)."""
    if run is None:
        return {"outcome": "no_junit", "detail": result.output_tail.strip()[-200:]}
    failed = [o for o in run.outcomes if o.failed]
    if not failed:
        return {"outcome": "pass", "detail": ""}
    wrong = [o for o in failed if not o.is_assertion]
    if wrong:
        cause = f"{wrong[0].error_type or wrong[0].status}: {wrong[0].message[:120]}"
        return {"outcome": "wrong_reason", "detail": cause, "failed": [asdict(o) for o in failed]}
    return {
        "outcome": "assertion",
        "detail": f"{len(failed)}/{len(run.outcomes)}",
        "failed": [o.id for o in failed],
    }


def gate_fails_before(ctx: VerifyContext, base: Path) -> Gate:
    attempts: list[dict] = []
    for i in range(1 + ctx.base_journey_retries):
        if i:
            ctx.log(
                f"Journey trên base: chạy lại lần {i}/{ctx.base_journey_retries}"
                f" (lần trước: {attempts[-1]['detail'][:80]})"
            )
        attempts.append(_attempt(*_journey_on_base(ctx, base, f"journey-base-{i + 1}")))
        if attempts[-1]["outcome"] in ("pass", "assertion"):
            break
    last = attempts[-1]
    retried = len(attempts) - 1
    evidence: dict = {"attempts": [{k: a[k] for k in ("outcome", "detail")} for a in attempts]}
    note = ""
    if retried:
        flaky = [a for a in attempts if a["outcome"] in ("wrong_reason", "no_junit")]
        note = (
            f" (sau {retried} lần chạy lại; {len(flaky)}/{len(attempts)} lần fail không ở"
            f" khẳng định, vd {flaky[0]['detail'][:80]})"
        )
    if last["outcome"] == "no_junit":
        return Gate(
            "acceptance_fails_before",
            "unknown",
            f"không có kết quả JUnit trên base: {last['detail']}{note}",
            evidence,
        )
    if last["outcome"] == "pass":
        return Gate(
            "acceptance_fails_before",
            "fail",
            f"journey PASS trên base: không chứng minh được tính năng/bug{note}",
            evidence,
        )
    if last["outcome"] == "wrong_reason":
        return Gate(
            "acceptance_fails_before",
            "fail",
            f"journey fail sai lý do trên base ({last['detail']}){note}",
            {**evidence, "failed": last["failed"]},
        )
    return Gate(
        "acceptance_fails_before",
        "pass",
        f"{last['detail']} test của journey fail ở khẳng định trên base{note}",
        {**evidence, "failed": last["failed"]},
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
    return classify_failures(
        frozenset().union(*(r.failed_ids for r in base_runs)),
        frozenset().union(*(r.ids for r in base_runs)),
        head_runs,
    )


def classify_failures(
    base_failed: frozenset[str], base_ids: frozenset[str] | None, head_runs: list[TestRun]
) -> dict[str, list[str]]:
    """`base_ids=None`: không biết test nào là mới (base lấy từ kho tri thức), nên test mới fail
    được tính như mọi test khác."""
    head_ids = frozenset().union(*(r.ids for r in head_runs))
    new_tests = head_ids - base_ids if base_ids is not None else frozenset()
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


BaseCheckout = Path | Callable[[], Path]


def _checkout(base: BaseCheckout) -> Path:
    return base() if callable(base) else base


def gate_no_regression(
    ctx: VerifyContext, base: BaseCheckout, cached: dict[str, Any] | None = None
) -> Gate:
    """`cached`: kết quả test của base đã đo trên cùng commit; có thì chỉ chạy nhánh."""
    base_runs: list[TestRun] = []
    head_runs: list[TestRun] = []
    sides: list[tuple[str, Path, list[TestRun]]] = [("head", ctx.candidate, head_runs)]
    if cached is None:
        sides.insert(0, ("base", _checkout(base), base_runs))
    for i in range(1, ctx.runs + 1):
        for side, cwd, bucket in sides:
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
    groups: dict
    if cached is None:
        groups = classify_regressions(base_runs, head_runs)
        # What the base itself does, for the knowledge library (docs/08): always vs sometimes.
        base_always = frozenset.intersection(*(r.failed_ids for r in base_runs))
        base_some = frozenset().union(*(r.failed_ids for r in base_runs))
        groups["base"] = {
            "failed_per_run": [len(r.failed_ids) for r in base_runs],
            "always_failing": sorted(base_always),
            "unstable": sorted(base_some - base_always),
        }
        base_counts = ", ".join(str(len(r.failed_ids)) for r in base_runs)
    else:
        # No "base" key: the knowledge library must not count the same measurement twice.
        known = frozenset(cached["always_failing"]) | frozenset(cached["unstable"])
        groups = classify_failures(known, None, head_runs)
        groups["base_cached"] = {
            "commit": cached.get("commit", ""),
            "failed_per_run": cached["failed_per_run"],
        }
        base_counts = (
            f"{', '.join(str(n) for n in cached['failed_per_run'])} (đo trước,"
            f" {len(cached['failed_per_run'])} lượt, không chạy lại)"
        )
    counts = (
        f"base: {base_counts} fail · "
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


def _broken_cause(output: str, checkout: Path) -> str:
    """The line that says why the tool cannot run (not the package manager's exit note), with
    the checkout's own path replaced so the text is the same wherever it ran."""
    lines = output.strip().splitlines()
    cause = next((line for line in lines if TOOL_BROKEN.search(line)), lines[-1] if lines else "")
    for form in {str(checkout), str(checkout.resolve()), checkout.as_posix()}:
        cause = cause.replace(form, "<repo>")
    return cause.strip()[:160]


def gate_static(ctx: VerifyContext, checkout: BaseCheckout, name: str) -> Gate | None:
    command = ctx.target.commands.get(name)
    if not command:
        return None
    ctx.log(f"{name}: {command}")
    head = ctx.runner(command, ctx.candidate, COMMAND_TIMEOUT)
    if head.ok:
        return Gate(f"static_{name}", "pass", f"'{command}' sạch trên nhánh")
    base = _checkout(checkout)  # base chỉ cần khi nhánh không sạch
    before = ctx.runner(command, base, COMMAND_TIMEOUT)
    if TOOL_BROKEN.search(head.output_tail) or TOOL_BROKEN.search(before.output_tail):
        both = not before.ok
        base_broken = both and bool(TOOL_BROKEN.search(before.output_tail))
        last = _broken_cause(head.output_tail, ctx.candidate)
        return Gate(
            f"static_{name}",
            "unknown",
            f"'{command}' không chạy được{' (hỏng sẵn trên base)' if both else ''}: {last}",
            {
                "command": command,
                "base": {
                    "broken": base_broken,
                    "cause": _broken_cause(before.output_tail, base) if base_broken else "",
                },
            },
        )
    if before.ok:
        return Gate(
            f"static_{name}",
            "fail",
            f"'{command}' sạch trên base nhưng lỗi trên nhánh ({diagnostics(head)} dòng)",
            {"command": command, "base": {"broken": False, "diagnostics": 0}},
        )
    h, b = diagnostics(head), diagnostics(before)
    status: Status = "pass" if h <= b else "fail"
    return Gate(
        f"static_{name}",
        status,
        f"{h} dòng lỗi/cảnh báo trên nhánh, {b} trên base",
        {"command": command, "base": {"broken": False, "diagnostics": b}},
    )


# ---- Chạy ------------------------------------------------------------------------------


def verify(ctx: VerifyContext) -> VerifyReport:
    if ctx.manifest.task_type == "chore":
        return verify_chore(ctx)
    base_sha = git(["rev-parse", f"{ctx.manifest.base}^{{commit}}"], ctx.candidate)
    head_sha = git(["rev-parse", "HEAD"], ctx.candidate)
    if not (ctx.candidate / ctx.manifest.journey).is_file():
        raise ConfigError(f"không thấy journey {ctx.manifest.journey} trên nhánh")
    commands = gate_commands_approved(ctx)
    gates: list[Gate] = [
        commands,
        gate_acceptance_unchanged(ctx),
        *gate_test_changes(ctx, base_sha),
    ]
    if commands.status == "fail":
        return _report(ctx, base_sha, head_sha, gates, installed=False)

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
    return _report(
        ctx,
        base_sha,
        head_sha,
        gates,
        installed=ctx.install and bool(ctx.target.commands.get("install")),
    )


def _report(
    ctx: VerifyContext, base_sha: str, head_sha: str, gates: list[Gate], *, installed: bool
) -> VerifyReport:
    return VerifyReport(
        spec=ctx.manifest.name,
        repo=ctx.target.name,
        base_ref=ctx.manifest.base,
        base_sha=base_sha,
        head_sha=head_sha,
        runs=ctx.runs,
        gates=gates,
        verified_at=datetime.now(UTC).isoformat(timespec="seconds"),
        installed=installed,
        task_type=ctx.manifest.task_type,
    )


def chore_manifest(candidate: Path, base: str) -> SpecManifest:
    """Manifest tạm cho một chore chạy không cần file spec: tên theo nhánh hiện tại."""
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], candidate)
    if branch == "HEAD":
        branch = git(["rev-parse", "--short", "HEAD"], candidate)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    return SpecManifest(name=f"chore-{name}", task_type="chore", base=base)


def verify_chore(ctx: VerifyContext) -> VerifyReport:
    """Chế độ nhẹ: không journey; base chỉ được chuẩn bị (clone, cài) khi thật sự cần."""
    base_sha = git(["rev-parse", f"{ctx.manifest.base}^{{commit}}"], ctx.candidate)
    head_sha = git(["rev-parse", "HEAD"], ctx.candidate)
    commands = gate_commands_approved(ctx)
    scope = gate_chore_scope(ctx, base_sha)
    gates: list[Gate] = [
        commands,
        scope,
        *(g for g in gate_test_changes(ctx, base_sha) if g.key == "test_integrity"),
    ]
    installed = False
    if "fail" not in (commands.status, scope.status):
        prepared: list[Path] = []

        def base() -> Path:
            if not prepared:
                prepared.append(prepare_base(ctx, base_sha))
                failed = _install(ctx, prepared[0], "base")
                if failed:
                    gates.append(failed)
            return prepared[0]

        failed_install = _install(ctx, ctx.candidate, "head")
        if failed_install:
            gates.append(failed_install)
        cached = ctx.base_cache(ctx.manifest.base, base_sha)
        if cached and len(cached.get("failed_per_run", [])) < 2:
            cached = None  # một lượt không phân biệt được test chập chờn
        if cached:
            ctx.log(f"Dùng kết quả test đã đo của base @ {base_sha[:7]} (không chạy lại base)")
        gates.append(gate_no_regression(ctx, base, cached))
        for name in ("typecheck", "lint"):
            gate = gate_static(ctx, base, name)
            if gate:
                gates.append(gate)
        installed = bool(prepared) and ctx.install and bool(ctx.target.commands.get("install"))
    return _report(ctx, base_sha, head_sha, gates, installed=installed)


def write_report(report: VerifyReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"{report.spec}.md"
    js = out_dir / f"{report.spec}.json"
    md.write_text(report.to_markdown(), encoding="utf-8")
    payload = {**asdict(report), "status": report.status}
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js
