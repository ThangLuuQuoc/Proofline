"""Đọc kết quả test dạng JUnit XML (vitest, pytest, jest-junit, ...) thành từng test pass/fail.

Gate cần biết **từng test** và **loại lỗi** (assertion hay lỗi môi trường), không chỉ exit code.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Status = Literal["passed", "failed", "error", "skipped"]

ASSERTION = re.compile(r"assert", re.IGNORECASE)


@dataclass(frozen=True)
class TestOutcome:
    __test__ = False  # not a pytest test class

    id: str
    status: Status
    error_type: str = ""
    message: str = ""

    @property
    def failed(self) -> bool:
        return self.status in ("failed", "error")

    @property
    def is_assertion(self) -> bool:
        """Fail ở một khẳng định (không phải ImportError, SyntaxError, lỗi môi trường...)."""
        return self.status == "failed" and bool(
            ASSERTION.search(f"{self.error_type} {self.message[:200]}")
        )


@dataclass(frozen=True)
class TestRun:
    __test__ = False  # not a pytest test class

    outcomes: tuple[TestOutcome, ...]

    @property
    def failed_ids(self) -> frozenset[str]:
        return frozenset(o.id for o in self.outcomes if o.failed)

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(o.id for o in self.outcomes)

    def by_id(self) -> dict[str, TestOutcome]:
        return {o.id: o for o in self.outcomes}


def _attr_int(el: ET.Element, name: str) -> int:
    try:
        return int(el.get(name) or 0)
    except ValueError:
        return 0


def parse_junit(path: Path) -> TestRun:
    """Một file JUnit XML → các test. Lỗi cấp suite (vd beforeAll, import) không gắn được vào
    test nào được ghi thành một test giả `<suite>::<suite error>` để không bị bỏ sót."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    outcomes: list[TestOutcome] = []
    for suite in suites:
        suite_name = suite.get("name") or suite.get("file") or "suite"
        failed_cases = 0
        for case in suite.findall("testcase"):
            classname = case.get("classname") or case.get("file") or suite_name
            case_id = f"{classname}::{case.get('name') or ''}"
            failure = case.find("failure")
            error = case.find("error")
            if failure is not None or error is not None:
                el = failure if failure is not None else error
                failed_cases += 1
                outcomes.append(
                    TestOutcome(
                        case_id,
                        "failed" if failure is not None else "error",
                        el.get("type") or "",
                        (el.get("message") or el.text or "").strip(),
                    )
                )
            elif case.find("skipped") is not None:
                outcomes.append(TestOutcome(case_id, "skipped"))
            else:
                outcomes.append(TestOutcome(case_id, "passed"))
        reported = _attr_int(suite, "errors") + _attr_int(suite, "failures")
        if reported > failed_cases and not suite.findall("testsuite"):
            outcomes.append(TestOutcome(f"{suite_name}::<suite error>", "error", "SuiteError"))
    return TestRun(tuple(outcomes))
