"""Tests for the CLI cred parsers.

All tests use ``tmp_path`` fixtures and never read the user's real
``~/.claude/`` or ``~/.codex/`` directories.
"""

import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from caqrs.providers._cli_creds import (
    OAuthCredential,
    TokenCredential,
    _decode_jwt_exp_ms,
    _resolve_codex_expiry_ms,
    claude_cli_creds_path,
    codex_cli_creds_path,
    format_expiry_iso,
    is_cred_fresh,
    load_claude_cli_cred,
    load_codex_cli_cred,
)

# === Path resolution ===


def test_claude_path_default_under_home(tmp_path: Path) -> None:
    assert claude_cli_creds_path(tmp_path) == tmp_path / ".claude/.credentials.json"


def test_codex_path_explicit_home_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ignored"))
    explicit = tmp_path / "explicit"
    assert codex_cli_creds_path(explicit) == explicit / "auth.json"


def test_codex_path_uses_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "via-env"
    monkeypatch.setenv("CODEX_HOME", str(target))
    assert codex_cli_creds_path() == target / "auth.json"


def test_codex_path_falls_back_to_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert codex_cli_creds_path() == tmp_path / ".codex/auth.json"


# === Claude credentials ===


def _write_claude(home: Path, payload: object) -> None:
    creds = home / ".claude/.credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps(payload), encoding="utf-8")


def test_load_claude_oauth(tmp_path: Path) -> None:
    _write_claude(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "anthropic-access",
                "refreshToken": "rt-refresh",
                "expiresAt": 1_900_000_000_000,
            },
        },
    )
    cred = load_claude_cli_cred(tmp_path)
    assert isinstance(cred, OAuthCredential)
    assert cred.provider == "anthropic"
    assert cred.access_token == "anthropic-access"
    assert cred.refresh_token == "rt-refresh"
    assert cred.expires_at_ms == 1_900_000_000_000


def test_load_claude_token_only(tmp_path: Path) -> None:
    _write_claude(
        tmp_path,
        {"claudeAiOauth": {"accessToken": "setup-tok", "expiresAt": 1_900_000_000_000}},
    )
    cred = load_claude_cli_cred(tmp_path)
    assert isinstance(cred, TokenCredential)
    assert cred.provider == "anthropic"
    assert cred.access_token == "setup-tok"


def test_load_claude_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_claude_cli_cred(tmp_path) is None


def test_load_claude_returns_none_for_malformed_json(tmp_path: Path) -> None:
    creds = tmp_path / ".claude/.credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("not json {{", encoding="utf-8")
    assert load_claude_cli_cred(tmp_path) is None


def test_load_claude_returns_none_for_missing_oauth_block(tmp_path: Path) -> None:
    _write_claude(tmp_path, {"otherKey": {}})
    assert load_claude_cli_cred(tmp_path) is None


def test_load_claude_returns_none_for_empty_token(tmp_path: Path) -> None:
    _write_claude(
        tmp_path,
        {"claudeAiOauth": {"accessToken": "", "expiresAt": 1_900_000_000_000}},
    )
    assert load_claude_cli_cred(tmp_path) is None


def test_load_claude_returns_none_for_invalid_expires(tmp_path: Path) -> None:
    _write_claude(
        tmp_path,
        {"claudeAiOauth": {"accessToken": "ok", "expiresAt": 0}},
    )
    assert load_claude_cli_cred(tmp_path) is None
    _write_claude(
        tmp_path,
        {"claudeAiOauth": {"accessToken": "ok", "expiresAt": "not-a-number"}},
    )
    assert load_claude_cli_cred(tmp_path) is None


# === JWT exp decoder ===


def _jwt_with_exp(exp: int | float | None | str | bool) -> str:
    """Build a minimal unsigned JWT with the given exp claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_obj: dict[str, object] = {} if exp is None else {"exp": exp}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).rstrip(b"=").decode()
    return f"{header}.{payload_b64}.signature"


def test_decode_jwt_exp_returns_ms() -> None:
    assert _decode_jwt_exp_ms(_jwt_with_exp(1_700_000_000)) == 1_700_000_000_000


def test_decode_jwt_exp_handles_short_token() -> None:
    assert _decode_jwt_exp_ms("only-one-part") is None


def test_decode_jwt_exp_handles_invalid_base64() -> None:
    assert _decode_jwt_exp_ms("hdr.@@@notbase64@@@.sig") is None


def test_decode_jwt_exp_handles_missing_exp() -> None:
    assert _decode_jwt_exp_ms(_jwt_with_exp(None)) is None


def test_decode_jwt_exp_rejects_non_numeric() -> None:
    assert _decode_jwt_exp_ms(_jwt_with_exp("string-exp")) is None
    assert _decode_jwt_exp_ms(_jwt_with_exp(True)) is None


def test_decode_jwt_exp_rejects_non_positive() -> None:
    assert _decode_jwt_exp_ms(_jwt_with_exp(0)) is None
    assert _decode_jwt_exp_ms(_jwt_with_exp(-1)) is None


# === Codex expiry cascade ===


def test_codex_expiry_uses_jwt_exp_when_present() -> None:
    token = _jwt_with_exp(1_750_000_000)
    assert _resolve_codex_expiry_ms(token, last_refresh="2026-01-01T00:00:00Z") == 1_750_000_000_000


def test_codex_expiry_falls_back_to_last_refresh_iso() -> None:
    base_iso = "2026-01-01T00:00:00+00:00"
    base_ms = int(datetime.fromisoformat(base_iso).timestamp() * 1000)
    expires = _resolve_codex_expiry_ms("opaque.access.token", last_refresh=base_iso)
    assert expires == base_ms + 60 * 60 * 1000


def test_codex_expiry_falls_back_to_last_refresh_epoch_ms() -> None:
    base_ms = 1_750_000_000_000
    expires = _resolve_codex_expiry_ms("opaque", last_refresh=base_ms)
    assert expires == base_ms + 60 * 60 * 1000


def test_codex_expiry_falls_back_to_now_when_unparseable() -> None:
    before = int(datetime.now(UTC).timestamp() * 1000)
    expires = _resolve_codex_expiry_ms("opaque", last_refresh="not-a-date")
    after = int(datetime.now(UTC).timestamp() * 1000) + 60 * 60 * 1000
    assert before + 60 * 60 * 1000 - 5_000 <= expires <= after + 5_000


# === Codex credentials ===


def _write_codex(codex_home: Path, payload: object) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")


def test_load_codex_oauth_full_payload(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_codex(
        codex_home,
        {
            "tokens": {
                "access_token": _jwt_with_exp(1_750_000_000),
                "refresh_token": "rt",
                "account_id": "acc-123",
                "id_token": "id-456",
            },
        },
    )
    cred = load_codex_cli_cred(codex_home)
    assert cred is not None
    assert cred.provider == "openai-codex"
    assert cred.refresh_token == "rt"
    assert cred.account_id == "acc-123"
    assert cred.id_token == "id-456"
    assert cred.expires_at_ms == 1_750_000_000_000


def test_load_codex_oauth_minimal_payload(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_codex(
        codex_home,
        {"tokens": {"access_token": "opaque", "refresh_token": "rt"}},
    )
    cred = load_codex_cli_cred(codex_home)
    assert cred is not None
    assert cred.account_id is None
    assert cred.id_token is None


def test_load_codex_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_codex_cli_cred(tmp_path / ".codex") is None


def test_load_codex_requires_refresh_token(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_codex(codex_home, {"tokens": {"access_token": "a"}})
    assert load_codex_cli_cred(codex_home) is None


def test_load_codex_returns_none_for_malformed_json(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text("garbage {{", encoding="utf-8")
    assert load_codex_cli_cred(codex_home) is None


# === Expiry helpers ===


def _token_cred(expires_at_ms: int) -> TokenCredential:
    return TokenCredential(
        provider="anthropic",
        access_token="t",
        expires_at_ms=expires_at_ms,
    )


def test_is_cred_fresh_with_far_future_expiry() -> None:
    future = int((time.time() + 3600) * 1000)
    assert is_cred_fresh(_token_cred(future)) is True


def test_is_cred_fresh_rejects_past_expiry() -> None:
    assert is_cred_fresh(_token_cred(1)) is False


def test_is_cred_fresh_skew_treats_near_expiry_as_stale() -> None:
    soon = int((time.time() + 30) * 1000)
    assert is_cred_fresh(_token_cred(soon)) is False
    assert is_cred_fresh(_token_cred(soon), skew_seconds=10) is True


def test_format_expiry_iso_renders_utc() -> None:
    cred = _token_cred(1_730_000_000_000)
    formatted = format_expiry_iso(cred)
    assert "+00:00" in formatted
    assert "T" in formatted
