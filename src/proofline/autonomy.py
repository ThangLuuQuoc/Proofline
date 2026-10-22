"""Tính mức tự chủ hiệu lực của một loại quyết định.

Mức hiệu lực = thấp nhất giữa: mức đã kiếm được (hoặc `current` khi chưa có nhật ký),
mức trần `max`, trần theo mức sẵn sàng của repo (chưa đạt `min_readiness` → tối đa L1),
và trần của kill switch (tối đa L2). Xem docs/04-execution-plan.md mục 3.
"""

from __future__ import annotations

from dataclasses import dataclass

from proofline.config import AutonomyConfig, ConfigError, DecisionConfig
from proofline.levels import AutonomyLevel, ReadinessLevel, min_autonomy, readiness_rank

AUTO_MERGE_DECISION = "auto_merge_low_risk"
BELOW_READINESS_CAP: AutonomyLevel = "L1"
KILL_SWITCH_CAP: AutonomyLevel = "L2"


@dataclass(frozen=True)
class EffectiveLevel:
    level: AutonomyLevel
    reasons: tuple[str, ...]


def required_readiness(
    autonomy: AutonomyConfig, decision_type: str, task_class: str | None = None
) -> ReadinessLevel | None:
    decision = _decision(autonomy, decision_type)
    if decision_type == AUTO_MERGE_DECISION and task_class is not None:
        by_class = autonomy.auto_merge.min_readiness_by_class
        if task_class in by_class:
            return by_class[task_class]
        if "default" in by_class:
            return by_class["default"]
    return decision.min_readiness


def effective_level(
    autonomy: AutonomyConfig,
    decision_type: str,
    *,
    readiness: ReadinessLevel,
    earned: AutonomyLevel | None = None,
    task_class: str | None = None,
) -> EffectiveLevel:
    decision = _decision(autonomy, decision_type)
    level = earned or decision.current
    reasons: list[str] = [f"điểm xuất phát {level}" + (" (đã kiếm được)" if earned else "")]

    capped = min_autonomy(level, decision.max)
    if capped != level:
        reasons.append(f"mức trần max={decision.max}")
    level = capped

    needed = required_readiness(autonomy, decision_type, task_class)
    if needed is not None and readiness_rank(readiness) < readiness_rank(needed):
        capped = min_autonomy(level, BELOW_READINESS_CAP)
        if capped != level:
            reasons.append(f"repo {readiness} chưa đạt {needed}")
        level = capped

    if autonomy.kill_switch:
        capped = min_autonomy(level, KILL_SWITCH_CAP)
        if capped != level:
            reasons.append("kill switch đang bật")
        level = capped

    return EffectiveLevel(level=level, reasons=tuple(reasons))


def _decision(autonomy: AutonomyConfig, decision_type: str) -> DecisionConfig:
    try:
        return autonomy.decisions[decision_type]
    except KeyError as exc:
        raise ConfigError(f"loại quyết định không tồn tại: '{decision_type}'") from exc
