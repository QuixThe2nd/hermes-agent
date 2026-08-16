"""Tests for dev-pipeline ROUTING decisions."""

from __future__ import annotations

import copy

import pytest

from plugins.dev_pipeline.pipeline import route_plan_contract


def _routable_contract() -> dict:
    return {
        "lane_hint": "cursor",
        "estimated_minutes": 20,
        "broad_flags": {
            "migration": False,
            "repo_wide_change": False,
            "toolchain_change": False,
            "multi_subsystem": False,
            "long_verification": False,
        },
        "blocked_reasons": [],
    }


def test_happy_path_routes_to_cursor():
    decision, block_kind, reason = route_plan_contract(_routable_contract())
    assert decision == "cursor"
    assert block_kind is None
    assert reason is None


@pytest.mark.parametrize(
    "flag_key",
    [
        "migration",
        "repo_wide_change",
        "toolchain_change",
        "multi_subsystem",
        "long_verification",
    ],
)
def test_broad_flag_routes_lane_unavailable(flag_key):
    contract = _routable_contract()
    contract["broad_flags"] = copy.deepcopy(contract["broad_flags"])
    contract["broad_flags"][flag_key] = True
    decision, block_kind, reason = route_plan_contract(contract)
    assert decision == "block"
    assert block_kind == "lane_unavailable"
    assert reason and "GLM endurance lane" in reason


def test_estimated_minutes_over_30_routes_lane_unavailable():
    contract = _routable_contract()
    contract["estimated_minutes"] = 31
    decision, block_kind, _reason = route_plan_contract(contract)
    assert decision == "block"
    assert block_kind == "lane_unavailable"


def test_lane_hint_broad_routes_lane_unavailable():
    contract = _routable_contract()
    contract["lane_hint"] = "broad"
    decision, block_kind, _reason = route_plan_contract(contract)
    assert decision == "block"
    assert block_kind == "lane_unavailable"


@pytest.mark.parametrize(
    ("reason", "expected_kind"),
    [
        ("missing_credentials", "missing_credentials"),
        ("missing_product_input", "missing_product_input"),
        ("infra_broken", "infra_broken"),
        ("acceptance_unverifiable", "acceptance_unverifiable"),
    ],
)
def test_blocked_reason_maps_to_block_kind(reason, expected_kind):
    contract = _routable_contract()
    contract["blocked_reasons"] = [reason]
    decision, block_kind, _reason = route_plan_contract(contract)
    assert decision == "block"
    assert block_kind == expected_kind
