"""Codex Usage integration for Home Assistant."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .auth import (
    AuthFileError,
    CodexAuthFile,
    RefreshTokenRejectedError,
    RefreshTokenResponseError,
    TOKEN_REFRESH_URL,
    access_token_needs_refresh,
    build_refresh_request,
    persist_refreshed_tokens,
    read_auth_file,
    refresh_rejection_from_response,
)
from .const import (
    CONF_AUTH_FILE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    USAGE_API_URL,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
SESSION_WINDOW_MINUTES = 5 * 60

type CodexUsageConfigEntry = ConfigEntry[CodexUsageCoordinator]


class _UsageUnauthorized(Exception):
    """Raised when the usage endpoint rejects the access token."""


async def async_setup_entry(hass: HomeAssistant, entry: CodexUsageConfigEntry) -> bool:
    """Set up Codex Usage from a config entry."""
    coordinator = CodexUsageCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CodexUsageConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: CodexUsageConfigEntry) -> None:
    """Handle options update."""
    coordinator: CodexUsageCoordinator = entry.runtime_data
    interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
    coordinator.update_interval = timedelta(seconds=interval)


class CodexUsageCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch Codex usage data."""

    config_entry: CodexUsageConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CodexUsageConfigEntry) -> None:
        """Initialize the coordinator."""
        interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch usage data from the Codex usage API."""
        auth_file = self.config_entry.data[CONF_AUTH_FILE]
        auth = await self._async_read_auth_file(auth_file)
        session = aiohttp_client.async_get_clientsession(self.hass)
        refreshed = False

        access_token = auth.access_token
        if access_token_needs_refresh(access_token):
            access_token = await self._async_refresh_access_token(session, auth_file, auth)
            refreshed = True

        try:
            raw = await _async_fetch_usage(session, access_token)
        except _UsageUnauthorized as err:
            if refreshed or not auth.refresh_token:
                raise ConfigEntryAuthFailed(
                    "Codex authentication failed. Run codex login again and update the auth file."
                ) from err

            auth = await self._async_read_auth_file(auth_file)
            access_token = await self._async_refresh_access_token(session, auth_file, auth)
            try:
                raw = await _async_fetch_usage(session, access_token)
            except _UsageUnauthorized as retry_err:
                raise ConfigEntryAuthFailed(
                    "Codex authentication failed after refreshing tokens. "
                    "Run codex login again and update the auth file."
                ) from retry_err

        return _parse_usage(raw)

    async def _async_read_auth_file(self, auth_file: str) -> CodexAuthFile:
        """Read the Codex auth file without treating parse errors as auth failure."""
        try:
            return await self.hass.async_add_executor_job(read_auth_file, auth_file)
        except AuthFileError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_refresh_access_token(
        self, session: aiohttp.ClientSession, auth_file: str, auth: CodexAuthFile
    ) -> str:
        """Refresh OAuth tokens and persist the updated auth file."""
        if not auth.refresh_token:
            raise ConfigEntryAuthFailed(
                "Codex auth file is missing a refresh token. Run codex login again and update "
                "the auth file."
            )

        try:
            refresh_response = await _async_request_token_refresh(session, auth.refresh_token)
        except RefreshTokenRejectedError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        try:
            return await self.hass.async_add_executor_job(
                persist_refreshed_tokens, auth_file, auth.data, refresh_response
            )
        except RefreshTokenResponseError as err:
            raise UpdateFailed(f"Codex token refresh returned unusable data: {err}") from err
        except OSError as err:
            raise UpdateFailed(f"Unable to persist refreshed Codex auth file: {err}") from err


async def _async_request_token_refresh(
    session: aiohttp.ClientSession, refresh_token: str
) -> dict[str, Any]:
    """Request fresh tokens from the Codex OAuth token endpoint."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Originator": "codex_cli_rs",
        "User-Agent": "hass-codex-usage",
    }

    try:
        async with session.post(
            TOKEN_REFRESH_URL,
            headers=headers,
            json=build_refresh_request(refresh_token),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if not 200 <= resp.status < 300:
                body = await resp.text()
                rejection = refresh_rejection_from_response(resp.status, body)
                if rejection is not None:
                    _LOGGER.warning("Codex refresh token was rejected: %s", rejection)
                    raise rejection
                raise UpdateFailed(f"Error refreshing Codex token: HTTP {resp.status}")

            raw = await resp.json()
    except RefreshTokenRejectedError:
        raise
    except aiohttp.ClientError as err:
        raise UpdateFailed(f"Error refreshing Codex token: {err}") from err
    except ValueError as err:
        raise UpdateFailed(f"Error decoding Codex token refresh response: {err}") from err

    if not isinstance(raw, dict):
        raise UpdateFailed("Codex token refresh response was not a JSON object")
    if not isinstance(raw.get("access_token"), str) or not raw["access_token"]:
        raise UpdateFailed("Codex token refresh response did not include an access token")
    return raw


async def _async_fetch_usage(session: aiohttp.ClientSession, access_token: str) -> dict[str, Any]:
    """Fetch usage data with a bearer access token."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Originator": "codex_cli_rs",
        "User-Agent": "hass-codex-usage",
    }

    try:
        async with session.get(
            USAGE_API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 401:
                raise _UsageUnauthorized()
            resp.raise_for_status()
            raw = await resp.json()
    except _UsageUnauthorized:
        raise
    except aiohttp.ClientError as err:
        raise UpdateFailed(f"Error fetching usage data: {err}") from err
    except ValueError as err:
        raise UpdateFailed(f"Error decoding usage data: {err}") from err

    if not isinstance(raw, dict):
        raise UpdateFailed("Codex usage response was not a JSON object")
    return raw


def _parse_usage(raw: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
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

    # Handle Credits (OpenAI can return a float 0.0 or a dict)
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

    reached = rate_limits.get("rateLimitReachedType") or rate_limits.get("rate_limit_reached_type")
    data["rate_limit_reached"] = reached or "none"

    return data


def _get_rate_limits(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the rate-limit object from known Codex response shapes."""
    rate_limits = raw.get("rateLimits") or raw.get("rate_limit")
    if isinstance(rate_limits, dict):
        return rate_limits

    codex_limits = raw.get("rateLimitsByLimitId", {}).get("codex")
    if isinstance(codex_limits, dict):
        return codex_limits

    return raw


def _get_window(rate_limits: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    """Return a usage window from any known key."""
    for key in keys:
        window = rate_limits.get(key)
        if isinstance(window, dict):
            return window
    return None


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


def _classify_window(window: dict[str, Any], fallback: str, *, now: datetime) -> str:
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


def _get_percent(window: dict[str, Any]) -> float | int | None:
    """Return a window utilization percentage."""
    if "usedPercent" in window:
        return _number_or_none(window["usedPercent"])
    return _number_or_none(window.get("used_percent"))


def _get_reset_time(window: dict[str, Any]) -> datetime | None:
    """Return a window reset timestamp."""
    reset_value = window.get("resetsAt") or window.get("resets_at") or window.get("reset_at")
    if reset_value is None:
        return None

    if isinstance(reset_value, int | float):
        return datetime.fromtimestamp(reset_value, UTC)

    if isinstance(reset_value, str):
        try:
            return datetime.fromisoformat(reset_value)
        except ValueError:
            return None

    return None


def _get_window_minutes(window: dict[str, Any]) -> int | float | None:
    """Return a window duration in minutes."""
    for key in ("windowDurationMins", "window_duration_mins", "window_minutes"):
        if key in window:
            return _number_or_none(window[key])
    if "limit_window_seconds" in window:
        v = _number_or_none(window["limit_window_seconds"])
        return v / 60 if v is not None else None
    return None


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


def _number_or_none(value: Any) -> float | int | None:
    """Return a numeric value when possible."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
