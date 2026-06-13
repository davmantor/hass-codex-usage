"""Config flow for Codex Usage integration."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_AUTH_FILE,
    CONF_SUBSCRIPTION_LEVEL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_AUTH_FILE,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class CodexUsageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Codex Usage."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            auth_file = user_input[CONF_AUTH_FILE].strip()
            auth_data = await self.hass.async_add_executor_job(_read_auth_file, auth_file)

            if auth_data is None:
                errors[CONF_AUTH_FILE] = "auth_file_unreadable"
            elif _get_access_token(auth_data) is None and _get_refresh_token(auth_data) is None:
                errors[CONF_AUTH_FILE] = "missing_access_token"
            else:
                account_name, subscription_level = _get_account_info(auth_data)
                title = _build_title(account_name, subscription_level)

                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_AUTH_FILE: auth_file,
                        CONF_ACCOUNT_NAME: account_name,
                        CONF_SUBSCRIPTION_LEVEL: subscription_level,
                    },
                    options={
                        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_FILE, default=DEFAULT_AUTH_FILE): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow."""
        return CodexUsageOptionsFlow()


class CodexUsageOptionsFlow(OptionsFlow):
    """Handle options for Codex Usage."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UPDATE_INTERVAL, default=current_interval): vol.All(
                        int, vol.Range(min=60, max=3600)
                    ),
                }
            ),
        )


def _read_auth_file(auth_file: str) -> dict[str, Any] | None:
    """Read the Codex auth file."""
    try:
        with Path(auth_file).expanduser().open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        _LOGGER.exception("Unable to read Codex auth file")
        return None

    return data if isinstance(data, dict) else None


def _get_access_token(auth_data: dict[str, Any]) -> str | None:
    """Return the Codex access token from auth.json."""
    tokens = auth_data.get("tokens")
    if not isinstance(tokens, dict):
        return None

    access_token = tokens.get("access_token")
    return access_token if isinstance(access_token, str) and access_token else None


def _get_refresh_token(auth_data: dict[str, Any]) -> str | None:
    """Return the Codex refresh token from auth.json."""
    tokens = auth_data.get("tokens")
    if not isinstance(tokens, dict):
        return None

    refresh_token = tokens.get("refresh_token")
    return refresh_token if isinstance(refresh_token, str) and refresh_token else None


def _get_account_info(auth_data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract display account details from the ID token, if present."""
    tokens = auth_data.get("tokens")
    if not isinstance(tokens, dict):
        return None, None

    claims = _decode_jwt_payload(tokens.get("id_token"))
    if claims is None:
        return None, None

    account_name = claims.get("email") or claims.get("name")
    subscription_level = claims.get("https://api.openai.com/auth/plan_type")
    return _string_or_none(account_name), _string_or_none(subscription_level)


def _decode_jwt_payload(token: Any) -> dict[str, Any] | None:
    """Decode JWT payload without verification for display-only metadata."""
    if not isinstance(token, str):
        return None

    parts = token.split(".")
    if len(parts) < 2:
        return None

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None

    return claims if isinstance(claims, dict) else None


def _string_or_none(value: Any) -> str | None:
    """Return a string value or None."""
    return value if isinstance(value, str) and value else None


def _build_title(account_name: str | None, subscription_level: str | None) -> str:
    """Build the config entry title."""
    title_parts = ["Codex Usage"]
    if account_name:
        title_parts.append(f"({account_name}")
        if subscription_level:
            title_parts.append(f"- {subscription_level})")
        else:
            title_parts[-1] += ")"
    return " ".join(title_parts)
