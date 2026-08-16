"""Planning-phase timeout and heartbeat-scope tests."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.dev_pipeline import executor as ex
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _executor_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
        "heartbeat_interval_seconds": 0.05,
    }
    cfg.update(overrides)
    return cfg


def _setup_planning_task(conn) -> tuple[str, int, dict]:
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "fix bug", "repo": "https://example.com/r.git"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run_id = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state({}, {"phase": ex.PHASE_PLANNING}),
    )
    meta = ex.load_run_metadata(conn, run_id)
    return task_id, run_id, meta


def _valid_moa_payload() -> str:
    contract = {
        "task_summary": "fix bug",
        "lane_hint": "cursor",
        "estimated_minutes": 10,
        "allowed_paths": ["src/"],
        "acceptance_commands": ["true"],
        "broad_flags": {
            "migration": False,
            "repo_wide_change": False,
            "toolchain_change": False,
            "multi_subsystem": False,
            "long_verification": False,
        },
        "blocked_reasons": [],
        "step_plan": [{"id": "s1", "description": "fix it", "verifiable": True}],
        "assumptions": [],
    }
    return json.dumps({
        "success": True,
        "partial": False,
        "advisors": [
            {"label": "advisor-1", "status": "ok", "advice": json.dumps(contract)},
        ],
    })


def _wrap_run_planning_with_consult(real_run_planning, consult_fn):
    def wrapper(task_text, summary, **kwargs):
        kwargs["consult_fn"] = consult_fn
        return real_run_planning(task_text, summary, **kwargs)

    return wrapper


def test_planning_timeout_blocks_task(kanban_home):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_planning_task(conn)
    executor = ex.DevExecutor(_executor_cfg(planning_timeout_seconds=1))
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_PLANNING)

    hang_release = threading.Event()

    def hung_consult(**_kwargs):
        # Bounded wait so the leaked daemon planning thread does not linger
        # forever after the executor times out.
        hang_release.wait(timeout=10)
        return json.dumps({"success": False})

    real_run_planning = ex.run_planning
    real_block = ex.block_dev_task
    block_calls: list[dict] = []

    def spy_block(conn, tid, kind, reason, **kwargs):
        block_calls.append({"kind": kind, "reason": reason})
        return real_block(conn, tid, kind, reason, **kwargs)

    started = time.monotonic()
    with (
        patch.object(
            ex,
            "run_planning",
            new=_wrap_run_planning_with_consult(real_run_planning, hung_consult),
        ),
        patch.object(ex, "build_repo_summary", return_value="summary"),
        patch.object(ex, "clone_repo", return_value=(True, "")),
        patch.object(ex, "block_dev_task", new=spy_block),
    ):
        executor._phase_planning(
            conn, task_id, run_id, meta, ex.pipeline_state(meta)
        )
    elapsed = time.monotonic() - started

    assert elapsed < 4
    assert block_calls
    assert block_calls[0]["kind"] == "planning_unavailable"
    assert "timed out" in block_calls[0]["reason"]
    assert task_id not in executor._active
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    conn.close()


def test_heartbeat_scope_fires_during_slow_planning(kanban_home):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_planning_task(conn)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_PLANNING)

    consult_window: dict[str, float] = {}

    def slow_consult(**_kwargs):
        consult_window["start"] = time.monotonic()
        time.sleep(0.3)
        consult_window["end"] = time.monotonic()
        return _valid_moa_payload()

    heartbeat_times: list[float] = []
    real_heartbeat = kb.heartbeat_claim

    def track_heartbeat(hb_conn, tid, **kwargs):
        heartbeat_times.append(time.monotonic())
        return real_heartbeat(hb_conn, tid, **kwargs)

    real_run_planning = ex.run_planning
    with (
        patch.object(
            ex,
            "run_planning",
            new=_wrap_run_planning_with_consult(real_run_planning, slow_consult),
        ),
        patch.object(ex, "build_repo_summary", return_value="summary"),
        patch.object(ex, "clone_repo", return_value=(True, "")),
        patch.object(kb, "heartbeat_claim", side_effect=track_heartbeat),
        patch.object(
            executor, "_heartbeat_now", wraps=executor._heartbeat_now
        ) as hb_spy,
    ):
        executor._phase_planning(
            conn, task_id, run_id, meta, ex.pipeline_state(meta)
        )

    assert hb_spy.call_count >= 1
    window_beats = [
        t
        for t in heartbeat_times
        if consult_window["start"] <= t <= consult_window["end"]
    ]
    assert window_beats

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "running"
    assert executor._active[task_id].phase == ex.PHASE_ROUTING
    meta_after = ex.load_run_metadata(conn, run_id)
    state = ex.pipeline_state(meta_after)
    assert state.get("phase") == ex.PHASE_ROUTING
    assert (state.get("contract") or {}).get("task_summary") == "fix bug"
    assert not any(
        t.name.startswith("dev-hb-") and t.is_alive() for t in threading.enumerate()
    )
    conn.close()
