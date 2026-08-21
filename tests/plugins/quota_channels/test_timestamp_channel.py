"""Managed timestamp voice-channel tests (no live Discord)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from plugins.quota_channels.core import (
    QuotaChannelsError,
    apply_position_moves,
    create_ts_channel,
    fmt_ts,
    maintain_timestamp_channel,
    persist_ts_channel_id,
    plan_ts_bottom_move,
    resolve_ts_channel,
    save_state,
    state_path,
    timestamp_channel_name,
    validate_quota_config,
)

HEADERS = {"Authorization": "Bot test", "Content-Type": "application/json"}
CONFIG = validate_quota_config(
    {"guild_id": "guild1", "category_id": "cat1", "channel_ids": {"codex": "ch1"}, "enabled_providers": ["codex"]}
)


def _http_json(payload, status=200):
    return lambda req, timeout=25.0: (status, json.dumps(payload).encode())


def _ch(cid, parent="cat1", pos=0):
    return {"id": cid, "parent_id": parent, "position": pos}


def _guild_fixture():
    return {
        "cat1": {"id": "cat1", "name": "Quotas • old", "type": 4, "parent_id": None, "position": 0},
        "q1": {"id": "q1", "name": "Codex: 1%", "type": 2, "parent_id": "cat1", "position": 0},
        "q2": {"id": "q2", "name": "Kimi: 2%", "type": 2, "parent_id": "cat1", "position": 1},
    }


def _guild_with_ts(ts_id="ts1", name="old", pos=2):
    ch = _guild_fixture()
    ch[ts_id] = {"id": ts_id, "name": name, "type": 2, "parent_id": "cat1", "position": pos}
    return ch


class FakeDiscord:
    def __init__(self, channels: dict):
        self.channels, self.posts, self.guild_patches = channels, [], []

    def __call__(self, req, timeout=25.0):
        method, url = req.get_method(), req.full_url
        body = req.data.decode() if req.data else ""
        if "/guilds/" in url:
            if method == "GET":
                return 200, json.dumps(list(self.channels.values())).encode()
            if method == "POST":
                data = json.loads(body)
                cid = f"created{len(self.channels)}"
                ch = {"id": cid, "name": data["name"], "type": 2, "parent_id": data["parent_id"], "position": 0}
                self.channels[cid], self.posts = ch, self.posts + [data]
                return 201, json.dumps(ch).encode()
            moves = json.loads(body)
            self.guild_patches.append(moves)
            for m in moves:
                self.channels[m["id"]]["position"] = m["position"]
            return 204, b""
        cid = url.rsplit("/", 1)[1]
        ch = self.channels.get(cid)
        if ch is None:
            return 404, b'{"message": "Not Found"}'
        if method == "GET":
            return 200, json.dumps(ch).encode()
        data = json.loads(body)
        if "name" in data:
            ch["name"] = data["name"]
        return 200, json.dumps(ch).encode()


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(
    "dt,expected",
    [
        ((2025, 8, 14, 18, 12), "14/8 6:12pm"),
        ((2025, 1, 5, 9, 7), "5/1 9:07am"),
        ((2025, 8, 14, 0, 5), "14/8 12:05am"),
        ((2025, 8, 14, 12, 0), "14/8 12:00pm"),
        ((2025, 12, 31, 23, 3), "31/12 11:03pm"),
    ],
)
def test_fmt_ts(dt, expected):
    assert fmt_ts(datetime(*dt).timestamp()) == expected


@pytest.mark.parametrize(
    "value",
    [None, 0, -1, "not-a-number", float("nan"), float("inf"), float("-inf"), 1e30, True, False],
)
def test_timestamp_channel_name_corrupt_is_pending(value):
    assert timestamp_channel_name(value) == "pending"


def test_timestamp_channel_name_valid():
    assert timestamp_channel_name(datetime(2025, 8, 14, 18, 12).timestamp()) == "14/8 6:12pm"


def test_save_state_merges_preserving_keys(hermes_home):
    state_path().write_text(json.dumps({"last_quota_success": 5, "ts_channels": {"cat1": "ts9"}, "extra": 1}))
    assert save_state(now_fn=lambda: 1234) == 1234
    assert json.loads(state_path().read_text()) == {
        "last_quota_success": 1234, "ts_channels": {"cat1": "ts9"}, "extra": 1,
    }


@pytest.mark.parametrize("initial", [None, "not json"])
def test_save_state_tolerates_missing_or_corrupt_file(hermes_home, initial):
    if initial:
        state_path().write_text(initial)
    assert save_state(now_fn=lambda: 42) == 42
    assert json.loads(state_path().read_text()) == {"last_quota_success": 42}


def test_persist_ts_channel_id_preserves_sibling_keys(hermes_home):
    state_path().write_text(json.dumps({"last_quota_success": 555, "extra": "keep"}))
    persist_ts_channel_id("cat1", "newts")
    state = json.loads(state_path().read_text())
    assert state == {"last_quota_success": 555, "extra": "keep", "ts_channels": {"cat1": "newts"}}


def test_resolve_ts_channel_accepts_stored_type2():
    state = {"ts_channels": {"cat1": "ts1"}}
    assert resolve_ts_channel(state, "cat1", HEADERS, http_fn=_http_json({"id": "ts1", "type": 2, "parent_id": "cat1"})) == "ts1"


@pytest.mark.parametrize(
    "state,payload,status",
    [
        ({}, {}, 200),
        ({"ts_channels": {}}, {}, 200),
        ({"ts_channels": {"cat1": "gone"}}, {"message": "Not Found"}, 404),
        ({"ts_channels": {"cat1": "ts1"}}, {"id": "ts1", "type": 2, "parent_id": "other"}, 200),
        ({"ts_channels": {"cat1": "ts1"}}, {"id": "ts1", "type": 2, "parent_id": None}, 200),
        ({"ts_channels": {"cat1": "ts1"}}, {"id": "ts1", "type": 0, "parent_id": "cat1"}, 200),
    ],
)
def test_resolve_ts_channel_rejects(state, payload, status):
    assert resolve_ts_channel(state, "cat1", HEADERS, http_fn=_http_json(payload, status)) is None


def test_resolve_ts_channel_never_adopts_by_name():
    urls = []
    state = {"ts_channels": {"cat1": "ts1"}}
    assert resolve_ts_channel(state, "cat1", HEADERS, http_fn=lambda r, t=25: (urls.append(r.full_url) or 404, b'{}')) is None
    assert urls and all(u.endswith("/channels/ts1") for u in urls)


def test_create_ts_channel_body_exact_keys_no_topic():
    bodies = []
    assert create_ts_channel(
        "guild1", "14/8 6:12pm", "cat1", HEADERS,
        http_fn=lambda r, t=25: (bodies.append(json.loads(r.data.decode())) or 201, json.dumps({"id": "newts"}).encode()),
    ) == "newts"
    assert bodies[0] == {"name": "14/8 6:12pm", "type": 2, "parent_id": "cat1"} and "topic" not in bodies[0]


def test_create_ts_channel_non_2xx_raises():
    with pytest.raises(QuotaChannelsError, match="channel create returned 400"):
        create_ts_channel("guild1", "pending", "cat1", HEADERS, http_fn=lambda r, t=25: (400, b'{}'))


@pytest.mark.parametrize(
    "channels,expected",
    [
        ([_ch("q1", pos=0), _ch("q2", pos=1), _ch("ts1", pos=0)], {"id": "ts1", "position": 2}),
        ([_ch("q1", pos=0), _ch("q2", pos=1), _ch("ts1", pos=2)], None),
        ([_ch("q1", pos=0), _ch("q2", pos=1), _ch("ts1", pos=1)], {"id": "ts1", "position": 2}),
        ([_ch("ts1", pos=0)], None),
        ([_ch("x1", parent="elsewhere", pos=9), _ch("ts1", pos=0)], None),
    ],
)
def test_plan_ts_bottom_move(channels, expected):
    assert plan_ts_bottom_move("ts1", channels, "cat1") == expected


def test_apply_position_moves_429_raises():
    with pytest.raises(QuotaChannelsError, match="channel-position PATCH rate-limited"):
        apply_position_moves("guild1", [{"id": "ts1", "position": 2}], HEADERS, http_fn=lambda r, t=25: (429, b'{}'))


def test_maintain_creates_moves_and_persists(hermes_home):
    state_path().write_text(json.dumps({"last_quota_success": 555}))
    discord = FakeDiscord(_guild_fixture())
    result = maintain_timestamp_channel(CONFIG, HEADERS, http_fn=discord)
    new_id = result["channel_id"]
    assert result["created"] and result["moved"] and result["rename"] == "unchanged"
    assert result["name"] == fmt_ts(555) == discord.channels[new_id]["name"]
    assert discord.posts == [{"name": result["name"], "type": 2, "parent_id": "cat1"}]
    assert discord.guild_patches == [[{"id": new_id, "position": 2}]]
    assert json.loads(state_path().read_text()) == {"last_quota_success": 555, "ts_channels": {"cat1": new_id}}


@pytest.mark.parametrize("state_text", [None, json.dumps({"last_quota_success": "bad"})])
def test_maintain_pending_for_missing_or_corrupt_timestamp(hermes_home, state_text):
    if state_text:
        state_path().write_text(state_text)
    discord = FakeDiscord(_guild_fixture())
    result = maintain_timestamp_channel(CONFIG, HEADERS, http_fn=discord)
    assert result["name"] == "pending" and discord.posts[0]["name"] == "pending"


def test_maintain_reuses_stored_id_when_valid(hermes_home):
    state_path().write_text(json.dumps({"last_quota_success": 555, "ts_channels": {"cat1": "ts1"}}))
    discord = FakeDiscord(_guild_with_ts("ts1", fmt_ts(555), 2))
    result = maintain_timestamp_channel(CONFIG, HEADERS, http_fn=discord)
    assert result == {"channel_id": "ts1", "name": fmt_ts(555), "rename": "unchanged", "created": False, "moved": False}
    assert discord.posts == [] and discord.guild_patches == []


def test_maintain_replaces_vanished_channel(hermes_home):
    state_path().write_text(json.dumps({"last_quota_success": 555, "ts_channels": {"cat1": "gone"}}))
    discord = FakeDiscord(_guild_fixture())
    result = maintain_timestamp_channel(CONFIG, HEADERS, http_fn=discord)
    state = json.loads(state_path().read_text())
    assert result["created"] and len(discord.posts) == 1
    assert state["ts_channels"]["cat1"] == result["channel_id"] and state["last_quota_success"] == 555


def test_maintain_position_patch_429_raises(hermes_home):
    state_path().write_text(json.dumps({"last_quota_success": 555, "ts_channels": {"cat1": "ts1"}}))
    discord = FakeDiscord(_guild_with_ts("ts1", "stale", 0))

    def http_429(req, timeout=25.0):
        return (429, b'{}') if req.get_method() == "PATCH" and "/guilds/" in req.full_url else discord(req, timeout)

    with pytest.raises(QuotaChannelsError, match="channel-position PATCH rate-limited"):
        maintain_timestamp_channel(CONFIG, HEADERS, http_fn=http_429)


@pytest.mark.parametrize("force,did_quota,state_before", [
    (False, False, {"last_quota_success": 999_999_990}),
    (True, True, {"ts_channels": {"cat1": "ts1"}}),
])
def test_run_tick_wiring(hermes_home, monkeypatch, force, did_quota, state_before):
    from plugins.quota_channels import core

    now = 1_000_000_000.0
    state_path().write_text(json.dumps(state_before))
    monkeypatch.setattr(core, "discord_headers", lambda: HEADERS)
    if force:
        monkeypatch.setattr(core, "run_provider_quota", lambda *a, **k: ("Codex", 1, "Codex: 50% • 1d left", "renamed"))
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        discord = FakeDiscord(_guild_with_ts("ts1", "old", 2))
    else:
        discord = FakeDiscord(_guild_fixture())

    result = core.run_tick(CONFIG, force=force, now_fn=lambda: now, sleep_fn=lambda _: None, http_fn=discord)
    ts = result["timestamp_channel"]

    assert result["did_quota"] is did_quota and result["success"] is True
    assert result["sorted"] is False
    if force:
        assert result["providers"] == {"Codex": {"remaining": 50, "reset_seconds": 1, "rename": "renamed"}}
        assert result["category"] == "renamed"
        assert ts["created"] is False and ts["channel_id"] == "ts1" and ts["name"] == fmt_ts(int(now))
        assert discord.channels["ts1"]["name"] == ts["name"]
    else:
        assert result["providers"] == {}
        assert ts["created"] is True and ts["name"] == fmt_ts(int(now - 10))

    state = json.loads(state_path().read_text())
    if force:
        assert state["last_quota_success"] == int(now) and state["ts_channels"] == {"cat1": "ts1"}
    else:
        assert state["last_quota_success"] == int(now - 10) and state["ts_channels"] == {"cat1": ts["channel_id"]}
