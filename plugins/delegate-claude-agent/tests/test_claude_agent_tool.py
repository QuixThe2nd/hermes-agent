"""Behavior-contract tests for delegate_claude_agent."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Reusable marker for tests that spawn a real (fake-binary) subprocess and
# signal it. The conftest live-system guard allows signals inside the test's
# own process subtree, so these do not need to bypass it — but we keep the
# marker for clarity and in case a stricter guard is added later.
_REAL_SUBPROC = pytest.mark.live_system_guard_bypass


def _write_fake_binary(tmp_path: Path) -> Path:
    """Write a fake claude-glm binary driven by FAKE_CLAUDE_* env vars."""
    script = f"""#!{sys.executable}
import json
import os
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "success")

argv_out = os.environ.get("FAKE_CLAUDE_ARGV_OUT")
if argv_out:
    with open(argv_out, "w", encoding="utf-8") as fh:
        json.dump(sys.argv, fh)

env_out = os.environ.get("FAKE_CLAUDE_ENV_OUT")
if env_out:
    with open(env_out, "w", encoding="utf-8") as fh:
        json.dump({{"HOME": os.environ.get("HOME"), "PATH": os.environ.get("PATH", "")}}, fh)

if mode == "sleep":
    try:
        time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "30")))
    except Exception:
        pass
    sys.exit(0)

if mode == "sleep_then_result":
    try:
        time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0.15")))
    except Exception:
        pass
    # Fall through to emit the normal result event after the silent period.

if mode == "garbage":
    sys.stdout.write("not json line 1\\nnot json line 2\\n")
    sys.stdout.flush()
    sys.exit(0)

if mode == "empty":
    sys.exit(0)

if mode == "exitnonzero":
    sys.stdout.write('{{"type":"result","subtype":"success","is_error":false,"result":"x"}}\\n')
    sys.stdout.flush()
    sys.exit(2)

if mode == "flood":
    noise_line = '{{"type":"assistant","message":"noise"}}\\n'
    target = int(os.environ.get("FAKE_CLAUDE_FLOOD_BYTES", str(3 * 1024 * 1024)))
    written = 0
    while written < target:
        sys.stdout.write(noise_line)
        written += len(noise_line.encode("utf-8"))
    sys.stdout.flush()
    # Fall through to emit the normal result event after the flood.

if mode == "flood_giant_result":
    pad = "x" * int(os.environ.get("FAKE_CLAUDE_GIANT_PAD", str(2 * 1024 * 1024)))
    giant = {{
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": pad,
        "session_id": "sess-giant",
        "num_turns": 1,
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "modelUsage": {{}},
        "permission_denials": [],
    }}
    sys.stdout.write(json.dumps(giant) + "\\n")
    sys.stdout.flush()
    sys.exit(0)

if mode == "early_result_then_flood":
    early = {{
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "not terminal",
    }}
    sys.stdout.write(json.dumps(early) + "\\n")
    noise_line = '{{"type":"assistant","message":"trailing noise"}}\\n'
    target = int(os.environ.get("FAKE_CLAUDE_FLOOD_BYTES", str(3 * 1024 * 1024)))
    written = 0
    while written < target:
        sys.stdout.write(noise_line)
        written += len(noise_line.encode("utf-8"))
    sys.stdout.flush()
    sys.exit(0)

models_str = os.environ.get("FAKE_CLAUDE_MODELS", "glm-5.2")
model_usage = {{}}
for _name in models_str.split(","):
    _name = _name.strip()
    if _name:
        model_usage[_name] = {{"input_tokens": 100, "output_tokens": 50}}

result = {{
    "type": "result",
    "subtype": os.environ.get("FAKE_CLAUDE_SUBTYPE", "success"),
    "is_error": os.environ.get("FAKE_CLAUDE_IS_ERROR", "false").lower() == "true",
    "result": os.environ.get("FAKE_CLAUDE_RESULT_TEXT", "Done."),
    "session_id": os.environ.get("FAKE_CLAUDE_SESSION_ID", "sess-claude-1"),
    "num_turns": int(os.environ.get("FAKE_CLAUDE_NUM_TURNS", "3")),
    "duration_ms": int(os.environ.get("FAKE_CLAUDE_DURATION_MS", "12345")),
    "total_cost_usd": float(os.environ.get("FAKE_CLAUDE_COST", "0.0123")),
    "modelUsage": model_usage,
    "permission_denials": json.loads(os.environ.get("FAKE_CLAUDE_DENIALS", "[]")),
}}
sys.stdout.write(json.dumps(result) + "\\n")
sys.stdout.flush()
"""
    binary = tmp_path / "claude-glm"
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    return _write_fake_binary(tmp_path)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    return workdir


def _patch_binary(monkeypatch, binary: Path) -> None:
    monkeypatch.setattr(
        "delegate_claude_agent.tool.resolve_claude_binary",
        lambda: str(binary),
    )


# ---------------------------------------------------------------------------
# Schema + registration
# ---------------------------------------------------------------------------

def test_schema_registration():
    from delegate_claude_agent.tool import DELEGATE_CLAUDE_AGENT_SCHEMA

    schema = DELEGATE_CLAUDE_AGENT_SCHEMA
    required = set(schema["parameters"]["required"])
    assert required == {"task", "workdir"}

    props = schema["parameters"]["properties"]
    assert props["model"]["default"] == "glm-5.2"
    assert props["timeout_seconds"]["default"] == 1800
    assert props["allowed_tools"]["default"] == "Read,Write,Edit,Glob,Grep,Bash"
    assert props["permission_mode"]["default"] == "acceptEdits"
    assert set(props["permission_mode"]["enum"]) == {"acceptEdits", "plan"}


# ---------------------------------------------------------------------------
# Binary resolution + gating
# ---------------------------------------------------------------------------

def test_check_fn_binary_found(monkeypatch, fake_binary):
    from delegate_claude_agent.tool import check_claude_agent_requirements

    monkeypatch.setattr(
        "delegate_claude_agent.tool.resolve_claude_binary", lambda: str(fake_binary)
    )
    assert check_claude_agent_requirements() is True


def test_check_fn_binary_missing(monkeypatch):
    from delegate_claude_agent.tool import check_claude_agent_requirements

    monkeypatch.setattr("delegate_claude_agent.tool.resolve_claude_binary", lambda: None)
    assert check_claude_agent_requirements() is False


def test_resolve_env_override(monkeypatch, fake_binary, tmp_path):
    from delegate_claude_agent.tool import resolve_claude_binary

    monkeypatch.setenv("CLAUDE_GLM_BIN", str(fake_binary))
    assert resolve_claude_binary() == str(fake_binary)


def test_resolve_env_override_must_be_executable_file(monkeypatch, tmp_path):
    from delegate_claude_agent.tool import resolve_claude_binary

    bogus = tmp_path / "nope"
    monkeypatch.setenv("CLAUDE_GLM_BIN", str(bogus))
    # Override points at a non-existent file → fall through to the rest.
    monkeypatch.setattr("delegate_claude_agent.tool.shutil.which", lambda name: None)
    # A plain non-existent Path: is_file() is naturally False, exercising the
    # real branch without the _flavour breakage a Path subclass would cause.
    monkeypatch.setattr(
        "delegate_claude_agent.tool._local_bin_claude_glm_path",
        lambda: Path("/nope/claude-glm"),
    )
    assert resolve_claude_binary() is None


def test_resolve_local_bin(monkeypatch, fake_binary):
    from delegate_claude_agent.tool import resolve_claude_binary

    monkeypatch.delenv("CLAUDE_GLM_BIN", raising=False)
    monkeypatch.setattr("delegate_claude_agent.tool.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "delegate_claude_agent.tool._local_bin_claude_glm_path", lambda: fake_binary
    )
    assert resolve_claude_binary() == str(fake_binary)


def test_resolve_path_fallbacks(monkeypatch, tmp_path):
    from delegate_claude_agent.tool import resolve_claude_binary

    monkeypatch.delenv("CLAUDE_GLM_BIN", raising=False)
    monkeypatch.setattr(
        "delegate_claude_agent.tool._local_bin_claude_glm_path",
        lambda: Path("/nope/claude-glm"),
    )
    monkeypatch.setattr(
        "delegate_claude_agent.tool.shutil.which",
        lambda name: "/usr/bin/claude-glm" if name == "claude-glm" else None,
    )
    assert resolve_claude_binary() == "/usr/bin/claude-glm"

    # Bare `claude` on PATH must NOT resolve — GLM wrapper only.
    monkeypatch.setattr(
        "delegate_claude_agent.tool.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    assert resolve_claude_binary() is None

    # Nothing resolvable → None.
    monkeypatch.setattr("delegate_claude_agent.tool.shutil.which", lambda name: None)
    assert resolve_claude_binary() is None


# ---------------------------------------------------------------------------
# Timeout clamping
# ---------------------------------------------------------------------------

def test_clamp_timeout_seconds():
    from delegate_claude_agent.tool import (
        DEFAULT_TIMEOUT_SECONDS,
        MAX_TIMEOUT_SECONDS,
        MIN_TIMEOUT_SECONDS,
        _clamp_timeout_seconds,
    )

    assert _clamp_timeout_seconds(59) == MIN_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(3601) == MAX_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds("garbage") == DEFAULT_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(None) == DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def test_parse_result_extracts_fields():
    from delegate_claude_agent.tool import parse_claude_agent_log

    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "All done.",
            "session_id": "sess-1",
            "num_turns": 4,
            "duration_ms": 999,
            "total_cost_usd": 0.05,
            "modelUsage": {
                "glm-5.2": {"input_tokens": 10},
                "claude-haiku-4-5": {"input_tokens": 5},
            },
            "permission_denials": [{"tool": "Bash"}],
        }
    )
    parsed = parse_claude_agent_log(line + "\n")
    assert parsed["subtype"] == "success"
    assert parsed["is_error"] is False
    assert parsed["result"] == "All done."
    assert parsed["session_id"] == "sess-1"
    assert parsed["num_turns"] == 4
    assert parsed["duration_ms"] == 999
    assert parsed["total_cost_usd"] == 0.05
    assert parsed["models_used"] == ["claude-haiku-4-5", "glm-5.2"]
    assert parsed["permission_denials"] == [{"tool": "Bash"}]


def test_parse_last_result_line_wins():
    from delegate_claude_agent.tool import parse_claude_agent_log

    first = json.dumps({"type": "result", "subtype": "success", "result": "old"})
    second = json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "new"})
    parsed = parse_claude_agent_log("\n".join([first, second]))
    assert parsed["result"] == "new"
    assert parsed["subtype"] == "error"


def test_parse_no_result_event_returns_empty():
    from delegate_claude_agent.tool import parse_claude_agent_log

    assert parse_claude_agent_log("not json\n") == {}
    assert parse_claude_agent_log("") == {}
    assert parse_claude_agent_log('{"type":"assistant","message":{}}') == {}


def test_parse_adversarial_metadata_normalization():
    from delegate_claude_agent.tool import parse_claude_agent_log

    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": {"nested": "report"},
            "session_id": {"bad": "id"},
            "num_turns": [1, 2],
            "duration_ms": "123",
            "total_cost_usd": {"usd": 1},
            "modelUsage": {"glm-5.2": {}},
            "permission_denials": "denied",
        }
    )
    parsed = parse_claude_agent_log(line)
    assert parsed["result"] == '{"nested": "report"}'
    assert parsed["session_id"] is None
    assert parsed["num_turns"] is None
    assert parsed["duration_ms"] is None
    assert parsed["total_cost_usd"] is None
    assert parsed["models_used"] == ["glm-5.2"]
    assert parsed["permission_denials"] == []


@pytest.mark.parametrize(
    "field_overrides,expected",
    [
        ({"num_turns": True}, {"num_turns": None, "success": True}),
        ({"total_cost_usd": float("nan")}, {"total_cost_usd": None, "success": True}),
        ({"total_cost_usd": float("inf")}, {"total_cost_usd": None, "success": True}),
        ({"is_error": "false"}, {"success": False}),
        ({"subtype": {"not": "string"}}, {"success": False}),
    ],
)
def test_parse_adversarial_fail_closed_success(
    field_overrides, expected, monkeypatch, repo, fake_binary
):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "session_id": "sess-1",
        "num_turns": 3,
        "duration_ms": 100,
        "total_cost_usd": 0.01,
        "modelUsage": {},
        "permission_denials": [],
    }
    event.update(field_overrides)

    def _fake_run(*_args, **_kwargs):
        log_text = json.dumps(event) + "\n"
        return None, "/tmp/fake.jsonl", log_text, log_text, 1.0, 0, False, 0

    monkeypatch.setattr(claude_agent_tool, "_run_and_stream", _fake_run)
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is expected["success"]
    if "num_turns" in expected:
        assert result["num_turns"] is expected["num_turns"]
    if "total_cost_usd" in expected:
        assert result["cost_usd"] is expected["total_cost_usd"]


def test_parse_coerces_integral_float_num_turns():
    from delegate_claude_agent.tool import parse_claude_agent_log

    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "num_turns": 3.0,
        }
    )
    parsed = parse_claude_agent_log(line)
    assert parsed["num_turns"] == 3


# ---------------------------------------------------------------------------
# Validation paths (no subprocess spawned)
# ---------------------------------------------------------------------------

def test_validation_errors_use_full_result_shape(monkeypatch, tmp_path, fake_binary):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)

    empty = json.loads(claude_agent_tool.delegate_claude_agent(task="", workdir=str(tmp_path)))
    assert empty["success"] is False
    assert empty["error"]
    assert empty["log_path"] is None
    assert "final_report" in empty
    assert "models_used" in empty
    assert empty["permission_denials"] == []

    relative = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir="relative/path")
    )
    assert relative["success"] is False
    assert "absolute path" in relative["error"]

    missing = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="x",
            workdir=str((tmp_path / "missing").resolve()),
        )
    )
    assert missing["success"] is False
    assert "does not exist" in missing["error"]


def test_permission_mode_validation_rejects_unknown(monkeypatch, repo, fake_binary):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="x",
            workdir=str(repo),
            permission_mode="yolo",
        )
    )
    assert result["success"] is False
    assert "permission_mode" in result["error"]
    assert "acceptEdits" in result["error"]


def test_binary_missing_returns_error(monkeypatch, repo):
    from delegate_claude_agent import tool as claude_agent_tool

    monkeypatch.setattr("delegate_claude_agent.tool.resolve_claude_binary", lambda: None)
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# Happy path + result parsing (real fake-binary subprocess)
# ---------------------------------------------------------------------------

@_REAL_SUBPROC
def test_happy_path_e2e(monkeypatch, repo, fake_binary, tmp_path):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "Implemented feature.")
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_ID", "sess-claude-xyz")
    monkeypatch.setenv("FAKE_CLAUDE_NUM_TURNS", "5")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.0775")
    monkeypatch.setenv("FAKE_CLAUDE_MODELS", "glm-5.2,claude-haiku-4-5")
    monkeypatch.setenv("FAKE_CLAUDE_DENIALS", '[{"tool":"Bash","reason":"nope"}]')
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_OUT", str(argv_out))

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="implement feature",
            workdir=str(repo),
        )
    )

    assert result["success"] is True
    assert result["error"] is None
    assert result["final_report"] == "Implemented feature."
    assert result["session_id"] == "sess-claude-xyz"
    assert result["num_turns"] == 5
    assert result["cost_usd"] == 0.0775
    assert result["models_used"] == ["claude-haiku-4-5", "glm-5.2"]
    assert result["permission_denials"] == [{"tool": "Bash", "reason": "nope"}]
    assert "claude-runs" in result["log_path"]
    assert Path(result["log_path"]).is_file()

    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert argv[0] == str(fake_binary)
    assert "-p" in argv
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "glm-5.2"
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Write,Edit,Glob,Grep,Bash"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[-1] == "implement feature"
    # --dangerously-skip-permissions must never be passed (refused under root).
    assert "--dangerously-skip-permissions" not in argv


@_REAL_SUBPROC
def test_plan_permission_mode_passed_through(monkeypatch, repo, fake_binary, tmp_path):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_OUT", str(argv_out))

    claude_agent_tool.delegate_claude_agent(
        task="plan something",
        workdir=str(repo),
        permission_mode="plan",
    )
    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert argv[argv.index("--permission-mode") + 1] == "plan"


@_REAL_SUBPROC
def test_is_error_path(monkeypatch, repo, fake_binary):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_SUBTYPE", "error")
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "true")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "Boom.")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert result["final_report"] == "Boom."
    assert "error" in result["error"] or "is_error" in result["error"]


@_REAL_SUBPROC
def test_malformed_output_no_result_event(monkeypatch, repo, fake_binary):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "no result event" in result["error"]
    assert Path(result["log_path"]).is_file()


@_REAL_SUBPROC
def test_nonzero_exit_reports_failure(monkeypatch, repo, fake_binary):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "exitnonzero")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "exited with code 2" in result["error"]


@_REAL_SUBPROC
def test_timeout_kills_process_group(monkeypatch, repo, fake_binary):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")

    # Fake a fast wall-clock so timeout_seconds=60 trips within milliseconds.
    start = time.monotonic()
    calls = {"n": 0}

    def _fake_monotonic():
        calls["n"] += 1
        return start + calls["n"] * 40

    monkeypatch.setattr("delegate_claude_agent.tool.time.monotonic", _fake_monotonic)
    monkeypatch.setattr(claude_agent_tool, "_MONITOR_POLL_SECONDS", 0.001)

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="timeout test",
            workdir=str(repo),
            timeout_seconds=60,
        )
    )
    assert result["success"] is False
    assert result["error"] == "timeout"


@_REAL_SUBPROC
def test_silent_period_then_final_result_not_stalled(monkeypatch, repo, fake_binary):
    """Healthy runs that emit nothing until the final result must not die as stalled."""
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep_then_result")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "0.15")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "Finished after quiet tool call.")

    # Simulate >180s of stdout silence on the first monitor tick while the hard
    # timeout (3600s) stays far away. Under the removed stall watchdog the run
    # completes once the subprocess emits its final result line.
    start = 1000.0
    calls = {"n": 0}

    def _fake_monotonic():
        calls["n"] += 1
        if calls["n"] == 1:
            return start
        if calls["n"] == 2:
            return start + 200.0
        return start + 200.0 + (calls["n"] - 2) * 0.01

    monkeypatch.setattr("delegate_claude_agent.tool.time.monotonic", _fake_monotonic)
    monkeypatch.setattr(claude_agent_tool, "_MONITOR_POLL_SECONDS", 0.01)

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="long quiet tool call",
            workdir=str(repo),
            timeout_seconds=3600,
        )
    )
    assert result["success"] is True
    assert result["error"] is None
    assert result["final_report"] == "Finished after quiet tool call."


@_REAL_SUBPROC
def test_child_env_guarantees_home_and_local_bin(monkeypatch, repo, fake_binary, tmp_path):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.delenv("HOME", raising=False)
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLAUDE_ENV_OUT", str(env_out))

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is True

    captured = json.loads(env_out.read_text(encoding="utf-8"))
    # HOME must be present and non-empty even though the caller env lacked it;
    # the wrapper runs with `set -u` and dies on an unbound $HOME.
    assert captured["HOME"] == str(Path.home())
    # ~/.local/bin is prepended so binary resolution works under minimal PATH.
    assert captured["PATH"].split(os.pathsep)[0] == str(Path.home() / ".local" / "bin")


@_REAL_SUBPROC
def test_flood_output_bounded_and_parses_final_result(monkeypatch, repo, fake_binary):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "flood")
    monkeypatch.setenv("FAKE_CLAUDE_FLOOD_BYTES", str(3 * 1024 * 1024))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "Survived flood.")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="flood test", workdir=str(repo))
    )

    assert result["success"] is True
    assert result["final_report"] == "Survived flood."
    assert result["log_truncated"] is True
    assert result["log_bytes_dropped"] > 0

    log_path = Path(result["log_path"])
    assert log_path.stat().st_size <= claude_agent_tool.LOG_MAX_FILE_BYTES
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    assert "...[truncated " in log_text
    assert "bytes]..." in log_text


@_REAL_SUBPROC
def test_giant_result_line_fails_closed(monkeypatch, repo, fake_binary):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "flood_giant_result")
    giant_pad = claude_agent_tool.LOG_TAIL_BYTES + (512 * 1024)
    monkeypatch.setenv("FAKE_CLAUDE_GIANT_PAD", str(giant_pad))

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="giant result", workdir=str(repo))
    )

    assert result["success"] is False
    assert result["log_truncated"] is True
    error = result["error"].lower()
    assert "truncat" in error
    log_path = Path(result["log_path"])
    assert log_path.stat().st_size <= claude_agent_tool.LOG_MAX_FILE_BYTES


@_REAL_SUBPROC
def test_truncated_log_does_not_accept_early_head_result(
    monkeypatch, repo, fake_binary
):
    from delegate_claude_agent import tool as claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "early_result_then_flood")
    monkeypatch.setenv("FAKE_CLAUDE_FLOOD_BYTES", str(3 * 1024 * 1024))

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="early result followed by trailing noise", workdir=str(repo)
        )
    )

    assert result["success"] is False
    assert result["final_report"] == ""
    assert result["log_truncated"] is True
    assert "result event missing" in result["error"]
    assert Path(result["log_path"]).stat().st_size <= (
        claude_agent_tool.LOG_MAX_FILE_BYTES
    )
