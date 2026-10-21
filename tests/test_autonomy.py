from __future__ import annotations

import pytest

from proofline.autonomy import effective_level, required_readiness
from proofline.config import ConfigError, ProoflineConfig


@pytest.fixture
def autonomy(config_dict):
    return ProoflineConfig.model_validate(config_dict).autonomy


def test_starts_at_current_without_history(autonomy):
    assert effective_level(autonomy, "auto_merge_low_risk", readiness="R4").level == "L0"


def test_earned_level_capped_by_max(autonomy):
    result = effective_level(autonomy, "spec_approval", readiness="R4", earned="L3")
    assert result.level == "L1"
    assert any("max=L1" in r for r in result.reasons)


def test_below_readiness_caps_at_l1(autonomy):
    result = effective_level(autonomy, "self_sourced_task", readiness="R3", earned="L3")
    assert result.level == "L1"
    assert any("chưa đạt R4" in r for r in result.reasons)


def test_readiness_met_keeps_earned_level(autonomy):
    assert effective_level(autonomy, "triage_accept", readiness="R2", earned="L4").level == "L4"


@pytest.mark.parametrize(
    ("task_class", "readiness", "expected"),
    [
        ("add_tests", "R2", "L4"),  # add_tests được xét từ R2
        ("typo", "R2", "L1"),  # các class khác cần R3
        ("typo", "R3", "L4"),
    ],
)
def test_auto_merge_readiness_by_task_class(autonomy, task_class, readiness, expected):
    result = effective_level(
        autonomy, "auto_merge_low_risk", readiness=readiness, earned="L4", task_class=task_class
    )
    assert result.level == expected


def test_required_readiness_without_task_class(autonomy):
    assert required_readiness(autonomy, "auto_merge_low_risk") == "R3"
    assert required_readiness(autonomy, "spec_approval") is None


def test_kill_switch_caps_at_l2(config_dict):
    config_dict["autonomy"]["kill_switch"] = True
    autonomy = ProoflineConfig.model_validate(config_dict).autonomy
    result = effective_level(autonomy, "triage_accept", readiness="R4", earned="L4")
    assert result.level == "L2"
    assert any("kill switch" in r for r in result.reasons)


def test_kill_switch_never_raises_level(config_dict):
    config_dict["autonomy"]["kill_switch"] = True
    autonomy = ProoflineConfig.model_validate(config_dict).autonomy
    assert effective_level(autonomy, "auto_merge_low_risk", readiness="R4").level == "L0"


def test_unknown_decision(autonomy):
    with pytest.raises(ConfigError, match="không tồn tại"):
        effective_level(autonomy, "ghost", readiness="R4")
