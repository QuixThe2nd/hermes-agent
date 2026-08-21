"""Tests for shared ``run_agent_cli`` line callbacks."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from tools.agent_cli_runner import run_agent_cli


@pytest.fixture
def fake_agent_script() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "tests" / "tools" / "fixtures" / "fake_cursor_agent.py"


def test_on_complete_line_handles_fragmented_json(monkeypatch, tmp_path, fake_agent_script):
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.05)
    env = {
        **dict(os.environ),
        "FAKE_CURSOR_FRAGMENT_INIT": "1",
        "FAKE_CURSOR_SESSION_ID": "frag-session",
    }
    lines: list[str] = []

    error_code, _log_path, log_text, _duration, returncode = run_agent_cli(
        [sys.executable, str(fake_agent_script), "-p"],
        workdir=str(tmp_path),
        timeout_seconds=30,
        stall_watchdog_seconds=5,
        log_dir=tmp_path / "logs",
        run_timestamp="frag",
        env=env,
        on_complete_line=lines.append,
    )

    assert returncode == 0
    assert error_code is None
    assert any("frag-session" in line for line in lines)
    assert "frag-session" in log_text


@pytest.mark.live_system_guard_bypass
def test_on_complete_line_invoked_while_process_blocked(monkeypatch, tmp_path, fake_agent_script):
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.05)
    env = {
        **dict(os.environ),
        "FAKE_CURSOR_BLOCK_AFTER_INIT": "1",
        "FAKE_CURSOR_SESSION_ID": "live-session",
    }
    seen: dict[str, bool] = {"session": False}

    def _on_line(line: str) -> None:
        if "live-session" in line:
            seen["session"] = True

    holder: dict = {}

    def _run() -> None:
        holder["result"] = run_agent_cli(
            [sys.executable, str(fake_agent_script), "-p"],
            workdir=str(tmp_path),
            timeout_seconds=30,
            stall_watchdog_seconds=30,
            log_dir=tmp_path / "logs",
            run_timestamp="live",
            env=env,
            on_complete_line=_on_line,
        )

    thread = threading.Thread(target=_run)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if seen["session"]:
            assert thread.is_alive()
            break
        time.sleep(0.05)
    assert seen["session"] is True

    logs = list((tmp_path / "logs").glob("*.jsonl"))
    assert logs
    name_parts = logs[0].stem.rsplit("-", 1)
    if len(name_parts) == 2 and name_parts[1].isdigit():
        pid = int(name_parts[1])
        try:
            os.kill(pid, signal.SIGTERM)  # windows-footgun: ok — POSIX test cleanup
        except (OSError, ProcessLookupError):
            pass
    thread.join(timeout=10)
    assert "result" in holder
