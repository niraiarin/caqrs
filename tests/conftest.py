"""Pytest configuration: hypothesis profiles, fixtures, and live-test gating."""

import os
from collections.abc import Iterable

import pytest
from hypothesis import HealthCheck, Verbosity, settings

from caqrs.providers import _cli_creds

_LIVE_ENV_VAR = "CAQRS_LIVE"
_LIVE_MARKER = "live"

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


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Iterable[pytest.Item],
) -> None:
    """Deselect ``@pytest.mark.live`` tests unless ``CAQRS_LIVE=1`` is set.

    Live tests touch real LLM endpoints and incur token cost; they are
    explicitly opted into via the env var, never via CI. Tests gated this
    way must register the ``live`` marker (declared in ``pyproject.toml``).
    Deselection (rather than runtime gating) keeps CI output clean and
    avoids running fixture setup for tests that won't execute.
    """
    if os.environ.get(_LIVE_ENV_VAR) == "1":
        return
    keep: list[pytest.Item] = []
    drop: list[pytest.Item] = []
    for item in items:
        if item.get_closest_marker(_LIVE_MARKER) is None:
            keep.append(item)
        else:
            drop.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep  # type: ignore[index]
