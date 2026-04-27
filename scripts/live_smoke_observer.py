#!/usr/bin/env python3
"""Standalone live smoke test for ObserverAgent.

Run manually against your LiteLLM gateway (or any OpenAI-compatible
endpoint). Reads connection details from the environment.

Usage::

    export CAQRS_LITELLM_BASE_URL=http://localhost:11500/v1
    export CAQRS_LITELLM_API_KEY=sk-litellm-local
    export CAQRS_LITELLM_MODEL=openrouter/anthropic/claude-opus-4.7
    uv run python scripts/live_smoke_observer.py

The script returns 0 on a successful round trip, 1 on failure. Useful
for shaking out gateway connectivity before opting into the pytest
live suite (``CAQRS_LIVE=1 uv run pytest tests/live/``).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

from caqrs.agents import ObserverAgent
from caqrs.providers import OpenAICompatProvider
from caqrs.schemas import DataDimension, ObserverInput

_DEFAULT_BASE_URL = "http://localhost:11500/v1"
_DEFAULT_API_KEY = "sk-litellm-local"
_SYNTHETIC_DATA_BLOCK = """\
Synthetic smoke-test data, source: caqrs-live-smoke (script).

AAPL: last_close=182.45, return_1m=+0.04, return_12m=+0.18, vol_30d=0.22
MSFT: last_close=415.20, return_1m=-0.01, return_12m=+0.32, vol_30d=0.18

News themes (last week, source: synthetic):
- AAPL Q1 earnings beat consensus; Services revenue +14%
- MSFT Azure outage in eastern US, resolved in 4 hours

Macro (source: synthetic):
- VIX 14.2, 10Y yield 4.2%, USDJPY 152.0
"""


class _SmokeObserver(ObserverAgent):
    def build_user_message(self, payload: ObserverInput) -> str:
        return (
            f"Observe the universe {list(payload.universe)} as of "
            f"{payload.as_of.isoformat()} for a {payload.horizon_days}-day "
            f"horizon.\n\n{_SYNTHETIC_DATA_BLOCK}\n"
            "Emit an ObserverArtifact summarising the regime."
        )


async def _main() -> int:
    model = os.environ.get("CAQRS_LITELLM_MODEL")
    if not model:
        print(
            "ERROR: CAQRS_LITELLM_MODEL not set. "
            "Set it to the LiteLLM model alias (e.g. "
            "'openrouter/anthropic/claude-opus-4.7').",
            file=sys.stderr,
        )
        return 1
    base_url = os.environ.get("CAQRS_LITELLM_BASE_URL", _DEFAULT_BASE_URL)
    api_key = os.environ.get("CAQRS_LITELLM_API_KEY", _DEFAULT_API_KEY)

    print(f"[smoke] target: {base_url}  model: {model}")

    provider = OpenAICompatProvider(base_url=base_url, api_key=api_key, model=model)
    agent = _SmokeObserver(provider=provider, max_output_tokens=2048)
    payload = ObserverInput(
        universe=("AAPL", "MSFT"),
        as_of=datetime.now(UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES, DataDimension.NEWS, DataDimension.MACRO),
    )

    result = await agent.run(payload)

    if not result.is_ok():
        print(f"[smoke] FAILED: {result.error}", file=sys.stderr)
        return 1

    artifact = result.output
    assert artifact is not None
    print("[smoke] OK")
    print(f"[smoke] universe: {artifact.universe}")
    print(f"[smoke] regime_summary: {artifact.regime_summary[:200]}")
    print(
        f"[smoke] tokens in/out: {result.metadata.token_in} / {result.metadata.token_out}",
    )
    print(f"[smoke] latency_ms: {result.metadata.latency_ms}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
