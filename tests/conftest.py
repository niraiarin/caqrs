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

# dotenv tooling (e.g. dotenvx) injects empty strings for blank lines in
# .env, which os.environ.get treats as set; coalesce empty/whitespace to
# the default profile so a placeholder env file does not break test
# collection.
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "").strip() or "dev")


@pytest.fixture(autouse=True)
def _no_keychain_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: tests never touch the real macOS Keychain.

    Tests that exercise Keychain behaviour explicitly should pass their
    own ``keychain_reader`` callable to the loader functions. ``monkeypatch``
    auto-teardown restores the original reader after each test.
    """
    monkeypatch.setattr(_cli_creds, "_real_keychain_reader", lambda _service, _account: None)


@pytest.fixture(autouse=True)
def _disable_data_client_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop client-default rate-limiter intervals + retry waits to 0
    inside tests.

    Production J-Quants ships with a 12s pacing default (free tier
    5 req/min) and 30/60/90s linear backoff on 429. Both are
    correct for live use but slow respx-mocked tests massively. The
    fixture patches both module-level constants to zero. Tests that
    *want* to assert specific pacing / backoff durations construct
    their own AsyncRateLimiter and / or patch ``asyncio.sleep``
    explicitly (see test_jquants_rate_limit.py) — those paths are
    unaffected because they bypass the patched constants.
    """
    monkeypatch.setattr(
        "caqrs.data.jquants.client._FREE_TIER_MIN_INTERVAL_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        "caqrs.data.jquants.client._DEFAULT_RETRY_SCHEDULE",
        (0, 0, 0),
    )


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
