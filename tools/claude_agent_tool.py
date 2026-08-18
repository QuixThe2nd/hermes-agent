#!/usr/bin/env python3
"""Delegate dev tasks to the Claude Code CLI (Alibaba GLM-5.2) as a subprocess.

Gating
------
The tool registers only when the Claude Code GLM wrapper is resolvable —
either via the ``CLAUDE_GLM_BIN`` env override, as an executable at
``~/.local/bin/claude-glm``, or as ``claude-glm``/``claude`` on PATH.

Credentials
-----------
The ``claude-glm`` wrapper injects the Alibaba coding-plan credentials at
runtime from ``<HERMES_HOME>/.env`` itself (it ``execve``s the real Claude
binary with ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_BASE_URL`` set). This tool
therefore NEVER places credentials in argv or in env additions — it only
guarantees ``HOME`` and a minimal ``PATH`` so the wrapper survives sparse
environments (it runs with ``set -u`` and dies on an unbound ``$HOME``).
``--dangerously-skip-permissions`` is deliberately NOT passed: it is refused
when the process runs as root.

Log format
----------
Stdout is streamed to ``<HERMES_HOME>/claude-runs/<timestamp>-<pid>.jsonl``.
With ``--output-format json`` the CLI emits one JSON document per line; the
handler scans for the last line that is valid JSON with ``"type": "result"``
and extracts session metadata, cost, model usage, and permission denials
from it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from tools.environments.local import build_subprocess_env
from tools.registry import registry

# Process-group signalling is POSIX-only. On Windows we degrade to
# proc.terminate()/proc.kill() (see _terminate_process), so keep the
# killpg paths behind a capability flag and use a SIGKILL fallback that
# exists at import time on every platform.
_KILLPG_SUPPORTED = hasattr(os, "killpg")
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = "glm-5.2"
DEFAULT_TIMEOUT_SECONDS = 0  # 0 = no wall-clock limit; stall watchdog still applies
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600
STALL_WATCHDOG_SECONDS = 600

DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash"
DEFAULT_PERMISSION_MODE = "acceptEdits"
_ALLOWED_PERMISSION_MODES = ("acceptEdits", "plan")

_MONITOR_POLL_SECONDS = 0.1
_TERMINATE_GRACE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Binary resolution + gating
# ---------------------------------------------------------------------------

def _local_bin_claude_glm_path() -> Path:
    return Path.home() / ".local" / "bin" / "claude-glm"


def resolve_claude_binary() -> Optional[str]:
    """Return the Claude Code (GLM) wrapper path, or None if not found.

    Search order:
    1. ``CLAUDE_GLM_BIN`` env override (must be an executable file).
    2. ``~/.local/bin/claude-glm``.
    3. ``claude-glm`` on PATH.
    4. bare ``claude`` on PATH.
    """
    try:
        override = os.environ.get("CLAUDE_GLM_BIN")
        if override:
            override_path = Path(override).expanduser()
            if override_path.is_file() and os.access(override_path, os.X_OK):
                return str(override_path)

        local = _local_bin_claude_glm_path()
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)

        found = shutil.which("claude-glm")
        if found:
            return found

        found = shutil.which("claude")
        if found:
            return found
    except Exception:
        pass
    return None


def check_claude_agent_requirements() -> bool:
    """Return True when the Claude Code (GLM) wrapper binary is available."""
    try:
        return resolve_claude_binary() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def parse_claude_agent_log(log_text: str) -> Dict[str, Any]:
    """Parse a Claude Code json log for the final ``type=result`` event.

    Scans every line for valid JSON; the last line whose parsed object has
    ``"type": "result"`` wins. Returns an empty dict when no result event is
    found (missing/malformed output).
    """
    result_event: Optional[Dict[str, Any]] = None
    for raw_line in log_text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result_event = event

    if not result_event:
        return {}

    model_usage = result_event.get("modelUsage")
    models_used: List[str] = []
    if isinstance(model_usage, dict):
        models_used = sorted(str(key) for key in model_usage.keys())

    permission_denials = result_event.get("permission_denials")
    if permission_denials is None:
        permission_denials = []

    return {
        "subtype": result_event.get("subtype"),
        "is_error": result_event.get("is_error"),
        "result": result_event.get("result"),
        "session_id": result_event.get("session_id"),
        "num_turns": result_event.get("num_turns"),
        "duration_ms": result_event.get("duration_ms"),
        "total_cost_usd": result_event.get("total_cost_usd"),
        "models_used": models_used,
        "permission_denials": permission_denials,
    }


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _clamp_timeout_seconds(timeout_seconds: int) -> int:
    try:
        value = int(timeout_seconds)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return 0  # unbounded; stall watchdog remains the dead-man switch
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, value))


def _check_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted

        return is_interrupted()
    except Exception:
        return False


def _signal_process_group(proc: subprocess.Popen, sig: signal.Signals) -> bool:
    if not _KILLPG_SUPPORTED:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), sig)  # windows-footgun: ok — guarded by _KILLPG_SUPPORTED
        return True
    except (OSError, ProcessLookupError, AttributeError):
        return False


def _signal_pgid(pgid: int, sig: signal.Signals) -> bool:
    if not _KILLPG_SUPPORTED:
        return False
    try:
        os.killpg(pgid, sig)  # windows-footgun: ok — guarded by _KILLPG_SUPPORTED
        return True
    except (OSError, ProcessLookupError):
        return False


def _wait_pgid_reap(pgid: int, timeout: float) -> None:
    if not _KILLPG_SUPPORTED:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)  # windows-footgun: ok — guarded by _KILLPG_SUPPORTED
        except ProcessLookupError:
            return
        except OSError as exc:
            if getattr(exc, "errno", None) == 3:
                return
            return
        time.sleep(0.05)


def _terminate_process(proc: subprocess.Popen, pgid: Optional[int] = None) -> None:
    if pgid is not None:
        if not _signal_pgid(pgid, signal.SIGTERM):
            try:
                proc.terminate()
            except Exception:
                pass
    elif not _signal_process_group(proc, signal.SIGTERM):
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except Exception:
        pass

    if pgid is not None:
        _signal_pgid(pgid, _SIGKILL)
        _wait_pgid_reap(pgid, _TERMINATE_GRACE_SECONDS)
    elif not _signal_process_group(proc, _SIGKILL):
        try:
            proc.kill()
        except Exception:
            pass

    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except Exception:
        pass


def _read_log_text(log_path: Path) -> str:
    if not log_path.is_file():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def _run_and_stream(
    cmd: List[str],
    *,
    workdir: str,
    timeout_seconds: int,
    log_dir: Path,
    run_timestamp: str,
) -> Tuple[Optional[str], str, str, float, Optional[int]]:
    """Spawn the agent, stream stdout to a log file, enforce watchdogs.

    Returns ``(error_code, log_path, log_text, duration_seconds, returncode)``.
    """
    start_mono = time.monotonic()
    last_byte_mono = start_mono

    # The wrapper runs with `set -u` and dies on an unbound $HOME in bare
    # environments (transient systemd units, cron). Guarantee HOME so any
    # sparse-env caller works, and prepend ~/.local/bin so binary resolution
    # stays consistent when PATH is minimal. NO credentials are injected here
    # — the wrapper pulls them from <HERMES_HOME>/.env at runtime.
    # scrub_secrets=False + inherit_profile_home=False preserves exact legacy
    # os.environ.copy() behavior while routing through the single env factory.
    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
    if not env.get("HOME"):
        env["HOME"] = str(Path.home())
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )

    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        pgid = None

    log_path = log_dir / f"{run_timestamp}-{proc.pid}.jsonl"
    reader_done = threading.Event()

    def _reader() -> None:
        nonlocal last_byte_mono
        try:
            assert proc.stdout is not None
            with open(log_path, "wb") as log_file:
                while True:
                    try:
                        chunk = proc.stdout.read1(4096)
                    except (OSError, ValueError):
                        break
                    if not chunk:
                        break
                    log_file.write(chunk)
                    log_file.flush()
                    last_byte_mono = time.monotonic()
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
            reader_done.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    error_code: Optional[str] = None
    while proc.poll() is None:
        if _check_interrupted():
            error_code = "interrupted"
            break

        now = time.monotonic()
        elapsed = now - start_mono
        if timeout_seconds > 0 and elapsed >= timeout_seconds:
            error_code = "timeout"
            break
        if now - last_byte_mono >= STALL_WATCHDOG_SECONDS:
            error_code = "stalled"
            break
        time.sleep(_MONITOR_POLL_SECONDS)

    if error_code is not None:
        _terminate_process(proc, pgid)

    reader_thread.join(timeout=_TERMINATE_GRACE_SECONDS + 1.0)
    duration = time.monotonic() - start_mono

    if reader_thread.is_alive():
        _terminate_process(proc, pgid)
        return (
            "incomplete_output",
            str(log_path),
            _read_log_text(log_path),
            duration,
            proc.poll() if proc.poll() is not None else -1,
        )

    log_text = _read_log_text(log_path)

    returncode = proc.poll()
    if returncode is None:
        try:
            returncode = proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except Exception:
            returncode = -1

    return error_code, str(log_path), log_text, duration, returncode


def _make_result(
    *,
    success: bool,
    final_report: str = "",
    error: Optional[str] = None,
    session_id: Optional[str] = None,
    duration_seconds: float = 0.0,
    num_turns: Optional[int] = None,
    cost_usd: Optional[float] = None,
    models_used: Optional[List[str]] = None,
    permission_denials: Optional[List[Any]] = None,
    log_path: Optional[str] = None,
    **extra: Any,
) -> str:
    payload: Dict[str, Any] = {
        "success": success,
        "error": error,
        "final_report": final_report,
        "session_id": session_id,
        "duration_seconds": duration_seconds,
        "num_turns": num_turns,
        "cost_usd": cost_usd,
        "models_used": models_used or [],
        "permission_denials": permission_denials,
        "log_path": log_path,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

def delegate_claude_agent(
    task: str,
    workdir: str,
    model: str = DEFAULT_CLAUDE_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    task_id: str | None = None,
) -> str:
    del task_id  # reserved for future correlation; not used yet

    if not task or not str(task).strip():
        return _make_result(
            success=False,
            error="task is required for delegate_claude_agent",
        )

    workdir_path = Path(workdir)
    if not workdir_path.is_absolute():
        return _make_result(
            success=False,
            error="workdir must be an absolute path",
        )
    if not workdir_path.is_dir():
        return _make_result(
            success=False,
            error=f"workdir does not exist or is not a directory: {workdir}",
        )

    mode = str(permission_mode or "").strip()
    if mode not in _ALLOWED_PERMISSION_MODES:
        return _make_result(
            success=False,
            error=(
                "permission_mode must be one of "
                f"{list(_ALLOWED_PERMISSION_MODES)}, got: {permission_mode!r}"
            ),
        )

    binary = resolve_claude_binary()
    if not binary:
        return _make_result(
            success=False,
            error=(
                "Claude Code (GLM) wrapper binary not found. Install the "
                "`claude-glm` wrapper at ~/.local/bin/claude-glm (or set "
                "CLAUDE_GLM_BIN), or place `claude-glm`/`claude` on PATH."
            ),
        )

    clamped_timeout = _clamp_timeout_seconds(timeout_seconds)
    model_name = str(model or "").strip() or DEFAULT_CLAUDE_MODEL
    tools_arg = str(allowed_tools or "").strip() or DEFAULT_ALLOWED_TOOLS

    log_dir = get_hermes_home() / "claude-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # NOTE: --dangerously-skip-permissions is intentionally omitted — it is
    # refused when running as root. The wrapper injects credentials itself.
    cmd = [
        binary,
        "-p",
        "--model",
        model_name,
        "--permission-mode",
        mode,
        "--allowedTools",
        tools_arg,
        "--output-format",
        "json",
        str(task).strip(),
    ]

    try:
        watchdog_error, log_path, log_text, duration, returncode = _run_and_stream(
            cmd,
            workdir=str(workdir_path),
            timeout_seconds=clamped_timeout,
            log_dir=log_dir,
            run_timestamp=run_timestamp,
        )
    except Exception as exc:
        logger.error("delegate_claude_agent spawn failed: %s", exc, exc_info=True)
        return _make_result(
            success=False,
            error=str(exc),
            duration_seconds=0.0,
        )

    parsed = parse_claude_agent_log(log_text)
    base_fields = {
        "final_report": _coerce_str(parsed.get("result")) or "",
        "session_id": parsed.get("session_id"),
        "duration_seconds": round(duration, 3),
        "num_turns": parsed.get("num_turns"),
        "cost_usd": parsed.get("total_cost_usd"),
        "models_used": parsed.get("models_used") or [],
        "permission_denials": parsed.get("permission_denials") or [],
        "log_path": log_path,
    }

    if watchdog_error:
        return _make_result(
            success=False,
            error=watchdog_error,
            **base_fields,
        )

    if returncode != 0:
        tail = log_text.strip()[-2000:] if log_text.strip() else ""
        return _make_result(
            success=False,
            error=f"Claude Code exited with code {returncode}" + (f": {tail}" if tail else ""),
            **base_fields,
        )

    if not parsed:
        return _make_result(
            success=False,
            error="no result event found in Claude Code output",
            **base_fields,
        )

    is_error = parsed.get("is_error")
    subtype = parsed.get("subtype")
    success = is_error is False and subtype == "success"

    return _make_result(
        success=success,
        error=None if success else (
            f"Claude Code result subtype={subtype!r} is_error={is_error!r}"
        ),
        **base_fields,
    )


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


DELEGATE_CLAUDE_AGENT_SCHEMA = {
    "name": "delegate_claude_agent",
    "description": (
        "Delegate a software development task to the Claude Code CLI running "
        "against Alibaba GLM-5.2 via the local claude-glm wrapper. The CLI "
        "performs code edits, terminal commands, and multi-step dev work "
        "inside the specified repository directory. Stdout is captured as "
        "JSON in a log under the Hermes home directory. Available only when "
        "the claude-glm wrapper binary is installed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The coding task brief for Claude Code to perform.",
            },
            "workdir": {
                "type": "string",
                "description": "Absolute path to the target git repo/workspace.",
            },
            "model": {
                "type": "string",
                "description": "Model to use for the run (via the claude-glm wrapper).",
                "default": DEFAULT_CLAUDE_MODEL,
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    f"Maximum wall-clock seconds before the run is terminated. "
                    f"0 (default) means no wall-clock limit; the stall watchdog "
                    f"still terminates runs with no output for "
                    f"{STALL_WATCHDOG_SECONDS}s. Positive values clamp to "
                    f"{MIN_TIMEOUT_SECONDS}–{MAX_TIMEOUT_SECONDS}."
                ),
                "default": DEFAULT_TIMEOUT_SECONDS,
            },
            "allowed_tools": {
                "type": "string",
                "description": (
                    "Comma-separated list of tools Claude Code may use."
                ),
                "default": DEFAULT_ALLOWED_TOOLS,
            },
            "permission_mode": {
                "type": "string",
                "description": (
                    "Claude Code permission mode. 'acceptEdits' auto-approves "
                    "file edits; 'plan' only plans without writing."
                ),
                "default": DEFAULT_PERMISSION_MODE,
                "enum": list(_ALLOWED_PERMISSION_MODES),
            },
        },
        "required": ["task", "workdir"],
    },
}


def _handle_delegate_claude_agent(args, **kw):
    return delegate_claude_agent(
        task=args.get("task", ""),
        workdir=args.get("workdir", ""),
        model=args.get("model", DEFAULT_CLAUDE_MODEL),
        timeout_seconds=args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        allowed_tools=args.get("allowed_tools", DEFAULT_ALLOWED_TOOLS),
        permission_mode=args.get("permission_mode", DEFAULT_PERMISSION_MODE),
        task_id=kw.get("task_id"),
    )


registry.register(
    name="delegate_claude_agent",
    toolset="delegation",
    schema=DELEGATE_CLAUDE_AGENT_SCHEMA,
    handler=_handle_delegate_claude_agent,
    check_fn=check_claude_agent_requirements,
    emoji="🤖",
    max_result_size_chars=100_000,
)
