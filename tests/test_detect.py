from __future__ import annotations

import json
from pathlib import Path

from proofline.detect import detect


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def node_repo(root: Path, scripts: dict, deps: dict, lockfile: str = "pnpm-lock.yaml") -> Path:
    write(root / "package.json", json.dumps({"scripts": scripts, "dependencies": deps}))
    write(root / lockfile, "")
    return root


def test_node_pnpm_next_with_port_and_playwright(tmp_path):
    repo = node_repo(
        tmp_path,
        {
            "dev": "next dev -p 3200",
            "test": "vitest run",
            "lint": "eslint",
            "typecheck": "tsc --noEmit",
        },
        {"next": "15.2.1"},
    )
    write(repo / "playwright.config.ts", "export default defineConfig({ testDir: './e2e' })")
    result = detect(repo)
    assert result.stack == "node/pnpm/next"
    assert result.commands == {
        "install": "pnpm install --frozen-lockfile",
        "test": "pnpm test",
        "lint": "pnpm lint",
        "typecheck": "pnpm typecheck",
    }
    assert result.app_runtime["start_command"] == "pnpm dev"
    assert result.app_runtime["healthcheck_url"] == "http://localhost:3200/"
    assert result.journeys_dir == "e2e/journeys"
    assert result.notes == []


def test_npm_default_test_script_is_ignored_and_default_port_noted(tmp_path):
    repo = node_repo(
        tmp_path,
        {"start": "vite", "test": 'echo "Error: no test specified" && exit 1'},
        {"vite": "5"},
        lockfile="package-lock.json",
    )
    result = detect(repo)
    assert result.commands == {"install": "npm ci"}
    assert result.app_runtime["start_command"] == "npm run start"
    assert result.app_runtime["healthcheck_url"] == "http://localhost:5173/"
    assert any("không có script test" in n for n in result.notes)
    assert any("mặc định của vite" in n for n in result.notes)


def test_node_without_port_keeps_app_runtime_disabled(tmp_path):
    repo = node_repo(tmp_path, {"start": "node server.js"}, {}, lockfile="yarn.lock")
    result = detect(repo)
    assert result.commands["install"] == "yarn install --frozen-lockfile"
    assert result.app_runtime == {"enabled": False}


def test_python_uv_project(tmp_path):
    write(tmp_path / "pyproject.toml", "[dependency-groups]\ndev = ['pytest', 'ruff', 'mypy']\n")
    write(tmp_path / "uv.lock", "")
    result = detect(tmp_path)
    assert result.stack == "python/uv"
    assert result.commands == {
        "install": "uv sync",
        "test": "uv run pytest -q",
        "lint": "uv run ruff check .",
        "typecheck": "uv run mypy .",
    }
    assert result.app_runtime == {"enabled": False}


def test_unknown_stack(tmp_path):
    result = detect(tmp_path)
    assert result.stack == "unknown"
    assert result.commands == {}
    assert result.notes
