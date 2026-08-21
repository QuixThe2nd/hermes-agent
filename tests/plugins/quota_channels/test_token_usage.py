"""Token usage channel tests for quota_channels."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from plugins.quota_channels.core import (
    QuotaChannelsError,
    PROVIDER_SPECS,
    category_name,
    cursor_agg_usage_body,
    fetch_codex_profile,
    fetch_cursor_aggregated_usage,
    fetch_zai_model_usage,
    format_compact_tokens,
    format_token_name,
    parse_codex_profile_tokens,
    parse_cursor_aggregated_usage,
    parse_zai_model_usage,
    redact_secrets,
    run_codex_token_provider,
    run_tick,
    run_token_provider,
    run_token_tick,
    unsupported_token_name,
    validate_quota_config,
)
from plugins.quota_channels.tool import handle_quota_channels_tick


@pytest.fixture(autouse=True)
def _restore_plugin_modules():
    prefixes = ("plugins.quota_channels", "hermes_cli.plugins")
    saved = {k: m for k, m in sys.modules.items() if k.startswith(prefixes)}
    yield
    for key in list(sys.modules):
        if key.startswith(prefixes):
            del sys.modules[key]
    sys.modules.update(saved)
    for key, mod in saved.items():
        if "." in key:
            parent_name, attr = key.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, attr, mod)


def _request_method(req) -> str:
    return getattr(req, "method", None) or req.get_method()


def _discord_ok(req, timeout=25.0):
    method = _request_method(req)
    if "discord.com" not in req.full_url:
        raise AssertionError(f"unexpected non-discord request: {req.full_url}")
    if method == "GET":
        return 200, json.dumps({"name": "old-name"}).encode()
    if method == "PATCH":
        return 200, json.dumps({"name": "patched"}).encode()
    raise AssertionError((method, req.full_url))


class HttpRecorder:
    def __init__(self, handler=_discord_ok):
        self.urls: list[str] = []
        self.handler = handler

    def __call__(self, req, timeout=25.0):
        self.urls.append(req.full_url)
        return self.handler(req, timeout)


def _patch_run_tick_http(monkeypatch, fake_http):
    """Inject fake HTTP into run_tick despite stale default-arg binding."""
    import plugins.quota_channels.core as core

    monkeypatch.setattr(core, "default_http", fake_http)
    original = core.run_tick

    def bound_run_tick(
        config,
        *,
        force=False,
        sleep_fn=core.time.sleep,
        now_fn=core.time.time,
        http_fn=fake_http,
    ):
        return original(
            config,
            force=force,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            http_fn=http_fn,
        )

    monkeypatch.setattr(core, "run_tick", bound_run_tick)
    monkeypatch.setattr("plugins.quota_channels.tool.run_tick", bound_run_tick)


def _base_section(**overrides):
    section = {
        "guild_id": "100",
        "category_id": "200",
        "channel_ids": {
            "codex": "301",
            "kimi": "302",
            "zai": "303",
            "cursor": "304",
            "grok": "305",
        },
        "enabled_providers": ["codex", "kimi", "zai", "cursor", "grok"],
    }
    section.update(overrides)
    return section


def _token_section(**overrides):
    token = {
        "enabled": True,
        "channel_ids": {
            "codex": "401",
            "zai": "402",
            "cursor": "403",
            "kimi": "404",
            "grok": "405",
        },
    }
    token.update(overrides)
    return token


    return config


def _write_tick_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "discord.env").write_text("DISCORD_BOT_TOKEN=bot-test-token\n")
    (secrets / "zai.env").write_text("ZAI_API_KEY=zai-secret-key\n")
    (tmp_path / ".env").write_text("KIMI_API_KEY=kimi-secret-key\n")
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": "codex-access-token",
                            "refresh_token": "codex-refresh-token",
                        }
                    },
                    "xai-oauth": {
                        "tokens": {
                            "access_token": "grok-access-token",
                            "refresh_token": "grok-refresh-token",
                        }
                    },
                }
            }
        )
    )
    cursor_dir = tmp_path / ".config" / "cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "auth.json").write_text(
        json.dumps({"accessToken": "cursor-access-token"})
    )
    monkeypatch.setattr("plugins.quota_channels.core.Path.home", lambda: tmp_path)


def _setup_tick_env(monkeypatch, tmp_path, *, token_usage=None, force_quota=True):
    _write_tick_credentials(monkeypatch, tmp_path)

    section = _base_section()
    if token_usage is not None:
        section["token_usage"] = token_usage

    config = validate_quota_config(section)

    monkeypatch.setattr(
        "plugins.quota_channels.core.run_provider_quota",
        lambda *a, **k: ("Codex", 3600, "Codex: 90% \u2022 1h left", "renamed"),
    )
    monkeypatch.setattr(
        "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
    )
    monkeypatch.setattr(
        "plugins.quota_channels.core.update_category", lambda *a, **k: "renamed"
    )
    monkeypatch.setattr(
        "plugins.quota_channels.core.save_state", lambda now_fn=None: 1_700_000_000
    )
    monkeypatch.setattr(
        "plugins.quota_channels.core.load_state",
        lambda: {"last_quota_success": 0},
    )

    return config


class TestV1ConfigMigration:
    FIXED_NOW = 1_700_000_000.0
    LAST_SUCCESS = 1_700_000_000

    QUOTA_RESETS = {
        "codex": ("Codex", 7200, "Codex: 90% \u2022 2h left"),
        "kimi": ("Kimi", 3600, "Kimi: 80% \u2022 1h left"),
        "zai": ("z.ai", 1800, "z.ai: 70% \u2022 30m left"),
        "cursor": ("Cursor", 900, "Cursor: 60%/50% \u2022 15m left"),
        "grok": ("Grok", 600, "Grok: 50% \u2022 10m left"),
    }

    def test_v1_config_full_tick_quota_only(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(_base_section())
        provider_calls = []

        def fake_run_provider(key, channel_id, headers, http_fn=None, now_fn=None):
            from plugins.quota_channels.core import rename_channel

            provider_calls.append((key, channel_id))
            label, reset_secs, name = self.QUOTA_RESETS[key]
            rename = rename_channel(channel_id, name, headers, http_fn=http_fn)
            return label, reset_secs, name, rename

        monkeypatch.setattr(
            "plugins.quota_channels.core.run_provider_quota", fake_run_provider
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state",
            lambda now_fn=None: self.LAST_SUCCESS,
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )

        quota_channel_ids = [config["channel_ids"][key] for key, _ in PROVIDER_SPECS]
        guild_channels = [
            {"id": "301", "position": 10},
            {"id": "302", "position": 11},
            {"id": "303", "position": 12},
            {"id": "304", "position": 13},
            {"id": "305", "position": 14},
            {"id": "999", "position": 20},
        ]
        rename_bodies = []
        category_bodies = []
        position_patches = []

        def fake_http(req, timeout=25.0):
            url = req.full_url
            method = _request_method(req)
            if method == "GET" and url.endswith("/guilds/100/channels"):
                return 200, json.dumps(guild_channels).encode()
            if "discord.com" in url and method == "GET":
                return 200, json.dumps({"name": "old-name"}).encode()
            if method == "PATCH" and url.endswith("/guilds/100/channels"):
                position_patches.append(json.loads(req.data.decode()))
                return 204, b""
            if method == "PATCH" and "/channels/200" in url:
                category_bodies.append(json.loads(req.data.decode()))
                return 200, json.dumps({"name": "patched"}).encode()
            if method == "PATCH":
                rename_bodies.append((url, json.loads(req.data.decode())))
                return 200, json.dumps({"name": "patched"}).encode()
            raise AssertionError((method, url))

        expected_category = category_name(
            self.LAST_SUCCESS, now_fn=lambda: self.FIXED_NOW
        )

        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=fake_http,
            now_fn=lambda: self.FIXED_NOW,
        )

        assert result["success"] is True
        assert result["did_quota"] is True
        assert "token_usage" not in result
        assert provider_calls == [
            (key, config["channel_ids"][key]) for key, _ in PROVIDER_SPECS
        ]
        assert rename_bodies == [
            (
                f"https://discord.com/api/v10/channels/{cid}",
                {"name": self.QUOTA_RESETS[key][2]},
            )
            for key, cid in [
                ("codex", "301"),
                ("kimi", "302"),
                ("zai", "303"),
                ("cursor", "304"),
                ("grok", "305"),
            ]
        ]
        assert category_bodies == [
            {"name": expected_category},
            {"name": expected_category},
        ]
        assert "Token" not in expected_category
        assert position_patches == [
            [
                {"id": "305", "position": 10},
                {"id": "304", "position": 11},
                {"id": "302", "position": 13},
                {"id": "301", "position": 14},
            ]
        ]
        move_ids = {move["id"] for patch in position_patches for move in patch}
        assert move_ids == {"301", "302", "304", "305"}
        assert move_ids <= set(quota_channel_ids)
        assert "999" not in move_ids


class TestConfigBackwardCompat:
    def test_old_config_without_token_usage(self):
        config = validate_quota_config(_base_section())
        assert config["token_usage"] is None

    def test_old_config_tick_has_no_token_key(self, monkeypatch, tmp_path):
        config = _setup_tick_env(monkeypatch, tmp_path, token_usage=None)
        recorder = HttpRecorder()
        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=recorder,
        )
        assert "token_usage" not in result
        assert not any("model-usage" in u for u in recorder.urls)
        assert not any("profiles/me" in u for u in recorder.urls)
        assert not any("GetAggregatedUsageEvents" in u for u in recorder.urls)


class TestTokenUsageDisabled:
    @pytest.mark.parametrize(
        "token_usage",
        [None, {"enabled": False, "channel_ids": {"codex": "401"}}],
    )
    def test_disabled_makes_no_token_calls(self, monkeypatch, tmp_path, token_usage):
        config = _setup_tick_env(monkeypatch, tmp_path, token_usage=token_usage)
        recorder = HttpRecorder()
        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=recorder,
        )
        assert "token_usage" not in result
        token_urls = [
            u
            for u in recorder.urls
            if any(
                marker in u
                for marker in (
                    "401",
                    "402",
                    "403",
                    "404",
                    "405",
                    "model-usage",
                    "profiles/me",
                    "GetAggregatedUsageEvents",
                )
            )
        ]
        assert token_urls == []


class TestPartialChannelMapping:
    def test_unmapped_providers_skipped(self, monkeypatch, tmp_path):
        token = _token_section(channel_ids={"codex": "401", "zai": "402"})
        config = _setup_tick_env(monkeypatch, tmp_path, token_usage=token)

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "profiles/me" in url:
                body = json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {
                                    "start_date": "2026-08-15",
                                    "tokens": 1000,
                                }
                            ]
                        }
                    }
                ).encode()
                return 200, body
            if "model-usage" in url:
                body = json.dumps(
                    {
                        "code": 200,
                        "data": {"totalUsage": {"totalTokensUsage": 5000}},
                    }
                ).encode()
                return 200, body
            return _discord_ok(req, timeout)

        fixed_now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=fake_http,
            now_fn=lambda: fixed_now,
        )
        providers = result["token_usage"]["providers"]
        assert providers["Cursor"]["status"] == "skipped"
        assert providers["Kimi"]["status"] == "skipped"
        assert providers["Grok"]["status"] == "skipped"
        assert providers["Codex"]["status"] in ("updated", "unchanged")
        assert providers["z.ai"]["status"] in ("updated", "unchanged")


class TestMergedCategoryOrdering:
    FIXED_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    LAST_SUCCESS = 1_700_000_000

    QUOTA_RESETS = {
        "codex": ("Codex", 7200, "Codex: 90% \u2022 2h left"),
        "kimi": ("Kimi", 3600, "Kimi: 80% \u2022 1h left"),
        "zai": ("z.ai", 1800, "z.ai: 70% \u2022 30m left"),
        "cursor": ("Cursor", 900, "Cursor: 60%/50% \u2022 15m left"),
        "grok": ("Grok", 600, "Grok: 50% \u2022 10m left"),
    }

    def test_quota_block_then_token_block_in_one_category(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(
            _base_section(token_usage=_token_section())
        )

        def fake_run_provider(key, channel_id, headers, http_fn=None, now_fn=None):
            label, reset_secs, name = self.QUOTA_RESETS[key]
            return label, reset_secs, name, "renamed"

        monkeypatch.setattr(
            "plugins.quota_channels.core.run_provider_quota", fake_run_provider
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state",
            lambda now_fn=None: self.LAST_SUCCESS,
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )

        guild_channels = [
            {"id": "301", "position": 10},
            {"id": "302", "position": 11},
            {"id": "303", "position": 12},
            {"id": "304", "position": 13},
            {"id": "305", "position": 14},
            {"id": "401", "position": 15},
            {"id": "404", "position": 16},
            {"id": "402", "position": 17},
            {"id": "403", "position": 18},
            {"id": "405", "position": 19},
            {"id": "999", "position": 25},
        ]
        position_patches = []
        sort_orders = []
        import plugins.quota_channels.core as core

        original_plan = core.plan_position_moves

        def capture_plan(entries, channels):
            ordered = sorted(entries, key=lambda item: item[2])
            sort_orders.append([cid for _, cid, _ in ordered])
            return original_plan(entries, channels)

        monkeypatch.setattr(core, "plan_position_moves", capture_plan)

        def fake_http(req, timeout=25.0):
            url = req.full_url
            method = _request_method(req)
            if "profiles/me" in url:
                body = json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {"start_date": "2026-08-21", "tokens": 1000}
                            ]
                        }
                    }
                ).encode()
                return 200, body
            if "model-usage" in url:
                body = json.dumps(
                    {
                        "code": 200,
                        "data": {"totalUsage": {"totalTokensUsage": 5000}},
                    }
                ).encode()
                return 200, body
            if "GetAggregatedUsageEvents" in url:
                body = json.dumps(
                    {"totalInputTokens": 100, "totalOutputTokens": 50}
                ).encode()
                return 200, body
            if method == "GET" and url.endswith("/guilds/100/channels"):
                return 200, json.dumps(guild_channels).encode()
            if "discord.com" in url and method == "GET":
                return 200, json.dumps({"name": "old-name"}).encode()
            if method == "PATCH" and url.endswith("/guilds/100/channels"):
                position_patches.append(json.loads(req.data.decode()))
                return 204, b""
            if method == "PATCH":
                return 200, json.dumps({"name": "patched"}).encode()
            raise AssertionError((method, url))

        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=fake_http,
            now_fn=lambda: self.FIXED_NOW,
        )

        assert result["token_usage"]["did_run"] is True
        assert sort_orders == [
            [
                "305",
                "304",
                "303",
                "302",
                "301",
                "401",
                "404",
                "402",
                "403",
                "405",
            ]
        ]
        assert len(position_patches) == 1
        move_ids = {move["id"] for move in position_patches[0]}
        managed_ids = {
            "301",
            "302",
            "303",
            "304",
            "305",
            "401",
            "402",
            "403",
            "404",
            "405",
        }
        assert move_ids <= managed_ids
        assert move_ids == {"301", "302", "304", "305"}
        assert "999" not in move_ids


class TestFormatting:
    @pytest.mark.parametrize(
        "count, expected",
        [
            (999, "999"),
            (1000, "1.0K"),
            (999_950, "1000.0K"),
            (1_000_000_000, "1.0B"),
        ],
    )
    def test_format_compact_tokens(self, count, expected):
        assert format_compact_tokens(count) == expected

    def test_format_token_name(self):
        assert format_token_name("Codex", 226_600_000) == "Codex: 226.6M tok/7d"

    def test_unsupported_token_name(self):
        assert unsupported_token_name("Kimi") == "Kimi: no token API"


class TestCodexTokenUsage:
    FIXED_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)

    def _buckets_payload(self):
        today = self.FIXED_NOW.date()
        buckets = []
        for offset in range(10):
            day = today - timedelta(days=offset)
            buckets.append(
                {"start_date": day.isoformat(), "tokens": (offset + 1) * 1000}
            )
        return json.dumps({"stats": {"daily_usage_buckets": buckets}})

    def test_parse_sums_latest_seven_calendar_days(self):
        text = self._buckets_payload()
        total = parse_codex_profile_tokens(
            text, now_fn=lambda: self.FIXED_NOW.timestamp()
        )
        assert total == sum((i + 1) * 1000 for i in range(7))

    @pytest.mark.parametrize(
        "payload",
        [
            "not-json",
            json.dumps({}),
            json.dumps({"stats": {"daily_usage_buckets": []}}),
            json.dumps(
                {
                    "stats": {
                        "daily_usage_buckets": [
                            {"start_date": "2026-08-21", "tokens": "x"}
                        ]
                    }
                }
            ),
        ],
    )
    def test_parse_errors(self, payload):
        with pytest.raises(QuotaChannelsError):
            parse_codex_profile_tokens(payload, now_fn=lambda: self.FIXED_NOW.timestamp())

    def test_fetch_codex_profile_url_and_bearer(self):
        seen = {}

        def fake_http(req, timeout=25.0):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            return 200, b"{}"

        status, _ = fetch_codex_profile("codex-access-token", http_fn=fake_http)
        assert status == 200
        assert seen["url"] == "https://chatgpt.com/backend-api/wham/profiles/me"
        assert seen["auth"] == "Bearer codex-access-token"

    def test_refresh_on_401(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        auth = tmp_path / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {
                                "access_token": "old-access",
                                "refresh_token": "old-refresh",
                            }
                        }
                    }
                }
            )
        )
        profile_calls = []
        refresh_calls = []

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "oauth/token" in url:
                refresh_calls.append(url)
                body = json.dumps({"access_token": "new-access"}).encode()
                return 200, body
            if "profiles/me" in url:
                profile_calls.append(req.headers.get("Authorization"))
                if len(profile_calls) == 1:
                    return 401, b"unauthorized"
                body = json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {
                                    "start_date": self.FIXED_NOW.date().isoformat(),
                                    "tokens": 42,
                                }
                            ]
                        }
                    }
                ).encode()
                return 200, body
            raise AssertionError(url)

        name, total = run_codex_token_provider(
            http_fn=fake_http, now_fn=lambda: self.FIXED_NOW.timestamp()
        )
        assert total == 42
        assert name == "Codex: 42 tok/7d"
        assert len(refresh_calls) == 1
        assert profile_calls[0] == "Bearer old-access"
        assert profile_calls[1] == "Bearer new-access"

    def test_non_200_redacts_tokens(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {
                                "access_token": "leak-access",
                                "refresh_token": "leak-refresh",
                            }
                        }
                    }
                }
            )
        )

        def fake_http(req, timeout=25.0):
            if "profiles/me" in req.full_url:
                return 500, b"error mentioning leak-access and leak-refresh"
            raise AssertionError(req.full_url)

        with pytest.raises(QuotaChannelsError) as exc:
            run_codex_token_provider(http_fn=fake_http)
        msg = str(exc.value)
        assert "leak-access" not in msg
        assert "leak-refresh" not in msg
        assert "[redacted]" in msg


class TestZaiTokenUsage:
    FIXED_NOW = datetime(2026, 8, 21, 15, 30, 45, tzinfo=timezone.utc)

    def test_fetch_emits_exact_utc_query(self):
        captured = {}

        def fake_http(req, timeout=25.0):
            captured["url"] = req.full_url
            return 200, json.dumps(
                {"code": 200, "data": {"totalUsage": {"totalTokensUsage": 1}}}
            ).encode()

        fetch_zai_model_usage(
            "zai-secret-key",
            http_fn=fake_http,
            now_fn=lambda: self.FIXED_NOW.timestamp(),
        )
        url = captured["url"]
        assert "startTime=2026-08-14%2015%3A30%3A45" in url
        assert "endTime=2026-08-21%2015%3A30%3A45" in url

    def test_parse_reads_total_tokens_usage(self):
        text = json.dumps(
            {"code": 200, "data": {"totalUsage": {"totalTokensUsage": 12345}}}
        )
        assert parse_zai_model_usage(text) == 12345

    def test_empty_200_body_is_error(self):
        with pytest.raises(QuotaChannelsError, match="empty"):
            parse_zai_model_usage("   ")

    @pytest.mark.parametrize(
        "payload",
        [
            "not-json",
            json.dumps({"code": 200, "data": {"totalUsage": {}}}),
            json.dumps({"code": 500, "message": "fail"}),
        ],
    )
    def test_malformed_or_missing_total(self, payload):
        with pytest.raises(QuotaChannelsError):
            parse_zai_model_usage(payload)

    def test_error_path_redacts_api_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "zai.env").write_text("ZAI_API_KEY=super-secret-zai\n")

        def fake_http(req, timeout=25.0):
            return 500, b"failure with super-secret-zai echoed"

        with pytest.raises(QuotaChannelsError) as exc:
            from plugins.quota_channels.core import run_zai_token_provider

            run_zai_token_provider(http_fn=fake_http)
        assert "super-secret-zai" not in str(exc.value)
        assert "[redacted]" in str(exc.value)


class TestCursorTokenUsage:
    FIXED_NOW_MS = 1_700_000_000_000

    def test_cursor_agg_usage_body_epoch_ms(self):
        body = cursor_agg_usage_body(self.FIXED_NOW_MS)
        assert body["endDate"] == str(self.FIXED_NOW_MS)
        assert body["startDate"] == str(self.FIXED_NOW_MS - 7 * 86_400_000)

    def test_parse_sums_input_output_ignores_cache(self):
        text = json.dumps(
            {
                "totalInputTokens": 100,
                "totalOutputTokens": 50,
                "totalCacheReadTokens": 9999,
                "totalCacheWriteTokens": 8888,
            }
        )
        assert parse_cursor_aggregated_usage(text) == 150

    @pytest.mark.parametrize(
        "payload",
        ["not-json", json.dumps({}), json.dumps({"totalInputTokens": 1})],
    )
    def test_malformed_or_missing_totals(self, payload):
        with pytest.raises(QuotaChannelsError):
            parse_cursor_aggregated_usage(payload)

    def test_401_message_mentions_relogin(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cursor_dir = tmp_path / ".config" / "cursor"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "auth.json").write_text(json.dumps({"accessToken": "cursor-jwt"}))
        monkeypatch.setattr("plugins.quota_channels.core.Path.home", lambda: tmp_path)

        def fake_http(req, timeout=25.0):
            if "GetAggregatedUsageEvents" in req.full_url:
                return 401, b"unauthorized"
            raise AssertionError(req.full_url)

        from plugins.quota_channels.core import run_cursor_token_provider

        with pytest.raises(QuotaChannelsError, match="agent login"):
            run_cursor_token_provider(http_fn=fake_http)


class TestUnsupportedProviders:
    @pytest.mark.parametrize("key,label", [("kimi", "Kimi"), ("grok", "Grok")])
    def test_unsupported_no_provider_http(self, key, label):
        patch_bodies = []

        def fake_http(req, timeout=25.0):
            method = _request_method(req)
            if "discord.com" not in req.full_url:
                raise AssertionError(f"unexpected non-discord request: {req.full_url}")
            if method == "GET":
                return 200, json.dumps({"name": "old-name"}).encode()
            if method == "PATCH":
                patch_bodies.append(json.loads(req.data.decode()))
                return 200, json.dumps({"name": "patched"}).encode()
            raise AssertionError((method, req.full_url))

        result = run_token_provider(
            key,
            label,
            "404",
            {"Authorization": "Bot x"},
            http_fn=fake_http,
        )
        assert result["status"] == "unsupported"
        assert patch_bodies == [{"name": unsupported_token_name(label)}]


class TestIsolation:
    def test_one_fetch_failure_does_not_block_others(self, monkeypatch, tmp_path):
        config = _setup_tick_env(
            monkeypatch,
            tmp_path,
            token_usage=_token_section(channel_ids={"codex": "401", "zai": "402"}),
        )
        fixed_now = datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp()

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "profiles/me" in url:
                return 500, b"codex down"
            if "model-usage" in url:
                body = json.dumps(
                    {
                        "code": 200,
                        "data": {"totalUsage": {"totalTokensUsage": 9000}},
                    }
                ).encode()
                return 200, body
            return _discord_ok(req, timeout)

        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=fake_http,
            now_fn=lambda: fixed_now,
        )
        providers = result["token_usage"]["providers"]
        assert providers["Codex"]["status"] == "failed"
        assert "codex down" in providers["Codex"]["error"]
        assert providers["z.ai"]["status"] in ("updated", "unchanged")

    def test_discord_rename_failure_does_not_block_next(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "zai.env").write_text("ZAI_API_KEY=zai-key\n")
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {
                                "access_token": "codex-access",
                                "refresh_token": "codex-refresh",
                            }
                        }
                    }
                }
            )
        )
        calls = {"zai": 0}

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "profiles/me" in url:
                body = json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {
                                    "start_date": "2026-08-21",
                                    "tokens": 10,
                                }
                            ]
                        }
                    }
                ).encode()
                return 200, body
            if "model-usage" in url:
                calls["zai"] += 1
                body = json.dumps(
                    {
                        "code": 200,
                        "data": {"totalUsage": {"totalTokensUsage": 20}},
                    }
                ).encode()
                return 200, body
            if "discord.com" in url and _request_method(req) == "PATCH" and "/401" in url:
                return 500, b"rename failed"
            return _discord_ok(req, timeout)

        headers = {"Authorization": "Bot x"}
        fixed_now = datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp()
        token_config = validate_quota_config(
            _base_section(
                token_usage=_token_section(channel_ids={"codex": "401", "zai": "402"})
            )
        )["token_usage"]
        result = run_token_tick(
            token_config,
            headers=headers,
            http_fn=fake_http,
            now_fn=lambda: fixed_now,
            did_quota=True,
        )
        assert result["providers"]["Codex"]["status"] == "failed"
        assert calls["zai"] == 1
        assert result["providers"]["z.ai"]["status"] in ("updated", "unchanged")


class TestTransientErrorPreservesName:
    def test_fetch_failure_skips_patch(self):
        patch_calls = []

        def fake_http(req, timeout=25.0):
            method = _request_method(req)
            if "profiles/me" in req.full_url:
                return 503, b"unavailable"
            if method == "PATCH":
                patch_calls.append(req.full_url)
            return _discord_ok(req, timeout)

        result = run_token_provider(
            "codex",
            "Codex",
            "401",
            {"Authorization": "Bot x"},
            http_fn=fake_http,
        )
        assert result["status"] == "failed"
        assert patch_calls == []


class TestTokenPhaseDespiteQuotaFailure:
    def test_token_runs_when_grok_quota_provider_fails(self, monkeypatch, tmp_path):
        config = _setup_tick_env(
            monkeypatch,
            tmp_path,
            token_usage=_token_section(channel_ids={"codex": "401"}),
        )

        def fake_run_provider(key, channel_id, headers, http_fn=None, now_fn=None):
            if key == "grok":
                raise QuotaChannelsError("grok quota boom")
            return "Codex", 3600, "Codex: 90% \u2022 1h left", "renamed"

        monkeypatch.setattr(
            "plugins.quota_channels.core.run_provider_quota", fake_run_provider
        )

        def fake_http(req, timeout=25.0):
            if "profiles/me" in req.full_url:
                body = json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {
                                    "start_date": "2026-08-21",
                                    "tokens": 777,
                                }
                            ]
                        }
                    }
                ).encode()
                return 200, body
            return _discord_ok(req, timeout)

        fixed_now = datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp()
        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=fake_http,
            now_fn=lambda: fixed_now,
        )
        assert "token_usage" in result
        assert result["token_usage"]["did_run"] is True
        assert result["providers"]["Grok"]["error"] == "grok quota boom"
        assert result["token_usage"]["providers"]["Codex"]["status"] in (
            "updated",
            "unchanged",
        )


class TestToolRunParity:
    def _write_config(self, hermes_home: Path, *, token_enabled: bool):
        section = _base_section(
            enabled_providers=["codex"],
            channel_ids={"codex": "301"},
        )
        if token_enabled:
            section["token_usage"] = _token_section(
                channel_ids={"codex": "401"},
            )
        (hermes_home / "config.yaml").write_text(yaml.safe_dump({"quota_channels": section}))

    def test_parity_enabled(self, monkeypatch, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        self._write_config(hermes_home, token_enabled=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        secrets = hermes_home / "secrets"
        secrets.mkdir()
        (secrets / "discord.env").write_text("DISCORD_BOT_TOKEN=bot-test\n")
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {"access_token": "tok", "refresh_token": "ref"}
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )

        def fake_http(req, timeout=25.0):
            if "profiles/me" in req.full_url:
                body = json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {"start_date": "2026-08-21", "tokens": 100}
                            ]
                        }
                    }
                ).encode()
                return 200, body
            if "wham/usage" in req.full_url:
                body = json.dumps(
                    {
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 5,
                                "reset_after_seconds": 3600,
                            }
                        }
                    }
                ).encode()
                return 200, body
            if "discord.com" in req.full_url:
                method = _request_method(req)
                if method == "GET":
                    return 200, json.dumps({"name": "old", "position": 1}).encode()
                if method == "PATCH":
                    return 200, json.dumps({"name": "new"}).encode()
            raise AssertionError(req.full_url)

        monkeypatch.setattr("plugins.quota_channels.core.default_http", fake_http)
        _patch_run_tick_http(monkeypatch, fake_http)
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        fixed_now = datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp()
        monkeypatch.setattr("plugins.quota_channels.core.time.time", lambda: fixed_now)
        kwargs = {
            "force": True,
            "sleep_fn": lambda _: None,
            "http_fn": fake_http,
            "now_fn": lambda: fixed_now,
        }

        from plugins.quota_channels.core import load_quota_config, run_tick

        direct = run_tick(load_quota_config(), **kwargs)
        tool = json.loads(handle_quota_channels_tick({"force": True}))

        from plugins.quota_channels.run import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                [
                    "--config",
                    str(hermes_home / "config.yaml"),
                    "--force-quota",
                    "--debug",
                ]
            )
        assert rc == 0
        cli = json.loads(buf.getvalue().strip())

        for result in (tool, cli):
            assert result["success"] is True
            assert "token_usage" in result
            assert result["token_usage"]["did_run"] is True

        assert tool["token_usage"]["providers"] == direct["token_usage"]["providers"]
        assert cli["token_usage"]["providers"] == direct["token_usage"]["providers"]

    def test_parity_disabled(self, monkeypatch, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        self._write_config(hermes_home, token_enabled=False)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        secrets = hermes_home / "secrets"
        secrets.mkdir()
        (secrets / "discord.env").write_text("DISCORD_BOT_TOKEN=bot-test\n")
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {"access_token": "tok", "refresh_token": "ref"}
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )

        def fake_http(req, timeout=25.0):
            if "wham/usage" in req.full_url:
                body = json.dumps(
                    {
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 5,
                                "reset_after_seconds": 3600,
                            }
                        }
                    }
                ).encode()
                return 200, body
            if "discord.com" in req.full_url:
                method = _request_method(req)
                if method == "GET":
                    return 200, json.dumps({"name": "old", "position": 1}).encode()
                if method == "PATCH":
                    return 200, json.dumps({"name": "new"}).encode()
            raise AssertionError(req.full_url)

        fixed_now = datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp()
        monkeypatch.setattr("plugins.quota_channels.core.default_http", fake_http)
        _patch_run_tick_http(monkeypatch, fake_http)
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        monkeypatch.setattr("plugins.quota_channels.core.time.time", lambda: fixed_now)

        from plugins.quota_channels.core import load_quota_config, run_tick

        tool = json.loads(handle_quota_channels_tick({"force": True}))
        direct = run_tick(
            load_quota_config(),
            force=True,
            sleep_fn=lambda _: None,
            http_fn=fake_http,
            now_fn=lambda: fixed_now,
        )
        assert "token_usage" not in tool
        assert "token_usage" not in direct


class TestRedactSecrets:
    def test_redact_secrets_replaces_values(self):
        text = "failed with secret-key and other text"
        assert redact_secrets(text, ("secret-key",)) == "failed with [redacted] and other text"


class TestPluginManagerE2E:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        yield hermes_home

    def _reload_plugins(self):
        for key in list(sys.modules):
            if key.startswith(("plugins.quota_channels", "hermes_cli.plugins")):
                del sys.modules[key]
        from hermes_cli.plugins import PluginManager
        from tools.registry import invalidate_check_fn_cache, registry

        mgr = PluginManager()
        mgr.discover_and_load(force=True)
        invalidate_check_fn_cache()
        return mgr, registry.get_entry("quota_channels_tick")

    def test_tool_registers_and_tick_with_token_usage(self, _isolate_env, monkeypatch):
        section = _base_section(
            enabled_providers=["codex"],
            channel_ids={"codex": "301"},
            token_usage=_token_section(channel_ids={"codex": "401"}),
        )
        (_isolate_env / "config.yaml").write_text(
            yaml.safe_dump({"quota_channels": section})
        )
        secrets = _isolate_env / "secrets"
        secrets.mkdir()
        (secrets / "discord.env").write_text("DISCORD_BOT_TOKEN=bot-test\n")
        (_isolate_env / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {"access_token": "tok", "refresh_token": "ref"}
                        }
                    }
                }
            )
        )

        _, entry = self._reload_plugins()
        assert entry is not None
        assert entry.check_fn() is True

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "profiles/me" in url:
                body = json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {"start_date": "2026-08-21", "tokens": 50}
                            ]
                        }
                    }
                ).encode()
                return 200, body
            if "wham/usage" in url:
                body = json.dumps(
                    {
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 1,
                                "reset_after_seconds": 3600,
                            }
                        }
                    }
                ).encode()
                return 200, body
            return _discord_ok(req, timeout)

        monkeypatch.setattr("plugins.quota_channels.core.default_http", fake_http)
        _patch_run_tick_http(monkeypatch, fake_http)
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        fixed_now = datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp()
        monkeypatch.setattr("plugins.quota_channels.core.time.time", lambda: fixed_now)

        payload = json.loads(entry.handler({"force": True}))
        assert payload["success"] is True
        assert "token_usage" in payload
        assert payload["token_usage"]["providers"]["Codex"]["status"] in (
            "updated",
            "unchanged",
        )

    def test_tick_without_token_usage_section(self, _isolate_env, monkeypatch):
        (_isolate_env / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "quota_channels": _base_section(
                        enabled_providers=["codex"],
                        channel_ids={"codex": "301"},
                    )
                }
            )
        )
        secrets = _isolate_env / "secrets"
        secrets.mkdir()
        (secrets / "discord.env").write_text("DISCORD_BOT_TOKEN=bot-test\n")
        (_isolate_env / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {"access_token": "tok", "refresh_token": "ref"}
                        }
                    }
                }
            )
        )

        _, entry = self._reload_plugins()
        assert entry.check_fn() is True

        token_calls = []

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "profiles/me" in url or "model-usage" in url:
                token_calls.append(url)
            if "wham/usage" in url:
                body = json.dumps(
                    {
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 1,
                                "reset_after_seconds": 3600,
                            }
                        }
                    }
                ).encode()
                return 200, body
            return _discord_ok(req, timeout)

        monkeypatch.setattr("plugins.quota_channels.core.default_http", fake_http)
        _patch_run_tick_http(monkeypatch, fake_http)
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )

        payload = json.loads(entry.handler({"force": True}))
        assert payload["success"] is True
        assert "token_usage" not in payload
        assert token_calls == []
