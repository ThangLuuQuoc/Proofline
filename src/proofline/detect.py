"""Dò stack và lệnh của một repo đích để sinh cấu hình ban đầu (người kiểm tra lại).

Chỉ đọc file trong repo, không chạy lệnh nào của repo. Không chắc thì để trống và ghi chú,
thay vì đoán.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NPM_DEFAULT_TEST = "no test specified"
PORT_FLAG = re.compile(r"(?:-p|--port)[ =](\d{2,5})")
PLAYWRIGHT_TEST_DIR = re.compile(r"testDir\s*:\s*['\"]([^'\"]+)['\"]")
DEFAULT_PORTS = {"next": 3000, "vite": 5173, "nuxt": 3000, "remix": 3000, "astro": 4321}


@dataclass
class Detection:
    stack: str = "unknown"
    commands: dict[str, str] = field(default_factory=dict)
    app_runtime: dict[str, Any] = field(default_factory=lambda: {"enabled": False})
    journeys_dir: str = "tests/journeys"
    default_branch: str | None = None
    notes: list[str] = field(default_factory=list)


def detect(repo: Path) -> Detection:
    result = Detection(default_branch=git_default_branch(repo))
    if (repo / "package.json").is_file():
        _detect_node(repo, result)
    elif any((repo / f).is_file() for f in ("pyproject.toml", "requirements.txt", "setup.py")):
        _detect_python(repo, result)
    elif (repo / "go.mod").is_file():
        result.stack = "go"
        result.commands = {"test": "go test ./...", "lint": "go vet ./..."}
    elif (repo / "Cargo.toml").is_file():
        result.stack = "rust"
        result.commands = {"test": "cargo test", "lint": "cargo clippy"}
    else:
        result.notes.append("không nhận ra stack; khai báo commands thủ công")
    return result


def git_default_branch(repo: Path) -> str | None:
    for args in (
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        ["git", "symbolic-ref", "--short", "HEAD"],
    ):
        try:
            out = subprocess.run(
                args,
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().removeprefix("origin/")
    return None


def _package_manager(repo: Path) -> str:
    for lockfile, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
        ("package-lock.json", "npm"),
    ):
        if (repo / lockfile).is_file():
            return manager
    return "npm"


EXEC = {"pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx", "npm": "npx"}

INSTALL = {
    "pnpm": "pnpm install --frozen-lockfile",
    "yarn": "yarn install --frozen-lockfile",
    "bun": "bun install --frozen-lockfile",
    "npm": "npm ci",
}


def _detect_node(repo: Path, result: Detection) -> None:
    try:
        package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        result.stack = "node"
        result.notes.append("package.json không đọc được")
        return
    scripts: dict[str, str] = package.get("scripts") or {}
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    pm = _package_manager(repo)
    framework = next((name for name in DEFAULT_PORTS if name in deps), None)
    result.stack = f"node/{pm}" + (f"/{framework}" if framework else "")

    run = "npm run" if pm == "npm" else pm
    commands = {"install": INSTALL[pm]}
    if "test" in scripts and NPM_DEFAULT_TEST not in scripts["test"]:
        commands["test"] = "npm test" if pm == "npm" else f"{pm} test"
    else:
        result.notes.append("không có script test")
    if "lint" in scripts:
        commands["lint"] = f"{run} lint"
    for name in ("typecheck", "type-check", "check-types", "tsc"):
        if name in scripts:
            commands["typecheck"] = f"{run} {name}"
            break
    if "vitest" in deps:
        # Kết quả từng test cho `proofline verify` ({files} trống = cả bộ test).
        commands["test_junit"] = (
            f"{EXEC[pm]} vitest run {{files}} --reporter=junit --outputFile={{junit}}"
        )
    elif "jest" in deps:
        result.notes.append("jest cần gói jest-junit để `proofline verify` đọc kết quả từng test")
    result.commands = commands

    script_name = "dev" if "dev" in scripts else ("start" if "start" in scripts else None)
    if script_name is None:
        result.notes.append("không có script dev/start; app_runtime để tắt")
    else:
        match = PORT_FLAG.search(scripts[script_name])
        port = int(match.group(1)) if match else DEFAULT_PORTS.get(framework or "", None)
        if port is None:
            result.notes.append(f"không dò được cổng của '{script_name}'; app_runtime để tắt")
        else:
            result.app_runtime = {
                "enabled": True,
                "start_command": f"{run} {script_name}",
                "seed_command": "",
                "healthcheck_url": f"http://localhost:{port}/",
                "startup_timeout_seconds": 180,
            }
            if not match:
                result.notes.append(f"cổng {port} là mặc định của {framework}, chưa chắc chắn")

    for config_name in ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs"):
        config = repo / config_name
        if config.is_file():
            match = PLAYWRIGHT_TEST_DIR.search(config.read_text(encoding="utf-8", errors="replace"))
            test_dir = (match.group(1) if match else "tests").strip("./") or "tests"
            result.journeys_dir = f"{test_dir}/journeys"
            break


def _detect_python(repo: Path, result: Detection) -> None:
    pyproject = repo / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
    if (repo / "uv.lock").is_file():
        result.stack, prefix, install = "python/uv", "uv run ", "uv sync"
    elif (repo / "poetry.lock").is_file():
        result.stack, prefix, install = "python/poetry", "poetry run ", "poetry install"
    else:
        result.stack, prefix = "python/pip", ""
        if (repo / "requirements.txt").is_file():
            install = "pip install -r requirements.txt"
        else:
            install = "pip install -e ."
    commands = {"install": install}
    if "pytest" in text or (repo / "tests").is_dir() or (repo / "pytest.ini").is_file():
        commands["test"] = f"{prefix}pytest -q"
        commands["test_junit"] = f"{prefix}pytest -q --junitxml={{junit}} {{files}}"
    else:
        result.notes.append("không thấy pytest")
    if "ruff" in text:
        commands["lint"] = f"{prefix}ruff check ."
    if "mypy" in text:
        commands["typecheck"] = f"{prefix}mypy ."
    result.commands = commands
    result.notes.append("app_runtime để tắt: chưa tự dò cách khởi chạy app Python")
