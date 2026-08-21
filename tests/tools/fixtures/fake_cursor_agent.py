#!/usr/bin/env python3
"""Fake Cursor ``agent`` CLI for Hermes restart-recovery tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _parse_resume(argv: list[str]) -> str | None:
    for arg in argv:
        if arg.startswith("--resume="):
            return arg.split("=", 1)[1]
    return None


def _write_pid_file() -> None:
    pid_file = os.environ.get("FAKE_CURSOR_PID_FILE")
    if not pid_file:
        return
    Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")


def main() -> int:
    _write_pid_file()
    mode = os.environ.get("FAKE_CURSOR_MODE", "success")
    session_id = os.environ.get("FAKE_CURSOR_SESSION_ID", "sess-fake-001")
    resume = _parse_resume(sys.argv[1:])
    block_after_init = os.environ.get("FAKE_CURSOR_BLOCK_AFTER_INIT") == "1"
    conflict_session = os.environ.get("FAKE_CURSOR_CONFLICT_SESSION_ID")
    delay = float(os.environ.get("FAKE_CURSOR_INIT_DELAY", "0") or "0")

    if resume:
        session_id = resume
    if conflict_session and resume:
        session_id = conflict_session

    if delay > 0:
        time.sleep(delay)

    # Fragment init JSON across writes when requested.
    init_event = {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
    }
    init_line = json.dumps(init_event)
    if os.environ.get("FAKE_CURSOR_FRAGMENT_INIT") == "1":
        mid = max(1, len(init_line) // 2)
        sys.stdout.write(init_line[:mid])
        sys.stdout.flush()
        time.sleep(0.05)
        sys.stdout.write(init_line[mid:] + "\n")
        sys.stdout.flush()
    else:
        _emit(init_event)

    if block_after_init:
        time.sleep(3600)
        return 0

    if mode == "success":
        _emit(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Done."}]},
            }
        )
        return 0
    if mode == "nonzero":
        _emit(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Failed."}]},
            }
        )
        return 2
    if mode == "action_required":
        _emit({"type": "error", "error_type": "ActionRequiredError", "message": "approve me"})
        return 0
    if mode == "no_init":
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
