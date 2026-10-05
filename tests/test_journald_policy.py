import pytest

from ops.journald_policy import render_policy


def test_default_policy_has_age_and_disk_limits() -> None:
    policy = render_policy()
    assert "MaxRetentionSec=14day" in policy
    assert "SystemMaxUse=1G" in policy
    assert "SystemKeepFree=5G" in policy


@pytest.mark.parametrize("days", [0, 366])
def test_invalid_retention_days_rejected(days: int) -> None:
    with pytest.raises(ValueError, match="retention days"):
        render_policy(days=days)


@pytest.mark.parametrize("size", ["0G", "1GiB", "-1G", "1g", "", "1G\\nSystemMaxUse=0"])
def test_invalid_sizes_rejected(size: str) -> None:
    with pytest.raises(ValueError, match="journal sizes"):
        render_policy(max_use=size)
