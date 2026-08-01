from __future__ import annotations

from datetime import UTC, datetime

from custom_components.hass_codex_usage import _parse_usage


SESSION_RESET = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
WEEK_RESET = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


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
