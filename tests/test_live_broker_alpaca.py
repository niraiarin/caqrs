"""Unit tests for LiveBrokerAlpaca — P4 first-cut.

ADR-0006 step 1 / step 2 dispatch:

- **Step 1** (this commit): every test below is decorated
  ``@pytest.mark.xfail(strict=True, reason="impl pending — P4")`` and
  exercises the public surface of
  :mod:`caqrs.execution.live_broker_alpaca`. The class methods raise
  ``NotImplementedError`` so all tests xfail cleanly.
- **Step 2** (next commit): bodies are implemented; xfail markers are
  removed in the same commit that turns the assertions green; the
  4 currently-deferred xfailed tests in
  ``tests/test_broker_contract.py`` flip to passing for the
  ``LiveBrokerAlpaca`` parametrize-id (the ``PaperBroker`` id retains
  its xfail via a runtime branch).

These tests cover LiveBrokerAlpaca-specific behaviour that the
broker-contract suite does not — the unit tests here pin
:class:`~caqrs.execution.live_broker_alpaca.LiveBrokerAlpaca` against
its individual NFR methods at a tighter granularity than the
broker-protocol contract permits. Per ADR-0009, the actual Alpaca
SDK integration is a follow-up PR; the live-submission code path is
short-circuited under default-off / kill-switch in this PR and never
attempts a venue connection.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from caqrs.execution.execution_report import ExecutionStatus
from caqrs.execution.live_broker_alpaca import LiveBrokerAlpaca
from caqrs.execution.paper_broker import PaperBroker
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import new_run_id
from caqrs.schemas.decision import DecisionAction, Side, TargetPosition


def _make_broker(
    *,
    enable_live_orders: bool = False,
    cap_usd: Decimal = Decimal("1000"),
) -> LiveBrokerAlpaca:
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    return LiveBrokerAlpaca(
        paper_broker=paper,
        live_broker_daily_loss_cap_usd=cap_usd,
        enable_live_orders=enable_live_orders,
    )


# --- T-LBA-1: idempotency key is a 64-char sha256 hex --------------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
def test_compute_idempotency_key_returns_64_char_sha256_hex() -> None:
    """NFR-LIVE-BROKER-4: ``compute_idempotency_key`` MUST return the
    full 64-char sha256 hex digest. ADR-0009 specifies the
    Alpaca-side ``client_order_id`` truncates to the leading 48 chars
    at submission time; the broker's public method exposes the
    untruncated form so callers can persist it for replay
    disambiguation."""
    broker = _make_broker()
    key = broker.compute_idempotency_key(
        cycle_id="0123456789abcdef",
        decision_run_id=new_run_id(),
        ticker="AAPL",
        side=Side.BUY,
        quantity=Decimal("100"),
    )
    assert isinstance(key, str)
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


# --- T-LBA-2: idempotency key is deterministic over equal inputs ---------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
def test_compute_idempotency_key_is_deterministic() -> None:
    """Same input tuple MUST produce the same key on every call. The
    NFR-LIVE-BROKER-4 contract test in the broker-protocol suite
    asserts this once; the unit test here pins the contract per
    method so a future refactor cannot regress quietly."""
    broker = _make_broker()
    decision_run_id = new_run_id()
    a = broker.compute_idempotency_key(
        cycle_id="deadbeefdeadbeef",
        decision_run_id=decision_run_id,
        ticker="MSFT",
        side=Side.SELL,
        quantity=Decimal("250.5"),
    )
    b = broker.compute_idempotency_key(
        cycle_id="deadbeefdeadbeef",
        decision_run_id=decision_run_id,
        ticker="MSFT",
        side=Side.SELL,
        quantity=Decimal("250.5"),
    )
    assert a == b


# --- T-LBA-3: idempotency key differs across input tuples ----------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
def test_compute_idempotency_key_changes_with_inputs() -> None:
    """Different input tuples MUST produce different keys. Trivial
    requirement for sha256, but the test pins it so a downstream
    refactor that, say, hashes only ``ticker`` cannot regress
    silently."""
    broker = _make_broker()
    cycle_id = "aaaaaaaaaaaaaaaa"
    decision_run_id = new_run_id()
    a = broker.compute_idempotency_key(
        cycle_id=cycle_id,
        decision_run_id=decision_run_id,
        ticker="AAPL",
        side=Side.BUY,
        quantity=Decimal("100"),
    )
    b = broker.compute_idempotency_key(
        cycle_id=cycle_id,
        decision_run_id=decision_run_id,
        ticker="MSFT",
        side=Side.BUY,
        quantity=Decimal("100"),
    )
    c = broker.compute_idempotency_key(
        cycle_id=cycle_id,
        decision_run_id=decision_run_id,
        ticker="AAPL",
        side=Side.BUY,
        quantity=Decimal("101"),
    )
    assert a != b
    assert a != c
    assert b != c


# --- T-LBA-4: kill-switch flips the engaged flag --------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
def test_kill_switch_engages_state() -> None:
    """``kill_switch()`` MUST flip the engaged flag; subsequent
    inspections MUST return ``True`` until ``reenable_after_human_approval``
    is called."""
    broker = _make_broker()
    assert broker.kill_switch_engaged is False
    broker.kill_switch()
    assert broker.kill_switch_engaged is True


# --- T-LBA-5: kill-switch causes execute() to skip ------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
@pytest.mark.asyncio
async def test_execute_skipped_when_kill_switch_engaged() -> None:
    """After ``kill_switch()`` is called, ``execute()`` MUST return
    SKIPPED with reason containing ``"kill switch engaged"`` even when
    ``enable_live_orders=True``. The kill-switch state takes
    precedence over the default-off flag."""
    broker = _make_broker(enable_live_orders=True)
    broker.kill_switch()
    action = FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        violations=(),
        source_decision_run_id=new_run_id(),
    )
    report = await broker.execute(action=action, prices={"AAPL": Decimal("180")})
    assert report.status is ExecutionStatus.SKIPPED
    assert "kill switch engaged" in (report.reason or "")


# --- T-LBA-6: execute skips when default-off ------------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
@pytest.mark.asyncio
async def test_execute_skipped_when_live_orders_disabled() -> None:
    """When ``enable_live_orders=False`` (the default), ``execute()``
    MUST return SKIPPED with reason ``"live orders disabled"`` —
    never silently no-op, never paper-passthrough."""
    broker = _make_broker(enable_live_orders=False)
    action = FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        violations=(),
        source_decision_run_id=new_run_id(),
    )
    report = await broker.execute(action=action, prices={"AAPL": Decimal("180")})
    assert report.status is ExecutionStatus.SKIPPED
    assert "live orders disabled" in (report.reason or "")


# --- T-LBA-7: execute pre-flights paper before live ----------------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
@pytest.mark.asyncio
async def test_execute_skips_when_paper_pre_flight_rejects() -> None:
    """NFR-LIVE-BROKER-3 dry-run parity: even with
    ``enable_live_orders=True`` and the kill switch disengaged, if
    PaperBroker.execute() returns non-FILLED, the live broker MUST
    short-circuit (no venue submission) and emit a SKIPPED report
    whose reason names the paper-pre-flight failure.

    Trigger: omit the ticker's price from ``prices`` so paper rejects
    with ``REJECTED`` status (missing price)."""
    broker = _make_broker(enable_live_orders=True)
    action = FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        violations=(),
        source_decision_run_id=new_run_id(),
    )
    report = await broker.execute(action=action, prices={})  # no price -> paper rejects
    assert report.status is ExecutionStatus.SKIPPED
    assert "paper pre-flight" in (report.reason or "").lower()


# --- T-LBA-8: cap breach engages kill-switch -----------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
def test_realized_loss_cap_breach_engages_kill_switch() -> None:
    """NFR-LIVE-BROKER-6: when the realized-loss accumulator exceeds
    ``live_broker_daily_loss_cap_usd``, the kill switch MUST be
    engaged automatically."""
    broker = _make_broker(cap_usd=Decimal("100"))
    assert broker.kill_switch_engaged is False
    broker.record_realized_loss(amount_usd=Decimal("50"))
    assert broker.kill_switch_engaged is False  # below cap
    broker.record_realized_loss(amount_usd=Decimal("60"))  # now total = 110 > 100
    assert broker.kill_switch_engaged is True
    assert broker.realized_loss_today_usd == Decimal("110")  # type: ignore[unreachable]


# --- T-LBA-9: reset_day clears the accumulator ---------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
def test_reset_day_clears_realized_loss_accumulator() -> None:
    """``reset_day()`` MUST zero the accumulator. The kill-switch
    state, however, MUST NOT be reset by ``reset_day()`` — re-enable
    requires the explicit human-approval workflow per ADR-0008
    NFR-LIVE-BROKER-1 / -5."""
    broker = _make_broker(cap_usd=Decimal("1000"))
    broker.record_realized_loss(amount_usd=Decimal("250"))
    assert broker.realized_loss_today_usd == Decimal("250")
    broker.reset_day()
    assert broker.realized_loss_today_usd == Decimal(0)
    # Independence: kill_switch_engaged is unaffected by reset_day
    broker.kill_switch()
    assert broker.kill_switch_engaged is True
    broker.reset_day()
    assert broker.kill_switch_engaged is True


# --- T-LBA-10: re-enable disengages kill-switch --------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — P4")
def test_reenable_after_human_approval_disengages_kill_switch() -> None:
    """``reenable_after_human_approval()`` MUST clear the kill-switch
    flag. Per ADR-0008 NFR-LIVE-BROKER-5 the caller is responsible
    for the two-step approval workflow; the broker exposes only the
    state-flip primitive."""
    broker = _make_broker()
    broker.kill_switch()
    assert broker.kill_switch_engaged is True
    broker.reenable_after_human_approval()
    assert broker.kill_switch_engaged is False
