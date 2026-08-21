"""Gateway integration for delegate_cursor_agent restart recovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.run import _build_gateway_agent_history
from tools.cursor_run_receipts import read_receipt
from tools.cursor_agent_tool import run_cursor_agent_cli_with_receipt


@pytest.fixture
def fake_agent_script() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "tests" / "tools" / "fixtures" / "fake_cursor_agent.py"


@pytest.fixture
def fake_binary(fake_agent_script, monkeypatch):
    monkeypatch.setattr(
        "tools.cursor_agent_tool.resolve_cursor_agent_binary",
        lambda: str(fake_agent_script),
    )
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.05)
    return str(fake_agent_script)


def _history(tool_call_id: str, workdir: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "delegate_cursor_agent",
                        "arguments": json.dumps({"task": "gw", "workdir": workdir}),
                    },
                }
            ],
        }
    ]


def test_build_gateway_history_injects_recovered_tool_result(fake_binary, tmp_path):
    json.loads(
        run_cursor_agent_cli_with_receipt(
            task="gw",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="gw-session",
            tool_call_id="gw-call",
        )
    )
    agent_history, _observed, note = _build_gateway_agent_history(
        _history("gw-call", str(tmp_path)),
        hermes_session_id="gw-session",
    )
    assert note and "terminal" in note.lower()
    assert agent_history[-1]["role"] == "tool"
    assert agent_history[-2]["role"] == "assistant"


def test_build_gateway_history_resume_note_includes_recovery_context(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CURSOR_SESSION_ID", "gw-resume")
    from tools.cursor_run_receipts import _atomic_write_json, cursor_runs_dir

    receipt_path = cursor_runs_dir() / "gw.receipt.json"
    _atomic_write_json(
        receipt_path,
        {
            "schema_version": 1,
            "run_id": "gw",
            "attempt_id": "a1",
            "hermes_session_id": "gw-resume-session",
            "tool_call_id": "gw-resume-call",
            "workdir": str(tmp_path),
            "prompt_hash": "sha256:x",
            "state": "running",
            "outcome": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "log_path": str(tmp_path / "partial.jsonl"),
            "owner_pid": 1,
            "owner_boot_id": "test",
            "model": None,
            "force": True,
            "timeout_seconds": 0,
            "execution_mode": "cli",
            "cursor_session_id": "gw-resume",
            "cloud_agent_id": None,
            "cloud_run_id": None,
            "resume_attempts": 0,
            "resumed": False,
            "terminal_result": None,
        },
    )
    agent_history, _observed, note = _build_gateway_agent_history(
        _history("gw-resume-call", str(tmp_path)),
        hermes_session_id="gw-resume-session",
    )
    assert agent_history[-1]["role"] == "tool"
    assert note and "Automatically resumed" in note


def test_build_gateway_history_skips_resume_for_unrelated_tool_call(fake_binary, tmp_path):
    json.loads(
        run_cursor_agent_cli_with_receipt(
            task="gw",
            workdir=str(tmp_path),
            model=None,
            timeout_seconds=0,
            force=True,
            hermes_session_id="gw-session",
            tool_call_id="real-call",
        )
    )
    agent_history, _observed, note = _build_gateway_agent_history(
        _history("other-call", str(tmp_path)),
        hermes_session_id="gw-session",
    )
    assert agent_history[-1]["role"] == "assistant"
    assert note is None or "no matching" in note
