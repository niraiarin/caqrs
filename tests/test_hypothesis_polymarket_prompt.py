"""Tests for HypothesisAgent.build_user_message including Polymarket signals."""

from datetime import UTC, datetime
from decimal import Decimal

from caqrs.agents.hypothesis import HypothesisAgent
from caqrs.agents.observer import ObserverAgent
from caqrs.agents.prompts import format_polymarket_block
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.observer import (
    DataDimension,
    ObserverArtifact,
    ObserverInput,
    PolymarketOutcome,
    PolymarketSignal,
)


def _meta() -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name="observer",
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=0,
        token_in=0,
        token_out=0,
    )


def _binary_signal(*, midpoint: Decimal = Decimal("0.62")) -> PolymarketSignal:
    return PolymarketSignal(
        market_id="12345",
        slug="fed-cuts-2026",
        question="Will the Fed cut in 2026?",
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        is_binary=True,
        outcomes=(
            PolymarketOutcome(label="Yes", token_id="100", midpoint=midpoint),
            PolymarketOutcome(label="No", token_id="200", midpoint=Decimal(1) - midpoint),
        ),
        fetched_at=datetime.now(UTC),
    )


def _observer_artifact(
    *,
    signals: tuple[PolymarketSignal, ...] = (),
) -> ObserverArtifact:
    return ObserverArtifact(
        metadata=_meta(),
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        regime_summary="up",
        polymarket_signals=signals,
    )


def test_hypothesis_user_message_includes_polymarket_block_when_present() -> None:
    agent = HypothesisAgent.__new__(HypothesisAgent)
    artifact = _observer_artifact(signals=(_binary_signal(),))
    msg = agent.build_user_message(artifact)
    assert "Polymarket implied probabilities" in msg
    assert "P(Yes)=0.62" in msg
    assert "fed-cuts-2026" in msg


def test_hypothesis_user_message_omits_block_without_signals() -> None:
    agent = HypothesisAgent.__new__(HypothesisAgent)
    artifact = _observer_artifact(signals=())
    msg = agent.build_user_message(artifact)
    assert "Polymarket" not in msg


def test_format_polymarket_block_returns_empty_string_for_empty_input() -> None:
    """Shared formatter contract: empty signals → empty string so callers
    can unconditionally append."""
    assert format_polymarket_block(()) == ""


def test_format_polymarket_block_handles_binary_with_yes_midpoint() -> None:
    line = format_polymarket_block((_binary_signal(midpoint=Decimal("0.73")),))
    assert "P(Yes)=0.73" in line
    assert "Will the Fed cut in 2026?" in line


def test_observer_and_hypothesis_share_identical_block_rendering() -> None:
    """Both agents must produce the same Polymarket block so a downstream
    log diff doesn't show spurious changes when the same signals propagate
    from Observer to Hypothesis."""
    sig = _binary_signal()
    observer_agent = ObserverAgent.__new__(ObserverAgent)
    hypothesis_agent = HypothesisAgent.__new__(HypothesisAgent)

    observer_input = ObserverInput(
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.MACRO,),
        polymarket_signals=(sig,),
    )
    artifact = _observer_artifact(signals=(sig,))

    observer_msg = observer_agent.build_user_message(observer_input)
    hypothesis_msg = hypothesis_agent.build_user_message(artifact)

    block_marker = "Polymarket implied probabilities"
    obs_block = observer_msg[observer_msg.index(block_marker) :]
    hyp_block = hypothesis_msg[hypothesis_msg.index(block_marker) :]
    assert obs_block == hyp_block
