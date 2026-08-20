"""quota_label bucket boundary tests."""

import pytest

from plugins.quota_channels.core import quota_label


@pytest.mark.parametrize(
    "elapsed, expected",
    [
        (29, "Updated: Just Now"),
        (30, "Updated: 30s+"),
        (59, "Updated: 30s+"),
        (60, "Updated: 1m+"),
        (299, "Updated: 1m+"),
        (300, "Updated: 5m+"),
        (599, "Updated: 5m+"),
        (600, "Updated: 10m+"),
        (899, "Updated: 10m+"),
        (900, "Updated: 15m+"),
        (1199, "Updated: 15m+"),
        (1200, "Updated: 20m+"),
        (1799, "Updated: 20m+"),
        (1800, "Updated: 30m+ (Delayed)"),
        (3600, "Updated: 30m+ (Delayed)"),
    ],
)
def test_quota_label_buckets(elapsed, expected):
    assert quota_label(elapsed) == expected
