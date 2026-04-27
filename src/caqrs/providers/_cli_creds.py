# SPDX-License-Identifier: MIT
# Ported from openclaw/openclaw at commit 22c9e82e835f4ef2cb3807f7fe6e148f4535a5ec:
#   - extensions/anthropic/cli-auth-seam.ts
#   - src/agents/cli-credentials.ts (parseClaudeCliOauthCredential,
#     readClaudeCliKeychainCredentials, readCodexKeychainCredentials,
#     decodeJwtExpiryMs, computeCodexKeychainAccount)
# Original work (c) OpenClaw contributors, used under the MIT licence.
# CAQRS as a whole is Apache-2.0; this file retains its MIT origin.
"""CLI credential parsers for the subscription-backed LLM providers.

Reads the login state of the official Anthropic ``claude`` and OpenAI
``codex`` CLIs. Credentials may live either on disk or in macOS Keychain;
the file is consulted first, then the Keychain as fallback.

File layouts (verified against OpenClaw at the cited commit):

- Claude CLI: ``~/.claude/.credentials.json``::

      {"claudeAiOauth": {"accessToken": "...",
                          "refreshToken": "...",
                          "expiresAt": <ms>}}

  ``refreshToken`` is optional; without it the cred is a short-lived
  ``token`` rather than a refreshable ``oauth`` cred.

- Codex CLI: ``$CODEX_HOME/auth.json`` (default ``~/.codex/auth.json``)::

      {"tokens": {"access_token": "...",
                  "refresh_token": "...",
                  "account_id": "...",
                  "id_token": "..."},
       "last_refresh": "<iso8601-or-epoch>"}

  Expiry resolution order: (1) JWT ``exp`` claim from ``access_token``,
  (2) ``last_refresh`` + 1 hour, (3) ``now`` + 1 hour.

Keychain layouts (macOS only, ``security`` CLI):

- Claude: service ``Claude Code-credentials``, no account; payload is
  the same JSON as the on-disk file.
- Codex: service ``Codex Auth``, account ``cli|<sha256(codex_home)[:16]>``;
  payload is the same JSON as the on-disk file.

The Keychain reader is injectable via the ``keychain_reader`` keyword on
the loader functions, which lets tests pin behaviour without spawning
``security`` subprocesses.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# === Constants ===

_CLAUDE_CLI_REL: Final[Path] = Path(".claude/.credentials.json")
_CODEX_CLI_DEFAULT_DIR: Final[Path] = Path(".codex")
_CODEX_CLI_FILENAME: Final[str] = "auth.json"
_FALLBACK_TTL_MS: Final[int] = 60 * 60 * 1000
_JWT_MIN_PARTS: Final[int] = 2  # header + payload (signature optional for parsing)

_CLAUDE_KEYCHAIN_SERVICE: Final[str] = "Claude Code-credentials"
_CODEX_KEYCHAIN_SERVICE: Final[str] = "Codex Auth"
_KEYCHAIN_TIMEOUT_S: Final[float] = 5.0
_CODEX_ACCOUNT_HASH_LEN: Final[int] = 16


# === Cred types ===


class OAuthCredential(BaseModel):
    """OAuth cred with refresh token (long-lived)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["oauth"] = "oauth"
    provider: str = Field(min_length=1, max_length=80)
    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    expires_at_ms: int = Field(gt=0)
    account_id: str | None = None
    id_token: str | None = None


class TokenCredential(BaseModel):
    """Short-lived token without refresh capability (Claude setup token)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["token"] = "token"
    provider: str = Field(min_length=1, max_length=80)
    access_token: str = Field(min_length=1)
    expires_at_ms: int = Field(gt=0)


CliCredential = OAuthCredential | TokenCredential

KeychainReader = Callable[[str, str | None], str | None]


# === Path resolution ===


def claude_cli_creds_path(home_dir: Path | None = None) -> Path:
    """Resolve the Claude CLI cred path. Override ``home_dir`` for tests."""
    return (home_dir or Path.home()) / _CLAUDE_CLI_REL


def codex_cli_creds_path(codex_home: Path | None = None) -> Path:
    """Resolve the Codex CLI cred path.

    Precedence: explicit ``codex_home`` arg, then ``$CODEX_HOME``, then
    ``~/.codex/``. Matches OpenClaw ``resolveCodexHomePath``.
    """
    if codex_home is not None:
        return codex_home / _CODEX_CLI_FILENAME
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser() / _CODEX_CLI_FILENAME
    return Path.home() / _CODEX_CLI_DEFAULT_DIR / _CODEX_CLI_FILENAME


def _resolve_codex_home(codex_home: Path | None = None) -> Path:
    """Resolve the directory portion of the Codex home (used for Keychain hashing)."""
    if codex_home is not None:
        return codex_home
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / _CODEX_CLI_DEFAULT_DIR


def _compute_codex_keychain_account(codex_home: Path) -> str:
    """Replicate OpenClaw ``computeCodexKeychainAccount``.

    Account = ``cli|<first 16 hex chars of sha256(codex_home)>``.
    The hash uses the directory path as a string, matching OpenClaw's
    behaviour.
    """
    digest = hashlib.sha256(str(codex_home).encode("utf-8")).hexdigest()
    return f"cli|{digest[:_CODEX_ACCOUNT_HASH_LEN]}"


# === JWT exp decoder (mirrors OpenClaw decodeJwtExpiryMs) ===


def _decode_jwt_exp_ms(token: str) -> int | None:
    """Decode the ``exp`` claim from a JWT into epoch ms. ``None`` on any failure."""
    parts = token.split(".")
    if len(parts) < _JWT_MIN_PARTS:
        return None
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        payload_raw = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int | float) or isinstance(exp, bool) or exp <= 0:
        return None
    return int(exp * 1000)


# === Keychain (macOS) ===


def _current_platform() -> str:
    """Indirection layer: mypy cannot platform-narrow through a function call.

    Without this, ``sys.platform != "darwin"`` is treated as always-True on
    Linux mypy runs, marking the rest of ``_real_keychain_reader`` as
    unreachable and failing the strict gate cross-platform.
    """
    return sys.platform


def _real_keychain_reader(service: str, account: str | None) -> str | None:
    """Read a generic-password secret via the macOS ``security`` CLI.

    Returns ``None`` on non-darwin, on subprocess failure, or when the
    Keychain entry is absent. The output is the raw secret string written
    to stdout by ``security -w``; callers parse it as JSON.
    """
    if _current_platform() != "darwin":
        return None
    args = ["security", "find-generic-password", "-s", service]
    if account:
        args.extend(["-a", account])
    args.append("-w")
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# === Pure parsers (file or Keychain payload) ===


def _read_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse_claude_payload(raw: object) -> CliCredential | None:
    """Parse a Claude cred payload (file or Keychain JSON). ``None`` on malformed."""
    if not isinstance(raw, dict):
        return None
    oauth = raw.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None

    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    expires_at = oauth.get("expiresAt")
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(expires_at, int | float)
        or isinstance(expires_at, bool)
        or expires_at <= 0
    ):
        return None

    expires_ms = int(expires_at)
    if isinstance(refresh, str) and refresh:
        return OAuthCredential(
            provider="anthropic",
            access_token=access,
            refresh_token=refresh,
            expires_at_ms=expires_ms,
        )
    return TokenCredential(
        provider="anthropic",
        access_token=access,
        expires_at_ms=expires_ms,
    )


def _resolve_codex_expiry_ms(access_token: str, last_refresh: object) -> int:
    """Apply OpenClaw's expiry-resolution cascade for Codex creds."""
    via_jwt = _decode_jwt_exp_ms(access_token)
    if via_jwt is not None:
        return via_jwt

    last_refresh_ms: int | None = None
    if isinstance(last_refresh, str) and last_refresh:
        try:
            normalized = last_refresh.replace("Z", "+00:00")
            last_refresh_ms = int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except ValueError:
            last_refresh_ms = None
    elif isinstance(last_refresh, int | float) and not isinstance(last_refresh, bool):
        last_refresh_ms = int(last_refresh)

    if last_refresh_ms is None:
        last_refresh_ms = int(datetime.now(UTC).timestamp() * 1000)
    return last_refresh_ms + _FALLBACK_TTL_MS


def _parse_codex_payload(raw: object) -> OAuthCredential | None:
    """Parse a Codex cred payload (file or Keychain JSON). ``None`` on malformed."""
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(refresh, str) or not refresh:
        return None

    expires_ms = _resolve_codex_expiry_ms(access, raw.get("last_refresh"))

    account_id_raw = tokens.get("account_id")
    id_token_raw = tokens.get("id_token")
    return OAuthCredential(
        provider="openai-codex",
        access_token=access,
        refresh_token=refresh,
        expires_at_ms=expires_ms,
        account_id=account_id_raw if isinstance(account_id_raw, str) else None,
        id_token=id_token_raw if isinstance(id_token_raw, str) else None,
    )


# === Public loaders (file → Keychain fallback) ===


def _load_keychain_json(
    keychain_reader: KeychainReader | None,
    service: str,
    account: str | None,
) -> object:
    """Invoke the Keychain reader (real or injected) and JSON-parse its output."""
    reader = keychain_reader if keychain_reader is not None else _real_keychain_reader
    secret = reader(service, account)
    if secret is None:
        return None
    try:
        return json.loads(secret)
    except ValueError:
        return None


def load_claude_cli_cred(
    home_dir: Path | None = None,
    *,
    keychain_reader: KeychainReader | None = None,
) -> CliCredential | None:
    """Load Claude CLI cred. File first, then macOS Keychain fallback.

    Pass ``keychain_reader=lambda *_: None`` (or any callable returning
    ``None``) to disable Keychain lookup, e.g. for hermetic tests.
    """
    path = claude_cli_creds_path(home_dir)
    if path.is_file():
        cred = _parse_claude_payload(_read_json_file(path))
        if cred is not None:
            return cred
    raw = _load_keychain_json(keychain_reader, _CLAUDE_KEYCHAIN_SERVICE, None)
    return _parse_claude_payload(raw)


def load_codex_cli_cred(
    codex_home: Path | None = None,
    *,
    keychain_reader: KeychainReader | None = None,
) -> OAuthCredential | None:
    """Load Codex CLI cred. File first, then macOS Keychain fallback."""
    path = codex_cli_creds_path(codex_home)
    if path.is_file():
        cred = _parse_codex_payload(_read_json_file(path))
        if cred is not None:
            return cred
    home = _resolve_codex_home(codex_home)
    account = _compute_codex_keychain_account(home)
    raw = _load_keychain_json(keychain_reader, _CODEX_KEYCHAIN_SERVICE, account)
    return _parse_codex_payload(raw)
