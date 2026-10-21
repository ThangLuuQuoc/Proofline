"""Thang đo dùng chung: mức tự chủ (L0–L4) và mức sẵn sàng của repo (R0–R4)."""

from typing import Literal

AutonomyLevel = Literal["L0", "L1", "L2", "L3", "L4"]
ReadinessLevel = Literal["R0", "R1", "R2", "R3", "R4"]

AUTONOMY_LEVELS: tuple[AutonomyLevel, ...] = ("L0", "L1", "L2", "L3", "L4")
READINESS_LEVELS: tuple[ReadinessLevel, ...] = ("R0", "R1", "R2", "R3", "R4")


def autonomy_rank(level: AutonomyLevel) -> int:
    return AUTONOMY_LEVELS.index(level)


def readiness_rank(level: ReadinessLevel) -> int:
    return READINESS_LEVELS.index(level)


def min_autonomy(*levels: AutonomyLevel) -> AutonomyLevel:
    return min(levels, key=autonomy_rank)
