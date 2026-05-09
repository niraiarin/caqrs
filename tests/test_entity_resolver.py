"""Test list for the EntityResolver — Phase E4 minimal slice (Codex 2026-05-03 review).

ADR-0006 step 1 / step 2 dispatch:

- **Step 1** (this commit): every test below is decorated
  ``@pytest.mark.xfail(strict=True, reason="impl pending — Phase E4")``
  and exercises the public surface of
  :mod:`caqrs.entities.resolver` plus the ``CycleRunner.entity_resolver``
  ctor hook. The factory raises ``NotImplementedError`` and the runner
  ignores the resolver in step 1, so all 5 tests xfail cleanly.
- **Step 2** (next commit): the factory body is implemented and the
  runner emits ``IDENTIFIER_RESOLVED`` events; the xfail markers are
  removed in the same commit that turns the assertions green.

Per Codex's review, this slice proves *only*:

- one cycle resolves J-Quants and EDINET identifiers to the same
  canonical issuer (T-RES-A1)
- a non-matching ticker yields a sentinel resolution with null fields
  (T-RES-A2)
- the resolver does NOT mutate the ObserverInput (T-RES-A3)
- IDENTIFIER_RESOLVED events appear once per universe member when
  the runner has a resolver wired (T-RES-A4)
- when no resolver is wired, the runner runs as before — no events
  (T-RES-A5)

Out of scope: vector search, fuzzy matching, GLEIF, multi-source joins.
Those are Phase E5+.

The runner-integration tests (T-RES-A4 / T-RES-A5) deliberately use a
**failure-result Observer stub** so the cycle aborts immediately after
the resolver hook fires; this lets the test exercise the resolver
without building fixtures for every downstream agent. The resolver
contract is "fire before Observer", not "fire before any specific
later phase".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel

from caqrs.agents.protocol import AgentResult
from caqrs.entities.in_memory import InMemoryEntityStore
from caqrs.entities.resolver import make_entity_resolver
from caqrs.entities.types import (
    Identifier,
    IdentifierKind,
    Issuer,
    new_issuer_id,
)
from caqrs.orchestrator import (
    CycleBudget,
    CycleEventKind,
    CycleRunner,
    EventLog,
    new_cycle_id,
)
from caqrs.schemas.audit import AuditReport
from caqrs.schemas.backtest_report import BacktestReport
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.decision import StrategyDecision
from caqrs.schemas.hypothesis_card import HypothesisCard
from caqrs.schemas.observer import (
    DataDimension,
    ObserverArtifact,
    ObserverInput,
)
from caqrs.schemas.research_plan import ResearchPlan
from caqrs.schemas.skeptic import SkepticReport

# --- minimal helpers -----------------------------------------------------


def _meta(*, agent: str) -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name=agent,
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=10,
        token_in=10,
        token_out=5,
    )


def _toyota_issuer() -> Issuer:
    """An EntityStore-shaped Toyota: J-Quants 72030 + EDINET E02144 +
    sec_code 7203 + JCN. Enough identifier diversity for the resolver
    to demonstrate cross-source canonicalisation."""
    return Issuer(
        id=new_issuer_id(),
        lei=None,
        jcn="1180301018771",
        display_name="Toyota Motor Corporation",
        identifiers=(
            Identifier(kind=IdentifierKind.JQUANTS_CODE, value="72030"),
            Identifier(kind=IdentifierKind.SEC_CODE, value="7203"),
            Identifier(kind=IdentifierKind.EDINET_CODE, value="E02144"),
            Identifier(kind=IdentifierKind.JCN, value="1180301018771"),
        ),
    )


def _observer_input(*, universe: tuple[str, ...]) -> ObserverInput:
    return ObserverInput(
        universe=universe,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES,),
    )


class _NeverCalled[I: BaseModel, O: BaseModel]:
    """Agent stub that raises if invoked. Used for the agents downstream
    of Observer in the resolver integration tests, where the cycle is
    expected to abort before any of them runs."""

    name: str = "never-called"

    async def run(self, payload: I, /) -> AgentResult[O]:  # pragma: no cover
        msg = f"{type(self).__name__}.run should not have been invoked"
        raise AssertionError(msg)


class _FailingObserver:
    """Observer stub returning a failure AgentResult. The runner will
    abort after Observer; the resolver hook fires *before* Observer,
    so its events are already in the log when we inspect."""

    name = "observer"

    async def run(self, payload: ObserverInput, /) -> AgentResult[ObserverArtifact]:
        return AgentResult[ObserverArtifact](
            output=None,
            error="stub-observer-fail (resolver-test-only)",
            metadata=_meta(agent="observer"),
        )


async def _never_call_backtest(_plan: ResearchPlan) -> BacktestReport:  # pragma: no cover
    msg = "backtest_executor should not have been invoked in resolver tests"
    raise AssertionError(msg)


def _build_resolver_test_runner(
    *,
    entity_resolver: object | None,
    event_log: EventLog,
) -> CycleRunner:
    """Construct a runner whose Observer fails immediately. The cycle
    will run resolver-hook -> Observer -> abort, leaving no
    downstream-agent fixtures required."""
    bg = CycleBudget(cycle_id=new_cycle_id(), token_cap=10_000, wallclock_seconds_cap=60.0)
    return CycleRunner(
        observer=_FailingObserver(),
        hypothesis=_NeverCalled[ObserverArtifact, HypothesisCard](),
        skeptic=_NeverCalled[HypothesisCard, SkepticReport](),
        research=_NeverCalled[BaseModel, ResearchPlan](),
        auditor=_NeverCalled[BaseModel, AuditReport](),
        decider=_NeverCalled[BaseModel, StrategyDecision](),
        backtest_executor=_never_call_backtest,
        event_log=event_log,
        budget=bg,
        entity_resolver=entity_resolver,  # type: ignore[arg-type]
    )


# --- T-RES-A1: jquants + edinet -> same canonical issuer -----------------


@pytest.mark.xfail(strict=True, reason="impl pending — Phase E4")
def test_jquants_and_edinet_codes_resolve_to_same_canonical_issuer() -> None:
    """An EntityStore seeded with Toyota carries both
    ``(JQUANTS_CODE, "72030")`` and ``(EDINET_CODE, "E02144")``. When
    the resolver walks ``ObserverInput.universe=("72030", "E02144")``,
    both lookups MUST resolve to the **same** ``canonical_issuer_id``
    — the proof of value Codex called out for Phase E4."""
    store = InMemoryEntityStore()
    toyota = _toyota_issuer()
    store.upsert_issuer(issuer=toyota)

    resolver = make_entity_resolver(store=store)
    obs_input = _observer_input(universe=("72030", "E02144"))
    resolutions = resolver(obs_input)

    assert len(resolutions) == 2
    issuer_ids = {r.canonical_issuer_id for r in resolutions}
    assert issuer_ids == {toyota.id}

    by_ticker = {r.input_ticker: r for r in resolutions}
    assert by_ticker["72030"].matched_kind == IdentifierKind.JQUANTS_CODE
    assert by_ticker["E02144"].matched_kind == IdentifierKind.EDINET_CODE


# --- T-RES-A2: unresolved ticker yields null sentinel --------------------


@pytest.mark.xfail(strict=True, reason="impl pending — Phase E4")
def test_unresolved_ticker_yields_null_resolution() -> None:
    """A ticker the store knows nothing about MUST yield an
    :class:`IdentifierResolution` with both ``canonical_issuer_id`` and
    ``matched_kind`` set to ``None``. The resolver never invents
    issuers."""
    store = InMemoryEntityStore()
    resolver = make_entity_resolver(store=store)
    obs_input = _observer_input(universe=("UNKNOWN_TICKER",))
    resolutions = resolver(obs_input)

    assert len(resolutions) == 1
    only = resolutions[0]
    assert only.input_ticker == "UNKNOWN_TICKER"
    assert only.canonical_issuer_id is None
    assert only.matched_kind is None


# --- T-RES-A3: resolver does not mutate ObserverInput --------------------


@pytest.mark.xfail(strict=True, reason="impl pending — Phase E4")
def test_resolver_does_not_mutate_observer_input() -> None:
    """The resolver is read-only against the input. Calling it MUST
    leave ``ObserverInput.universe`` exactly as the caller passed it
    in (no dedup, no canonicalisation, no reordering)."""
    store = InMemoryEntityStore()
    store.upsert_issuer(issuer=_toyota_issuer())
    resolver = make_entity_resolver(store=store)
    original_universe = ("72030", "E02144")
    obs_input = _observer_input(universe=original_universe)
    resolver(obs_input)
    assert obs_input.universe == original_universe


# --- T-RES-A4: CycleRunner emits one IDENTIFIER_RESOLVED per ticker ------


@pytest.mark.xfail(strict=True, reason="impl pending — Phase E4")
@pytest.mark.asyncio
async def test_cycle_runner_emits_one_identifier_resolved_event_per_universe_member() -> None:
    """When wired with a resolver, ``CycleRunner.run()`` MUST emit
    exactly one ``IDENTIFIER_RESOLVED`` event per universe element
    BEFORE the Observer is invoked. We use a failing Observer so the
    cycle aborts immediately after the resolver hook fires; the
    resolver events MUST be in the event log regardless."""
    store = InMemoryEntityStore()
    store.upsert_issuer(issuer=_toyota_issuer())
    resolver = make_entity_resolver(store=store)

    log = EventLog()
    runner = _build_resolver_test_runner(entity_resolver=resolver, event_log=log)
    await runner.run(_observer_input(universe=("72030", "E02144")))

    resolved_events = log.filter_by_kind(CycleEventKind.IDENTIFIER_RESOLVED)
    assert len(resolved_events) == 2
    tickers = {e.payload["input_ticker"] for e in resolved_events}
    assert tickers == {"72030", "E02144"}
    issuer_ids = {e.payload["canonical_issuer_id"] for e in resolved_events}
    assert len(issuer_ids) == 1


# --- T-RES-A5: no resolver -> no events (regression guard, not xfailed) -


@pytest.mark.asyncio
async def test_cycle_runner_without_resolver_emits_no_identifier_events() -> None:
    """The resolver hook is opt-in. A runner constructed without
    ``entity_resolver=`` MUST emit zero ``IDENTIFIER_RESOLVED`` events
    — the cycle runs as it did before Phase E4.

    NB: this test is **not** xfailed in step 1 because it passes both
    pre- and post-impl: pre-impl the runner ignores the resolver
    altogether (no events emitted regardless of wiring); post-impl the
    runner emits events only when the resolver is configured. The test
    is a regression guard against accidental "always emit" wiring,
    not a feature-completion proof. Only the 4 new-feature tests above
    carry ``xfail(strict=True)``.
    """
    log = EventLog()
    runner = _build_resolver_test_runner(entity_resolver=None, event_log=log)
    await runner.run(_observer_input(universe=("72030", "E02144")))

    resolved_events = log.filter_by_kind(CycleEventKind.IDENTIFIER_RESOLVED)
    assert resolved_events == ()
