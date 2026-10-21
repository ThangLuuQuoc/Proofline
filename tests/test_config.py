from __future__ import annotations

import pytest
from conftest import REPO_CONFIG

from proofline.config import ConfigError, ProoflineConfig, load_config, resolve_model


def test_repo_config_is_valid():
    cfg = load_config(REPO_CONFIG)
    assert {"triage", "fast_probe", "heavy_coding", "senior_reviewer", "lead_agent"} <= set(
        cfg.models.roles
    )
    assert cfg.autonomy.decisions["customer_reply"].max == "L1"
    assert not {"main", "master"} & set(cfg.autonomy.auto_merge.target_branches)


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
