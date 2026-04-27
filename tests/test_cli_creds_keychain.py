"""Tests for Keychain fallback and the platform-specific reader.

The autouse fixture in ``conftest.py`` disables the real Keychain for
all tests; tests in this file pass an explicit ``keychain_reader`` to
exercise the fallback behaviour.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from caqrs.providers import _cli_creds
from caqrs.providers._cli_creds import (
    OAuthCredential,
    TokenCredential,
    _compute_codex_keychain_account,
    _real_keychain_reader,
    load_claude_cli_cred,
    load_codex_cli_cred,
)

# === Codex Keychain account hash ===


def test_codex_keychain_account_format() -> None:
    account = _compute_codex_keychain_account(Path("/Users/test/.codex"))
    assert account.startswith("cli|")
    assert len(account) == len("cli|") + 16
    # Must be deterministic
    assert account == _compute_codex_keychain_account(Path("/Users/test/.codex"))


def test_codex_keychain_account_differs_per_path() -> None:
    a = _compute_codex_keychain_account(Path("/Users/a/.codex"))
    b = _compute_codex_keychain_account(Path("/Users/b/.codex"))
    assert a != b


# === Real Keychain reader (platform behaviour) ===


def test_real_keychain_reader_returns_none_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _real_keychain_reader("any-service", None) is None


def test_real_keychain_reader_handles_security_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    fake_run = MagicMock(return_value=MagicMock(returncode=1, stdout=""))
    monkeypatch.setattr("caqrs.providers._cli_creds.subprocess.run", fake_run)
    assert _real_keychain_reader("Claude Code-credentials", None) is None
    fake_run.assert_called_once()


def test_real_keychain_reader_returns_stripped_stdout_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    fake_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout='{"some":"payload"}\n'),
    )
    monkeypatch.setattr("caqrs.providers._cli_creds.subprocess.run", fake_run)
    assert _real_keychain_reader("Claude Code-credentials", None) == '{"some":"payload"}'


# === Claude: Keychain fallback ===


def test_claude_keychain_used_when_file_missing(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "kc-access",
                "refreshToken": "kc-refresh",
                "expiresAt": 1_900_000_000_000,
            },
        },
    )

    def reader(service: str, account: str | None) -> str | None:
        assert service == "Claude Code-credentials"
        assert account is None
        return payload

    cred = load_claude_cli_cred(tmp_path, keychain_reader=reader)
    assert isinstance(cred, OAuthCredential)
    assert cred.access_token == "kc-access"
    assert cred.refresh_token == "kc-refresh"


def test_claude_keychain_returns_token_when_no_refresh(tmp_path: Path) -> None:
    payload = json.dumps(
        {"claudeAiOauth": {"accessToken": "tok", "expiresAt": 1_900_000_000_000}},
    )
    cred = load_claude_cli_cred(tmp_path, keychain_reader=lambda *_: payload)
    assert isinstance(cred, TokenCredential)
    assert cred.access_token == "tok"


def test_claude_file_takes_precedence_over_keychain(tmp_path: Path) -> None:
    creds_dir = tmp_path / ".claude"
    creds_dir.mkdir()
    (creds_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "from-file",
                    "refreshToken": "rt",
                    "expiresAt": 1_900_000_000_000,
                },
            },
        ),
        encoding="utf-8",
    )
    keychain_call_count = 0

    def reader(_service: str, _account: str | None) -> str | None:
        nonlocal keychain_call_count
        keychain_call_count += 1
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "from-keychain",
                    "expiresAt": 1_900_000_000_000,
                },
            },
        )

    cred = load_claude_cli_cred(tmp_path, keychain_reader=reader)
    assert cred is not None
    assert cred.access_token == "from-file"
    assert keychain_call_count == 0


def test_claude_keychain_consulted_when_file_invalid(tmp_path: Path) -> None:
    creds_dir = tmp_path / ".claude"
    creds_dir.mkdir()
    (creds_dir / ".credentials.json").write_text("garbage", encoding="utf-8")

    def reader(_service: str, _account: str | None) -> str | None:
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "from-keychain",
                    "refreshToken": "rt",
                    "expiresAt": 1_900_000_000_000,
                },
            },
        )

    cred = load_claude_cli_cred(tmp_path, keychain_reader=reader)
    assert cred is not None
    assert cred.access_token == "from-keychain"


def test_claude_keychain_returns_none_for_malformed_payload(tmp_path: Path) -> None:
    cred = load_claude_cli_cred(tmp_path, keychain_reader=lambda *_: "not json")
    assert cred is None


def test_claude_keychain_returns_none_when_reader_yields_none(tmp_path: Path) -> None:
    cred = load_claude_cli_cred(tmp_path, keychain_reader=lambda *_: None)
    assert cred is None


# === Codex: Keychain fallback ===


def test_codex_keychain_used_when_file_missing(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    expected_account = _compute_codex_keychain_account(codex_home)
    payload = json.dumps(
        {
            "tokens": {
                "access_token": "kc-access",
                "refresh_token": "kc-refresh",
                "account_id": "acc",
            },
        },
    )

    def reader(service: str, account: str | None) -> str | None:
        assert service == "Codex Auth"
        assert account == expected_account
        return payload

    cred = load_codex_cli_cred(codex_home, keychain_reader=reader)
    assert cred is not None
    assert cred.access_token == "kc-access"
    assert cred.account_id == "acc"


def test_codex_file_takes_precedence_over_keychain(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {"tokens": {"access_token": "from-file", "refresh_token": "rt"}},
        ),
        encoding="utf-8",
    )
    reader_calls = 0

    def reader(_service: str, _account: str | None) -> str | None:
        nonlocal reader_calls
        reader_calls += 1
        return None

    cred = load_codex_cli_cred(codex_home, keychain_reader=reader)
    assert cred is not None
    assert cred.access_token == "from-file"
    assert reader_calls == 0


def test_codex_keychain_consulted_when_file_invalid(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("garbage", encoding="utf-8")

    payload = json.dumps(
        {"tokens": {"access_token": "from-keychain", "refresh_token": "rt"}},
    )
    cred = load_codex_cli_cred(codex_home, keychain_reader=lambda *_: payload)
    assert cred is not None
    assert cred.access_token == "from-keychain"


def test_codex_keychain_uses_resolved_home_for_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit codex_home, the account hash uses CODEX_HOME or ~/.codex."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "via-env"))
    expected_account = _compute_codex_keychain_account(tmp_path / "via-env")

    captured: list[tuple[str, str | None]] = []

    def reader(service: str, account: str | None) -> str | None:
        captured.append((service, account))
        return None

    load_codex_cli_cred(keychain_reader=reader)
    assert captured == [("Codex Auth", expected_account)]


# === Default fixture isolation ===


def test_default_loaders_do_not_touch_real_keychain(tmp_path: Path) -> None:
    """The autouse fixture in conftest.py keeps the real Keychain off-limits."""
    assert load_claude_cli_cred(tmp_path) is None
    assert load_codex_cli_cred(tmp_path / ".codex") is None
    # Direct call to the (monkeypatched) module-level reader yields None too.
    assert _cli_creds._real_keychain_reader("any", None) is None
