"""Restart-safe receipts for ``delegate_cursor_agent`` runs.

Persists run metadata (never plaintext prompts or secrets) under
``$HERMES_HOME/cursor-runs`` so gateway resume can reconcile terminal logs
or resume a canonical Cursor CLI session exactly once after interruption.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RECEIPT_DIR_NAME = "cursor-runs"
LOCK_SUFFIX = ".lock"
MAX_RESUME_ATTEMPTS = 1

TERMINAL_OUTCOMES = frozenset({
    "success",
    "interrupted",
    "timeout",
    "stalled",
    "incomplete_output",
    "action_required",
    "failed",
    "cancelled",
    "error",
    "expired",
})

CONTINUATION_PROMPT = (
    "Hermes restarted while this Cursor agent run was in progress. "
    "Inspect the current workspace and conversation history, continue "
    "from where you left off, and do not repeat actions that are already "
    "complete."
)


def cursor_runs_dir() -> Path:
    return get_hermes_home() / RECEIPT_DIR_NAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_boot_id() -> str:
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        if boot_path.is_file():
            value = boot_path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        pass
    return f"pid-{os.getpid()}"


def hash_prompt(task: str) -> str:
    digest = hashlib.sha256(str(task or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _receipt_path(run_id: str) -> Path:
    return cursor_runs_dir() / f"{run_id}.receipt.json"


def _lock_path(run_id: str) -> Path:
    return cursor_runs_dir() / f"{run_id}{LOCK_SUFFIX}"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def read_receipt(path: Path | str) -> Optional[Dict[str, Any]]:
    receipt_path = Path(path)
    if not receipt_path.is_file():
        return None
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_receipt(run_id: str) -> Optional[Dict[str, Any]]:
    return read_receipt(_receipt_path(run_id))


def create_receipt(
    *,
    hermes_session_id: str,
    tool_call_id: Optional[str],
    workdir: str,
    prompt_hash: str,
    log_path: str,
    model: Optional[str],
    force: bool,
    timeout_seconds: int,
    execution_mode: str,
    cloud_agent_id: Optional[str] = None,
    cloud_run_id: Optional[str] = None,
) -> Tuple[str, Path]:
    """Atomically create a restrictive receipt before spawn."""
    run_id = uuid.uuid4().hex
    attempt_id = uuid.uuid4().hex
    now = _utc_now_iso()
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "hermes_session_id": hermes_session_id,
        "tool_call_id": tool_call_id,
        "workdir": workdir,
        "prompt_hash": prompt_hash,
        "state": "pending",
        "outcome": None,
        "created_at": now,
        "updated_at": now,
        "log_path": log_path,
        "owner_pid": os.getpid(),
        "owner_boot_id": get_boot_id(),
        "model": model,
        "force": bool(force),
        "timeout_seconds": int(timeout_seconds),
        "execution_mode": execution_mode,
        "cursor_session_id": None,
        "cloud_agent_id": cloud_agent_id,
        "cloud_run_id": cloud_run_id,
        "resume_attempts": 0,
        "resumed": False,
        "terminal_result": None,
    }
    path = _receipt_path(run_id)
    _atomic_write_json(path, payload)
    return run_id, path


def update_receipt(path: Path | str, **fields: Any) -> Optional[Dict[str, Any]]:
    receipt_path = Path(path)
    current = read_receipt(receipt_path)
    if current is None:
        return None
    current.update(fields)
    current["updated_at"] = _utc_now_iso()
    _atomic_write_json(receipt_path, current)
    return current


def persist_cursor_session_id(path: Path | str, session_id: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    current = read_receipt(path)
    if current is None:
        return
    if current.get("cursor_session_id") and current["cursor_session_id"] != sid:
        logger.warning(
            "Ignoring conflicting cursor session id on receipt %s",
            Path(path).name,
        )
        return
    update_receipt(
        path,
        cursor_session_id=sid,
        state="running",
    )


def parse_init_session_id(line: str) -> Optional[str]:
    stripped = (line or "").strip()
    if not stripped:
        return None
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    if event.get("type") != "system" or event.get("subtype") != "init":
        return None
    sid = event.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return None


def make_session_id_persister(path: Path | str) -> Callable[[str], None]:
    def _persist(line: str) -> None:
        sid = parse_init_session_id(line)
        if sid:
            persist_cursor_session_id(path, sid)

    return _persist


def finalize_receipt(
    path: Path | str,
    *,
    outcome: str,
    terminal_result: Optional[Dict[str, Any]] = None,
    cursor_session_id: Optional[str] = None,
    log_path: Optional[str] = None,
    attempt_id: Optional[str] = None,
    resumed: Optional[bool] = None,
    resume_attempts: Optional[int] = None,
    cloud_agent_id: Optional[str] = None,
    cloud_run_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    fields: Dict[str, Any] = {
        "state": "terminal",
        "outcome": outcome,
        "terminal_result": terminal_result,
    }
    if cursor_session_id is not None:
        fields["cursor_session_id"] = cursor_session_id
    if log_path is not None:
        fields["log_path"] = log_path
    if attempt_id is not None:
        fields["attempt_id"] = attempt_id
    if resumed is not None:
        fields["resumed"] = bool(resumed)
    if resume_attempts is not None:
        fields["resume_attempts"] = int(resume_attempts)
    if cloud_agent_id is not None:
        fields["cloud_agent_id"] = cloud_agent_id
    if cloud_run_id is not None:
        fields["cloud_run_id"] = cloud_run_id
    return update_receipt(path, **fields)


def is_terminal_receipt(receipt: Dict[str, Any]) -> bool:
    if receipt.get("state") == "terminal":
        return True
    outcome = receipt.get("outcome")
    return isinstance(outcome, str) and outcome in TERMINAL_OUTCOMES


def find_receipt_for_binding(
    hermes_session_id: str,
    tool_call_id: Optional[str],
    *,
    include_terminal: bool = True,
) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """Locate the newest receipt bound to session + tool call identity."""
    if not hermes_session_id:
        return None
    root = cursor_runs_dir()
    if not root.is_dir():
        return None
    matches: list[tuple[float, Path, Dict[str, Any]]] = []
    for path in root.glob("*.receipt.json"):
        data = read_receipt(path)
        if not data:
            continue
        if data.get("hermes_session_id") != hermes_session_id:
            continue
        bound = data.get("tool_call_id")
        if tool_call_id:
            if not isinstance(bound, str) or not bound.strip() or bound != tool_call_id:
                continue
        elif bound:
            continue
        if not include_terminal and is_terminal_receipt(data):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        matches.append((mtime, path, data))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    _, path, data = matches[0]
    return path, data


def find_receipt_for_tool_call(
    hermes_session_id: str,
    tool_call_id: Optional[str],
) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """Locate the newest non-terminal receipt bound to session + tool call."""
    return find_receipt_for_binding(
        hermes_session_id,
        tool_call_id,
        include_terminal=False,
    )


def find_receipt_by_run_id(run_id: str) -> Optional[Tuple[Path, Dict[str, Any]]]:
    path = _receipt_path(run_id)
    data = read_receipt(path)
    if data is None:
        return None
    return path, data


@contextmanager
def receipt_run_lock(run_id: str) -> Iterator[bool]:
    """Acquire an exclusive OS file lock for resume; yields False when contended."""
    lock_path = _lock_path(run_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if hasattr(fcntl, "flock"):  # windows-footgun: ok — hasattr-gated POSIX flock
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        else:
            acquired = True
        yield acquired
    finally:
        if acquired and hasattr(fcntl, "flock"):  # windows-footgun: ok — hasattr-gated POSIX flock
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def receipt_matches_binding(
    receipt: Dict[str, Any],
    *,
    hermes_session_id: str,
    tool_call_id: Optional[str],
) -> bool:
    if receipt.get("hermes_session_id") != hermes_session_id:
        return False
    bound = receipt.get("tool_call_id")
    if tool_call_id:
        return isinstance(bound, str) and bound.strip() != "" and bound == tool_call_id
    return not bound
