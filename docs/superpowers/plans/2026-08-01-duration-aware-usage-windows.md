# Duration-Aware Usage Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify Codex rate-limit windows by their declared duration so a weekly-only primary window populates Weekly sensors while the existing Session sensors remain registered but unavailable.

**Architecture:** Keep the change inside the existing parser in `custom_components/hass_codex_usage/__init__.py`. Add one selector that examines both API positions, classifies each window using duration first and reset horizon second, and falls back to the legacy position when the payload is ambiguous; inject the current time into parsing so fallback and pace tests are deterministic.

**Tech Stack:** Python 3.12, Home Assistant `DataUpdateCoordinator`, pytest, Ruff, Black

## Global Constraints

- Keep the integration domain `hass_codex_usage` and all existing entity unique IDs unchanged.
- Do not remove or rename the Session sensors.
- Do not change OAuth, config-entry data, the dashboard, or the Codex subscription usage endpoint.
- Treat a declared duration of 300 minutes or less as Session and a longer duration as Weekly.
- Use reset horizon only when declared duration is unavailable.
- Preserve legacy positional behavior when duration and reset horizon are ambiguous.
- If no Session window is selected, omit its data keys so the existing entities become unavailable.
- Keep changes surgical and verify them with tests, syntax, formatting, and lint checks.

## File Structure

- Create `tests/test_usage.py`: isolated parser behavior tests using fixed timestamps.
- Modify `custom_components/hass_codex_usage/__init__.py`: window selection, fallback classification, and deterministic time injection.
- Modify `README.md`: document duration-aware routing and temporary Session sensor unavailability.

---

### Task 1: Route Windows by Declared Duration

**Files:**
- Create: `tests/test_usage.py`
- Modify: `custom_components/hass_codex_usage/__init__.py:220-341`

**Interfaces:**
- Consumes: `_get_window(rate_limits, *keys)`, `_get_window_minutes(window)`, `_get_percent(window)`, and `_get_reset_time(window)`.
- Produces: `_select_usage_windows(rate_limits) -> tuple[dict[str, Any] | None, dict[str, Any] | None]`, returning `(session_window, weekly_window)`.
- Produces: `_classify_window(window, fallback) -> str`, returning either `"session"` or `"week"`.

- [ ] **Step 1: Write the failing duration-routing tests**

Create `tests/test_usage.py` with:

```python
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
```

- [ ] **Step 2: Run the tests and verify the weekly-only case fails**

Run:

```powershell
pytest tests/test_usage.py -v
```

Expected: two tests pass and `test_parse_routes_weekly_only_primary_to_week_sensors` fails because `session_usage_percent` is present and `week_usage_percent` is absent.

- [ ] **Step 3: Add duration-aware selection**

In `custom_components/hass_codex_usage/__init__.py`, add the constant below `PLATFORMS`:

```python
SESSION_WINDOW_MINUTES = 5 * 60
```

Replace the positional window parsing at the start of `_parse_usage` with:

```python
def _parse_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse raw Codex usage responses into a flat sensor data dict."""
    data: dict[str, Any] = {}
    rate_limits = _get_rate_limits(raw)

    session_window, week_window = _select_usage_windows(rate_limits)
    if session_window:
        data["session_usage_percent"] = _get_percent(session_window)
        data["session_reset_time"] = _get_reset_time(session_window)

    if week_window:
        utilization = _get_percent(week_window)
        reset_time = _get_reset_time(week_window)
        data["week_usage_percent"] = utilization
        data["week_reset_time"] = reset_time
        data["week_usage_pace"] = _calculate_pace(
            utilization, reset_time, _get_window_minutes(week_window)
        )

    credits = rate_limits.get("credits")
    if isinstance(credits, dict):
        data["credits_balance"] = _number_or_none(credits.get("balance"))
        data["credits_enabled"] = credits.get("hasCredits")
    elif isinstance(credits, int | float):
        data["credits_balance"] = float(credits)
        data["credits_enabled"] = credits > 0
    else:
        data["credits_balance"] = 0.0
        data["credits_enabled"] = False

    reached = rate_limits.get("rateLimitReachedType") or rate_limits.get(
        "rate_limit_reached_type"
    )
    data["rate_limit_reached"] = reached or "none"

    return data
```

Add these helpers immediately after `_get_window`:

```python
def _select_usage_windows(
    rate_limits: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Select session and weekly windows independently of API position."""
    session_window: dict[str, Any] | None = None
    week_window: dict[str, Any] | None = None
    positioned_windows = (
        ("session", _get_window(rate_limits, "primary", "primary_window")),
        ("week", _get_window(rate_limits, "secondary", "secondary_window")),
    )

    for fallback, window in positioned_windows:
        if window is None:
            continue
        category = _classify_window(window, fallback)
        if category == "session" and session_window is None:
            session_window = window
        elif category == "week" and week_window is None:
            week_window = window

    return session_window, week_window


def _classify_window(window: dict[str, Any], fallback: str) -> str:
    """Classify a rate-limit window, preferring its declared duration."""
    duration = _get_window_minutes(window)
    if duration is not None and duration > 0:
        return "session" if duration <= SESSION_WINDOW_MINUTES else "week"
    return fallback
```

- [ ] **Step 4: Run the duration-routing tests and verify they pass**

Run:

```powershell
pytest tests/test_usage.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit duration-aware routing**

```powershell
git add -- tests/test_usage.py custom_components/hass_codex_usage/__init__.py
git commit -m "Fix usage window classification by duration"
```

Expected: one commit containing only the parser and its three tests.

---

### Task 2: Add Reset-Horizon Fallback and Deterministic Pace

**Files:**
- Modify: `tests/test_usage.py`
- Modify: `custom_components/hass_codex_usage/__init__.py:220-360`

**Interfaces:**
- Consumes: `_select_usage_windows(rate_limits, *, now)` from Task 1, extended with an optional fixed time.
- Produces: `_parse_usage(raw, *, now=None) -> dict[str, Any]` for deterministic parser tests.
- Produces: `_classify_window(window, fallback, *, now) -> str`, using duration, then reset horizon, then fallback.
- Produces: `_calculate_pace(utilization, reset_time, window_minutes, *, now=None) -> float | None`.

- [ ] **Step 1: Add failing fallback and pace tests**

Append to `tests/test_usage.py`:

```python
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


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
```

Also change the datetime import at the top of `tests/test_usage.py` to:

```python
from datetime import UTC, datetime, timedelta
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```powershell
pytest tests/test_usage.py::test_parse_uses_reset_horizon_for_durationless_weekly_primary tests/test_usage.py::test_parse_preserves_positions_when_reset_horizon_is_ambiguous tests/test_usage.py::test_parse_calculates_pace_for_weekly_window_selected_from_primary -v
```

Expected: all three fail with `TypeError: _parse_usage() got an unexpected keyword argument 'now'`.

- [ ] **Step 3: Inject time and implement reset-horizon fallback**

Change `_parse_usage`, `_select_usage_windows`, `_classify_window`, and `_calculate_pace` in `custom_components/hass_codex_usage/__init__.py` to use these signatures and bodies:

```python
def _parse_usage(
    raw: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Parse raw Codex usage responses into a flat sensor data dict."""
    data: dict[str, Any] = {}
    rate_limits = _get_rate_limits(raw)

    session_window, week_window = _select_usage_windows(rate_limits, now=now)
    if session_window:
        data["session_usage_percent"] = _get_percent(session_window)
        data["session_reset_time"] = _get_reset_time(session_window)

    if week_window:
        utilization = _get_percent(week_window)
        reset_time = _get_reset_time(week_window)
        data["week_usage_percent"] = utilization
        data["week_reset_time"] = reset_time
        data["week_usage_pace"] = _calculate_pace(
            utilization,
            reset_time,
            _get_window_minutes(week_window),
            now=now,
        )

    credits = rate_limits.get("credits")
    if isinstance(credits, dict):
        data["credits_balance"] = _number_or_none(credits.get("balance"))
        data["credits_enabled"] = credits.get("hasCredits")
    elif isinstance(credits, int | float):
        data["credits_balance"] = float(credits)
        data["credits_enabled"] = credits > 0
    else:
        data["credits_balance"] = 0.0
        data["credits_enabled"] = False

    reached = rate_limits.get("rateLimitReachedType") or rate_limits.get(
        "rate_limit_reached_type"
    )
    data["rate_limit_reached"] = reached or "none"

    return data


def _select_usage_windows(
    rate_limits: dict[str, Any], *, now: datetime | None = None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Select session and weekly windows independently of API position."""
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)

    session_window: dict[str, Any] | None = None
    week_window: dict[str, Any] | None = None
    positioned_windows = (
        ("session", _get_window(rate_limits, "primary", "primary_window")),
        ("week", _get_window(rate_limits, "secondary", "secondary_window")),
    )

    for fallback, window in positioned_windows:
        if window is None:
            continue
        category = _classify_window(window, fallback, now=current_time)
        if category == "session" and session_window is None:
            session_window = window
        elif category == "week" and week_window is None:
            week_window = window

    return session_window, week_window


def _classify_window(
    window: dict[str, Any], fallback: str, *, now: datetime
) -> str:
    """Classify a window by duration, reset horizon, then legacy position."""
    duration = _get_window_minutes(window)
    if duration is not None and duration > 0:
        return "session" if duration <= SESSION_WINDOW_MINUTES else "week"

    reset_time = _get_reset_time(window)
    if reset_time is not None:
        if reset_time.tzinfo is None:
            reset_time = reset_time.replace(tzinfo=UTC)
        if reset_time > now + timedelta(minutes=SESSION_WINDOW_MINUTES):
            return "week"

    return fallback


def _calculate_pace(
    utilization: float | int | None,
    reset_time: datetime | None,
    window_minutes: float | int | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Calculate how far usage is ahead of or behind the quota window."""
    if utilization is None or reset_time is None or not window_minutes:
        return None

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    if reset_time.tzinfo is None:
        reset_time = reset_time.replace(tzinfo=UTC)

    window_seconds = window_minutes * 60
    elapsed = window_seconds - (reset_time - current_time).total_seconds()
    percent_elapsed = (elapsed / window_seconds) * 100
    return round(utilization - percent_elapsed, 1)
```

- [ ] **Step 4: Run all parser tests and verify they pass**

Run:

```powershell
pytest tests/test_usage.py -v
```

Expected: `6 passed`.

- [ ] **Step 5: Commit fallback classification and deterministic timing**

```powershell
git add -- tests/test_usage.py custom_components/hass_codex_usage/__init__.py
git commit -m "Add usage window compatibility fallbacks"
```

Expected: one commit containing the three fallback/pace tests and time-aware parser changes.

---

### Task 3: Document Behavior and Run Full Verification

**Files:**
- Modify: `README.md:5-12`

**Interfaces:**
- Consumes: duration-aware behavior completed in Tasks 1 and 2.
- Produces: user-facing documentation explaining when Session sensors are unavailable.

- [ ] **Step 1: Update the Sensors section**

Add this paragraph after the sensor list in `README.md`:

```markdown
Usage windows are identified by their backend-reported duration rather than by
their `primary` or `secondary` position. If OpenAI returns only the weekly
window, the Session sensors remain installed but report `Unavailable`; they
resume automatically if a short-term window returns.
```

- [ ] **Step 2: Run the complete test suite**

Run:

```powershell
pytest -q
```

Expected: `15 passed` with no failures.

- [ ] **Step 3: Run syntax, formatting, and lint checks**

Run:

```powershell
python -m compileall -q custom_components/hass_codex_usage tests
black --check custom_components/hass_codex_usage tests
ruff check custom_components/hass_codex_usage tests
```

Expected: all commands exit with code 0; Black reports that files would be left unchanged, and Ruff reports `All checks passed!`.

- [ ] **Step 4: Review the final diff for scope and whitespace errors**

Run:

```powershell
git diff --check
git diff -- custom_components/hass_codex_usage/__init__.py tests/test_usage.py README.md
```

Expected: `git diff --check` prints nothing. The diff contains only parser classification, parser tests, and the README note; it does not change sensor definitions, authentication, configuration, or dashboard files.

- [ ] **Step 5: Commit documentation**

```powershell
git add -- README.md
git commit -m "Document duration-aware usage sensors"
```

Expected: one documentation-only commit.
