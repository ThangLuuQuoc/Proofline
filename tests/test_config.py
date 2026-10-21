from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TEMPLATE_CONFIG

from proofline.config import ConfigError, ProoflineConfig, load_config, resolve_model


def test_template_config_is_valid():
    cfg = load_config(TEMPLATE_CONFIG)
    assert {"triage", "fast_probe", "heavy_coding", "senior_reviewer", "lead_agent"} <= set(
        cfg.models.roles
    )
    assert cfg.autonomy.decisions["customer_reply"].max == "L1"
    assert not {"main", "master"} & set(cfg.autonomy.auto_merge.target_branches)
    # Bản mẫu đi kèm công cụ không được chứa repo đích cụ thể của ai.
    assert cfg.target_repos == []


def test_workspace_is_relative_to_config_file(config_dict, write_config, tmp_path, monkeypatch):
    path = write_config(config_dict)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert load_config(path).workspace == path.parent.resolve()


def test_default_config_is_proofline_yaml_in_cwd(config_dict, write_config, monkeypatch):
    path = write_config(config_dict)
    monkeypatch.delenv("PROOFLINE_CONFIG", raising=False)
    monkeypatch.chdir(path.parent)
    assert load_config().version == "test"


def test_missing_config_hints_workspace_init(tmp_path, monkeypatch):
    monkeypatch.delenv("PROOFLINE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="proofline workspace init"):
        load_config()


def test_unknown_sections_are_kept(config_dict):
    config_dict["outer_loop"] = {"enabled": False}
    cfg = ProoflineConfig.model_validate(config_dict)
    assert cfg.model_extra["outer_loop"] == {"enabled": False}


def test_resolve_model_default(config_dict):
    cfg = ProoflineConfig.model_validate(config_dict)
    resolved = resolve_model(cfg, "heavy_coding", env={})
    assert (resolved.model, resolved.source) == ("default-model", "default")
    assert resolved.fallback == ("fallback-model",)


def test_resolve_model_repo_override(config_dict):
    cfg = ProoflineConfig.model_validate(config_dict)
    resolved = resolve_model(cfg, "heavy_coding", repo="svc", env={})
    assert (resolved.model, resolved.source) == ("repo-model", "repo")


def test_resolve_model_env_beats_repo(config_dict):
    cfg = ProoflineConfig.model_validate(config_dict)
    env = {"PROOFLINE_MODEL_HEAVY_CODING": "env-model"}
    resolved = resolve_model(cfg, "heavy_coding", repo="svc", env=env)
    assert (resolved.model, resolved.source) == ("env-model", "env")


def test_repo_without_override_uses_default(config_dict):
    cfg = ProoflineConfig.model_validate(config_dict)
    resolved = resolve_model(cfg, "triage", repo="svc", env={})
    assert (resolved.model, resolved.source) == ("triage-model", "default")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"role": "nope"}, "vai trò không tồn tại"),
        ({"role": "triage", "repo": "nope"}, "không có repo đích"),
    ],
)
def test_resolve_model_errors(config_dict, kwargs, message):
    cfg = ProoflineConfig.model_validate(config_dict)
    with pytest.raises(ConfigError, match=message):
        resolve_model(cfg, env={}, **kwargs)


def _break_provider(c):
    c["models"]["roles"]["triage"]["provider"] = "openai"


def _break_current_over_max(c):
    c["autonomy"]["decisions"]["spec_approval"]["current"] = "L3"


def _break_auto_merge_main(c):
    c["autonomy"]["auto_merge"]["target_branches"] = ["develop", "main"]


def _break_override_role(c):
    c["target_repos"][0]["model_overrides"] = {"ghost_role": "x"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_break_provider, "provider chưa khai báo"),
        (_break_current_over_max, "vượt max"),
        (_break_auto_merge_main, "nhánh bảo vệ"),
        (_break_override_role, "vai trò không tồn tại"),
    ],
)
def test_invalid_config_is_rejected(config_dict, write_config, mutate, message):
    mutate(config_dict)
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(config_dict))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="không tìm thấy config"):
        load_config(tmp_path / "missing.yaml")


def test_non_mapping(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


def test_config_path_from_env(monkeypatch, config_dict, write_config):
    path = write_config(config_dict)
    monkeypatch.setenv("PROOFLINE_CONFIG", str(path))
    assert load_config().version == "test"


def test_target_repo_artifacts_dir(config_dict):
    repo = ProoflineConfig.model_validate(config_dict).target_repo("svc")
    assert repo.journeys_dir == "tests/journeys"
    assert repo.artifacts_dir(Path("r"), Path("w")) == Path("r") / ".proofline"
    config_dict["target_repos"][0]["artifacts_location"] = "workspace"
    repo = ProoflineConfig.model_validate(config_dict).target_repo("svc")
    assert repo.artifacts_dir(Path("r"), Path("w")) == Path("w") / "targets" / "svc"


def test_invalid_artifacts_location(config_dict, write_config):
    config_dict["target_repos"][0]["artifacts_location"] = "elsewhere"
    with pytest.raises(ConfigError):
        load_config(write_config(config_dict))
