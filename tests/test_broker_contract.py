"""Parametrized broker contract suite — NFR-LIVE-BROKER-1..7.

Asserts the seven non-functional safety requirements from
:doc:`docs/decisions/0008-live-broker-safety` against any concrete
``BrokerProtocol`` implementation. The fixture is parametrized so the
same suite runs against PaperBroker today and against LiveBroker once
P4 lands; LiveBroker's slot is intentionally left as a TODO comment in
the fixture.

Two-step TDD dispatch (ADR-0006):

- **Step 1**: every NFR test is decorated
  ``@pytest.mark.xfail(strict=True, reason="impl pending — Task #87 ...")``
  and the body raises ``NotImplementedError``. Running
  ``pytest tests/test_broker_contract.py -q`` proves the red phase:
  every test is ``xfailed`` (and ``strict=True`` would catch any
  ``xpassed`` from a stub that accidentally returned).
- **Step 2**: tests for the NFRs that PaperBroker actually satisfies
  (NFR-1 default-off in the "PaperBroker is not a live broker" reading,
  NFR-2 credential isolation, NFR-7 distinct event taxonomy) get the
  real assertion bodies and have ``xfail`` removed. Tests that
  genuinely require a real LiveBroker (NFR-3, -4, -5, -6) keep
  ``xfail`` with a documented reason; flipping them to ``passed`` is
  the LiveBroker PR's job.

Conventions:

- Tests are channel-agnostic: no stdout, no readline. They construct
  a broker through the fixture and exercise its public surface.
- Static checks (NFR-2) run against the implementation module via
  ``inspect.getsource`` — no I/O, fast, fully deterministic.
- Behavioural checks (NFR-7) construct a one-shot CycleRunner with a
  real PaperBroker and assert against the EventLog. This is
  duplicative of ``test_orchestrator_paper_broker.py`` but scoped to
  the *taxonomy* check rather than wiring.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from caqrs.execution.paper_broker import PaperBroker

if TYPE_CHECKING:
    from caqrs.execution.protocol import BrokerProtocol


# === Fixture: parametrize across broker implementations =====================


@pytest.fixture(
    params=[
        pytest.param(
            lambda: PaperBroker(initial_capital_usd=Decimal("100000")),
            id="PaperBroker",
        ),
        # LiveBroker will be added in P4 PR; the contract suite already
        # has the slot. Adding the param is the only test-side change
        # required when LiveBroker lands.
    ],
)
def broker(request: pytest.FixtureRequest) -> BrokerProtocol:
    factory: Callable[[], BrokerProtocol] = request.param
    return factory()


# === NFR-LIVE-BROKER-1: Default-off ========================================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-1"


@pytest.mark.xfail(
    strict=True,
    reason="impl pending — Task #87 step 1 (assertion authored in step 2)",
)
def test_default_off_for_live_brokers_only(broker: BrokerProtocol) -> None:
    """A live broker MUST expose ``enable_live_orders`` defaulting to
    ``False``; a paper broker MUST NOT claim to be a live broker.

    Both shapes are valid contract states; the contract is that **one
    or the other** is true, not silently ambiguous (no live broker that
    forgot the flag, no paper broker that pretends to be live).

    Step 2 will replace the body with: read ``enable_live_orders`` if
    present and assert it is False; otherwise scan for paper-side
    "live" surface markers (``submit_live_order``, ``live_session``, …)
    and assert their absence.
    """
    raise NotImplementedError("Task #87 step 1 placeholder; assertion authored in step 2")


# === NFR-LIVE-BROKER-2: Credential isolation ===============================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-2"


@pytest.mark.xfail(
    strict=True,
    reason="impl pending — Task #87 step 1 (assertion authored in step 2)",
)
def test_paper_broker_does_not_import_live_broker_env_vars() -> None:
    """Static-import audit: PaperBroker source MUST NOT mention any
    ``LIVE_BROKER_*`` env var. A future LiveBroker reads
    ``LIVE_BROKER_*`` creds; if they leak into the paper code path, the
    credential-isolation perimeter is broken.

    Step 2 will replace the body with: ``inspect.getsource`` on the
    paper_broker module and assert that no ``LIVE_BROKER_*`` token
    appears (string literal, comment, docstring — even a docstring
    mention is conservatively flagged).
    """
    raise NotImplementedError("Task #87 step 1 placeholder; assertion authored in step 2")


@pytest.mark.xfail(
    strict=True,
    reason="impl pending — Task #87 step 1 (assertion authored in step 2)",
)
def test_broker_does_not_leak_credentials_across_classes(
    broker: BrokerProtocol,
) -> None:
    """Generalisation of the above: whichever broker is under test,
    its source MUST NOT reference env vars belonging to a different
    broker family. The "different family" is decided dynamically: a
    broker that exposes ``enable_live_orders`` is treated as a live
    broker (foreign prefixes = data-source vars like ``JQUANTS_``,
    ``EDINET_``); a broker that does not is treated as paper (foreign
    prefix = ``LIVE_BROKER_``).

    Step 2 will replace the body with the dynamic source-inspection
    assertion.
    """
    raise NotImplementedError("Task #87 step 1 placeholder; assertion authored in step 2")


# === NFR-LIVE-BROKER-3: Dry-run parity =====================================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-3"


@pytest.mark.xfail(
    strict=True,
    reason="PaperBroker has no separate dry-run mode (it IS the dry-run); "
    "NFR-LIVE-BROKER-3 applies to LiveBroker pre-flight wiring",
)
@pytest.mark.asyncio
async def test_dry_run_does_not_change_broker_state(broker: BrokerProtocol) -> None:
    """LiveBroker MUST expose a ``dry_run=True`` execute path that
    invokes ``PaperBroker.execute`` internally and asserts the result
    is ``ExecutionStatus.FILLED`` before any venue submission. State
    on the live broker (positions, idempotency-key log, kill-switch
    counter) MUST be unchanged after a dry-run call.

    For PaperBroker this NFR is N/A — there is no separate dry-run
    mode because the entire broker IS the dry-run. The xfail here
    documents the contract for LiveBroker; the assertion will be
    authored when LiveBroker lands.
    """
    raise NotImplementedError("LiveBroker dry-run-parity assertion deferred to P4 PR")


# === NFR-LIVE-BROKER-4: Idempotency key on every order =====================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-4"


@pytest.mark.xfail(
    strict=True,
    reason="PaperBroker doesn't implement idempotency keys; "
    "NFR-LIVE-BROKER-4 is a LiveBroker contract",
)
def test_idempotency_key_is_deterministic(broker: BrokerProtocol) -> None:
    """LiveBroker MUST expose a ``compute_idempotency_key`` helper
    such that for any
    ``(cycle_id, decision_run_id, ticker, side, quantity)`` tuple,
    repeated invocations return the same sha256 hex digest.

    Spec key derivation:
    ``sha256_hex((cycle_id, decision_run_id, ticker, side, quantity))``
    (canonical-JSON of the same fields is also acceptable; the exact
    serialization is a P4 ADR-0009 detail).

    For PaperBroker this NFR is N/A — paper has no venue, no replay
    semantics, no idempotency contract. The xfail documents the
    expected helper signature.
    """
    raise NotImplementedError("LiveBroker idempotency-key assertion deferred to P4 PR")


# === NFR-LIVE-BROKER-5: Kill-switch ========================================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-5"


@pytest.mark.xfail(
    strict=True,
    reason="PaperBroker is synchronous and has no in-flight order state; "
    "NFR-LIVE-BROKER-5 is a LiveBroker contract",
)
@pytest.mark.asyncio
async def test_kill_switch_aborts_within_one_cycle(broker: BrokerProtocol) -> None:
    """LiveBroker MUST expose a ``kill_switch()`` method that:

    1. Aborts any in-flight orders within ≤ 1 CycleRunner iteration.
    2. Causes subsequent ``execute()`` calls to return
       ``ExecutionStatus.SKIPPED`` with reason ``"kill switch engaged"``
       until a human re-enables via the NFR-1 approval workflow.

    For PaperBroker this NFR is structurally N/A: synchronous,
    in-process execution has no "in-flight" window to abort. The xfail
    documents the expected interface; the assertion will be written
    when LiveBroker lands.
    """
    raise NotImplementedError("LiveBroker kill-switch assertion deferred to P4 PR")


# === NFR-LIVE-BROKER-6: Broker-level daily loss cap ========================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-6"


@pytest.mark.xfail(
    strict=True,
    reason="PaperBroker exposes realized_pnl_usd but no cap-trigger; "
    "NFR-LIVE-BROKER-6 is a LiveBroker contract (independent state)",
)
def test_broker_level_daily_loss_cap_independent_from_gateway(
    broker: BrokerProtocol,
) -> None:
    """LiveBroker MUST expose ``live_broker_daily_loss_cap_usd``
    (config) and an internal realized-loss accumulator that triggers
    ``kill_switch()`` (NFR-5) when the cap is breached. The accumulator
    MUST NOT share state with ``PolicyGatewayConfig.daily_loss_limit_usd``
    — the duplicate computation is the safety property (defense in
    depth), not an inefficiency.

    For PaperBroker this NFR is N/A — paper has ``realized_pnl_usd``
    but no cap and no auto-kill-switch. The xfail documents the
    expected surface (cap config attribute + internal accumulator).
    """
    raise NotImplementedError("LiveBroker daily-loss-cap assertion deferred to P4 PR")


# === NFR-LIVE-BROKER-7: Distinct event taxonomy ============================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-7"


@pytest.mark.xfail(
    strict=True,
    reason="impl pending — Task #87 step 1 (assertion authored in step 2)",
)
@pytest.mark.asyncio
async def test_paper_broker_uses_broker_executed_not_broker_live_kinds() -> None:
    """A happy-path PaperBroker cycle MUST emit ``BROKER_EXECUTED`` and
    MUST NOT emit any ``BROKER_LIVE_*`` event kind. NFR-LIVE-BROKER-7
    reserves ``BROKER_LIVE_*`` for the future LiveBroker; if PaperBroker
    starts emitting them, the audit-grade "did this cycle touch real
    money?" grep is broken.

    Step 2 will replace the body with: run one CycleRunner iteration
    end-to-end (stub agents + real PaperBroker + EventLog), scan the
    log for ``BROKER_EXECUTED`` presence and ``BROKER_LIVE_*`` absence
    (both by enum-member name and by serialized string value).
    """
    raise NotImplementedError("Task #87 step 1 placeholder; assertion authored in step 2")
