"""Chạy lệnh của repo đích: có timeout, dừng cả cây tiến trình (pnpm/npm sinh tiến trình con)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

OUTPUT_TAIL_CHARS = 2000


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int | None  # None = quá thời gian
    output_tail: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _popen_kwargs() -> dict:
    # Output được đọc theo UTF-8; ép tiến trình Python con in UTF-8 (Windows mặc định cp1252).
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if sys.platform == "win32":
        return {"env": env, "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"env": env, "start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=10)
    if sys.platform != "win32":
        # SIGKILL is asynchronous and wait() only covers the shell: the app it started (still
        # holding the port) may outlive it for a moment. Wait until the whole group is gone.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.05)


def run_command(command: str, cwd: Path, timeout: float) -> CommandResult:
    started = time.monotonic()
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **_popen_kwargs(),
        )
        try:
            exit_code: int | None = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            exit_code = None
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
    return CommandResult(
        command, exit_code, output[-OUTPUT_TAIL_CHARS:], time.monotonic() - started
    )


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


@dataclass(frozen=True)
class AppCheckResult:
    healthy: bool
    detail: str
    output_tail: str


def check_app_starts(
    start_command: str,
    healthcheck_url: str,
    cwd: Path,
    startup_timeout: float,
    poll_interval: float = 2.0,
) -> AppCheckResult:
    """Khởi chạy app, chờ healthcheck trả 2xx/3xx, rồi luôn dừng app."""
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            start_command,
            shell=True,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **_popen_kwargs(),
        )
        deadline = time.monotonic() + startup_timeout
        healthy = False
        detail = f"không qua healthcheck {healthcheck_url} trong {startup_timeout:.0f}s"
        try:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    detail = f"app thoát sớm (exit {proc.returncode})"
                    break
                if http_ok(healthcheck_url):
                    healthy = True
                    detail = f"healthcheck {healthcheck_url} OK"
                    break
                time.sleep(poll_interval)
        finally:
            kill_tree(proc)
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
    return AppCheckResult(healthy, detail, output[-OUTPUT_TAIL_CHARS:])
