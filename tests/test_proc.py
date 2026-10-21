from __future__ import annotations

import socket
import sys

from proofline.proc import check_app_starts, run_command

PY = f'"{sys.executable}"'


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_run_command_captures_exit_code_and_output(tmp_path):
    result = run_command(f"{PY} -c \"print('xin chào'); raise SystemExit(3)\"", tmp_path, 30)
    assert result.exit_code == 3
    assert not result.ok
    assert "xin chào" in result.output_tail


def test_run_command_timeout_kills_process(tmp_path):
    result = run_command(f'{PY} -c "import time; time.sleep(30)"', tmp_path, 1)
    assert result.exit_code is None
    assert result.seconds < 20


def test_check_app_starts_healthy_then_stops(tmp_path):
    port = free_port()
    result = check_app_starts(
        f"{PY} -m http.server {port} --bind 127.0.0.1",
        f"http://127.0.0.1:{port}/",
        tmp_path,
        startup_timeout=20,
        poll_interval=0.5,
    )
    assert result.healthy, result.detail
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0  # app đã bị dừng


def test_check_app_exits_early(tmp_path):
    result = check_app_starts(
        f'{PY} -c "raise SystemExit(2)"', "http://127.0.0.1:1/", tmp_path, 10, 0.2
    )
    assert not result.healthy
    assert "exit 2" in result.detail
