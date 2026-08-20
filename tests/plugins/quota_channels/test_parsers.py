"""Parser and provider fetch tests for quota_channels."""

from __future__ import annotations

import json
import struct
import urllib.request
from datetime import datetime, timezone

import pytest

from plugins.quota_channels.core import (
    QuotaChannelsError,
    fetch_codex_usage,
    fetch_cursor_usage,
    fetch_grok_usage,
    fetch_kimi_usage,
    fetch_zai_usage,
    format_codex_name,
    format_cursor_name,
    format_grok_name,
    format_kimi_name,
    format_zai_name,
    parse_codex_usage,
    parse_cursor_usage,
    parse_grok_usage,
    parse_kimi_usage,
    parse_zai_usage,
    run_codex_provider,
    run_cursor_provider,
    run_grok_provider,
    run_kimi_provider,
    run_zai_provider,
)


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    n = value
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_key(field: int, wire: int) -> bytes:
    return _pb_varint((field << 3) | wire)


def _pb_float32(field: int, value: float) -> bytes:
    raw = struct.pack("<f", value)
    return _pb_key(field, 5) + raw


def _pb_length_delimited(field: int, data: bytes) -> bytes:
    return _pb_key(field, 2) + _pb_varint(len(data)) + data


def _pb_varint_field(field: int, value: int) -> bytes:
    return _pb_key(field, 0) + _pb_varint(value)


def _build_grok_grpc_body(used_pct: float, period_end: int) -> bytes:
    timestamp = _pb_varint_field(1, period_end)
    config = _pb_float32(1, used_pct) + _pb_length_delimited(5, timestamp)
    message = _pb_length_delimited(1, config)
    return b"\x00" + len(message).to_bytes(4, "big") + message


class TestParseCodexUsage:
    def test_rounds_used_percent_and_days(self):
        payload = json.dumps(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 12.6,
                        "reset_after_seconds": 86400 * 3 + 1,
                    }
                }
            }
        )
        remaining, days = parse_codex_usage(payload)
        assert remaining == 87
        assert days == 4
        assert format_codex_name(remaining, days) == "Codex: 87% \u2022 4d left"


class TestParseKimiUsage:
    def test_string_numbers_and_z_suffix(self):
        reset = datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc)
        payload = json.dumps(
            {
                "usage": {
                    "remaining": "42",
                    "resetTime": reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            }
        )
        now = reset.timestamp() - 86400
        remaining, days = parse_kimi_usage(payload, now_fn=lambda: now)
        assert remaining == 42
        assert days == 1
        assert format_kimi_name(remaining, days) == "Kimi: 42% \u2022 1d left"


class TestParseZaiUsage:
    def test_picks_largest_next_reset_time(self):
        payload = json.dumps(
            {
                "data": {
                    "limits": [
                        {"percentage": 10, "nextResetTime": 1_700_000_000_000},
                        {"percentage": 55, "nextResetTime": 1_800_000_000_000},
                    ]
                }
            }
        )
        now = 1_800_000_000_000 / 1000 - 86400 * 2
        remaining, days = parse_zai_usage(payload, now_fn=lambda: now)
        assert remaining == 45
        assert days == 2
        assert format_zai_name(remaining, days) == "z.ai: 45% \u2022 2d left"


class TestParseCursorUsage:
    def test_dual_pool_format(self):
        end_ms = int((datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()) * 1000)
        payload = json.dumps(
            {
                "planUsage": {"autoPercentUsed": 12.9, "apiPercentUsed": 33.2},
                "billingCycleEnd": str(end_ms),
            }
        )
        now = end_ms / 1000 - 86400 * 5
        auto_remaining, api_remaining, days = parse_cursor_usage(
            payload, now_fn=lambda: now
        )
        assert auto_remaining == 88
        assert api_remaining == 67
        assert days == 5
        assert (
            format_cursor_name(auto_remaining, api_remaining, days)
            == "Cursor: 88%/67% \u2022 5d left"
        )


class TestParseGrokUsage:
    def test_float32_field_and_timestamp(self):
        period_end = int(datetime(2026, 8, 30, tzinfo=timezone.utc).timestamp())
        body = _build_grok_grpc_body(23.6, period_end)
        now = period_end - 86400 * 4
        remaining, days = parse_grok_usage(body, now_fn=lambda: now)
        assert remaining == 76
        assert days == 4
        assert format_grok_name(remaining, days) == "Grok: 76% \u2022 4d left"


class TestMockedProviderFetch:
    def test_codex_fetch_and_parse(self, monkeypatch, tmp_path):
        auth = tmp_path / "auth.json"
        auth.write_text(
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
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        def fake_http(req, timeout=25.0):
            if "wham/usage" in req.full_url:
                body = json.dumps(
                    {
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 5,
                                "reset_after_seconds": 86400,
                            }
                        }
                    }
                ).encode()
                return 200, body
            raise AssertionError(req.full_url)

        name, days, label = run_codex_provider(http_fn=fake_http)
        assert label == "Codex"
        assert days == 1
        assert name == "Codex: 95% \u2022 1d left"
        status, _ = fetch_codex_usage("tok", http_fn=fake_http)
        assert status == 200

    def test_kimi_fetch_and_parse(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text('KIMI_API_KEY="kimi-key"\n')
        reset = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)

        def fake_http(req, timeout=25.0):
            assert "kimi.com" in req.full_url
            body = json.dumps(
                {
                    "usage": {
                        "remaining": "80",
                        "resetTime": reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                }
            ).encode()
            return 200, body

        name, days, label = run_kimi_provider(
            http_fn=fake_http, now_fn=lambda: reset.timestamp() - 3600
        )
        assert label == "Kimi"
        assert name == "Kimi: 80% \u2022 1d left"
        status, _ = fetch_kimi_usage("kimi-key", http_fn=fake_http)
        assert status == 200

    def test_zai_fetch_and_parse(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "zai.env").write_text("ZAI_API_KEY=raw-zai-key\n")

        def fake_http(req, timeout=25.0):
            assert req.headers.get("Authorization") == "raw-zai-key"
            body = json.dumps(
                {
                    "data": {
                        "limits": [{"percentage": 20, "nextResetTime": 2_000_000_000_000}]
                    }
                }
            ).encode()
            return 200, body

        name, days, label = run_zai_provider(
            http_fn=fake_http, now_fn=lambda: 2_000_000_000_000 / 1000 - 86400
        )
        assert label == "z.ai"
        assert name == "z.ai: 80% \u2022 1d left"
        status, _ = fetch_zai_usage("raw-zai-key", http_fn=fake_http)
        assert status == 200

    def test_cursor_fetch_and_parse(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cursor_dir = tmp_path / ".config" / "cursor"
        cursor_dir.mkdir(parents=True)
        end_ms = int((datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp()) * 1000)
        (cursor_dir / "auth.json").write_text(json.dumps({"accessToken": "cursor-jwt"}))
        monkeypatch.setattr(
            "plugins.quota_channels.core.Path.home", lambda: tmp_path
        )

        def fake_http(req, timeout=25.0):
            assert req.method == "POST"
            assert req.data == b"{}"
            body = json.dumps(
                {
                    "planUsage": {"autoPercentUsed": 10, "apiPercentUsed": 40},
                    "billingCycleEnd": str(end_ms),
                }
            ).encode()
            return 200, body

        name, days, label = run_cursor_provider(
            http_fn=fake_http, now_fn=lambda: end_ms / 1000 - 86400 * 2
        )
        assert label == "Cursor"
        assert name == "Cursor: 90%/60% \u2022 2d left"
        status, _ = fetch_cursor_usage("cursor-jwt", http_fn=fake_http)
        assert status == 200

    def test_grok_fetch_and_parse(self, monkeypatch, tmp_path):
        auth = tmp_path / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "providers": {
                        "xai-oauth": {
                            "tokens": {"access_token": "grok-tok", "refresh_token": "grok-ref"}
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        period_end = int(datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())
        body = _build_grok_grpc_body(10.0, period_end)

        def fake_http(req, timeout=25.0):
            if "GetGrokCreditsConfig" in req.full_url:
                return 200, body
            raise AssertionError(req.full_url)

        name, days, label = run_grok_provider(
            http_fn=fake_http, now_fn=lambda: period_end - 86400 * 3
        )
        assert label == "Grok"
        assert name == "Grok: 90% \u2022 3d left"
        status, fetched = fetch_grok_usage("grok-tok", http_fn=fake_http)
        assert status == 200
        remaining, parsed_days = parse_grok_usage(
            fetched, now_fn=lambda: period_end - 86400 * 3
        )
        assert remaining == 90
        assert parsed_days == 3

    def test_grok_missing_config_raises(self):
        with pytest.raises(QuotaChannelsError, match="no config message"):
            parse_grok_usage(b"\x00\x00\x00\x00\x00")


class TestMalformedParserResponses:
    @pytest.mark.parametrize(
        "payload",
        [
            "not-json",
            json.dumps([]),
            json.dumps({"rate_limit": {}}),
            json.dumps(
                {"rate_limit": {"primary_window": {"used_percent": "abc", "reset_after_seconds": 0}}}
            ),
            json.dumps(
                {"rate_limit": {"primary_window": {"used_percent": 0, "reset_after_seconds": "x"}}}
            ),
        ],
        ids=["invalid_json", "non_dict", "missing_primary", "bad_used_percent", "bad_reset"],
    )
    def test_codex_malformed(self, payload):
        with pytest.raises(QuotaChannelsError):
            parse_codex_usage(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            "not-json",
            json.dumps([]),
            json.dumps({}),
            json.dumps({"usage": {"remaining": "abc", "resetTime": "2026-01-01T00:00:00Z"}}),
            json.dumps({"usage": {"remaining": 1, "resetTime": "not-a-date"}}),
            json.dumps({"usage": {"remaining": 1, "resetTime": "2026-01-01T00:00:00"}}),
        ],
        ids=[
            "invalid_json",
            "non_dict",
            "missing_usage",
            "bad_remaining",
            "bad_reset",
            "timezone_less_reset",
        ],
    )
    def test_kimi_malformed(self, payload):
        with pytest.raises(QuotaChannelsError):
            parse_kimi_usage(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            "not-json",
            json.dumps([]),
            json.dumps({"data": {"limits": []}}),
            json.dumps(
                {"data": {"limits": [{"percentage": "x", "nextResetTime": 1_000_000_000_000}]}}
            ),
            json.dumps(
                {"data": {"limits": [{"percentage": 10, "nextResetTime": "bad"}]}}
            ),
            json.dumps({"data": {"limits": ["bad"]}}),
            json.dumps({"data": "bad"}),
            json.dumps({"data": {"limits": [None]}}),
        ],
        ids=[
            "invalid_json",
            "non_dict",
            "no_limits",
            "bad_percentage",
            "bad_reset",
            "non_mapping_limit",
            "non_dict_data",
            "null_limit_entry",
        ],
    )
    def test_zai_malformed(self, payload):
        with pytest.raises(QuotaChannelsError):
            parse_zai_usage(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            "not-json",
            json.dumps([]),
            json.dumps({}),
            json.dumps({"planUsage": {"autoPercentUsed": "x", "apiPercentUsed": 0}}),
            json.dumps(
                {
                    "planUsage": {"autoPercentUsed": 0, "apiPercentUsed": 0},
                    "billingCycleEnd": "not-numeric",
                }
            ),
        ],
        ids=["invalid_json", "non_dict", "missing_plan", "bad_percent", "bad_billing_end"],
    )
    def test_cursor_malformed(self, payload):
        with pytest.raises(QuotaChannelsError):
            parse_cursor_usage(payload)

    def test_grok_truncated_grpc_frame(self):
        body = _build_grok_grpc_body(10.0, 1_700_000_000)
        with pytest.raises(QuotaChannelsError):
            parse_grok_usage(body[:8])

    def test_grok_trailing_grpc_garbage(self):
        body = _build_grok_grpc_body(10.0, 1_700_000_000) + b"\x01\x02\x03"
        with pytest.raises(QuotaChannelsError):
            parse_grok_usage(body)

    def test_grok_truncated_protobuf_varint(self):
        # gRPC header + protobuf key byte with no varint continuation/end
        body = b"\x00\x00\x00\x00\x01\x80"
        with pytest.raises(QuotaChannelsError):
            parse_grok_usage(body)

    def test_grok_truncated_fixed32_field(self):
        # field 1 wire 5 (float32) key present but only 2 payload bytes
        partial = _pb_key(1, 5) + b"\x00\x00"
        message = _pb_length_delimited(1, partial)
        body = b"\x00" + len(message).to_bytes(4, "big") + message
        with pytest.raises(QuotaChannelsError):
            parse_grok_usage(body)

    def test_grok_garbage_bytes(self):
        with pytest.raises(QuotaChannelsError):
            parse_grok_usage(b"\xff\xfe\xfd\xfc\xfb")


class TestOAuthRefreshRetry:
    def test_codex_401_refresh_and_retry(self, monkeypatch, tmp_path):
        auth = tmp_path / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {
                                "access_token": "old-tok",
                                "refresh_token": "old-ref",
                            }
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        usage_calls = []
        refresh_calls = []

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "oauth/token" in url:
                refresh_calls.append(req)
                body = json.dumps(
                    {"access_token": "new-tok", "refresh_token": "new-ref"}
                ).encode()
                return 200, body
            if "wham/usage" in url:
                usage_calls.append(req.headers.get("Authorization"))
                if len(usage_calls) == 1:
                    return 401, b"unauthorized"
                body = json.dumps(
                    {
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 10,
                                "reset_after_seconds": 86400,
                            }
                        }
                    }
                ).encode()
                return 200, body
            raise AssertionError(url)

        name, days, label = run_codex_provider(http_fn=fake_http)
        assert label == "Codex"
        assert name == "Codex: 90% \u2022 1d left"
        assert days == 1
        assert len(refresh_calls) == 1
        assert len(usage_calls) == 2
        assert usage_calls[0] == "Bearer old-tok"
        assert usage_calls[1] == "Bearer new-tok"

        saved = json.loads(auth.read_text(encoding="utf-8"))
        assert saved["providers"]["openai-codex"]["tokens"]["access_token"] == "new-tok"
        assert saved["providers"]["openai-codex"]["tokens"]["refresh_token"] == "new-ref"

    def test_grok_401_refresh_and_retry(self, monkeypatch, tmp_path):
        auth = tmp_path / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "providers": {
                        "xai-oauth": {
                            "tokens": {
                                "access_token": "old-grok",
                                "refresh_token": "old-grok-ref",
                            }
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        period_end = int(datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())
        body = _build_grok_grpc_body(15.0, period_end)

        billing_calls = []
        refresh_calls = []

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "auth.x.ai/oauth2/token" in url:
                refresh_calls.append(req)
                payload = json.dumps({"access_token": "new-grok", "refresh_token": "new-grok-ref"})
                return 200, payload.encode()
            if "GetGrokCreditsConfig" in url:
                billing_calls.append(req.headers.get("Authorization"))
                if len(billing_calls) == 1:
                    return 401, b"unauthorized"
                return 200, body
            raise AssertionError(url)

        name, days, label = run_grok_provider(
            http_fn=fake_http, now_fn=lambda: period_end - 86400
        )
        assert label == "Grok"
        assert name == "Grok: 85% \u2022 1d left"
        assert days == 1
        assert len(refresh_calls) == 1
        assert len(billing_calls) == 2
        assert billing_calls[0] == "Bearer old-grok"
        assert billing_calls[1] == "Bearer new-grok"

        saved = json.loads(auth.read_text(encoding="utf-8"))
        assert saved["providers"]["xai-oauth"]["tokens"]["access_token"] == "new-grok"
        assert saved["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "new-grok-ref"

    def test_codex_refresh_failure_on_401(self, monkeypatch, tmp_path):
        auth = tmp_path / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {
                                "access_token": "old-tok",
                                "refresh_token": "old-ref",
                            }
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "oauth/token" in url:
                return 400, b"bad refresh"
            if "wham/usage" in url:
                return 401, b"unauthorized"
            raise AssertionError(url)

        with pytest.raises(QuotaChannelsError, match="codex token refresh failed"):
            run_codex_provider(http_fn=fake_http)

    def test_grok_refresh_failure_on_401(self, monkeypatch, tmp_path):
        auth = tmp_path / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "providers": {
                        "xai-oauth": {
                            "tokens": {
                                "access_token": "old-grok",
                                "refresh_token": "old-grok-ref",
                            }
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "auth.x.ai/oauth2/token" in url:
                return 400, b"bad refresh"
            if "GetGrokCreditsConfig" in url:
                return 401, b"unauthorized"
            raise AssertionError(url)

        with pytest.raises(QuotaChannelsError, match="xai re-login required"):
            run_grok_provider(http_fn=fake_http)
