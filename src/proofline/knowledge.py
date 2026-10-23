"""Kho tri thức theo domain, bước K0 (docs/08-domain-knowledge.md): sự thật đo được.

`proofline verify` và `proofline assess --run` đo repo; các số đo đáng biết (test fail sẵn trên
base, test không ổn định, công cụ hỏng sẵn, app có khởi chạy được không…) được ghi vào
`<workspace>/<knowledge.path>/repo/<repo>/facts.yaml`. Mỗi sự thật gắn với ref và commit đã đo:
khi ref đó trỏ sang commit khác, sự thật **hết hạn** (đo lại mới dùng tiếp). Đo lại mà điều kiện
không còn thì sự thật bị **rút** kèm lý do. Không ghi gì vào repo đích.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from proofline.assess import Assessment
from proofline.config import ProoflineConfig, TargetRepo
from proofline.verify import VerifyReport

FACTS_FILENAME = "facts.yaml"
DEFAULT_KNOWLEDGE_DIR = "knowledge"
SAMPLE_SIZE = 10
# Tiêu chí assess có chạy lệnh thật (chỉ khi --run); kết quả của chúng là số đo về repo.
MEASURED_CRITERIA = {
    "container_builds": "Container build",
    "test_command_runs": "Lệnh test",
    "app_starts_and_healthy": "App khởi chạy",
    "ci_green_on_default_branch": "CI trên nhánh mặc định",
}

FactStatus = Literal["active", "expired", "retired"]


class Scope(BaseModel):
    ref: str
    commit: str


class Fact(BaseModel):
    id: str
    kind: Literal["fact"] = "fact"
    source: str
    statement: str
    scope: Scope
    status: FactStatus = "active"
    status_reason: str = ""
    first_seen: str
    last_measured: str
    measurements: int = 1
    evidence: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class FactsFile(BaseModel):
    repo: str
    facts: list[Fact] = Field(default_factory=list)
    # Số đo thô cộng dồn qua các lần đo trên cùng commit (vd danh sách test fail của base),
    # để một lần đo không xoá điều lần trước đã thấy.
    observations: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def by_id(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts}


@dataclass(frozen=True)
class Measurement:
    """Một điều đã đo. `holds=False`: đã đo và điều kiện không còn (rút sự thật cũ nếu có)."""

    key: str
    holds: bool
    statement: str = ""
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class Change:
    fact_id: str
    action: Literal["added", "updated", "confirmed", "retired", "expired"]
    statement: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def knowledge_root(config: ProoflineConfig) -> Path:
    path = config.extra_value("knowledge", "path", default=DEFAULT_KNOWLEDGE_DIR)
    return config.workspace / str(path)


def facts_path(config: ProoflineConfig, repo: str) -> Path:
    return knowledge_root(config) / "repo" / repo / FACTS_FILENAME


def load_facts(config: ProoflineConfig, repo: str) -> FactsFile:
    path = facts_path(config, repo)
    if not path.is_file():
        return FactsFile(repo=repo)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return FactsFile.model_validate({"repo": repo, **data})


def save_facts(config: ProoflineConfig, facts: FactsFile) -> Path:
    path = facts_path(config, facts.repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Sự thật đo được về repo '{facts.repo}' (docs/08-domain-knowledge.md, bước K0).\n"
        "# Proofline tự ghi từ `verify` và `assess --run`; sửa tay sẽ bị ghi đè ở lần đo sau.\n"
    )
    body = yaml.safe_dump(
        facts.model_dump(mode="json", exclude={"repo"}), allow_unicode=True, sort_keys=False
    )
    path.write_text(header + body, encoding="utf-8")
    return path


def resolve(repo_path: Path, ref: str) -> str | None:
    out = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def refresh_expiry(facts: FactsFile, repo_path: Path) -> list[Change]:
    """Sự thật nào có ref đã trỏ sang commit khác thì hết hạn."""
    changes = []
    for fact in facts.facts:
        if fact.status != "active":
            continue
        current = resolve(repo_path, fact.scope.ref) if repo_path.is_dir() else None
        if current == fact.scope.commit:
            continue
        fact.status = "expired"
        fact.status_reason = (
            f"`{fact.scope.ref}` đã đổi: {fact.scope.commit[:7]} → {current[:7]}; cần đo lại"
            if current
            else f"không còn thấy `{fact.scope.ref}` trong repo"
        )
        changes.append(Change(fact.id, "expired", fact.statement))
    return changes


def record(
    config: ProoflineConfig,
    target: TargetRepo,
    *,
    source: str,
    scope: Scope,
    measurements: Iterable[Measurement],
    evidence: str = "",
) -> list[Change]:
    """Gộp các số đo mới vào facts.yaml của repo; trả về những gì đã đổi."""
    facts = load_facts(config, target.name)
    repo_path = (config.workspace / target.local_path).resolve()
    changes = apply(
        facts,
        repo_path,
        source=source,
        scope=scope,
        measurements=measurements,
        evidence=evidence,
    )
    save_facts(config, facts)
    return changes


def apply(
    facts: FactsFile,
    repo_path: Path,
    *,
    source: str,
    scope: Scope,
    measurements: Iterable[Measurement],
    evidence: str = "",
) -> list[Change]:
    changes = refresh_expiry(facts, repo_path)
    existing = facts.by_id()
    now = _now()
    for m in measurements:
        fact_id = f"{source}.{m.key}@{scope.ref}"
        old = existing.get(fact_id)
        if not m.holds:
            if old is not None and old.status != "retired":
                old.status = "retired"
                old.status_reason = f"đo lại ở {scope.commit[:7]}: không còn đúng"
                old.last_measured = now
                changes.append(Change(fact_id, "retired", old.statement))
            continue
        if old is None:
            fact = Fact(
                id=fact_id,
                source=source,
                statement=m.statement,
                scope=scope,
                first_seen=now,
                last_measured=now,
                evidence=evidence,
                data=m.data or {},
            )
            facts.facts.append(fact)
            existing[fact_id] = fact
            changes.append(Change(fact_id, "added", m.statement))
            continue
        same = old.statement == m.statement and old.status == "active"
        old.statement = m.statement
        old.scope = scope
        old.status = "active"
        old.status_reason = ""
        old.last_measured = now
        old.measurements += 1
        old.evidence = evidence or old.evidence
        old.data = m.data or {}
        changes.append(Change(fact_id, "confirmed" if same else "updated", m.statement))
    return changes


def _sample(ids: list[str]) -> list[str]:
    return ids[:SAMPLE_SIZE]


def _base_tests(report: VerifyReport) -> dict[str, Any] | None:
    for gate in report.gates:
        if gate.key == "no_regression" and gate.evidence.get("base"):
            return gate.evidence["base"]
    return None


INCONCLUSIVE = ("wrong_reason", "no_junit")


def accumulate_journey_runs(facts: FactsFile, report: VerifyReport) -> dict[str, Any] | None:
    """Gộp các lần chạy journey trên base (gồm lần chạy lại) qua các lần đo trên cùng commit."""
    gate = next((g for g in report.gates if g.key == "acceptance_fails_before"), None)
    attempts = (gate.evidence.get("attempts") if gate else None) or []
    if not attempts:
        return None
    key = f"verify.journey@{report.base_ref}"
    prev = facts.observations.get(key)
    if not (prev and prev.get("commit") == report.base_sha):
        prev = {"runs": 0, "inconclusive": 0, "retries": 0, "causes": []}
    flaky = [a for a in attempts if a["outcome"] in INCONCLUSIVE]
    causes = list(prev["causes"])
    for a in flaky:
        cause = a["detail"][:120]
        if cause and cause not in causes:
            causes.append(cause)
    combined = {
        "runs": prev["runs"] + len(attempts),
        "inconclusive": prev["inconclusive"] + len(flaky),
        "retries": prev["retries"] + len(attempts) - 1,
        "causes": causes[:5],
    }
    facts.observations[key] = {"commit": report.base_sha, **combined}
    return combined


def journey_measurements(report: VerifyReport, journey: dict[str, Any] | None) -> list[Measurement]:
    if journey is None:
        return []
    ref, sha = report.base_ref, report.base_sha[:7]
    count = journey["inconclusive"]
    return [
        Measurement(
            "journey.inconclusive_on_base",
            count > 0,
            f"Journey chạy trên `{ref}` @ {sha} fail không ở khẳng định {count}/{journey['runs']}"
            f" lần (vd {journey['causes'][0] if journey['causes'] else '?'}); verify đã chạy lại"
            f" {journey['retries']} lần. Môi trường chập chờn: kết luận cần nhiều lần chạy"
            if count
            else "",
            dict(journey),
        )
    ]


def accumulate_base_tests(facts: FactsFile, report: VerifyReport) -> dict[str, Any] | None:
    """Gộp kết quả test của base với các lần đo trước **trên cùng commit**: một test chỉ là
    "fail ở mọi lượt" nếu fail ở mọi lượt của mọi lần đo; fail ở lượt nào đó thì không ổn định."""
    new = _base_tests(report)
    if new is None:
        return None
    key = f"verify.tests@{report.base_ref}"
    prev = facts.observations.get(key)
    if prev and prev.get("commit") == report.base_sha:
        always = set(prev["always_failing"]) & set(new["always_failing"])
        some = {*prev["always_failing"], *prev["unstable"], *new["always_failing"]}
        some |= set(new["unstable"])
        combined = {
            "measurements": prev.get("measurements", 1) + 1,
            "failed_per_run": [*prev["failed_per_run"], *new["failed_per_run"]],
            "always_failing": sorted(always),
            "unstable": sorted(some - always),
        }
    else:
        combined = {"measurements": 1, **new}
    facts.observations[key] = {"commit": report.base_sha, **combined}
    return combined


def measurements_from_verify(
    report: VerifyReport, base: dict[str, Any] | None = None
) -> list[Measurement]:
    """Những gì verify đo được về **base** (không phải về thay đổi đang kiểm). `base`: kết quả
    test của base đã cộng dồn (mặc định: chỉ lần đo này)."""
    ref, sha = report.base_ref, report.base_sha[:7]
    found: list[Measurement] = []
    gates = {g.key: g for g in report.gates}
    base = base or _base_tests(report)
    if base:
        always, unstable = base["always_failing"], base["unstable"]
        per_run = ", ".join(str(n) for n in base["failed_per_run"])
        if base.get("measurements", 1) > 1:
            per_run = f"{per_run}, qua {base['measurements']} lần đo"
        found.append(
            Measurement(
                "tests.preexisting_failures",
                bool(always),
                f"{len(always)} test fail sẵn trên `{ref}` @ {sha} ở mọi lượt chạy"
                f" (vd {always[0]}); đây không phải lỗi do thay đổi mới gây ra"
                if always
                else "",
                {"count": len(always), "sample": _sample(always)},
            )
        )
        # One run cannot tell an unstable test from a stable one: measure only with ≥ 2.
        if len(base["failed_per_run"]) >= 2:
            found.append(
                Measurement(
                    "tests.unstable_on_base",
                    bool(unstable),
                    f"{len(unstable)} test fail không ổn định trên `{ref}` @ {sha} (số test fail"
                    f" theo lượt: {per_run}; vd {unstable[0]}); có thể phụ thuộc thứ tự hoặc"
                    " trạng thái còn lại giữa các lượt, cần chạy nhiều lượt khi so"
                    if unstable
                    else "",
                    {
                        "count": len(unstable),
                        "failed_per_run": base["failed_per_run"],
                        "sample": _sample(unstable),
                    },
                )
            )
    for gate in report.gates:
        if not gate.key.startswith("static_"):
            continue
        name = gate.key.removeprefix("static_")
        info = gate.evidence.get("base")
        if not info:
            continue  # nhánh sạch nên base không được đo
        command = gate.evidence.get("command", name)
        found.append(
            Measurement(
                f"static.{name}.broken",
                bool(info.get("broken")),
                f"`{command}` không chạy được trên `{ref}` @ {sha}: {info.get('cause', '')}"
                if info.get("broken")
                else "",
                {"command": command, "cause": info.get("cause", "")},
            )
        )
        if "diagnostics" in info:
            count = info["diagnostics"]
            found.append(
                Measurement(
                    f"static.{name}.baseline",
                    count > 0,
                    f"`{command}` có sẵn {count} dòng lỗi/cảnh báo trên `{ref}` @ {sha};"
                    " gate chỉ xét phần tăng thêm"
                    if count
                    else "",
                    {"command": command, "diagnostics": count},
                )
            )
    if report.installed:
        install = gates.get("install_base")
        command = install.evidence.get("command", "install") if install else "install"
        found.append(
            Measurement(
                "install.broken",
                install is not None,
                f"`{command}` lỗi trên `{ref}` @ {sha}: {install.evidence.get('output', '')}"
                if install
                else "",
                {"command": command},
            )
        )
    return found


def measurements_from_assess(assessment: Assessment) -> list[Measurement]:
    """Các tiêu chí assess có chạy lệnh thật; `unknown` là chưa đo nên không ghi."""
    found = []
    for c in assessment.criteria:
        label = MEASURED_CRITERIA.get(c.key)
        if label is None or c.status == "unknown":
            continue
        ok = c.status == "pass"
        found.append(
            Measurement(
                f"{c.key}.{'ok' if ok else 'fails'}",
                True,
                f"{label}: {'đạt' if ok else 'không đạt'}: {c.detail}",
                {"criterion": c.key, "status": c.status},
            )
        )
        # Kết quả ngược lại của cùng tiêu chí (nếu đã ghi trước đây) không còn đúng.
        found.append(Measurement(f"{c.key}.{'fails' if ok else 'ok'}", False))
    return found


def checkout_scope(repo_path: Path) -> Scope | None:
    """Ref và commit của checkout đang đo (nhánh hiện tại; HEAD tách rời thì dùng chính commit)."""
    commit = resolve(repo_path, "HEAD")
    if commit is None:
        return None
    out = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    ref = out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else commit
    return Scope(ref=ref, commit=commit)


def record_verify(
    config: ProoflineConfig, target: TargetRepo, report: VerifyReport, evidence: Path | str = ""
) -> list[Change]:
    facts = load_facts(config, target.name)
    base = accumulate_base_tests(facts, report)
    journey = accumulate_journey_runs(facts, report)
    changes = apply(
        facts,
        (config.workspace / target.local_path).resolve(),
        source="verify",
        scope=Scope(ref=report.base_ref, commit=report.base_sha),
        measurements=[
            *measurements_from_verify(report, base),
            *journey_measurements(report, journey),
        ],
        evidence=_display(config, evidence),
    )
    save_facts(config, facts)
    return changes


def record_assess(
    config: ProoflineConfig,
    target: TargetRepo,
    assessment: Assessment,
    repo_path: Path,
    evidence: str = "",
) -> list[Change]:
    scope = checkout_scope(repo_path)
    if scope is None:
        return []
    return record(
        config,
        target,
        source="assess",
        scope=scope,
        measurements=measurements_from_assess(assessment),
        evidence=evidence,
    )


def current_facts(config: ProoflineConfig, target: TargetRepo) -> FactsFile:
    """Đọc facts.yaml, cập nhật hết hạn theo trạng thái repo hiện tại (ghi lại nếu có đổi)."""
    facts = load_facts(config, target.name)
    repo_path = (config.workspace / target.local_path).resolve()
    if refresh_expiry(facts, repo_path):
        save_facts(config, facts)
    return facts


def _display(config: ProoflineConfig, path: Path | str) -> str:
    if not path:
        return ""
    path = Path(path)
    try:
        return path.resolve().relative_to(config.workspace.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def describe_changes(changes: list[Change]) -> list[str]:
    label = {
        "added": "mới",
        "updated": "cập nhật",
        "confirmed": "xác nhận lại",
        "retired": "rút (không còn đúng)",
        "expired": "hết hạn (base đổi)",
    }
    return [f"{label[c.action]}: {c.statement}" for c in changes]
