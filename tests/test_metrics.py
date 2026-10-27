# ruff: noqa: F811  (fixture `setup` lấy từ test_verify)
"""Gate chỉ số nghiệp vụ (docs/09-lessons-macroeconomics.md mục 3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from test_verify import PY, approve_journey, commit, run_verify, setup, sh, write  # noqa: F401
from typer.testing import CliRunner

from proofline.cli import app
from proofline.metrics import MetricError, MetricSpec, extract

SPEC = ".proofline/specs/mul.yaml"
# Ghi tỷ lệ sai số của "mô hình" ra {out}; mô hình tốt hơn khi quality.ERROR nhỏ hơn.
MEASURE = (
    "import json, sys\n\nimport quality\n\n"
    "ratio = quality.ERROR\n"
    "json.dump({'outcomes': {'vn.exports': {'ratio': ratio}}}, open(sys.argv[1], 'w'))\n"
)


def metric(**overrides) -> dict:
    return {
        "name": "mae_ratio",
        "command": f"{PY} measure.py {{out}}",
        "value": 'outcomes["vn.exports"].ratio',
        "threshold": {"max": 0.8},
        "files": ["measure.py"],
        **overrides,
    }


def set_error(repo: Path, error: float) -> None:
    write(repo / "quality.py", f"ERROR = {error}\n")


def with_metrics(repo: Path, config: Path, *metrics: dict, base_error: float = 1.0) -> None:
    """Đo trên base (main) với `base_error`, khai báo chỉ số trong spec, rồi người duyệt lại."""
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "--allow-empty", "-m", "first approval")
    sh(repo, "checkout", "-q", "main")
    write(repo / "measure.py", MEASURE)
    set_error(repo, base_error)
    commit(repo, "measure on base")
    sh(repo, "checkout", "-q", "feature")
    sh(repo, "-c", "core.editor=true", "merge", "-q", "--no-edit", "main")
    manifest = yaml.safe_load((repo / SPEC).read_text(encoding="utf-8"))
    manifest["kind"] = "model"
    manifest["metrics"] = list(metrics)
    write(repo / SPEC, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
    commit(repo, "declare metrics")
    approve_journey(repo, config)
    commit(repo, "approve")


def test_metric_better_than_threshold_and_base_passes(setup):
    repo, _, config = setup
    with_metrics(repo, config, metric())
    set_error(repo, 0.5)
    commit(repo, "better model")
    report, gates = run_verify(repo, config)
    gate = gates["metric_mae_ratio"]
    assert gate.status == "pass", gate.detail
    assert gate.evidence["head"] == 0.5 and gate.evidence["base"] == 1.0
    assert gates["metrics_unchanged"].status == "pass"
    assert report.status == "pass", report.to_markdown()


def test_metric_missing_the_threshold_fails_until_a_human_waives_this_commit(setup, monkeypatch):
    repo, _, config = setup
    with_metrics(repo, config, metric())
    set_error(repo, 0.9)
    commit(repo, "not good enough")
    _, gates = run_verify(repo, config)
    assert gates["metric_mae_ratio"].status == "fail"
    assert "không đạt ngưỡng" in gates["metric_mae_ratio"].detail
    head = sh(repo, "rev-parse", "HEAD")
    waived = CliRunner().invoke(
        app,
        ["spec", "waive-metric", str(repo / SPEC), "mae_ratio", "--reason", "ra mắt thử nghiệm"]
        + ["--config", str(config)],
        input=f"{head[:7]}\n",
    )
    assert waived.exit_code == 0, waived.output
    gate = run_verify(repo, config)[1]["metric_mae_ratio"]
    assert gate.status == "pass" and "ra mắt thử nghiệm" in gate.detail
    # The waiver is for that commit only.
    write(repo / "README.md", "x\n")
    commit(repo, "another commit")
    assert run_verify(repo, config)[1]["metric_mae_ratio"].status == "fail"


def test_metric_worse_than_base_fails_within_the_threshold(setup):
    repo, _, config = setup
    with_metrics(repo, config, metric(threshold={"max": 2.0}), base_error=0.4)
    set_error(repo, 0.5)
    commit(repo, "worse than base")
    gate = run_verify(repo, config)[1]["metric_mae_ratio"]
    assert gate.status == "fail" and "xấu hơn base" in gate.detail


def test_tolerance_against_base(setup):
    repo, _, config = setup
    with_metrics(
        repo, config, metric(threshold={"max": 2.0}, vs_base={"not_worse_by": 0.2}), base_error=0.4
    )
    set_error(repo, 0.5)
    commit(repo, "slightly worse")
    assert run_verify(repo, config)[1]["metric_mae_ratio"].status == "pass"


def test_changing_the_measurement_after_approval_fails_without_measuring(setup):
    repo, _, config = setup
    with_metrics(repo, config, metric())
    write(repo / "measure.py", MEASURE.replace("ratio = ", "ratio = 0 * "))
    commit(repo, "make the metric easy")
    report, gates = run_verify(repo, config)
    unchanged = gates["metrics_unchanged"]
    assert unchanged.status == "fail" and unchanged.evidence["changed_files"] == ["measure.py"]
    assert "metric_mae_ratio" not in gates
    assert report.status == "fail"


def test_loosening_the_threshold_after_approval_fails(setup):
    repo, _, config = setup
    with_metrics(repo, config, metric())
    manifest = yaml.safe_load((repo / SPEC).read_text(encoding="utf-8"))
    manifest["metrics"][0]["threshold"] = {"max": 5.0}
    write(repo / SPEC, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
    commit(repo, "loosen")
    gate = run_verify(repo, config)[1]["metrics_unchanged"]
    assert gate.status == "fail" and "định nghĩa đã đổi" in gate.detail


def test_live_metric_only_reports_by_default(setup):
    repo, _, config = setup
    with_metrics(repo, config, metric(kind="live"))
    set_error(repo, 0.9)
    commit(repo, "live, not good enough")
    report, gates = run_verify(repo, config)
    gate = gates["metric_mae_ratio"]
    assert gate.status == "unknown" and "không chặn" in gate.detail
    assert report.status == "unknown"


def test_a_model_spec_without_metrics_cannot_be_approved(setup):
    repo, _, config = setup
    manifest = yaml.safe_load((repo / SPEC).read_text(encoding="utf-8"))
    manifest["kind"] = "model"
    write(repo / SPEC, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
    write(repo / "tests/journeys/test_journey.py", "def test_x():\n    assert False\n")
    result = CliRunner().invoke(
        app, ["spec", "approve", str(repo / SPEC), "--config", str(config)], input="x\n"
    )
    assert result.exit_code == 2 and "kind: model" in result.output


def test_metric_definition_is_validated():
    with pytest.raises(ValidationError, match="out"):
        MetricSpec(name="m", command="run", value="a", threshold={"max": 1})
    with pytest.raises(ValidationError, match="threshold"):
        MetricSpec(name="m", command="run {out}", value="a")
    m = MetricSpec(name="m", command="run {out}", value="a", threshold={"min": 0.6})
    assert m.direction == "higher" and m.is_blocking
    assert m.meets_threshold(0.7) and not m.meets_threshold(0.5)
    assert m.worse_than(0.6, 0.7) and not m.worse_than(0.8, 0.7)


def test_extract_paths():
    data = {"outcomes": {"vn.cpi": {"ratio": 1.5, "eps": [{"e": 2}]}}, "flag": True}
    assert extract(data, 'outcomes["vn.cpi"].ratio') == 1.5
    assert extract(data, "outcomes['vn.cpi'].eps[0].e") == 2.0
    with pytest.raises(MetricError):
        extract(data, "outcomes.missing")
    with pytest.raises(MetricError, match="không phải một số"):
        extract(data, "flag")
