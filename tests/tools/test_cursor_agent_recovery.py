"""Restart-safe recovery tests for delegate_cursor_agent (fake CLI)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.cursor_run_receipts import (
    cursor_runs_dir,
    read_receipt,
    receipt_run_lock,
    update_receipt,
)
from tools.cursor_agent_tool import (
    recover_delegate_cursor_agent_history,
    run_cursor_agent_cli_with_receipt,
)


@pytest.fixture
def fake_agent_script() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "tests" / "tools" / "fixtures" / "fake_cursor_agent.py"


@pytest.fixture
def fake_binary(fake_agent_script, monkeypatch):
    binary = str(fake_agent_script)
    monkeypatch.setattr(
        "tools.cursor_agent_tool.resolve_cursor_agent_binary",
        lambda: binary,
    )
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.05)
    return binary


def _dangling_history(tool_call_id: str, workdir: str, task: str = "do work") -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "delegate_cursor_agent",
                        "arguments": json.dumps({"task": task, "workdir": workdir}),
                    },
                }
            ],
        }
    ]


def test_receipt_created_before_spawn(fake_binary, tmp_path):
    result = json.loads(
        run_cursor_agent_cli_with_receipt(
            task="hello",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="sess-a",
            tool_call_id="call-1",
        )
    )
    assert result["success"] is True
    receipts = list(cursor_runs_dir().glob("*.receipt.json"))
    assert len(receipts) == 1
    receipt = read_receipt(receipts[0])
    assert receipt["hermes_session_id"] == "sess-a"
    assert receipt["tool_call_id"] == "call-1"
    assert receipt["prompt_hash"].startswith("sha256:")
    assert "hello" not in json.dumps(receipt)
    assert oct(receipts[0].stat().st_mode & 0o777) == "0o600"


def _wait_for_fake_child_pid(pid_file: Path, *, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.is_file():
            text = pid_file.read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
        time.sleep(0.05)
    raise AssertionError(f"fake child pid file never appeared: {pid_file}")


def _kill_fake_child(pid_file: Path) -> int:
    child_pid = _wait_for_fake_child_pid(pid_file)
    os.kill(child_pid, 9)  # windows-footgun: ok — POSIX test cleanup
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)  # windows-footgun: ok — POSIX liveness probe
        except ProcessLookupError:
            return child_pid
        time.sleep(0.05)
    raise AssertionError(f"fake child pid {child_pid} was not reaped")


@pytest.mark.live_system_guard_bypass
def test_live_session_id_persisted_while_blocked(fake_binary, tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CURSOR_BLOCK_AFTER_INIT", "1")
    monkeypatch.setenv("FAKE_CURSOR_SESSION_ID", "sess-live")
    monkeypatch.setenv("FAKE_CURSOR_PID_FILE", str(pid_file))
    holder: dict = {}

    def _run() -> None:
        holder["result"] = run_cursor_agent_cli_with_receipt(
            task="block",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="sess-live",
            tool_call_id="call-live",
        )

    thread = threading.Thread(target=_run)
    thread.start()
    receipt_path = None
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        for path in cursor_runs_dir().glob("*.receipt.json"):
            receipt = read_receipt(path)
            if receipt and receipt.get("cursor_session_id") == "sess-live":
                receipt_path = path
                assert thread.is_alive()
                break
        if receipt_path is not None:
            break
        time.sleep(0.05)
    assert receipt_path is not None

    _kill_fake_child(pid_file)
    thread.join(timeout=10)
    assert "result" in holder
    result = json.loads(holder["result"])
    assert result["success"] is False
    assert result.get("outcome") == "failed"
    assert "-9" in str(result.get("error") or "")


def test_fragmented_init_persisted(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CURSOR_FRAGMENT_INIT", "1")
    monkeypatch.setenv("FAKE_CURSOR_SESSION_ID", "frag-id")
    json.loads(
        run_cursor_agent_cli_with_receipt(
            task="frag",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="sess-frag",
            tool_call_id="call-frag",
        )
    )
    receipt = read_receipt(next(cursor_runs_dir().glob("*.receipt.json")))
    assert receipt["cursor_session_id"] == "frag-id"


@pytest.mark.live_system_guard_bypass
def test_resume_once_after_crash_after_init(fake_binary, tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CURSOR_BLOCK_AFTER_INIT", "1")
    monkeypatch.setenv("FAKE_CURSOR_SESSION_ID", "resume-sess")
    monkeypatch.setenv("FAKE_CURSOR_PID_FILE", str(pid_file))
    invocations: list[list[str]] = []
    real_popen = __import__("subprocess").Popen

    def _track_popen(cmd, **kwargs):
        invocations.append(list(cmd))
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr("tools.agent_cli_runner.subprocess.Popen", _track_popen)

    thread = threading.Thread(
        target=lambda: run_cursor_agent_cli_with_receipt(
            task="original task",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="sess-resume",
            tool_call_id="call-resume",
        )
    )
    thread.start()
    receipt_path = None
    for _ in range(60):
        for path in cursor_runs_dir().glob("*.receipt.json"):
            receipt = read_receipt(path)
            if receipt and receipt.get("cursor_session_id") == "resume-sess":
                receipt_path = path
                break
        if receipt_path:
            break
        time.sleep(0.05)
    assert receipt_path is not None
    _kill_fake_child(pid_file)
    thread.join(timeout=10)

    # First CLI child was SIGKILL'd after init; simulate a gateway crash before
    # the tool handler persisted a terminal receipt/outcome.
    update_receipt(
        receipt_path,
        state="running",
        outcome=None,
        terminal_result=None,
        resume_attempts=0,
    )
    monkeypatch.delenv("FAKE_CURSOR_BLOCK_AFTER_INIT", raising=False)

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history("call-resume", str(tmp_path)),
        hermes_session_id="sess-resume",
    )
    assert note and "Automatically resumed" in note
    assert history[-1]["role"] == "tool"
    resume_argv = [cmd for cmd in invocations if any(a.startswith("--resume=") for a in cmd)]
    cold_argv = [cmd for cmd in invocations if not any(a.startswith("--resume=") for a in cmd)]
    assert len(resume_argv) == 1
    assert len(cold_argv) == 1
    assert any("resume-sess" in arg for arg in resume_argv[0])


def test_no_automatic_run_without_session_id(fake_binary, tmp_path):
    receipt_path = cursor_runs_dir() / "manual.receipt.json"
    from tools.cursor_run_receipts import _atomic_write_json

    _atomic_write_json(
        receipt_path,
        {
            "schema_version": 1,
            "run_id": "manual",
            "attempt_id": "a1",
            "hermes_session_id": "sess-none",
            "tool_call_id": "call-none",
            "workdir": str(tmp_path),
            "prompt_hash": "sha256:x",
            "state": "running",
            "outcome": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "log_path": str(tmp_path / "empty.jsonl"),
            "owner_pid": os.getpid(),
            "owner_boot_id": "test",
            "model": None,
            "force": True,
            "timeout_seconds": 0,
            "execution_mode": "cli",
            "cursor_session_id": None,
            "cloud_agent_id": None,
            "cloud_run_id": None,
            "resume_attempts": 0,
            "resumed": False,
            "terminal_result": None,
        },
    )
    history, note = recover_delegate_cursor_agent_history(
        _dangling_history("call-none", str(tmp_path)),
        hermes_session_id="sess-none",
    )
    assert history[-1]["role"] == "assistant"
    assert note and "no canonical Cursor session id" in note


def test_terminal_log_reconciliation_skips_resume(fake_binary, tmp_path):
    first = json.loads(
        run_cursor_agent_cli_with_receipt(
            task="done",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="sess-term",
            tool_call_id="call-term",
        )
    )
    assert first["success"] is True
    receipt = read_receipt(next(cursor_runs_dir().glob("*.receipt.json")))
    assert receipt["state"] == "terminal"

    with patch("tools.cursor_agent_tool.run_cursor_agent_cli_with_receipt") as resume_mock:
        history, note = recover_delegate_cursor_agent_history(
            _dangling_history("call-term", str(tmp_path)),
            hermes_session_id="sess-term",
        )
        resume_mock.assert_not_called()
    assert history[-1]["role"] == "tool"
    assert note and "terminal" in note.lower()


@pytest.mark.parametrize(
    "mode,outcome",
    [
        ("success", "success"),
        ("nonzero", "failed"),
        ("action_required", "action_required"),
    ],
)
def test_terminal_outcomes_do_not_resume(fake_binary, tmp_path, monkeypatch, mode, outcome):
    monkeypatch.setenv("FAKE_CURSOR_MODE", mode)
    result = json.loads(
        run_cursor_agent_cli_with_receipt(
            task="terminal",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="sess-out",
            tool_call_id=f"call-{mode}",
        )
    )
    receipt = read_receipt(next(cursor_runs_dir().glob("*.receipt.json")))
    assert receipt["outcome"] == outcome
    with patch("tools.cursor_agent_tool.run_cursor_agent_cli_with_receipt") as resume_mock:
        recover_delegate_cursor_agent_history(
            _dangling_history(f"call-{mode}", str(tmp_path)),
            hermes_session_id="sess-out",
        )
        resume_mock.assert_not_called()


def test_concurrent_resume_lock_single_winner(tmp_path):
    from tools.cursor_run_receipts import create_receipt

    _run_id, _path = create_receipt(
        hermes_session_id="sess-lock",
        tool_call_id="call-lock",
        workdir=str(tmp_path),
        prompt_hash="sha256:x",
        log_path=str(tmp_path / "x.jsonl"),
        model=None,
        force=True,
        timeout_seconds=0,
        execution_mode="cli",
    )
    with receipt_run_lock(_run_id) as first:
        assert first is True
        with receipt_run_lock(_run_id) as second:
            assert second is False


def test_unrelated_session_cannot_recover(fake_binary, tmp_path):
    run_cursor_agent_cli_with_receipt(
        task="mine",
        workdir=str(tmp_path),
        model=None,
        timeout_seconds=0,
        force=True,
        hermes_session_id="sess-owner",
        tool_call_id="call-owner",
    )
    history, note = recover_delegate_cursor_agent_history(
        _dangling_history("call-owner", str(tmp_path)),
        hermes_session_id="sess-other",
    )
    assert history[-1]["role"] == "assistant"
    assert note and "no matching restart receipt" in note


def test_second_resume_uses_same_canonical_session(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CURSOR_SESSION_ID", "canonical-1")
    from tools.cursor_run_receipts import _atomic_write_json

    receipt_path = cursor_runs_dir() / "second.receipt.json"
    _atomic_write_json(
        receipt_path,
        {
            "schema_version": 1,
            "run_id": "second",
            "attempt_id": "a1",
            "hermes_session_id": "sess-second",
            "tool_call_id": "call-second",
            "workdir": str(tmp_path),
            "prompt_hash": "sha256:x",
            "state": "running",
            "outcome": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "log_path": str(tmp_path / "first.jsonl"),
            "owner_pid": os.getpid(),
            "owner_boot_id": "test",
            "model": None,
            "force": True,
            "timeout_seconds": 0,
            "execution_mode": "cli",
            "cursor_session_id": "canonical-1",
            "cloud_agent_id": None,
            "cloud_run_id": None,
            "resume_attempts": 0,
            "resumed": False,
            "terminal_result": None,
        },
    )
    recover_delegate_cursor_agent_history(
        _dangling_history("call-second", str(tmp_path)),
        hermes_session_id="sess-second",
    )
    receipt = read_receipt(receipt_path)
    assert receipt["cursor_session_id"] == "canonical-1"
    assert receipt["resume_attempts"] >= 1
    assert receipt.get("resumed") is True
    assert "resume-" in receipt["log_path"] or receipt["log_path"].endswith(".jsonl")


def test_conflicting_resume_session_id_fails_closed(fake_binary, tmp_path, monkeypatch):
    from tools.cursor_run_receipts import _atomic_write_json

    receipt_path = cursor_runs_dir() / "conflict.receipt.json"
    _atomic_write_json(
        receipt_path,
        {
            "schema_version": 1,
            "run_id": "conflict",
            "attempt_id": "a1",
            "hermes_session_id": "sess-conflict",
            "tool_call_id": "call-conflict",
            "workdir": str(tmp_path),
            "prompt_hash": "sha256:x",
            "state": "running",
            "outcome": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "log_path": str(tmp_path / "seed.jsonl"),
            "owner_pid": os.getpid(),
            "owner_boot_id": "test",
            "model": None,
            "force": True,
            "timeout_seconds": 0,
            "execution_mode": "cli",
            "cursor_session_id": "expected-sess",
            "cloud_agent_id": None,
            "cloud_run_id": None,
            "resume_attempts": 0,
            "resumed": False,
            "terminal_result": None,
        },
    )
    monkeypatch.setenv("FAKE_CURSOR_CONFLICT_SESSION_ID", "wrong-sess")
    result = json.loads(
        run_cursor_agent_cli_with_receipt(
            task="resume",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="sess-conflict",
            tool_call_id="call-conflict",
            resume_session_id="expected-sess",
            continuation=True,
            existing_receipt_path=receipt_path,
            existing_run_id="conflict",
            resume_attempt_increment=True,
        )
    )
    assert result["success"] is False
    assert "conflicting" in (result.get("error") or "").lower()


def test_resume_nonzero_is_terminal_no_loop(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CURSOR_MODE", "nonzero")
    monkeypatch.setenv("FAKE_CURSOR_SESSION_ID", "nz-sess")
    receipt_path = cursor_runs_dir() / "nz.receipt.json"
    from tools.cursor_run_receipts import _atomic_write_json

    _atomic_write_json(
        receipt_path,
        {
            "schema_version": 1,
            "run_id": "nz",
            "attempt_id": "a1",
            "hermes_session_id": "sess-nz",
            "tool_call_id": "call-nz",
            "workdir": str(tmp_path),
            "prompt_hash": "sha256:x",
            "state": "running",
            "outcome": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "log_path": str(tmp_path / "x.jsonl"),
            "owner_pid": os.getpid(),
            "owner_boot_id": "test",
            "model": None,
            "force": True,
            "timeout_seconds": 0,
            "execution_mode": "cli",
            "cursor_session_id": "nz-sess",
            "cloud_agent_id": None,
            "cloud_run_id": None,
            "resume_attempts": 0,
            "resumed": False,
            "terminal_result": None,
        },
    )
    history, _note = recover_delegate_cursor_agent_history(
        _dangling_history("call-nz", str(tmp_path)),
        hermes_session_id="sess-nz",
    )
    assert history[-1]["role"] == "tool"
    receipt = read_receipt(receipt_path)
    assert receipt["state"] == "terminal"
    with patch("tools.cursor_agent_tool.run_cursor_agent_cli_with_receipt") as resume_mock:
        recover_delegate_cursor_agent_history(
            _dangling_history("call-nz", str(tmp_path)),
            hermes_session_id="sess-nz",
        )
        resume_mock.assert_not_called()


def test_handle_function_call_forwards_tool_call_id_to_receipt(monkeypatch, tmp_path):
    from model_tools import handle_function_call

    captured: dict[str, str | None] = {}

    def _fake_delegate(**kwargs):
        captured["task_id"] = kwargs.get("task_id")
        captured["tool_call_id"] = kwargs.get("tool_call_id")
        return json.dumps({"success": True})

    monkeypatch.setattr("tools.cursor_agent_tool.delegate_cursor_agent", _fake_delegate)

    handle_function_call(
        "delegate_cursor_agent",
        {"task": "x", "workdir": str(tmp_path.resolve())},
        task_id="hermes-sess-1",
        tool_call_id="call-xyz",
    )

    assert captured["task_id"] == "hermes-sess-1"
    assert captured["tool_call_id"] == "call-xyz"


def test_cloud_delegate_finalizes_terminal_receipt(monkeypatch, tmp_path):
    from tests.tools.test_cursor_agent_tool import _install_cloud_happy_path
    from tools import cursor_agent_tool

    _install_cloud_happy_path(monkeypatch, tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="cloud task",
            workdir=str(workdir.resolve()),
            task_id="cloud-session",
            tool_call_id="cloud-call",
        )
    )

    assert result["success"] is True
    receipts = list(cursor_runs_dir().glob("*.receipt.json"))
    assert len(receipts) == 1
    receipt = read_receipt(receipts[0])
    assert receipt["state"] == "terminal"
    assert receipt["outcome"] == "success"
    assert receipt["tool_call_id"] == "cloud-call"
    assert receipt["cloud_agent_id"]
    assert receipt["cloud_run_id"]
    assert isinstance(receipt.get("terminal_result"), dict)
