from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.hass_codex_usage import _parse_usage

SESSION_RESET = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
WEEK_RESET = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def test_parse_routes_normal_windows_by_declared_duration() -> None:
    raw = {
        "rateLimits": {
            "primary": {
                "usedPercent": 12,
                "windowDurationMins": 300,
                "resetsAt": int(SESSION_RESET.timestamp()),
            },
            "secondary": {
                "usedPercent": 34,
                "windowDurationMins": 10_080,
                "resetsAt": int(WEEK_RESET.timestamp()),
            },
        }
    }

    parsed = _parse_usage(raw)

    assert parsed["session_usage_percent"] == 12
    assert parsed["session_reset_time"] == SESSION_RESET
    assert parsed["week_usage_percent"] == 34
    assert parsed["week_reset_time"] == WEEK_RESET


def test_parse_routes_weekly_only_primary_to_week_sensors() -> None:
    raw = {
        "rateLimits": {
            "primary": {
                "usedPercent": 41,
                "windowDurationMins": 10_080,
                "resetsAt": int(WEEK_RESET.timestamp()),
            },
            "secondary": None,
        }
    }

    parsed = _parse_usage(raw)

    assert "session_usage_percent" not in parsed
    assert "session_reset_time" not in parsed
    assert parsed["week_usage_percent"] == 41
    assert parsed["week_reset_time"] == WEEK_RESET


def test_parse_restored_snake_case_short_window_as_session() -> None:
    raw = {
        "rate_limit": {
            "primary_window": {
                "used_percent": 9,
                "limit_window_seconds": 18_000,
                "reset_at": int(SESSION_RESET.timestamp()),
            },
            "secondary_window": None,
        }
    }

    parsed = _parse_usage(raw)

    assert parsed["session_usage_percent"] == 9
    assert parsed["session_reset_time"] == SESSION_RESET
    assert "week_usage_percent" not in parsed
    assert "week_reset_time" not in parsed


def test_parse_uses_reset_horizon_for_durationless_weekly_primary() -> None:
    reset_time = datetime(2026, 8, 1, 18, 1, tzinfo=UTC)
    raw = {
        "rateLimits": {
            "primary": {
                "usedPercent": 23,
                "resetsAt": int(reset_time.timestamp()),
            }
        }
    }

    parsed = _parse_usage(raw, now=NOW)

    assert "session_usage_percent" not in parsed
    assert parsed["week_usage_percent"] == 23
    assert parsed["week_reset_time"] == reset_time
    assert parsed["week_usage_pace"] is None


def test_parse_uses_reset_horizon_without_pace_for_negative_primary_duration() -> None:
    reset_time = datetime(2026, 8, 1, 18, 1, tzinfo=UTC)
    raw = {
        "rateLimits": {
            "primary": {
                "usedPercent": 23,
                "windowDurationMins": -1,
                "resetsAt": int(reset_time.timestamp()),
            }
        }
    }

    parsed = _parse_usage(raw, now=NOW)

    assert "session_usage_percent" not in parsed
    assert parsed["week_usage_percent"] == 23
    assert parsed["week_reset_time"] == reset_time
    assert parsed["week_usage_pace"] is None


def test_parse_preserves_positions_when_reset_horizon_is_ambiguous() -> None:
    primary_reset = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)
    secondary_reset = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
    raw = {
        "rateLimits": {
            "primary": {
                "usedPercent": 11,
                "resetsAt": int(primary_reset.timestamp()),
            },
            "secondary": {
                "usedPercent": 22,
                "resetsAt": int(secondary_reset.timestamp()),
            },
        }
    }

    parsed = _parse_usage(raw, now=NOW)

    assert parsed["session_usage_percent"] == 11
    assert parsed["week_usage_percent"] == 22


def test_parse_calculates_pace_for_weekly_window_selected_from_primary() -> None:
    reset_time = NOW + timedelta(days=6)
    raw = {
        "rateLimits": {
            "primary": {
                "usedPercent": 42,
                "windowDurationMins": 10_080,
                "resetsAt": int(reset_time.timestamp()),
            }
        }
    }

    parsed = _parse_usage(raw, now=NOW)

    assert parsed["week_usage_pace"] == 27.7


def test_parse_reads_credits_and_reached_type_from_real_wham_usage_shape() -> None:
    """The live /wham/usage endpoint nests credits and reached-type at the
    top level of the response, as siblings of `rate_limit` -- not inside it."""
    raw = {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": False,
            "limit_reached": True,
            "primary_window": {
                "used_percent": 100,
                "limit_window_seconds": 18_000,
                "reset_at": int(SESSION_RESET.timestamp()),
            },
            "secondary_window": {
                "used_percent": 90,
                "limit_window_seconds": 604_800,
                "reset_at": int(WEEK_RESET.timestamp()),
            },
        },
        "credits": {
            "has_credits": True,
            "unlimited": False,
            "overage_limit_reached": False,
            "balance": "194.7980000000",
        },
        "rate_limit_reached_type": {
            "type": "rate_limit_reached",
            "details": "default",
        },
    }

    parsed = _parse_usage(raw, now=NOW)

    assert parsed["credits_balance"] == 194.798
    assert parsed["credits_enabled"] is True
    assert parsed["rate_limit_reached"] == "rate_limit_reached"
