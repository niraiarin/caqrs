"""Local conftest for ``tests/live/``: shared fixtures for live smoke tests.

Each fixture reads connection details from the environment so the same
test runs against a developer's local LiteLLM gateway and against a
shared CI gateway (when one exists). Defaults target the user's local
LiteLLM router on port 11500 (per CLAUDE.md project layout).
"""

import os

import pytest

from caqrs.providers import OpenAICompatProvider

_DEFAULT_BASE_URL = "http://localhost:11500/v1"
_DEFAULT_API_KEY = "sk-litellm-local"
_MODEL_ENV_VAR = "CAQRS_LITELLM_MODEL"
_BASE_URL_ENV_VAR = "CAQRS_LITELLM_BASE_URL"
_API_KEY_ENV_VAR = "CAQRS_LITELLM_API_KEY"
_JQUANTS_ENV_VAR = "JQUANTS_API_KEY"


@pytest.fixture
def litellm_provider() -> OpenAICompatProvider:
    """Build an OpenAICompatProvider for the local LiteLLM gateway.

    Different LiteLLM deployments expose different model aliases
    (openrouter/..., ollama/...) so the model id is mandatory. The
    fixture short-circuits via pytest.skip when the env var is
    missing so live tests deselect rather than fail on a clean
    machine.
    """
    model = os.environ.get(_MODEL_ENV_VAR)
    if not model:
        pytest.skip(
            f"{_MODEL_ENV_VAR} not set; set it to the LiteLLM model alias to run live smoke tests.",
        )
    base_url = os.environ.get(_BASE_URL_ENV_VAR, _DEFAULT_BASE_URL)
    api_key = os.environ.get(_API_KEY_ENV_VAR, _DEFAULT_API_KEY)
    return OpenAICompatProvider(base_url=base_url, api_key=api_key, model=model)


@pytest.fixture
def jquants_api_key() -> str:
    """Return the J-Quants API key from the env, or skip if missing.

    Live J-Quants tests need a real key (free-tier registration is
    sufficient); deselect when running on a machine without one
    configured rather than fail.
    """
    key = os.environ.get(_JQUANTS_ENV_VAR)
    if not key:
        pytest.skip(
            f"{_JQUANTS_ENV_VAR} not set; sign up at jpx-jquants.com (free tier) to run.",
        )
    return key
