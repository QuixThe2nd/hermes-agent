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

    def read1(self, size: int = -1) -> bytes:
        chunk = self._stream.read1(4096 if size < 0 else size)
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
            "call_id": "call-same-1",
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


def test_parse_dedupes_duplicate_delegations_without_identity_keys():
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
    parsed = parse_cursor_agent_log("\n".join([duplicate, duplicate]))
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


def test_parse_distinct_call_ids_same_args_produce_two_records():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    def _tool_call(call_id: str) -> str:
        return json.dumps(
            {
                "type": "tool_call",
                "call_id": call_id,
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

    log = "\n".join([_tool_call("call-a"), _tool_call("call-b")])
    parsed = parse_cursor_agent_log(log)
    assert len(parsed["delegations"]) == 2
    assert parsed["delegations"][0] == parsed["delegations"][1]


def test_parse_started_and_completed_same_call_id_one_record():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    started = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-xyz",
            "subtype": "started",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Review module",
                        "subagentType": "explore",
                        "model": "kimi-k3-high",
                    }
                }
            },
        }
    )
    completed = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-xyz",
            "subtype": "completed",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Review module",
                        "subagentType": "explore",
                        "model": "kimi-k3-high",
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([started, completed]))
    assert len(parsed["delegations"]) == 1


def test_parse_camel_and_snake_subagent_type_keys():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    camel = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-camel",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Camel case",
                        "subagentType": "explore",
                        "model": "m1",
                    }
                }
            },
        }
    )
    snake = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-snake",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Snake case",
                        "subagent_type": "implementer",
                        "model": "m2",
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([camel, snake]))
    assert len(parsed["delegations"]) == 2
    assert parsed["delegations"][0]["subagent_type"] == "explore"
    assert parsed["delegations"][1]["subagent_type"] == "implementer"


def test_action_required_plain_text_line():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    line = (
        "ActionRequiredError: Named models unavailable Free plans can only use Auto. "
        "Switch to Auto or upgrade plans to continue."
    )
    parsed = parse_cursor_agent_log(line)
    assert parsed["action_required"] is not None
    assert "Named models unavailable" in parsed["action_required"]["detail"]


def test_action_required_json_string_line():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    line = (
        "ActionRequiredError: Named models unavailable Free plans can only use Auto. "
        "Switch to Auto or upgrade plans to continue."
    )
    parsed = parse_cursor_agent_log(json.dumps(line))
    assert parsed["action_required"] is not None
    assert "Named models unavailable" in parsed["action_required"]["detail"]


def test_action_required_json_string_prose_mention_not_triggered():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    line = json.dumps("We saw an ActionRequiredError in docs but recovered.")
    parsed = parse_cursor_agent_log(line)
    assert parsed["action_required"] is None


def test_action_required_malformed_lines_do_not_crash():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    log = "\n".join(['{"broken json', "random garbage", "Error: something else"])
    parsed = parse_cursor_agent_log(log)
    assert parsed["action_required"] is None


def _task_call_event(**extra_fields) -> str:
    base = {
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
    base.update(extra_fields)
    return json.dumps(base)


def test_parse_dedupes_dict_valued_call_id():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    event = _task_call_event(call_id={"a": 1, "b": 2})
    parsed = parse_cursor_agent_log("\n".join([event, event]))
    assert len(parsed["delegations"]) == 1


def test_parse_dedupes_list_valued_call_id():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    event = _task_call_event(call_id=["x", 1])
    parsed = parse_cursor_agent_log("\n".join([event, event]))
    assert len(parsed["delegations"]) == 1


def test_parse_dedupes_dict_valued_call_id_key_order_stable():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    first = _task_call_event(call_id={"b": 2, "a": 1})
    second = _task_call_event(call_id={"a": 1, "b": 2})
    parsed = parse_cursor_agent_log("\n".join([first, second]))
    assert len(parsed["delegations"]) == 1


def test_parse_dedupes_nested_dict_list_identity_keys():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    tool_call_id = json.dumps(
        {
            "type": "tool_call",
            "toolCallId": {"id": "nested", "seq": [1, 2]},
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Nested id",
                        "subagentType": "explore",
                        "model": "m1",
                    }
                }
            },
        }
    )
    agent_id = json.dumps(
        {
            "type": "tool_call",
            "agentId": ["agent", 42],
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Agent id list",
                        "subagentType": "explore",
                        "model": "m2",
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([tool_call_id, tool_call_id, agent_id, agent_id]))
    assert len(parsed["delegations"]) == 2


def test_parse_dedupes_dict_list_content_fallback():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    duplicate = json.dumps(
        {
            "type": "tool_call",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": {"goal": "review", "scope": ["auth"]},
                        "subagentType": "explore",
                        "model": ["kimi-k3-high"],
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([duplicate, duplicate]))
    assert len(parsed["delegations"]) == 1


def test_parse_non_hashable_values_do_not_crash():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    samples = [
        {
            "type": "tool_call",
            "call_id": {"b": 2, "a": 1},
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "x",
                        "model": "m",
                    }
                }
            },
        },
        {
            "type": "tool_call",
            "call_id": ["x", 1],
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": {"x": 1},
                        "model": ["m"],
                    }
                }
            },
        },
    ]
    for event in samples:
        parsed = parse_cursor_agent_log(json.dumps(event))
        assert len(parsed["delegations"]) == 1


def test_parse_distinct_scalar_call_ids():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    def _event(call_id):
        return json.dumps(
            {
                "type": "tool_call",
                "call_id": call_id,
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "scalar id",
                            "subagentType": "explore",
                            "model": "m",
                        }
                    }
                },
            }
        )

    log = "\n".join([_event(True), _event(1), _event(False), _event(0), _event(1.0)])
    parsed = parse_cursor_agent_log(log)
    assert len(parsed["delegations"]) == 5


def test_parse_distinct_nested_scalar_call_ids():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    def _event(call_id):
        return json.dumps(
            {
                "type": "tool_call",
                "call_id": call_id,
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "nested scalar id",
                            "subagentType": "explore",
                            "model": "m",
                        }
                    }
                },
            }
        )

    log = "\n".join(
        [
            _event({"flag": True}),
            _event({"flag": 1}),
            _event({"flag": False}),
            _event({"flag": 0}),
            _event({"value": 1}),
            _event({"value": 1.0}),
        ]
    )
    parsed = parse_cursor_agent_log(log)
    assert len(parsed["delegations"]) == 6


def _kill_process_group_or_pid(pgid: int | None, pid: int | None) -> None:
    import os
    import signal

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    if pid is not None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


@pytest.mark.live_system_guard_bypass
def test_incremental_stdout_updates_log_before_child_exit(monkeypatch, tmp_path):
    import os
    import subprocess
    import sys

    from tools import cursor_agent_tool

    monkeypatch.setattr(cursor_agent_tool, "STALL_WATCHDOG_SECONDS", 5)
    monkeypatch.setattr(cursor_agent_tool, "_MONITOR_POLL_SECONDS", 0.05)

    spawn_info: dict[str, int | None] = {"pid": None, "pgid": None}
    real_popen = subprocess.Popen

    def _capturing_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawn_info["pid"] = proc.pid
        try:
            spawn_info["pgid"] = os.getpgid(proc.pid)
        except (OSError, ProcessLookupError):
            spawn_info["pgid"] = None
        return proc

    monkeypatch.setattr(cursor_agent_tool.subprocess, "Popen", _capturing_popen)

    child_script = (
        "import sys, time\n"
        "sys.stdout.write('chunk1\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(1.25)\n"
        "sys.stdout.write('chunk2\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.1)\n"
    )
    cmd = [sys.executable, "-c", child_script]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    result_holder: dict = {}

    def run() -> None:
        result_holder["result"] = cursor_agent_tool._run_and_stream(
            cmd,
            workdir=str(tmp_path),
            timeout_seconds=60,
            log_dir=log_dir,
            run_timestamp="test",
        )

    thread = threading.Thread(target=run)
    thread.start()

    child_pid: int | None = None
    pgid: int | None = None
    try:
        found_chunk = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            logs = list(log_dir.glob("*.jsonl"))
            if logs:
                name_parts = logs[0].stem.rsplit("-", 1)
                if child_pid is None and len(name_parts) == 2 and name_parts[1].isdigit():
                    child_pid = int(name_parts[1])
                    try:
                        pgid = os.getpgid(child_pid)
                    except (OSError, ProcessLookupError):
                        pgid = None
                if "chunk1" in logs[0].read_text(encoding="utf-8", errors="replace"):
                    found_chunk = True
                    assert thread.is_alive(), "run finished before chunk1 reached the log"
                    assert "result" not in result_holder, "run finished before chunk1 reached the log"
                    if child_pid is not None:
                        os.kill(child_pid, 0)
                    break
            time.sleep(0.05)

        thread.join(timeout=10)
        assert "result" in result_holder
        error_code, _log_path, log_text, _duration, returncode = result_holder["result"]

        assert found_chunk
        if child_pid is not None:
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
        assert error_code != "stalled"
        assert "chunk1" in log_text
        assert "chunk2" in log_text
        assert returncode == 0
    finally:
        cleanup_pgid = pgid if pgid is not None else spawn_info.get("pgid")
        cleanup_pid = child_pid if child_pid is not None else spawn_info.get("pid")
        _kill_process_group_or_pid(
            cleanup_pgid if isinstance(cleanup_pgid, int) else None,
            cleanup_pid if isinstance(cleanup_pid, int) else None,
        )
        thread.join(timeout=10)


@pytest.mark.live_system_guard_bypass
def test_terminate_process_kills_sigterm_resistant_descendant(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    import sys

    from tools.cursor_agent_tool import _terminate_process

    monkeypatch.setattr("tools.cursor_agent_tool._TERMINATE_GRACE_SECONDS", 0.5)

    desc_pid_file = tmp_path / "desc.pid"
    leader_script = f"""
import signal
import subprocess
import sys
import time
from pathlib import Path

desc_script = '''
import signal
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(30)
'''

desc = subprocess.Popen([sys.executable, "-c", desc_script])
Path({str(desc_pid_file)!r}).write_text(str(desc.pid))
time.sleep(30)
"""

    proc = subprocess.Popen(
        [sys.executable, "-c", leader_script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        pgid = None

    desc_pid: int | None = None
    try:
        for _ in range(100):
            if desc_pid_file.is_file():
                desc_pid = int(desc_pid_file.read_text(encoding="utf-8").strip())
                break
            time.sleep(0.05)
        assert desc_pid is not None

        _terminate_process(proc, pgid)

        assert proc.poll() is not None
        assert proc.returncode == -signal.SIGTERM

        with pytest.raises(ProcessLookupError):
            os.kill(desc_pid, 0)
    finally:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except (OSError, ProcessLookupError):
                        pass
        if desc_pid is not None:
            try:
                os.kill(desc_pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
def test_child_env_guarantees_home_and_local_bin(monkeypatch, tmp_path):
    import os

    from tools import cursor_agent_tool

    captured = {}

    class _EnvCapturePopen(_FakePopen):
        def __init__(self, cmd, *, cwd=None, stdout=None, stderr=None, env=None, **kwargs):
            super().__init__(cmd, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)
            captured["env"] = env
            self.stdout = _FakeStdoutWithEof(b"")

        def poll(self):
            if self.stdout.eof_reached.is_set():
                return 0
            return None

    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr("tools.cursor_agent_tool.subprocess.Popen", _EnvCapturePopen)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="x",
            workdir=str(workdir.resolve()),
        )
    )

    assert result["success"] is True
    env = captured["env"]
    assert env is not None
    # HOME must be present and non-empty even though the caller env lacked it;
    # the agent wrapper runs with `set -u` and dies on unbound $HOME.
    assert env["HOME"] == str(Path.home())
    # ~/.local/bin is prepended so binary resolution works under minimal PATH.
    assert env["PATH"].split(os.pathsep)[0] == str(Path.home() / ".local" / "bin")
