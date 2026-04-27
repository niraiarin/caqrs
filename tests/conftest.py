"""Pytest configuration: hypothesis profiles and global fixtures."""

import os

import pytest
from hypothesis import HealthCheck, Verbosity, settings

from caqrs.providers import _cli_creds

settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "dev",
    max_examples=50,
    verbosity=Verbosity.normal,
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True)
def _no_keychain_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: tests never touch the real macOS Keychain.

    Tests that exercise Keychain behaviour explicitly should pass their
    own ``keychain_reader`` callable to the loader functions. ``monkeypatch``
    auto-teardown restores the original reader after each test.
    """
    monkeypatch.setattr(_cli_creds, "_real_keychain_reader", lambda _service, _account: None)
