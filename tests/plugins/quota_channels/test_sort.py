"""Voice-channel sort planning tests."""

import pytest

from plugins.quota_channels.core import QuotaChannelsError, plan_position_moves


class TestPlanPositionMoves:
    def test_reassigns_slots_ascending_by_days(self):
        entries = [
            ("Codex", "c1", 7),
            ("Kimi", "c2", 1),
            ("z.ai", "c3", 3),
        ]
        guild_channels = [
            {"id": "c1", "position": 10},
            {"id": "c2", "position": 11},
            {"id": "c3", "position": 12},
        ]
        moves = plan_position_moves(entries, guild_channels)
        assert moves == [
            {"id": "c2", "position": 10},
            {"id": "c3", "position": 11},
            {"id": "c1", "position": 12},
        ]

    def test_stable_ties_keep_relative_order(self):
        entries = [
            ("Codex", "c1", 5),
            ("Kimi", "c2", 5),
            ("z.ai", "c3", 5),
        ]
        guild_channels = [
            {"id": "c1", "position": 1},
            {"id": "c2", "position": 2},
            {"id": "c3", "position": 3},
        ]
        assert plan_position_moves(entries, guild_channels) == []

    def test_no_patch_when_order_already_correct(self):
        entries = [
            ("Kimi", "c2", 1),
            ("z.ai", "c3", 3),
            ("Codex", "c1", 7),
        ]
        guild_channels = [
            {"id": "c2", "position": 4},
            {"id": "c3", "position": 5},
            {"id": "c1", "position": 6},
        ]
        assert plan_position_moves(entries, guild_channels) == []

    def test_missing_channel_is_error(self):
        entries = [("Codex", "c1", 1)]
        with pytest.raises(QuotaChannelsError, match="expected 1 managed voice channels"):
            plan_position_moves(entries, [])

    def test_quota_block_then_token_block(self):
        entries = [
            ("Codex", "q1", (0, 7200)),
            ("Kimi", "q2", (0, 3600)),
            ("Codex", "t1", (1, 0)),
            ("Kimi", "t2", (1, 1)),
            ("z.ai", "t3", (1, 2)),
        ]
        guild_channels = [
            {"id": "q1", "position": 12},
            {"id": "q2", "position": 10},
            {"id": "t1", "position": 14},
            {"id": "t2", "position": 11},
            {"id": "t3", "position": 13},
            {"id": "unmanaged", "position": 15},
        ]
        moves = plan_position_moves(entries, guild_channels)
        assert moves == [
            {"id": "q1", "position": 11},
            {"id": "t1", "position": 12},
            {"id": "t2", "position": 13},
            {"id": "t3", "position": 14},
        ]
        managed_ids = {move["id"] for move in moves}
        assert "unmanaged" not in managed_ids
