"""Chỉ số nghiệp vụ của một spec (docs/09-lessons-macroeconomics.md mục 3).

Spec khai báo lệnh đo (ghi JSON ra `{out}`), đường dẫn tới số trong JSON và ngưỡng; người duyệt
chúng cùng journey. Verify chạy lệnh trên base và trên nhánh rồi so.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

PATH_TOKEN = re.compile(
    r"\.?(?P<key>[A-Za-z_][\w-]*)|\[\s*(?P<q>[\"'])(?P<quoted>.*?)(?P=q)\s*\]|\[(?P<index>\d+)\]"
)


class MetricError(Exception):
    """Không đo được (lệnh lỗi, không có file JSON, đường dẫn không tới một số)."""


class Threshold(BaseModel):
    max: float | None = None
    min: float | None = None


class VsBase(BaseModel):
    not_worse_by: float = 0.0


class MetricSpec(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    command: str  # ghi JSON ra {out}
    value: str  # vd outcomes["vn.exports_yoy"].ratio
    kind: Literal["fixture", "live"] = "fixture"
    threshold: Threshold = Field(default_factory=Threshold)
    better: Literal["lower", "higher"] | None = None
    vs_base: VsBase | None = None
    blocking: bool | None = None  # mặc định: fixture chặn, live chỉ báo cáo
    # Code tính chỉ số: khoá blob lúc duyệt, đổi thì là đổi thước đo.
    files: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> MetricSpec:
        if "{out}" not in self.command:
            raise ValueError(f"metric {self.name}: command phải ghi JSON ra {{out}}")
        if self.direction is None:
            raise ValueError(f"metric {self.name}: cần threshold.max/min hoặc better")
        parse_path(self.value)
        return self

    @property
    def direction(self) -> Literal["lower", "higher"] | None:
        if self.better:
            return self.better
        if self.threshold.max is not None:
            return "lower"
        if self.threshold.min is not None:
            return "higher"
        return None

    @property
    def is_blocking(self) -> bool:
        return self.blocking if self.blocking is not None else self.kind == "fixture"

    def threshold_text(self) -> str:
        parts = []
        if self.threshold.max is not None:
            parts.append(f"≤ {self.threshold.max:g}")
        if self.threshold.min is not None:
            parts.append(f"≥ {self.threshold.min:g}")
        return " và ".join(parts) or "không có ngưỡng"

    def meets_threshold(self, value: float) -> bool:
        if self.threshold.max is not None and value > self.threshold.max:
            return False
        return not (self.threshold.min is not None and value < self.threshold.min)

    def worse_than(self, head: float, base: float) -> bool:
        """Nhánh xấu hơn base quá mức cho phép (mặc định: không được xấu hơn chút nào)."""
        tolerance = self.vs_base.not_worse_by if self.vs_base else 0.0
        if self.direction == "lower":
            return head > base + tolerance
        return head < base - tolerance


def parse_path(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    pos = 0
    while pos < len(path):
        match = PATH_TOKEN.match(path, pos)
        if not match or match.end() == pos:
            raise ValueError(f"đường dẫn không hợp lệ: {path!r} (ở vị trí {pos})")
        if match["key"] is not None:
            tokens.append(match["key"])
        elif match["quoted"] is not None:
            tokens.append(match["quoted"])
        else:
            tokens.append(int(match["index"]))
        pos = match.end()
    if not tokens:
        raise ValueError("đường dẫn rỗng")
    return tokens


def extract(data: Any, path: str) -> float:
    node = data
    for token in parse_path(path):
        try:
            node = node[token]
        except (KeyError, IndexError, TypeError) as exc:
            raise MetricError(f"không có {token!r} trong {path}") from exc
    if isinstance(node, bool) or not isinstance(node, int | float) or not math.isfinite(node):
        raise MetricError(f"{path} không phải một số: {node!r}")
    return float(node)


def fingerprint(metrics: list[MetricSpec], file_blobs: dict[str, str]) -> str:
    """Định nghĩa chỉ số + blob của code tính chúng; rỗng khi spec không có chỉ số."""
    if not metrics:
        return ""
    payload = {
        "metrics": [m.model_dump(exclude_none=True) for m in metrics],
        "files": dict(sorted(file_blobs.items())),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
