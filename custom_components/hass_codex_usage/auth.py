"""Codex OAuth auth-file helpers."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_REFRESH_URL = "https://auth.openai.com/oauth/token"
ACCESS_TOKEN_REFRESH_WINDOW = timedelta(minutes=5)

_TOKEN_FIELDS = ("id_token", "access_token", "refresh_token")


class AuthFileError(Exception):
    """Raised when the Codex auth file cannot be read as valid JSON."""


class RefreshTokenRejectedError(Exception):
    """Raised when the refresh token is permanently rejected."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class RefreshTokenResponseError(Exception):
    """Raised when a token refresh response is unusable."""


@dataclass(frozen=True, slots=True)
class CodexAuthFile:
    """Parsed Codex auth file data and extracted token strings."""

    data: dict[str, Any]
    access_token: str | None
    refresh_token: str | None


def read_auth_file(auth_file: str) -> CodexAuthFile:
    """Read the Codex auth file and extract token strings."""
    path = Path(auth_file).expanduser()
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except OSError as err:
        raise AuthFileError(f"Unable to read Codex auth file: {err}") from err
    except json.JSONDecodeError as err:
        raise AuthFileError(f"Codex auth file contains invalid JSON: {err}") from err

    if not isinstance(data, dict):
        raise AuthFileError("Codex auth file must contain a JSON object")

    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return CodexAuthFile(data=data, access_token=None, refresh_token=None)

    return CodexAuthFile(
        data=data,
        access_token=_string_or_none(tokens.get("access_token")),
        refresh_token=_string_or_none(tokens.get("refresh_token")),
    )


def access_token_needs_refresh(
    access_token: str | None,
    *,
    now: datetime | None = None,
    refresh_window: timedelta = ACCESS_TOKEN_REFRESH_WINDOW,
) -> bool:
    """Return whether an access token is missing, expired, or near expiry."""
    if not access_token:
        return True

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    try:
        expires_at = _jwt_expiration(access_token)
    except ValueError:
        return True

    if expires_at is None:
        return False
    return expires_at <= now + refresh_window


def build_refresh_request(refresh_token: str) -> dict[str, str]:
    """Build the Codex OAuth refresh request body."""
    return {
        "client_id": CODEX_OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }


def persist_refreshed_tokens(
    auth_file: str,
    auth_data: dict[str, Any],
    refresh_response: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """Merge refreshed token fields into auth data and atomically persist it."""
    access_token = apply_refreshed_tokens(auth_data, refresh_response, now=now)
    if access_token is None:
        raise RefreshTokenResponseError("Token refresh response did not include an access token")
    write_auth_file_atomically(auth_file, auth_data)
    return access_token


def apply_refreshed_tokens(
    auth_data: dict[str, Any],
    refresh_response: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    """Merge returned token fields into auth data and return the access token."""
    tokens = auth_data.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
        auth_data["tokens"] = tokens

    for field in _TOKEN_FIELDS:
        value = refresh_response.get(field)
        if isinstance(value, str) and value:
            tokens[field] = value

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    auth_data["last_refresh"] = now.astimezone(UTC).isoformat()

    return _string_or_none(tokens.get("access_token"))


def write_auth_file_atomically(auth_file: str, auth_data: dict[str, Any]) -> None:
    """Write auth data to the original auth file using an atomic replace."""
    path = Path(auth_file).expanduser()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    stat_mode: int | None = None
    try:
        stat_mode = path.stat().st_mode & 0o777
    except OSError:
        stat_mode = None

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    tmp_path = Path(tmp_name)
    try:
        if stat_mode is not None:
            os.chmod(tmp_path, stat_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = -1
            json.dump(auth_data, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        raise


def refresh_rejection_from_response(
    status: int, body: str
) -> RefreshTokenRejectedError | None:
    """Return a permanent refresh-token rejection for known auth failures."""
    code = _extract_refresh_error_code(body)
    normalized_code = code.lower() if code else None

    if normalized_code == "refresh_token_expired":
        return RefreshTokenRejectedError(
            "Codex refresh token has expired. Run codex login again and update the auth file.",
            code,
        )
    if normalized_code == "refresh_token_reused":
        return RefreshTokenRejectedError(
            "Codex refresh token was already used. Run codex login again and update the auth file.",
            code,
        )
    if normalized_code == "refresh_token_invalidated":
        return RefreshTokenRejectedError(
            "Codex refresh token was revoked. Run codex login again and update the auth file.",
            code,
        )
    if normalized_code == "invalid_grant":
        return RefreshTokenRejectedError(
            "Codex refresh token is invalid or expired. Run codex login again and update the auth file.",
            code,
        )
    if status == 401:
        return RefreshTokenRejectedError(
            "Codex refresh token was rejected. Run codex login again and update the auth file.",
            code,
        )

    return None


def _jwt_expiration(token: str) -> datetime | None:
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    if exp is None:
        return None
    if not isinstance(exp, int | float):
        raise ValueError("JWT exp claim is not numeric")
    expires_at = datetime.fromtimestamp(exp, UTC)
    return expires_at


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        raise ValueError("Invalid JWT format")

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as err:
        raise ValueError("Invalid JWT payload") from err

    if not isinstance(claims, dict):
        raise ValueError("JWT payload must be a JSON object")
    return claims


def _extract_refresh_error_code(body: str) -> str | None:
    if not body.strip():
        return None

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return None

    if not isinstance(decoded, dict):
        return None

    error = decoded.get("error")
    if isinstance(error, dict):
        return _string_or_none(error.get("code"))
    if isinstance(error, str):
        return error

    return _string_or_none(decoded.get("code"))


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
