"""`proofline target add`: thêm một repo đích vào workspace từ git URL hoặc đường dẫn có sẵn.

Repo clone từ URL nằm trong <workspace>/repos/<tên>/. Cấu hình dò được ghi vào
<workspace>/targets/<tên>/target.yaml (mỗi repo một file, không sửa proofline.yaml).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from proofline.config import TARGET_FILENAME, ConfigError, ProoflineConfig, targets_dir
from proofline.detect import Detection, detect
from proofline.proc import run_command

REPOS_DIR = "repos"
CLONE_TIMEOUT = 900.0
SCP_LIKE_URL = re.compile(r"^[\w.-]+@[\w.-]+:")
VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class AddedTarget:
    name: str
    repo_path: Path
    target_file: Path
    cloned: bool
    detection: Detection


def is_git_url(source: str) -> bool:
    return "://" in source or bool(SCP_LIKE_URL.match(source))


def name_from_source(source: str) -> str:
    tail = re.split(r"[/:\\]", source.rstrip("/\\"))[-1]
    return tail.removesuffix(".git")


def add_target(
    config: ProoflineConfig,
    source: str,
    *,
    name: str | None = None,
    branch: str | None = None,
) -> AddedTarget:
    workspace = config.workspace
    name = name or name_from_source(source)
    if not VALID_NAME.match(name):
        raise ConfigError(f"tên repo không hợp lệ: '{name}' (dùng --name)")
    if any(repo.name == name for repo in config.target_repos):
        raise ConfigError(f"repo đích '{name}' đã có trong workspace")
    target_file = targets_dir(workspace) / name / TARGET_FILENAME

    if is_git_url(source):
        repo_path = workspace / REPOS_DIR / name
        if repo_path.exists():
            raise ConfigError(f"{repo_path} đã tồn tại")
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        branch_arg = f'--branch "{branch}" ' if branch else ""
        result = run_command(
            f'git clone {branch_arg}"{source}" "{repo_path}"', workspace, CLONE_TIMEOUT
        )
        if not result.ok:
            raise ConfigError(f"git clone thất bại: {result.output_tail.strip()[-400:]}")
        cloned = True
    else:
        repo_path = Path(source).expanduser().resolve()
        if not repo_path.is_dir():
            raise ConfigError(f"không tìm thấy thư mục repo: {repo_path}")
        cloned = False

    detection = detect(repo_path)
    entry = {
        "name": name,
        "local_path": _relative_or_absolute(repo_path, workspace),
        "default_branch": branch or detection.default_branch or "main",
        "commands": detection.commands,
        "app_runtime": detection.app_runtime,
        "journeys_dir": detection.journeys_dir,
        "artifacts_location": "repo",
    }
    if cloned:
        entry["source_url"] = source
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(_render(entry, detection), encoding="utf-8")
    return AddedTarget(name, repo_path, target_file, cloned, detection)


def _relative_or_absolute(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:  # Windows: khác ổ đĩa
        return path.as_posix()


def _render(entry: dict, detection: Detection) -> str:
    header = [
        f"# Repo đích '{entry['name']}' (stack {detection.stack}), tạo bởi `proofline target add`.",
        "# Proofline không sửa file có sẵn của repo; file nó sinh ra nằm trong .proofline/.",
        "# Các giá trị được tự dò từ file trong repo; hãy kiểm tra trước khi chạy `--run`.",
    ]
    header += [f"# Lưu ý: {note}" for note in detection.notes]
    body = yaml.safe_dump(entry, allow_unicode=True, sort_keys=False)
    return "\n".join(header) + "\n" + body
