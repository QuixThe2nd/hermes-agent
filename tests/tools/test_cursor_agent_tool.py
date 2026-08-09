"""Behavior-contract tests for delegate_cursor_agent."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest


SAMPLE_STREAM_JSON = "\n".join(
    [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "sess-abc-123",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "Review auth module",
                            "subagentType": "explore",
                            "model": "kimi-k3-high",
                        }
                    }
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Partial progress update."}]
                },
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "Implement fix",
                            "subagent_type": "implementer",
                            "model": "composer-2.5-fast",
                        }
                    }
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Final implementation report."}]
                },
            }
        ),
    ]
)


class _FakeStdoutWithEof:
    def __init__(self, data: bytes = b""):
        self._stream = io.BytesIO(data)
        self.eof_reached = threading.Event()

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(4096 if size < 0 else size)
        if not chunk:
            self.eof_reached.set()
        return chunk

    def close(self):
        self._stream.close()


class _FakePopen:
    instances: list["_FakePopen"] = []

    def __init__(
        self,
        cmd,
        *,
        cwd=None,
        stdout=None,
        stderr=None,
        stdin=None,
        start_new_session=False,
        **kwargs,
    ):
        del stderr, stdin, start_new_session, kwargs
        self.cmd = cmd
        self.cwd = cwd
        self.stdout = stdout
        self._returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.pid = 4242 + len(_FakePopen.instances)
        _FakePopen.instances.append(self)

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def kill(self):
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        del timeout
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def set_exit(self, code: int) -> None:
        self._returncode = code


class _StreamingFakePopen(_FakePopen):
    """Popen that exposes canned stream-json on stdout and exits after EOF."""

    def __init__(self, cmd, *, cwd=None, stdout=None, stderr=None, **kwargs):
        super().__init__(cmd, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)
        self.stdout = _FakeStdoutWithEof(SAMPLE_STREAM_JSON.encode("utf-8"))
        self._returncode = None

    def poll(self):
        if self._returncode is not None:
            return self._returncode
        if isinstance(self.stdout, _FakeStdoutWithEof) and self.stdout.eof_reached.is_set():
            self._returncode = 0
        return self._returncode


class _StalledFakePopen(_FakePopen):
    """Never emits stdout bytes and never exits until terminated."""

    def __init__(self, cmd, *, cwd=None, stdout=None, stderr=None, **kwargs):
        super().__init__(cmd, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)
        self.stdout = _FakeStdoutWithEof(b"")
        self._returncode = None

    def poll(self):
        return None


class _TimeoutFakePopen(_StalledFakePopen):
    pass


class _NonZeroExitPopen(_StreamingFakePopen):
    def poll(self):
        if self._returncode is not None:
            return self._returncode
        if isinstance(self.stdout, _FakeStdoutWithEof) and self.stdout.eof_reached.is_set():
            self._returncode = 1
        return self._returncode


@pytest.fixture(autouse=True)
def _reset_fake_popen():
    _FakePopen.instances.clear()
    yield
    _FakePopen.instances.clear()


def test_schema_registration():
    import tools.cursor_agent_tool  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("delegate_cursor_agent")
    assert entry is not None
    assert entry.toolset == "delegation"
    assert entry.max_result_size_chars == 100_000

    schema = entry.schema
    required = set(schema["parameters"]["required"])
    assert required == {"task", "workdir"}

    props = schema["parameters"]["properties"]
    assert props["model"]["default"] == "kimi-k3-high"
    assert props["timeout_seconds"]["default"] == 900
    assert props["force"]["default"] is True


def test_check_fn_binary_found(monkeypatch):
    from tools.cursor_agent_tool import check_cursor_agent_requirements

    monkeypatch.setattr("tools.cursor_agent_tool.shutil.which", lambda name: "/usr/bin/agent")
    assert check_cursor_agent_requirements() is True


def test_check_fn_binary_missing(monkeypatch):
    from tools.cursor_agent_tool import check_cursor_agent_requirements

    monkeypatch.setattr("tools.cursor_agent_tool.shutil.which", lambda name: None)

    class _MissingPath(Path):
        def is_file(self):
            return False

    monkeypatch.setattr("tools.cursor_agent_tool._local_bin_agent_path", lambda: _MissingPath("/nope/agent"))
    assert check_cursor_agent_requirements() is False


def test_clamp_timeout_seconds():
    from tools.cursor_agent_tool import (
        DEFAULT_TIMEOUT_SECONDS,
        MAX_TIMEOUT_SECONDS,
        MIN_TIMEOUT_SECONDS,
        _clamp_timeout_seconds,
    )

    assert _clamp_timeout_seconds(59) == MIN_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(1801) == MAX_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds("garbage") == DEFAULT_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(None) == DEFAULT_TIMEOUT_SECONDS


def test_parse_stream_json_log():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    parsed = parse_cursor_agent_log(SAMPLE_STREAM_JSON)

    assert parsed["session_id"] == "sess-abc-123"
    assert parsed["final_report"] == "Final implementation report."
    assert len(parsed["delegations"]) == 2
    assert parsed["delegations"][0] == {
        "description": "Review auth module",
        "subagent_type": "explore",
        "model": "kimi-k3-high",
    }
    assert parsed["delegations"][1] == {
        "description": "Implement fix",
        "subagent_type": "implementer",
        "model": "composer-2.5-fast",
    }


def test_parse_dedupes_duplicate_delegations():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    duplicate = json.dumps(
        {
            "type": "tool_call",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Same task",
                        "subagentType": "explore",
                        "model": "kimi-k3-high",
                    }
                }
            },
        }
    )
    log = "\n".join([duplicate, duplicate])
    parsed = parse_cursor_agent_log(log)
    assert len(parsed["delegations"]) == 1


def test_action_required_structured_event_only():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    log_line = json.dumps(
        {
            "type": "error",
            "error_type": "ActionRequiredError",
            "message": "Approve file write",
        }
    )
    parsed = parse_cursor_agent_log(log_line + "\n")
    assert parsed["action_required"] is not None
    assert "Approve file write" in parsed["action_required"]["detail"]


def test_action_required_not_triggered_by_assistant_mention():
    from tools import cursor_agent_tool

    log_text = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "sess-1",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "We saw an ActionRequiredError in docs but recovered.",
                            }
                        ]
                    },
                }
            ),
        ]
    )

    parsed = cursor_agent_tool.parse_cursor_agent_log(log_text)
    assert parsed["action_required"] is None


def test_action_required_error_handler(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    log_text = json.dumps(
        {
            "type": "error",
            "error_type": "ActionRequiredError",
            "message": "Needs approval",
        }
    )

    class _ActionRequiredPopen(_FakePopen):
        def __init__(self, cmd, *, cwd=None, stdout=None, stderr=None, **kwargs):
            super().__init__(cmd, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)
            self.stdout = _FakeStdoutWithEof((log_text + "\n").encode("utf-8"))

        def poll(self):
            if self.stdout.eof_reached.is_set():
                return 0
            return None

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _ActionRequiredPopen)
    monkeypatch.setattr(cursor_agent_tool, "STALL_WATCHDOG_SECONDS", 60)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="do something",
            workdir=str(workdir.resolve()),
        )
    )

    assert result["success"] is False
    assert result["error"] == "action_required"
    assert result["error_type"] == "ActionRequiredError"
    assert "Needs approval" in result["detail"]


def test_validation_errors_use_full_result_shape(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")

    empty = json.loads(cursor_agent_tool.delegate_cursor_agent(task="", workdir=str(tmp_path)))
    assert empty["success"] is False
    assert empty["error"]
    assert empty["log_path"] is None
    assert "final_report" in empty
    assert "delegations" in empty

    relative = json.loads(
        cursor_agent_tool.delegate_cursor_agent(task="x", workdir="relative/path")
    )
    assert relative["success"] is False
    assert "absolute path" in relative["error"]

    missing = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="x",
            workdir=str((tmp_path / "missing").resolve()),
        )
    )
    assert missing["success"] is False
    assert "does not exist" in missing["error"]


def test_stall_path_terminates_process(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _StalledFakePopen)
    monkeypatch.setattr(cursor_agent_tool, "STALL_WATCHDOG_SECONDS", 0.01)
    monkeypatch.setattr(cursor_agent_tool, "_MONITOR_POLL_SECONDS", 0.005)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="stall test",
            workdir=str(workdir.resolve()),
            timeout_seconds=900,
        )
    )

    assert result["success"] is False
    assert result["error"] == "stalled"
    assert _FakePopen.instances
    proc = _FakePopen.instances[0]
    assert proc.terminated or proc.killed


def test_timeout_path_terminates_process(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    start = time.monotonic()
    calls = {"n": 0}

    def _fake_monotonic():
        calls["n"] += 1
        return start + calls["n"] * 40

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _TimeoutFakePopen)
    monkeypatch.setattr("tools.cursor_agent_tool.time.monotonic", _fake_monotonic)
    monkeypatch.setattr(cursor_agent_tool, "STALL_WATCHDOG_SECONDS", 9999)
    monkeypatch.setattr(cursor_agent_tool, "_MONITOR_POLL_SECONDS", 0.001)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="timeout test",
            workdir=str(workdir.resolve()),
            timeout_seconds=60,
        )
    )

    assert result["success"] is False
    assert result["error"] == "timeout"
    assert _FakePopen.instances
    proc = _FakePopen.instances[0]
    assert proc.terminated or proc.killed


def test_interrupt_path_terminates_process(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _StalledFakePopen)
    monkeypatch.setattr(cursor_agent_tool, "STALL_WATCHDOG_SECONDS", 9999)
    monkeypatch.setattr(cursor_agent_tool, "_MONITOR_POLL_SECONDS", 0.001)
    monkeypatch.setattr("tools.cursor_agent_tool._check_interrupted", lambda: True)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="interrupt test",
            workdir=str(workdir.resolve()),
        )
    )

    assert result["success"] is False
    assert result["error"] == "interrupted"
    proc = _FakePopen.instances[0]
    assert proc.terminated or proc.killed


def test_non_zero_exit_reports_failure(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _NonZeroExitPopen)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="fail test",
            workdir=str(workdir.resolve()),
        )
    )

    assert result["success"] is False
    assert "exited with code 1" in result["error"]


def test_happy_path_e2e(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _StreamingFakePopen)

    workdir = tmp_path / "repo"
    workdir.mkdir()

    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="implement feature",
            workdir=str(workdir.resolve()),
        )
    )

    assert result["success"] is True
    assert result["final_report"] == "Final implementation report."
    assert len(result["delegations"]) == 2
    assert result["session_id"] == "sess-abc-123"
    assert result["error"] is None
    assert "cursor-runs" in result["log_path"]
    assert Path(result["log_path"]).is_file()

    proc = _FakePopen.instances[0]
    assert proc.cmd == [
        "/usr/bin/agent",
        "-p",
        "--trust",
        "--force",
        "--model",
        "kimi-k3-high",
        "--output-format",
        "stream-json",
        "implement feature",
    ]
    assert proc.cwd == str(workdir.resolve())


@pytest.mark.parametrize(
    "force_value,expect_force_flag",
    [
        (False, False),
        ("false", False),
        ("0", False),
    ],
)
def test_force_coercion_omits_flag(monkeypatch, tmp_path, force_value, expect_force_flag):
    from tools import cursor_agent_tool

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _StreamingFakePopen)

    workdir = tmp_path / "repo"
    workdir.mkdir()

    cursor_agent_tool.delegate_cursor_agent(
        task="no force",
        workdir=str(workdir.resolve()),
        force=force_value,
    )

    proc = _FakePopen.instances[0]
    has_force = "--force" in proc.cmd
    assert has_force is expect_force_flag
    if expect_force_flag:
        assert proc.cmd.index("--force") == proc.cmd.index("--trust") + 1


def test_handler_force_string_false(monkeypatch, tmp_path):
    from tools.cursor_agent_tool import _handle_delegate_cursor_agent

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _StreamingFakePopen)

    workdir = tmp_path / "repo"
    workdir.mkdir()

    _handle_delegate_cursor_agent(
        {
            "task": "handler force",
            "workdir": str(workdir.resolve()),
            "force": "false",
        }
    )

    proc = _FakePopen.instances[0]
    assert "--force" not in proc.cmd
