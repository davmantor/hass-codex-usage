from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant import data_entry_flow

from custom_components.hass_codex_usage import _async_migrate_legacy_unique_id
from custom_components.hass_codex_usage.auth import build_account_unique_id
from custom_components.hass_codex_usage.config_flow import CodexUsageConfigFlow
from custom_components.hass_codex_usage.const import (
    CONF_ACCOUNT_NAME,
    CONF_AUTH_FILE,
    DOMAIN,
)


def jwt_with_payload(payload: dict[str, Any]) -> str:
    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    header = encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = encode(json.dumps(payload).encode())
    return f"{header}.{body}.{encode(b'sig')}"


def auth_payload(
    *,
    email: str | None = None,
    account_id: str | None = None,
    claim_account_id: str | None = None,
) -> dict[str, Any]:
    claims: dict[str, Any] = {"https://api.openai.com/auth/plan_type": "pro"}
    if email:
        claims["email"] = email
    if claim_account_id:
        claims["https://api.openai.com/auth"] = {"chatgpt_account_id": claim_account_id}

    tokens: dict[str, Any] = {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "id_token": jwt_with_payload(claims),
    }
    if account_id:
        tokens["account_id"] = account_id

    return {"tokens": tokens}


def write_auth_file(tmp_path: Path, name: str, payload: dict[str, Any]) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class FakeFlowManager:
    """Minimal stand-in for hass.config_entries.flow."""

    def async_progress_by_handler(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def async_abort(self, flow_id: str) -> None:
        return None


class FakeConfigEntry:
    """Minimal stand-in for a created ConfigEntry."""

    def __init__(self, result: dict[str, Any], unique_id: str | None) -> None:
        self.data = result["data"]
        self.title = result["title"]
        self.unique_id = unique_id
        self.source = "user"
        self.state = None


class FakeConfigEntries:
    """Minimal stand-in for hass.config_entries."""

    def __init__(self) -> None:
        self.flow = FakeFlowManager()
        self._entries: dict[tuple[str, str], FakeConfigEntry] = {}

    def async_entry_for_domain_unique_id(
        self, handler: str, unique_id: str
    ) -> FakeConfigEntry | None:
        return self._entries.get((handler, unique_id))

    def async_update_entry(self, entry: FakeConfigEntry, *, unique_id: str) -> bool:
        entry.unique_id = unique_id
        return True

    def add(self, handler: str, entry: FakeConfigEntry) -> None:
        self._entries[(handler, entry.unique_id)] = entry


class FakeHass:
    """Minimal stand-in for HomeAssistant, enough to drive the config flow."""

    def __init__(self) -> None:
        self.config_entries = FakeConfigEntries()

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        return target(*args)


def make_flow(hass: FakeHass, flow_id: str) -> CodexUsageConfigFlow:
    flow = CodexUsageConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = flow_id
    flow.context = {"source": "user"}
    return flow


async def run_user_step(hass: FakeHass, flow_id: str, auth_file: str) -> dict[str, Any]:
    """Run the user step and register the entry the way Home Assistant would."""
    flow = make_flow(hass, flow_id)
    result = await flow.async_step_user({CONF_AUTH_FILE: auth_file})
    if result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY:
        hass.config_entries.add(DOMAIN, FakeConfigEntry(result, flow.unique_id))
    return result


def test_build_unique_id_prefers_stored_account_id() -> None:
    payload = auth_payload(
        email="user@example.com", account_id="acct-123", claim_account_id="claim-456"
    )

    assert build_account_unique_id(payload, "/config/auth.json") == "acct-123"


def test_build_unique_id_falls_back_to_id_token_account_claim() -> None:
    payload = auth_payload(email="user@example.com", claim_account_id="claim-456")

    assert build_account_unique_id(payload, "/config/auth.json") == "claim-456"


def test_build_unique_id_falls_back_to_account_name() -> None:
    payload = auth_payload(email="user@example.com")

    assert build_account_unique_id(payload, "/config/auth.json") == "user@example.com"


def test_build_unique_id_falls_back_to_auth_file_path() -> None:
    payload = {"tokens": {"access_token": "access-token"}}
    expected = str(Path("~/.codex/auth.json").expanduser())

    assert build_account_unique_id(payload, "~/.codex/auth.json") == expected


@pytest.mark.asyncio
async def test_two_accounts_create_separate_entries(tmp_path: Path) -> None:
    hass = FakeHass()
    first_file = write_auth_file(
        tmp_path, "first.json", auth_payload(email="first@example.com", account_id="acct-1")
    )
    second_file = write_auth_file(
        tmp_path, "second.json", auth_payload(email="second@example.com", account_id="acct-2")
    )

    first = await run_user_step(hass, "flow-1", first_file)
    second = await run_user_step(hass, "flow-2", second_file)

    assert first["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert second["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert first["title"] == "Codex Usage (first@example.com - pro)"
    assert second["title"] == "Codex Usage (second@example.com - pro)"
    assert first["data"][CONF_ACCOUNT_NAME] == "first@example.com"
    assert second["data"][CONF_ACCOUNT_NAME] == "second@example.com"
    assert first["data"][CONF_AUTH_FILE] != second["data"][CONF_AUTH_FILE]


@pytest.mark.asyncio
async def test_same_account_is_rejected_as_duplicate(tmp_path: Path) -> None:
    hass = FakeHass()
    payload = auth_payload(email="user@example.com", account_id="acct-1")
    first_file = write_auth_file(tmp_path, "first.json", payload)
    copied_file = write_auth_file(tmp_path, "copy.json", payload)

    first = await run_user_step(hass, "flow-1", first_file)
    assert first["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    with pytest.raises(data_entry_flow.AbortFlow) as err:
        await run_user_step(hass, "flow-2", copied_file)

    assert err.value.reason == "already_configured"


@pytest.mark.asyncio
async def test_legacy_entry_is_rekeyed_to_the_account_id(tmp_path: Path) -> None:
    hass = FakeHass()
    auth_file = write_auth_file(
        tmp_path, "legacy.json", auth_payload(email="user@example.com", account_id="acct-1")
    )
    entry = FakeConfigEntry(
        {"title": "Codex Usage", "data": {CONF_AUTH_FILE: auth_file}}, DOMAIN
    )

    await _async_migrate_legacy_unique_id(hass, entry)

    assert entry.unique_id == "acct-1"


@pytest.mark.asyncio
async def test_legacy_entry_keeps_unique_id_when_auth_file_is_unreadable(tmp_path: Path) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(
        {"title": "Codex Usage", "data": {CONF_AUTH_FILE: str(tmp_path / "missing.json")}},
        DOMAIN,
    )

    await _async_migrate_legacy_unique_id(hass, entry)

    assert entry.unique_id == DOMAIN


@pytest.mark.asyncio
async def test_rekeyed_entry_blocks_adding_the_same_account_again(tmp_path: Path) -> None:
    hass = FakeHass()
    payload = auth_payload(email="user@example.com", account_id="acct-1")
    auth_file = write_auth_file(tmp_path, "legacy.json", payload)
    entry = FakeConfigEntry(
        {"title": "Codex Usage", "data": {CONF_AUTH_FILE: auth_file}}, DOMAIN
    )

    await _async_migrate_legacy_unique_id(hass, entry)
    hass.config_entries.add(DOMAIN, entry)

    with pytest.raises(data_entry_flow.AbortFlow) as err:
        await run_user_step(hass, "flow-2", write_auth_file(tmp_path, "copy.json", payload))

    assert err.value.reason == "already_configured"
