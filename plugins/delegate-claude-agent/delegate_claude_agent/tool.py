#!/usr/bin/env python3
"""Delegate dev tasks to the Claude Code CLI (Alibaba GLM-5.2) as a subprocess.

Gating
------
The tool registers only when the Claude Code GLM wrapper is resolvable —
either via the ``CLAUDE_GLM_BIN`` env override, as an executable at
``~/.local/bin/claude-glm``, or as ``claude-glm`` on PATH.

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
With ``--output-format stream-json`` (plus ``--verbose``) the CLI emits
NDJSON event objects, one per line, as the run progresses. The handler scans
every line for valid JSON and keeps the last object with ``"type": "result"``
to extract session metadata, cost, model usage, and permission denials.
Long quiet periods between events (e.g. during tool calls) are normal; only
the caller's hard timeout bounds wall-clock run length.
"""

from __future__ import annotations

import json
import logging
import math
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

logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = "glm-5.2"
DEFAULT_TIMEOUT_SECONDS = 1800
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600

DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash"
DEFAULT_PERMISSION_MODE = "acceptEdits"
_ALLOWED_PERMISSION_MODES = ("acceptEdits", "plan")

_MONITOR_POLL_SECONDS = 0.1
_TERMINATE_GRACE_SECONDS = 2.0

LOG_HEAD_BYTES = 64 * 1024
LOG_TAIL_BYTES = 1 * 1024 * 1024
LOG_TRUNCATION_MARKER_OVERHEAD = 256
LOG_MAX_FILE_BYTES = LOG_HEAD_BYTES + LOG_TAIL_BYTES + LOG_TRUNCATION_MARKER_OVERHEAD


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

def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _coerce_session_id(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    return None


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return None
    return None


def _coerce_finite_float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        coerced = float(value)
        if math.isfinite(coerced):
            return coerced
        return None
    return None


def _coerce_permission_denials(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


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

    return {
        "subtype": result_event.get("subtype"),
        "is_error": result_event.get("is_error"),
        "result": _coerce_str(result_event.get("result")),
        "session_id": _coerce_session_id(result_event.get("session_id")),
        "num_turns": _coerce_int_or_none(result_event.get("num_turns")),
        "duration_ms": _coerce_finite_float_or_none(result_event.get("duration_ms")),
        "total_cost_usd": _coerce_finite_float_or_none(
            result_event.get("total_cost_usd")
        ),
        "models_used": models_used,
        "permission_denials": _coerce_permission_denials(
            result_event.get("permission_denials")
        ),
    }


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _clamp_timeout_seconds(timeout_seconds: int) -> int:
    try:
        value = int(timeout_seconds)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, value))


def _check_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted

        return is_interrupted()
    except Exception:
        return False


def _signal_process_group(proc: subprocess.Popen, sig: signal.Signals) -> bool:
    try:
        os.killpg(os.getpgid(proc.pid), sig)  # windows-footgun: ok — linux/macos-only plugin
        return True
    except (OSError, ProcessLookupError, AttributeError):
        return False


def _signal_pgid(pgid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pgid, sig)  # windows-footgun: ok — linux/macos-only plugin
        return True
    except (OSError, ProcessLookupError):
        return False


def _wait_pgid_reap(pgid: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)  # windows-footgun: ok — linux/macos-only plugin
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
        _signal_pgid(pgid, signal.SIGKILL)  # windows-footgun: ok — linux/macos-only plugin
        _wait_pgid_reap(pgid, _TERMINATE_GRACE_SECONDS)
    elif not _signal_process_group(proc, signal.SIGKILL):  # windows-footgun: ok — linux/macos-only plugin
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
    with open(log_path, "rb") as log_file:
        data = log_file.read(LOG_MAX_FILE_BYTES + 1)
    if len(data) > LOG_MAX_FILE_BYTES:
        data = data[:LOG_MAX_FILE_BYTES]
    return data.decode("utf-8", errors="replace")


def _run_and_stream(
    cmd: List[str],
    *,
    workdir: str,
    timeout_seconds: int,
    log_dir: Path,
    run_timestamp: str,
) -> Tuple[Optional[str], str, str, str, float, Optional[int], bool, int]:
    """Spawn the agent, stream stdout to a log file, enforce hard timeout.

    Returns
    ``(error_code, log_path, log_text, result_parse_text, duration_seconds,
    returncode, log_truncated, log_bytes_dropped)``. When output is truncated,
    ``result_parse_text`` contains only the rolling tail so an early result in
    the retained head cannot be mistaken for the terminal result.
    """
    start_mono = time.monotonic()

    # The wrapper runs with `set -u` and dies on an unbound $HOME in bare
    # environments (transient systemd units, cron). Guarantee HOME so any
    # sparse-env caller works, and prepend ~/.local/bin so binary resolution
    # stays consistent when PATH is minimal. NO credentials are injected here
    # — the wrapper pulls them from <HERMES_HOME>/.env at runtime.
    env = os.environ.copy()
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
    reader_state: Dict[str, Any] = {
        "log_truncated": False,
        "log_bytes_dropped": 0,
        "result_parse_text": "",
    }

    def _append_tail(log_file, tail_buf: bytearray, bytes_dropped: int) -> None:
        if bytes_dropped > 0:
            marker = f"\n...[truncated {bytes_dropped} bytes]...\n".encode("utf-8")
            reader_state["log_truncated"] = True
            reader_state["log_bytes_dropped"] = bytes_dropped
            reader_state["result_parse_text"] = bytes(tail_buf).decode(
                "utf-8", errors="replace"
            )
        elif tail_buf:
            marker = b""
        else:
            return

        head_size = log_file.tell()
        available = max(0, LOG_MAX_FILE_BYTES - head_size)
        if not marker and not tail_buf:
            return

        payload = marker + bytes(tail_buf)
        if len(payload) > available:
            if len(marker) >= available:
                log_file.write(marker[:available])
            else:
                tail_room = available - len(marker)
                log_file.write(marker + bytes(tail_buf)[-tail_room:])
        else:
            log_file.write(payload)
        log_file.flush()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            with open(log_path, "wb") as log_file:
                head_written = 0
                tail_buf = bytearray()
                bytes_dropped = 0
                in_tail = False

                while True:
                    try:
                        chunk = proc.stdout.read1(4096)
                    except (OSError, ValueError):
                        break
                    if not chunk:
                        break

                    offset = 0
                    while offset < len(chunk):
                        if not in_tail:
                            head_room = LOG_HEAD_BYTES - head_written
                            if head_room <= 0:
                                in_tail = True
                                continue
                            take = min(head_room, len(chunk) - offset)
                            log_file.write(chunk[offset : offset + take])
                            head_written += take
                            offset += take
                            if head_written >= LOG_HEAD_BYTES:
                                in_tail = True
                            continue

                        take = len(chunk) - offset
                        tail_buf.extend(chunk[offset : offset + take])
                        offset += take
                        if len(tail_buf) > LOG_TAIL_BYTES:
                            excess = len(tail_buf) - LOG_TAIL_BYTES
                            bytes_dropped += excess
                            del tail_buf[:excess]

                _append_tail(log_file, tail_buf, bytes_dropped)
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
        if elapsed >= timeout_seconds:
            error_code = "timeout"
            break
        time.sleep(_MONITOR_POLL_SECONDS)

    if error_code is not None:
        _terminate_process(proc, pgid)

    reader_thread.join(timeout=_TERMINATE_GRACE_SECONDS + 1.0)
    duration = time.monotonic() - start_mono

    if reader_thread.is_alive():
        _terminate_process(proc, pgid)
        incomplete_log_text = _read_log_text(log_path)
        return (
            "incomplete_output",
            str(log_path),
            incomplete_log_text,
            incomplete_log_text,
            duration,
            proc.poll() if proc.poll() is not None else -1,
            reader_state["log_truncated"],
            reader_state["log_bytes_dropped"],
        )

    log_text = _read_log_text(log_path)
    result_parse_text = (
        str(reader_state["result_parse_text"])
        if reader_state["log_truncated"]
        else log_text
    )

    returncode = proc.poll()
    if returncode is None:
        try:
            returncode = proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except Exception:
            returncode = -1

    return (
        error_code,
        str(log_path),
        log_text,
        result_parse_text,
        duration,
        returncode,
        reader_state["log_truncated"],
        reader_state["log_bytes_dropped"],
    )


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
        "permission_denials": permission_denials or [],
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
                "CLAUDE_GLM_BIN), or place `claude-glm` on PATH."
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
        "stream-json",
        "--verbose",
        str(task).strip(),
    ]

    try:
        (
            run_error,
            log_path,
            log_text,
            result_parse_text,
            duration,
            returncode,
            log_truncated,
            log_bytes_dropped,
        ) = _run_and_stream(
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

    parsed = parse_claude_agent_log(result_parse_text)
    base_fields: Dict[str, Any] = {
        "final_report": parsed.get("result") or "",
        "session_id": parsed.get("session_id"),
        "duration_seconds": round(duration, 3),
        "num_turns": parsed.get("num_turns"),
        "cost_usd": parsed.get("total_cost_usd"),
        "models_used": parsed.get("models_used") or [],
        "permission_denials": parsed.get("permission_denials") or [],
        "log_path": log_path,
    }
    if log_truncated:
        base_fields["log_truncated"] = True
        base_fields["log_bytes_dropped"] = log_bytes_dropped

    if run_error:
        return _make_result(
            success=False,
            error=run_error,
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
        if log_truncated:
            return _make_result(
                success=False,
                error=(
                    "log truncated; result event missing or incomplete "
                    f"({log_bytes_dropped} bytes dropped)"
                ),
                **base_fields,
            )
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


DELEGATE_CLAUDE_AGENT_SCHEMA = {
    "name": "delegate_claude_agent",
    "description": (
        "Delegate a software development task to the Claude Code CLI running "
        "against Alibaba GLM-5.2 via the local claude-glm wrapper. The CLI "
        "performs code edits, terminal commands, and multi-step dev work "
        "inside the specified repository directory. Stdout is captured as "
        "NDJSON in a log under the Hermes home directory. Available only when "
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
                    f"Maximum wall-clock seconds before the run is terminated "
                    f"({MIN_TIMEOUT_SECONDS}–{MAX_TIMEOUT_SECONDS})."
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
