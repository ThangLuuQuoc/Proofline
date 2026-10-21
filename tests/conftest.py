from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "proofline.yaml"

MINIMAL_CONFIG: dict[str, Any] = {
    "version": "test",
    "factory": {"name": "test"},
    "target_repos": [
        {
            "name": "svc",
            "local_path": "../svc",
            "model_overrides": {"heavy_coding": "repo-model"},
        }
    ],
    "models": {
        "providers": {"anthropic": {"api_key_env": "ANTHROPIC_API_KEY"}},
        "roles": {
            "heavy_coding": {
                "provider": "anthropic",
                "model": "default-model",
                "fallback": ["fallback-model"],
            },
            "triage": {"provider": "anthropic", "model": "triage-model"},
        },
    },
    "autonomy": {
        "kill_switch": False,
        "decisions": {
            "spec_approval": {"current": "L1", "max": "L1", "min_readiness": None},
            "triage_accept": {"current": "L1", "max": "L4", "min_readiness": "R2"},
            "auto_merge_low_risk": {"current": "L0", "max": "L4", "min_readiness": "R3"},
            "self_sourced_task": {"current": "L0", "max": "L3", "min_readiness": "R4"},
        },
        "auto_merge": {
            "target_branches": ["develop", "staging"],
            "min_readiness_by_class": {"add_tests": "R2", "default": "R3"},
        },
    },
}


@pytest.fixture
def config_dict() -> dict[str, Any]:
    return copy.deepcopy(MINIMAL_CONFIG)


@pytest.fixture
def write_config(tmp_path: Path):
    def _write(data: dict[str, Any]) -> Path:
        path = tmp_path / "proofline.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return path

    return _write
