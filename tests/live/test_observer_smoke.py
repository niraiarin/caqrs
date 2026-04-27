"""Live smoke test for ObserverAgent against a real LiteLLM gateway.

Gated by ``CAQRS_LIVE=1``. Validates the full structured-output round
trip: the agent's system prompt + user message reach the gateway, the
model emits a tool call, the provider parses it, and the artifact
validates against ``ObserverArtifact``.

Synthetic data is injected via ``build_user_message`` so the model has
something concrete to summarise. The test does **not** assert any
content claims — only that the round trip succeeds and the artifact
type checks. Semantic correctness against real market data lands later
once data tools (P2) are wired in.
"""

from datetime import UTC, datetime

import pytest

from caqrs.agents import ObserverAgent
from caqrs.providers import OpenAICompatProvider
from caqrs.schemas import DataDimension, ObserverArtifact, ObserverInput

_SYNTHETIC_DATA_BLOCK = """\
Synthetic smoke-test data, source: caqrs-live-smoke 2026-04-27.

AAPL: last_close=182.45, return_1m=+0.04, return_12m=+0.18, vol_30d=0.22
MSFT: last_close=415.20, return_1m=-0.01, return_12m=+0.32, vol_30d=0.18

News themes (last week, source: synthetic):
- AAPL Q1 earnings beat consensus; Services revenue +14%
- MSFT Azure outage in eastern US, resolved in 4 hours

Macro (source: synthetic):
- VIX 14.2
- 10Y yield 4.2%
- USDJPY 152.0

Data quality:
- All prices are EOD; intraday data unavailable in this smoke test.
"""


class _ObserverSmokeAgent(ObserverAgent):
    """Test-only Observer that injects synthetic data into the user message.

    The production Observer relies on the orchestrator to inject
    real data via the user message (data tools land in P2). For the
    smoke test we mimic that injection inline.
    """

    def build_user_message(self, payload: ObserverInput) -> str:
        request = (
            f"Observe the universe {list(payload.universe)} as of "
            f"{payload.as_of.isoformat()} for a {payload.horizon_days}-day "
            f"horizon. Requested dimensions: "
            f"{[d.value for d in payload.dimensions]}.\n\n"
            f"{_SYNTHETIC_DATA_BLOCK}\n"
            f"Emit an ObserverArtifact summarising the regime."
        )
        return request


@pytest.mark.live
async def test_observer_runs_against_litellm(
    litellm_provider: OpenAICompatProvider,
) -> None:
    agent = _ObserverSmokeAgent(provider=litellm_provider, max_output_tokens=2048)

    payload = ObserverInput(
        universe=("AAPL", "MSFT"),
        as_of=datetime.now(UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES, DataDimension.NEWS, DataDimension.MACRO),
    )

    result = await agent.run(payload)

    assert result.is_ok(), f"Observer failed: {result.error}"
    artifact = result.output
    assert isinstance(artifact, ObserverArtifact)
    assert artifact.regime_summary  # non-empty per schema
    assert set(artifact.universe) == set(payload.universe), (
        "Observer should preserve the requested universe."
    )
    # Token accounting must round-trip from provider into RunMetadata.
    assert result.metadata.token_in > 0
    assert result.metadata.token_out > 0
    assert result.metadata.latency_ms >= 0
