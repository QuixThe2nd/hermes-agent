"""Idle gate behavior against real sqlite state.db files."""

from __future__ import annotations

import time

import pytest

from hermes_state import SessionDB
from plugins.auto_update.idle import evaluate_idle


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "state.db"
    SessionDB(db_path=path)
    return path


def test_missing_db_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    snap = evaluate_idle(idle_minutes=8, db_path=home / "state.db")
    assert snap.idle is False
    assert snap.blockers[0].code == "db_missing"


def test_recent_activity_blocks(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-recent", "cli")
    db.append_message(sid, role="user", content="hello")
    db.append_message(sid, role="assistant", content="hi", finish_reason="stop")
    snap = evaluate_idle(idle_minutes=60, db_path=db_path)
    assert snap.idle is False
    assert any(b.code == "recent_activity" for b in snap.blockers)


def test_unanswered_user_work_blocks(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-unanswered", "cli")
    db.append_message(sid, role="user", content="still waiting")
    snap = evaluate_idle(idle_minutes=0, db_path=db_path, now=time.time() + 1000)
    assert snap.idle is False
    assert any(b.code == "unanswered" for b in snap.blockers)


def test_streaming_blocks(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-stream", "cli")
    db.append_message(sid, role="user", content="go")
    db.append_message(sid, role="assistant", content="partial", finish_reason=None)
    snap = evaluate_idle(idle_minutes=0, db_path=db_path, now=time.time() + 1000)
    assert snap.idle is False
    assert any(b.code == "streaming" for b in snap.blockers)


def test_compression_lock_blocks(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-compress", "cli")
    assert db.try_acquire_compression_lock(sid, holder="pid=123", ttl_seconds=60)
    snap = evaluate_idle(idle_minutes=0, db_path=db_path, now=time.time())
    assert snap.idle is False
    assert any(b.code == "compression" for b in snap.blockers)


def test_idle_when_no_active_sessions(db_path):
    snap = evaluate_idle(idle_minutes=8, db_path=db_path)
    assert snap.idle is True
