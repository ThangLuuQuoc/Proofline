"""Đọc và kiểm tra `config/proofline.yaml`, chọn model theo vai trò.

Chỉ các mục mà code đang dùng được khai báo kiểu chặt; các mục khác vẫn được giữ
nguyên (extra="allow") để config có thể đi trước code.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from proofline.levels import AutonomyLevel, ReadinessLevel, autonomy_rank

DEFAULT_CONFIG_PATH = Path("config/proofline.yaml")
CONFIG_PATH_ENV = "PROOFLINE_CONFIG"
MODEL_ENV_PREFIX = "PROOFLINE_MODEL_"


class ConfigError(Exception):
    """Config không đọc được hoặc không hợp lệ."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


class FactoryConfig(_Section):
    name: str
    workspace_root: str = "."
    worktrees_dir: str = ".worktrees"
    log_dir: str = "logs"
    state_db: str = "state/proofline.db"


class AppRuntime(_Section):
    enabled: bool = False
    start_command: str = ""
    seed_command: str = ""
    healthcheck_url: str = ""
    startup_timeout_seconds: int = Field(default=120, gt=0)
    base_url: str = ""


ARTIFACTS_DIRNAME = ".proofline"
WORKSPACE_TARGETS_DIR = "targets"


class TargetRepo(_Section):
    name: str
    local_path: str
    default_branch: str = "main"
    commands: dict[str, str] = Field(default_factory=dict)
    app_runtime: AppRuntime = Field(default_factory=AppRuntime)
    # Nơi Proofline thêm kịch bản người dùng mới; chọn chỗ bộ chạy test của repo tìm thấy.
    journeys_dir: str = "tests/journeys"
    # repo: file Proofline sinh ra nằm trong <repo>/.proofline/ (đi qua PR);
    # workspace: nằm ngoài repo, trong <workspace>/targets/<tên repo>/.
    artifacts_location: Literal["repo", "workspace"] = "repo"
    model_overrides: dict[str, str] = Field(default_factory=dict)

    def artifacts_dir(self, repo_path: Path, workspace: Path) -> Path:
        if self.artifacts_location == "workspace":
            return workspace / WORKSPACE_TARGETS_DIR / self.name
        return repo_path / ARTIFACTS_DIRNAME


class ProviderConfig(_Section):
    api_key_env: str


class RoleConfig(_Section):
    provider: str
    model: str
    fallback: list[str] = Field(default_factory=list)


class ModelsConfig(_Section):
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]

    @model_validator(mode="after")
    def _roles_use_known_providers(self) -> ModelsConfig:
        for role, cfg in self.roles.items():
            if cfg.provider not in self.providers:
                raise ValueError(f"vai trò '{role}' dùng provider chưa khai báo: '{cfg.provider}'")
        return self


class DecisionConfig(_Section):
    current: AutonomyLevel
    max: AutonomyLevel
    min_readiness: ReadinessLevel | None = None

    @model_validator(mode="after")
    def _current_within_max(self) -> DecisionConfig:
        if autonomy_rank(self.current) > autonomy_rank(self.max):
            raise ValueError(f"current ({self.current}) vượt max ({self.max})")
        return self


class AutoMergeConfig(_Section):
    target_branches: list[str] = Field(default_factory=list)
    eligible_task_classes: list[str] = Field(default_factory=list)
    min_readiness_by_class: dict[str, ReadinessLevel] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _never_protected_branches(self) -> AutoMergeConfig:
        protected = {"main", "master"} & set(self.target_branches)
        if protected:
            raise ValueError(f"không được tự merge vào nhánh bảo vệ: {sorted(protected)}")
        return self


class AutonomyConfig(_Section):
    kill_switch: bool = False
    audit_sample_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    decisions: dict[str, DecisionConfig]
    auto_merge: AutoMergeConfig = Field(default_factory=AutoMergeConfig)


class ReadinessConfig(_Section):
    levels: dict[ReadinessLevel, dict[str, Any]] = Field(default_factory=dict)

    def threshold(self, level: ReadinessLevel, key: str, default: Any) -> Any:
        return self.levels.get(level, {}).get(key, default)


class ProoflineConfig(_Section):
    version: str
    factory: FactoryConfig
    target_repos: list[TargetRepo] = Field(default_factory=list)
    models: ModelsConfig
    autonomy: AutonomyConfig
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)

    def extra_value(self, *keys: str, default: Any = None) -> Any:
        """Đọc một giá trị trong các mục chưa khai báo kiểu, vd ("evals", "golden_set_path")."""
        node: Any = self.model_extra or {}
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @model_validator(mode="after")
    def _overrides_use_known_roles(self) -> ProoflineConfig:
        for repo in self.target_repos:
            unknown = set(repo.model_overrides) - set(self.models.roles)
            if unknown:
                raise ValueError(
                    f"repo '{repo.name}' override vai trò không tồn tại: {sorted(unknown)}"
                )
        return self

    def target_repo(self, name: str) -> TargetRepo:
        for repo in self.target_repos:
            if repo.name == name:
                return repo
        raise ConfigError(f"không có repo đích tên '{name}'")


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))


def load_config(path: str | Path | None = None) -> ProoflineConfig:
    config_path = resolve_config_path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"không tìm thấy config: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML không hợp lệ trong {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config phải là một mapping: {config_path}")
    try:
        return ProoflineConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"config không hợp lệ ({config_path}):\n{exc}") from exc


@dataclass(frozen=True)
class ResolvedModel:
    role: str
    provider: str
    model: str
    fallback: tuple[str, ...]
    source: Literal["env", "repo", "default"]


def resolve_model(
    config: ProoflineConfig,
    role: str,
    *,
    repo: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedModel:
    """Chọn model cho một vai trò: biến môi trường > override theo repo > mặc định."""
    if role not in config.models.roles:
        raise ConfigError(f"vai trò không tồn tại: '{role}'")
    role_cfg = config.models.roles[role]
    env = os.environ if env is None else env

    env_value = env.get(MODEL_ENV_PREFIX + role.upper())
    if env_value:
        model, source = env_value, "env"
    elif repo is not None and role in config.target_repo(repo).model_overrides:
        model, source = config.target_repo(repo).model_overrides[role], "repo"
    else:
        model, source = role_cfg.model, "default"

    return ResolvedModel(
        role=role,
        provider=role_cfg.provider,
        model=model,
        fallback=tuple(role_cfg.fallback),
        source=source,
    )
