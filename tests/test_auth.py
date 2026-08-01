from __future__ import annotations

import base64
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


AUTH_MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "hass_codex_usage" / "auth.py"
)


def load_auth_module():
    spec = importlib.util.spec_from_file_location("hass_codex_usage_auth", AUTH_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


auth = load_auth_module()


def jwt_with_payload(payload: dict[str, object]) -> str:
    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    header = encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = encode(json.dumps(payload).encode())
    signature = encode(b"sig")
    return f"{header}.{body}.{signature}"


def test_access_token_needs_refresh_when_missing_expired_or_near_expiry() -> None:
    now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    expired = jwt_with_payload({"exp": int((now - timedelta(seconds=1)).timestamp())})
    near_expiry = jwt_with_payload({"exp": int((now + timedelta(minutes=4)).timestamp())})

    assert auth.access_token_needs_refresh(None, now=now)
    assert auth.access_token_needs_refresh(expired, now=now)
    assert auth.access_token_needs_refresh(near_expiry, now=now)


def test_access_token_does_not_refresh_outside_window() -> None:
    now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    fresh = jwt_with_payload({"exp": int((now + timedelta(minutes=6)).timestamp())})

    assert not auth.access_token_needs_refresh(fresh, now=now)


def test_build_refresh_request_uses_codex_client_id() -> None:
    request = auth.build_refresh_request("refresh-token")

    assert request == {
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "grant_type": "refresh_token",
        "refresh_token": "refresh-token",
    }


def test_persist_refreshed_tokens_updates_returned_fields_and_keeps_existing_values(
    tmp_path: Path,
) -> None:
    auth_file = tmp_path / "auth.json"
    original = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "old-id",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "account_id": "account-id",
        },
        "last_refresh": "2026-06-01T00:00:00+00:00",
    }
    auth_file.write_text(json.dumps(original), encoding="utf-8")
    loaded = auth.read_auth_file(str(auth_file))
    now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)

    access_token = auth.persist_refreshed_tokens(
        str(auth_file),
        loaded.data,
        {"access_token": "new-access"},
        now=now,
    )

    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert access_token == "new-access"
    assert saved["tokens"] == {
        "id_token": "old-id",
        "access_token": "new-access",
        "refresh_token": "old-refresh",
        "account_id": "account-id",
    }
    assert saved["last_refresh"] == "2026-06-13T12:00:00+00:00"


def test_invalid_auth_json_is_transient_file_error(tmp_path: Path) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{invalid", encoding="utf-8")

    with pytest.raises(auth.AuthFileError):
        auth.read_auth_file(str(auth_file))


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (400, {"error": {"code": "refresh_token_expired"}}, "expired"),
        (401, {"error": {"code": "refresh_token_reused"}}, "already used"),
        (400, {"error": "invalid_grant"}, "invalid or expired"),
    ],
)
def test_refresh_rejection_classifies_permanent_failures(
    status: int, body: dict[str, object], message: str
) -> None:
    rejection = auth.refresh_rejection_from_response(status, json.dumps(body))

    assert rejection is not None
    assert message in str(rejection)


def test_refresh_rejection_treats_server_error_as_transient() -> None:
    rejection = auth.refresh_rejection_from_response(500, json.dumps({"error": "temporary"}))

    assert rejection is None
