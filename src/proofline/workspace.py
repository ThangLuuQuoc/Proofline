"""Workspace của người dùng: nơi chứa config, state, playbook, artifacts — tách khỏi repo công cụ.

`init_workspace` chép bản mẫu đi kèm gói (`proofline/templates/`) vào một thư mục mới.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from proofline.config import CONFIG_FILENAME

TEMPLATES = files("proofline") / "templates"
WORKSPACE_DIRS = ("state", "logs", "targets", "evals/golden-set", "evals/decision-replay")
GITIGNORE = """# Dữ liệu chạy của Proofline (không commit)
state/
logs/
.worktrees/
repos/
.env
"""


class WorkspaceExistsError(Exception):
    """Thư mục đã có proofline.yaml."""


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def init_workspace(path: Path) -> InitResult:
    """Tạo workspace từ bản mẫu. Không ghi đè file nào đã có."""
    if (path / CONFIG_FILENAME).exists():
        raise WorkspaceExistsError(f"{path / CONFIG_FILENAME} đã tồn tại")
    path.mkdir(parents=True, exist_ok=True)
    result = InitResult()
    _copy_tree(TEMPLATES, path, path, result)
    for rel in WORKSPACE_DIRS:
        (path / rel).mkdir(parents=True, exist_ok=True)
    gitignore = path / ".gitignore"
    if gitignore.exists():
        result.kept.append(".gitignore")
    else:
        gitignore.write_text(GITIGNORE, encoding="utf-8")
        result.created.append(".gitignore")
    return result


def _copy_tree(source: Traversable, dest: Path, root: Path, result: InitResult) -> None:
    for item in source.iterdir():
        if item.name.startswith(("_", ".")):
            continue
        target = dest / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _copy_tree(item, target, root, result)
            continue
        rel = target.relative_to(root).as_posix()
        if target.exists():
            result.kept.append(rel)
        else:
            target.write_bytes(item.read_bytes())
            result.created.append(rel)
