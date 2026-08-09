#!/usr/bin/env python3
"""Delegate dev tasks to the Cursor Agent CLI as a subprocess.

Gating
------
The tool registers only when the Cursor Agent CLI binary is available on
PATH (``agent``) or as an executable at ``~/.local/bin/agent``.

Log format
----------
Stdout is streamed to ``<HERMES_HOME>/cursor-runs/<timestamp>-<pid>.jsonl``
as newline-delimited JSON (stream-json). Each line is one event from the
Cursor Agent run; the handler parses assistant text, delegation records,
and session metadata from that log after a successful exit.
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
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_constants import get_hermes_home
from tools.registry import registry
from utils import is_truthy_value

logger = logging.getLogger(__name__)

DEFAULT_CURSOR_AGENT_MODEL = "kimi-k3-high"
DEFAULT_TIMEOUT_SECONDS = 900
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 1800
STALL_WATCHDOG_SECONDS = 180

_MONITOR_POLL_SECONDS = 0.1
_TERMINATE_GRACE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Binary resolution + gating
# ---------------------------------------------------------------------------

def _local_bin_agent_path() -> Path:
    return Path.home() / ".local" / "bin" / "agent"


def resolve_cursor_agent_binary() -> Optional[str]:
    """Return the Cursor Agent CLI path, or None if not found."""
    try:
        found = shutil.which("agent")
        if found:
            return found
        local = _local_bin_agent_path()
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    except Exception:
        pass
    return None


def check_cursor_agent_requirements() -> bool:
    """Return True when the Cursor Agent CLI binary is available."""
    try:
        return resolve_cursor_agent_binary() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Stream-json parsing
# ---------------------------------------------------------------------------

def _find_task_tool_calls(obj: Any) -> List[Dict[str, Any]]:
    """Recursively collect dicts that look like taskToolCall payloads."""
    found: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        if "taskToolCall" in obj and isinstance(obj["taskToolCall"], dict):
            found.append(obj["taskToolCall"])
        for value in obj.values():
            found.extend(_find_task_tool_calls(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_task_tool_calls(item))
    return found


def _extract_assistant_text(event: Dict[str, Any]) -> str:
    """Extract plain text from an assistant stream-json event."""
    message = event.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    parts: List[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"text", "output_text"}:
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def _is_action_required_event(event: Dict[str, Any]) -> bool:
    """Return True when a parsed JSON event structurally indicates action required."""
    if event.get("error_type") == "ActionRequiredError":
        return True

    if event.get("type") != "error":
        return False

    for key in ("error_type", "name", "code"):
        if event.get(key) == "ActionRequiredError":
            return True

    err = event.get("error")
    if isinstance(err, dict):
        for key in ("type", "name", "code", "error_type"):
            if err.get(key) == "ActionRequiredError":
                return True
    elif isinstance(err, str) and err == "ActionRequiredError":
        return True

    return False


def _extract_action_required_detail(event: Dict[str, Any]) -> str:
    for key in ("message", "error", "detail", "description"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Action required"


def _is_action_required_plain_text(text: str) -> bool:
    stripped = text.strip()
    return stripped == "ActionRequiredError" or stripped.startswith("ActionRequiredError:")


def _extract_action_required_plain_detail(text: str) -> str:
    stripped = text.strip()
    if stripped == "ActionRequiredError":
        return ""
    if stripped.startswith("ActionRequiredError:"):
        return stripped[len("ActionRequiredError:") :].strip()
    return "Action required"


def _delegation_key(record: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    return (
        record.get("description"),
        record.get("subagent_type"),
        record.get("model"),
    )


def _canonicalize_dedupe_token(value: Any) -> Any:
    """Convert a value into a deterministic hashable token for dedupe keys."""
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        if value != value:
            return ("float", "nan")
        if value == float("inf"):
            return ("float", "inf")
        if value == float("-inf"):
            return ("float", "-inf")
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, bytes):
        return ("bytes", value)
    try:
        if isinstance(value, dict):
            return (
                "dict",
                tuple(
                    (_canonicalize_dedupe_token(k), _canonicalize_dedupe_token(v))
                    for k, v in sorted(
                        value.items(),
                        key=lambda item: json.dumps(item[0], sort_keys=True, default=str),
                    )
                ),
            )
        if isinstance(value, (list, tuple)):
            return ("list", tuple(_canonicalize_dedupe_token(item) for item in value))
        if isinstance(value, set):
            return (
                "set",
                tuple(
                    sorted(
                        json.dumps(_canonicalize_dedupe_token(item), sort_keys=True, default=str)
                        for item in value
                    )
                ),
            )
        return ("json", json.dumps(value, sort_keys=True, default=str))
    except Exception:
        pass
    try:
        return ("json", json.dumps(value, sort_keys=True, default=str))
    except Exception:
        pass
    try:
        return ("repr", repr(value))
    except Exception:
        return ("type", type(value).__name__)


def _delegation_dedupe_key(
    event: Dict[str, Any],
    task_call: Dict[str, Any],
    record: Dict[str, Any],
) -> Tuple[Any, ...]:
    call_id = event.get("call_id")
    if call_id is not None:
        return ("call_id", _canonicalize_dedupe_token(call_id))

    for source in (event, task_call):
        if not isinstance(source, dict):
            continue
        for key in ("toolCallId", "agentId"):
            value = source.get(key)
            if value is not None:
                return (key, _canonicalize_dedupe_token(value))

    return ("content",) + tuple(
        _canonicalize_dedupe_token(part) for part in _delegation_key(record)
    )


def parse_cursor_agent_log(log_text: str) -> Dict[str, Any]:
    """Parse a stream-json log into structured fields."""
    session_id: Optional[str] = None
    delegations: List[Dict[str, Any]] = []
    seen_delegations: Set[Tuple[Any, ...]] = set()
    final_report = ""
    action_required: Optional[Dict[str, Any]] = None

    for raw_line in log_text.splitlines():
        if not raw_line.strip():
            continue

        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            if _is_action_required_plain_text(raw_line):
                action_required = {
                    "detail": _extract_action_required_plain_detail(raw_line),
                }
            continue

        if isinstance(event, str):
            if _is_action_required_plain_text(event):
                action_required = {
                    "detail": _extract_action_required_plain_detail(event),
                }
            continue

        if not isinstance(event, dict):
            continue

        if _is_action_required_event(event):
            action_required = {
                "detail": _extract_action_required_detail(event),
            }

        if (
            session_id is None
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            init_sid = event.get("session_id")
            if isinstance(init_sid, str) and init_sid.strip():
                session_id = init_sid.strip()

        if event.get("type") == "tool_call":
            for task_call in _find_task_tool_calls(event):
                args = task_call.get("args") if isinstance(task_call.get("args"), dict) else {}
                record = {
                    "description": args.get("description"),
                    "subagent_type": args.get("subagentType") or args.get("subagent_type"),
                    "model": args.get("model"),
                }
                key = _delegation_dedupe_key(event, task_call, record)
                if key not in seen_delegations:
                    seen_delegations.add(key)
                    delegations.append(record)

        if event.get("type") == "assistant":
            text = _extract_assistant_text(event)
            if text:
                final_report = text

    return {
        "session_id": session_id,
        "delegations": delegations,
        "final_report": final_report,
        "action_required": action_required,
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
        os.killpg(os.getpgid(proc.pid), sig)
        return True
    except (OSError, ProcessLookupError, AttributeError):
        return False


def _signal_pgid(pgid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except (OSError, ProcessLookupError):
        return False


def _wait_pgid_reap(pgid: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
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
        _signal_pgid(pgid, signal.SIGKILL)
        _wait_pgid_reap(pgid, _TERMINATE_GRACE_SECONDS)
    elif not _signal_process_group(proc, signal.SIGKILL):
        try:
            proc.kill()
        except Exception:
            pass

    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except Exception:
        pass


def _make_result(
    *,
    success: bool,
    final_report: str = "",
    delegations: Optional[List[Dict[str, Any]]] = None,
    duration_seconds: float = 0.0,
    session_id: Optional[str] = None,
    log_path: Optional[str] = None,
    error: Optional[str] = None,
    **extra: Any,
) -> str:
    payload: Dict[str, Any] = {
        "success": success,
        "final_report": final_report,
        "delegations": delegations or [],
        "duration_seconds": duration_seconds,
        "session_id": session_id,
        "log_path": log_path,
        "error": error,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


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

    # NOTE(future-me): the agent wrapper script runs with `set -u` and dies on
    # unbound $HOME in bare environments (transient systemd units, cron). The
    # gateway unit sets HOME, but guarantee it here so any sparse-env caller
    # works. Also prepend ~/.local/bin so binary resolution stays consistent
    # when PATH is minimal.
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
        if elapsed >= timeout_seconds:
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


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

def delegate_cursor_agent(
    task: str,
    workdir: str,
    model: str = DEFAULT_CURSOR_AGENT_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    force: bool = True,
    task_id: str | None = None,
) -> str:
    del task_id  # reserved for future correlation; not used yet

    if not task or not str(task).strip():
        return _make_result(
            success=False,
            error="task is required for delegate_cursor_agent",
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

    binary = resolve_cursor_agent_binary()
    if not binary:
        return _make_result(
            success=False,
            error=(
                "Cursor Agent CLI binary not found. Install the `agent` CLI and "
                "ensure it is on PATH or at ~/.local/bin/agent."
            ),
        )

    clamped_timeout = _clamp_timeout_seconds(timeout_seconds)
    model_name = str(model or "").strip() or DEFAULT_CURSOR_AGENT_MODEL
    force_enabled = is_truthy_value(force, default=True)

    log_dir = get_hermes_home() / "cursor-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    cmd = [
        binary,
        "-p",
        "--trust",
    ]
    if force_enabled:
        cmd.append("--force")
    cmd.extend(
        [
            "--model",
            model_name,
            "--output-format",
            "stream-json",
            str(task).strip(),
        ]
    )

    try:
        watchdog_error, log_path, log_text, duration, returncode = _run_and_stream(
            cmd,
            workdir=str(workdir_path),
            timeout_seconds=clamped_timeout,
            log_dir=log_dir,
            run_timestamp=run_timestamp,
        )
    except Exception as exc:
        logger.error("delegate_cursor_agent spawn failed: %s", exc, exc_info=True)
        return _make_result(
            success=False,
            error=str(exc),
            duration_seconds=0.0,
        )

    parsed = parse_cursor_agent_log(log_text)
    base_fields = {
        "final_report": parsed.get("final_report") or "",
        "delegations": parsed.get("delegations") or [],
        "duration_seconds": round(duration, 3),
        "session_id": parsed.get("session_id"),
        "log_path": log_path,
    }

    if parsed.get("action_required"):
        detail = parsed["action_required"].get("detail", "")
        return _make_result(
            success=False,
            error="action_required",
            error_type="ActionRequiredError",
            detail=detail,
            **base_fields,
        )

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
            error=f"Cursor Agent exited with code {returncode}" + (f": {tail}" if tail else ""),
            **base_fields,
        )

    return _make_result(
        success=True,
        error=None,
        **base_fields,
    )


CURSOR_AGENT_SCHEMA = {
    "name": "delegate_cursor_agent",
    "description": (
        "Delegate a software development task to the Cursor Agent CLI running "
        "as a local subprocess. The CLI performs code edits, terminal commands, "
        "and multi-step dev work inside the specified repository directory. "
        "Stdout is captured as stream-json in a log under the Hermes home "
        "directory. Available only when the Cursor Agent CLI binary is installed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The development task for Cursor Agent to perform.",
            },
            "workdir": {
                "type": "string",
                "description": "Absolute path to the target project directory.",
            },
            "model": {
                "type": "string",
                "description": "Cursor Agent model to use for the run.",
                "default": DEFAULT_CURSOR_AGENT_MODEL,
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    f"Maximum wall-clock seconds before the run is terminated "
                    f"({MIN_TIMEOUT_SECONDS}–{MAX_TIMEOUT_SECONDS})."
                ),
                "default": DEFAULT_TIMEOUT_SECONDS,
            },
            "force": {
                "type": "boolean",
                "description": (
                    "When true, pass --force so Cursor Agent may write files "
                    "and run commands without interactive approval."
                ),
                "default": True,
            },
        },
        "required": ["task", "workdir"],
    },
}


def _handle_delegate_cursor_agent(args, **kw):
    return delegate_cursor_agent(
        task=args.get("task", ""),
        workdir=args.get("workdir", ""),
        model=args.get("model", DEFAULT_CURSOR_AGENT_MODEL),
        timeout_seconds=args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        force=is_truthy_value(args.get("force", True), default=True),
        task_id=kw.get("task_id"),
    )


registry.register(
    name="delegate_cursor_agent",
    toolset="delegation",
    schema=CURSOR_AGENT_SCHEMA,
    handler=_handle_delegate_cursor_agent,
    check_fn=check_cursor_agent_requirements,
    emoji="🖥️",
    max_result_size_chars=100_000,
)
