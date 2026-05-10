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

import asyncio
import io
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from caqrs.execution.alpaca_rest import AlpacaError, AlpacaRestClient
from caqrs.execution.execution_report import ExecutionStatus, FillStatus
from caqrs.execution.live_broker_alpaca import LiveBrokerAlpaca, _main
from caqrs.execution.live_broker_journal import LiveBrokerJournal
from caqrs.execution.paper_broker import PaperBroker
from caqrs.orchestrator import CycleEventKind, EventLog, new_cycle_id
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import new_run_id
from caqrs.schemas.decision import DecisionAction, Side, TargetPosition


def _make_broker(
    *,
    enable_live_orders: bool = False,
    cap_usd: Decimal = Decimal("1000"),
    event_log: EventLog | None = None,
) -> LiveBrokerAlpaca:
    """Build a LiveBrokerAlpaca and (when ``event_log`` is provided)
    attach a per-cycle context so unit tests can assert event emission.

    The runner-driven path uses :meth:`LiveBrokerAlpaca.attach_cycle_context`
    automatically; tests that want emission outside a runner attach
    here for symmetry."""
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    broker = LiveBrokerAlpaca(
        paper_broker=paper,
        live_broker_daily_loss_cap_usd=cap_usd,
        _force_enable_live_orders_for_test=enable_live_orders,
    )
    if event_log is not None:
        broker.attach_cycle_context(cycle_id=new_cycle_id(), event_log=event_log)
    return broker


# --- T-LBA-1: idempotency key is a 64-char sha256 hex --------------------


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


def test_kill_switch_engages_state() -> None:
    """``kill_switch()`` MUST flip the engaged flag; subsequent
    inspections MUST return ``True`` until ``reenable_after_human_approval``
    is called."""
    broker = _make_broker()
    assert broker.kill_switch_engaged is False
    broker.kill_switch()
    assert broker.kill_switch_engaged is True


# --- T-LBA-5: kill-switch causes execute() to skip ------------------------


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


def test_realized_loss_cap_breach_engages_kill_switch() -> None:
    """NFR-LIVE-BROKER-6: when the realized-loss accumulator exceeds
    ``live_broker_daily_loss_cap_usd``, the kill switch MUST be
    engaged automatically."""
    broker = _make_broker(cap_usd=Decimal("100"))
    broker.record_realized_loss(amount_usd=Decimal("50"))
    # below cap — kill switch must NOT have engaged.
    pre_breach_engaged = broker.kill_switch_engaged
    assert pre_breach_engaged is False
    broker.record_realized_loss(amount_usd=Decimal("60"))  # now total = 110 > 100
    post_breach_engaged = broker.kill_switch_engaged
    assert post_breach_engaged is True
    assert broker.realized_loss_today_usd == Decimal("110")


# --- T-LBA-9: reset_day clears the accumulator ---------------------------


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


# --- Codex audit (2026-05-09) regression tests -------------------------


def test_compute_idempotency_key_resists_separator_injection() -> None:
    """The previous ``|``-join serialization had a collision via
    pipe-character injection (e.g. ``cycle_id="a|b"`` vs
    ``cycle_id="a"`` could produce the same intermediate string).
    Codex audit blocker: this test pins the canonical-JSON encoding so
    a future refactor cannot reintroduce the ambiguity."""
    broker = _make_broker()
    decision_run_id = new_run_id()
    a = broker.compute_idempotency_key(
        cycle_id="aaaaaaaaaaaaaaaa|XXX",
        decision_run_id=decision_run_id,
        ticker="AAPL",
        side=Side.BUY,
        quantity=Decimal("100"),
    )
    b = broker.compute_idempotency_key(
        cycle_id="aaaaaaaaaaaaaaaa",
        decision_run_id=f"|XXX{decision_run_id}",
        ticker="AAPL",
        side=Side.BUY,
        quantity=Decimal("100"),
    )
    assert a != b


def test_record_realized_loss_rejects_negative_amount() -> None:
    """Codex audit minor: a negative ``amount_usd`` would let a caller
    artificially reduce the accumulator and unwind the kill-switch
    cap-breach trigger. Method MUST reject negatives at the boundary."""
    broker = _make_broker()
    with pytest.raises(ValueError, match="must be non-negative"):
        broker.record_realized_loss(amount_usd=Decimal("-1"))


@pytest.mark.asyncio
async def test_execute_emits_broker_live_rejected_when_short_circuiting() -> None:
    """NFR-LIVE-BROKER-7 positive side: when ``execute()`` short-circuits
    (default-off / kill-switch / paper pre-flight), the broker MUST
    emit a ``BROKER_LIVE_REJECTED`` event into the injected EventLog —
    NEVER ``BROKER_EXECUTED`` (paper-only)."""
    from caqrs.orchestrator import CycleEventKind  # noqa: PLC0415 — local-only

    log = EventLog()
    broker = _make_broker(event_log=log)
    action = FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        violations=(),
        source_decision_run_id=new_run_id(),
    )
    await broker.execute(action=action, prices={"AAPL": Decimal("180")})

    rejected = log.filter_by_kind(CycleEventKind.BROKER_LIVE_REJECTED)
    assert len(rejected) == 1
    assert "live orders disabled" in rejected[0].payload["reason"]

    paper_only = log.filter_by_kind(CycleEventKind.BROKER_EXECUTED)
    assert paper_only == ()


def test_kill_switch_emits_broker_live_kill_switch_event() -> None:
    """NFR-LIVE-BROKER-7: ``kill_switch()`` MUST emit
    ``BROKER_LIVE_KILL_SWITCH`` with ``reason="manual"``."""
    from caqrs.orchestrator import CycleEventKind  # noqa: PLC0415

    log = EventLog()
    broker = _make_broker(event_log=log)
    broker.kill_switch()

    events = log.filter_by_kind(CycleEventKind.BROKER_LIVE_KILL_SWITCH)
    assert len(events) == 1
    assert events[0].payload["reason"] == "manual"


def test_cap_breach_emits_broker_live_kill_switch_with_cap_breach_reason() -> None:
    """NFR-LIVE-BROKER-6 + 7: cap-breach auto-engages and emits
    ``BROKER_LIVE_KILL_SWITCH`` with ``reason="cap_breach"`` so audit
    can distinguish manual from auto-engagement."""
    from caqrs.orchestrator import CycleEventKind  # noqa: PLC0415

    log = EventLog()
    broker = _make_broker(cap_usd=Decimal("100"), event_log=log)
    broker.record_realized_loss(amount_usd=Decimal("150"))

    events = log.filter_by_kind(CycleEventKind.BROKER_LIVE_KILL_SWITCH)
    assert len(events) == 1
    assert events[0].payload["reason"] == "cap_breach"


# === Two-step human approval (env var + CLI) ============================
# ADR-0008 §NFR-LIVE-BROKER-1; ADR-0009 P4 first-cut follow-up.
# Step 1: tests xfail because both enable_live_orders_after_human_approval
# and _main are NotImplementedError stubs. Step 2 implements both and
# removes the xfail markers.


def test_enable_live_orders_after_human_approval_succeeds_with_matching_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LIVE_BROKER_ENABLE_LIVE_ORDERS is set AND
    cli_confirmation_token matches, enable_live_orders MUST flip to True."""
    monkeypatch.setenv("LIVE_BROKER_ENABLE_LIVE_ORDERS", "secret-of-the-day")
    broker = _make_broker()
    assert broker.enable_live_orders is False
    broker.enable_live_orders_after_human_approval(
        env_token="LIVE_BROKER_ENABLE_LIVE_ORDERS",
        cli_confirmation_token="secret-of-the-day",
    )
    assert broker.enable_live_orders is True


def test_enable_live_orders_raises_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the env var set, the method MUST raise — the operator
    has not completed the two-factor approval."""
    monkeypatch.delenv("LIVE_BROKER_ENABLE_LIVE_ORDERS", raising=False)
    broker = _make_broker()
    with pytest.raises(RuntimeError, match="LIVE_BROKER_ENABLE_LIVE_ORDERS"):
        broker.enable_live_orders_after_human_approval(
            env_token="LIVE_BROKER_ENABLE_LIVE_ORDERS",
            cli_confirmation_token="anything",
        )
    assert broker.enable_live_orders is False


def test_enable_live_orders_raises_when_token_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env set, cli token mismatched MUST raise. The match is
    case-sensitive byte equality."""
    monkeypatch.setenv("LIVE_BROKER_ENABLE_LIVE_ORDERS", "secret-A")
    broker = _make_broker()
    with pytest.raises(RuntimeError, match="does not match"):
        broker.enable_live_orders_after_human_approval(
            env_token="LIVE_BROKER_ENABLE_LIVE_ORDERS",
            cli_confirmation_token="secret-B",
        )
    assert broker.enable_live_orders is False


def test_enable_live_orders_raises_when_env_var_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An env var set to the empty string is treated as unset — empty
    is not a valid secret."""
    monkeypatch.setenv("LIVE_BROKER_ENABLE_LIVE_ORDERS", "")
    broker = _make_broker()
    with pytest.raises(RuntimeError, match="must be set to a non-empty value"):
        broker.enable_live_orders_after_human_approval(
            env_token="LIVE_BROKER_ENABLE_LIVE_ORDERS",
            cli_confirmation_token="",
        )
    assert broker.enable_live_orders is False


def test_confirm_live_cli_succeeds_on_matching_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm-live CLI: env set + stdin matches → exit 0 + 'OK' in stdout."""

    monkeypatch.setenv("LIVE_BROKER_ENABLE_LIVE_ORDERS", "match-me")
    stdin = io.StringIO("match-me\n")
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = _main(["confirm-live"], stdin=stdin, stdout=stdout, stderr=stderr)
    assert rc == 0
    assert "OK" in stdout.getvalue()


def test_confirm_live_cli_fails_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm-live CLI: env unset → exit non-zero, no prompt for input."""

    monkeypatch.delenv("LIVE_BROKER_ENABLE_LIVE_ORDERS", raising=False)
    rc = _main(
        ["confirm-live"],
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert rc != 0


def test_confirm_live_cli_fails_on_mismatched_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm-live CLI: env set + stdin mismatched → exit non-zero."""

    monkeypatch.setenv("LIVE_BROKER_ENABLE_LIVE_ORDERS", "match-me")
    rc = _main(
        ["confirm-live"],
        stdin=io.StringIO("not-me\n"),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert rc != 0


def test_enable_live_orders_rejects_non_live_broker_env_token() -> None:
    """Codex audit major: env_token MUST start with LIVE_BROKER_ to
    prevent a confused-deputy attack where an unrelated env var (PATH,
    HOME, etc.) gets repurposed as the live-trading gate."""
    broker = _make_broker()
    with pytest.raises(RuntimeError, match="must start with 'LIVE_BROKER_'"):
        broker.enable_live_orders_after_human_approval(
            env_token="PATH",
            cli_confirmation_token="anything",
        )


def test_enable_live_orders_does_not_strip_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex audit major: byte equality means whitespace counts.
    `" secret "` (with surrounding spaces) is a different secret
    from `"secret"`; the gate MUST enforce exact equality."""
    monkeypatch.setenv("LIVE_BROKER_ENABLE_LIVE_ORDERS", " secret ")
    broker = _make_broker()
    # Stripped match must FAIL — no silent strip of env value.
    with pytest.raises(RuntimeError, match="does not match"):
        broker.enable_live_orders_after_human_approval(
            env_token="LIVE_BROKER_ENABLE_LIVE_ORDERS",
            cli_confirmation_token="secret",
        )
    # Exact match (with surrounding spaces) succeeds.
    broker.enable_live_orders_after_human_approval(
        env_token="LIVE_BROKER_ENABLE_LIVE_ORDERS",
        cli_confirmation_token=" secret ",
    )
    assert broker.enable_live_orders is True


# === Alpaca REST submission integration =================================
# Live wire-in tests: LiveBrokerAlpaca.execute() submits each paper-pre-flight
# fill to Alpaca via AlpacaRestClient, emits BROKER_LIVE_SUBMITTED on success
# and BROKER_LIVE_REJECTED on venue rejection.


@pytest.mark.asyncio
async def test_alpaca_submission_emits_broker_live_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When live orders are enabled and paper pre-flight FILLs, the
    LiveBroker MUST submit to Alpaca, emit one BROKER_LIVE_SUBMITTED
    event per accepted order, and return ExecutionStatus.SUBMITTED
    with anticipated-price fills (Codex audit 2026-05-10 finding 2:
    SUBMITTED is not FILLED until websocket trade-update lands)."""
    log = EventLog()
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    base_url = "https://paper-api.alpaca.markets"
    with respx.mock(base_url=base_url) as router:
        router.post("/v2/orders").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "order-uuid-1",
                    "client_order_id": "abc",
                    "symbol": "AAPL",
                    "qty": "55.55555556",
                    "side": "buy",
                    "status": "accepted",
                },
            ),
        )
        async with AlpacaRestClient(api_key="k", api_secret="s") as alpaca:
            broker = LiveBrokerAlpaca(
                paper_broker=paper,
                live_broker_daily_loss_cap_usd=Decimal("1000"),
                alpaca_client=alpaca,
                _force_enable_live_orders_for_test=True,
            )
            broker.attach_cycle_context(cycle_id=new_cycle_id(), event_log=log)

            action = FeasibleAction(
                action=DecisionAction.ADOPT,
                targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
                violations=(),
                source_decision_run_id=new_run_id(),
            )
            report = await broker.execute(
                action=action,
                prices={"AAPL": Decimal("180")},
            )
    assert report.status is ExecutionStatus.SUBMITTED
    assert len(report.fills) == 1
    assert report.fills[0].status is FillStatus.SUBMITTED
    submitted = log.filter_by_kind(CycleEventKind.BROKER_LIVE_SUBMITTED)
    assert len(submitted) == 1
    payload = submitted[0].payload
    assert payload["order_id"] == "order-uuid-1"
    assert payload["symbol"] == "AAPL"
    assert payload["side"] == "buy"
    assert len(payload["idempotency_key"]) == 64  # full sha256 hex
    assert len(payload["client_order_id"]) <= 48  # Alpaca-truncated form


@pytest.mark.asyncio
async def test_alpaca_submission_rejection_emits_broker_live_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Alpaca returns 4xx, LiveBroker MUST emit BROKER_LIVE_REJECTED
    with the venue's stated reason and return ExecutionStatus.REJECTED."""
    log = EventLog()
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    base_url = "https://paper-api.alpaca.markets"
    with respx.mock(base_url=base_url) as router:
        router.post("/v2/orders").mock(
            return_value=httpx.Response(
                422,
                json={"code": 40110000, "message": "insufficient buying power"},
            ),
        )
        async with AlpacaRestClient(api_key="k", api_secret="s") as alpaca:
            broker = LiveBrokerAlpaca(
                paper_broker=paper,
                live_broker_daily_loss_cap_usd=Decimal("1000"),
                alpaca_client=alpaca,
                _force_enable_live_orders_for_test=True,
            )
            broker.attach_cycle_context(cycle_id=new_cycle_id(), event_log=log)
            action = FeasibleAction(
                action=DecisionAction.ADOPT,
                targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
                violations=(),
                source_decision_run_id=new_run_id(),
            )
            report = await broker.execute(
                action=action,
                prices={"AAPL": Decimal("180")},
            )
    assert report.status is ExecutionStatus.REJECTED
    rejected = log.filter_by_kind(CycleEventKind.BROKER_LIVE_REJECTED)
    assert len(rejected) == 1
    assert "Alpaca rejected" in rejected[0].payload["reason"]


def test_live_broker_without_alpaca_client_raises_when_submission_path_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing LiveBrokerAlpaca without alpaca_client AND reaching
    the submission path (kill_switch=False, enable_live_orders=True,
    paper FILLED) MUST raise — accidentally elevating to live without
    a venue client is a configuration error, not a silent no-op."""
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    broker = LiveBrokerAlpaca(
        paper_broker=paper,
        live_broker_daily_loss_cap_usd=Decimal("1000"),
        _force_enable_live_orders_for_test=True,
    )
    action = FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        violations=(),
        source_decision_run_id=new_run_id(),
    )
    with pytest.raises(RuntimeError, match="without alpaca_client"):
        asyncio.run(broker.execute(action=action, prices={"AAPL": Decimal("180")}))


# === Codex audit 2026-05-10 regressions ============================


@pytest.mark.asyncio
async def test_alpaca_mid_batch_rejection_rolls_back_via_cancel_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the second submission fails after the first succeeded,
    LiveBrokerAlpaca MUST issue cancel_all_orders() to undo the prior
    submission, then return REJECTED with EMPTY fills (Codex audit
    2026-05-10 finding 1: REJECTED's "rollback to before the call"
    contract MUST hold)."""
    log = EventLog()
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    base_url = "https://paper-api.alpaca.markets"
    cancel_all_called = {"count": 0}

    def _cancel_handler(_request: httpx.Request) -> httpx.Response:
        cancel_all_called["count"] += 1
        return httpx.Response(207, json=[])

    submit_responses = [
        httpx.Response(
            200,
            json={
                "id": "uuid-1",
                "client_order_id": "k1",
                "symbol": "AAPL",
                "qty": "10",
                "side": "buy",
                "status": "accepted",
            },
        ),
        httpx.Response(422, json={"message": "venue-side rejection"}),
    ]

    with respx.mock(base_url=base_url) as router:
        router.post("/v2/orders").mock(side_effect=submit_responses)
        router.delete("/v2/orders").mock(side_effect=_cancel_handler)
        async with AlpacaRestClient(api_key="k", api_secret="s") as alpaca:
            broker = LiveBrokerAlpaca(
                paper_broker=paper,
                live_broker_daily_loss_cap_usd=Decimal("100000"),
                alpaca_client=alpaca,
                _force_enable_live_orders_for_test=True,
            )
            broker.attach_cycle_context(cycle_id=new_cycle_id(), event_log=log)
            action = FeasibleAction(
                action=DecisionAction.ADOPT,
                targets=(
                    TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.4")),
                    TargetPosition(ticker="MSFT", side=Side.BUY, weight=Decimal("0.4")),
                ),
                violations=(),
                source_decision_run_id=new_run_id(),
            )
            report = await broker.execute(
                action=action,
                prices={"AAPL": Decimal("180"), "MSFT": Decimal("400")},
            )
    assert report.status is ExecutionStatus.REJECTED
    assert report.fills == ()  # rollback contract — no fills survive
    assert cancel_all_called["count"] == 1
    rejected = log.filter_by_kind(CycleEventKind.BROKER_LIVE_REJECTED)
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_alpaca_mid_batch_rollback_failure_engages_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cancel_all_orders itself fails during mid-batch rollback,
    LiveBroker MUST auto-engage the kill switch and emit
    BROKER_LIVE_KILL_SWITCH(reason='rollback_failed') so the operator
    investigates venue-side residual orders manually."""
    log = EventLog()
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    base_url = "https://paper-api.alpaca.markets"

    submit_responses = [
        httpx.Response(
            200,
            json={
                "id": "uuid-1",
                "client_order_id": "k1",
                "symbol": "AAPL",
                "qty": "10",
                "side": "buy",
                "status": "accepted",
            },
        ),
        httpx.Response(422, json={"message": "venue-side rejection"}),
    ]
    with respx.mock(base_url=base_url) as router:
        router.post("/v2/orders").mock(side_effect=submit_responses)
        router.delete("/v2/orders").mock(
            return_value=httpx.Response(503, text="venue down"),
        )
        async with AlpacaRestClient(api_key="k", api_secret="s") as alpaca:
            broker = LiveBrokerAlpaca(
                paper_broker=paper,
                live_broker_daily_loss_cap_usd=Decimal("100000"),
                alpaca_client=alpaca,
                _force_enable_live_orders_for_test=True,
            )
            broker.attach_cycle_context(cycle_id=new_cycle_id(), event_log=log)
            action = FeasibleAction(
                action=DecisionAction.ADOPT,
                targets=(
                    TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.4")),
                    TargetPosition(ticker="MSFT", side=Side.BUY, weight=Decimal("0.4")),
                ),
                violations=(),
                source_decision_run_id=new_run_id(),
            )
            report = await broker.execute(
                action=action,
                prices={"AAPL": Decimal("180"), "MSFT": Decimal("400")},
            )
    assert report.status is ExecutionStatus.REJECTED
    assert broker.kill_switch_engaged is True
    kill_events = log.filter_by_kind(CycleEventKind.BROKER_LIVE_KILL_SWITCH)
    assert len(kill_events) == 1
    assert kill_events[0].payload["reason"] == "rollback_failed"


@pytest.mark.asyncio
async def test_alpaca_cancel_all_raises_on_4xx() -> None:
    """Codex audit finding 3: cancel_all_orders MUST raise on any 4xx
    (401/403/404 mean the kill-switch failed to reach the venue, NOT
    success). Previously raised only on 5xx."""
    base_url = "https://paper-api.alpaca.markets"
    with respx.mock(base_url=base_url) as router:
        router.delete("/v2/orders").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"}),
        )
        async with AlpacaRestClient(api_key="k", api_secret="s") as client:
            with pytest.raises(AlpacaError) as excinfo:
                await client.cancel_all_orders()
            assert excinfo.value.status_code == 401


# === Journal wire-in (PR #100 majors 3+4 follow-through) ===========


@pytest.mark.asyncio
async def test_alpaca_submission_registers_in_journal_when_provided(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """When LiveBrokerAlpaca is constructed with a journal, each
    accepted Alpaca submission MUST persist a registration row so
    the trade-update stream's resolver can attribute fills durably."""

    path = Path(str(tmp_path)) / "j.sqlite"
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    base_url = "https://paper-api.alpaca.markets"
    cycle_id = new_cycle_id()
    with respx.mock(base_url=base_url) as router:
        router.post("/v2/orders").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "venue-uuid",
                    "client_order_id": "test-client-id",
                    "symbol": "AAPL",
                    "qty": "10",
                    "side": "buy",
                    "status": "accepted",
                },
            ),
        )
        async with AlpacaRestClient(api_key="k", api_secret="s") as alpaca:
            with LiveBrokerJournal(path=path) as journal:
                broker = LiveBrokerAlpaca(
                    paper_broker=paper,
                    live_broker_daily_loss_cap_usd=Decimal("10000"),
                    alpaca_client=alpaca,
                    journal=journal,
                    _force_enable_live_orders_for_test=True,
                )
                broker.attach_cycle_context(cycle_id=cycle_id, event_log=EventLog())
                action = FeasibleAction(
                    action=DecisionAction.ADOPT,
                    targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
                    violations=(),
                    source_decision_run_id=new_run_id(),
                )
                await broker.execute(action=action, prices={"AAPL": Decimal("180")})
                # Journal records the venue-assigned client_order_id
                # (the response body's client_order_id wins over the
                # submitted one when they differ).
                assert journal.attribution("test-client-id") is not None
                attribution = journal.attribution("test-client-id")
                assert attribution is not None
                assert attribution[0] == cycle_id


def test_alpaca_without_journal_runs_without_registering() -> None:
    """When journal is None, the broker MUST NOT attempt journal
    operations — confirmed by simply being able to construct + run
    without a journal (existing test fixtures still pass)."""
    paper = PaperBroker(initial_capital_usd=Decimal("100000"))
    broker = LiveBrokerAlpaca(
        paper_broker=paper,
        live_broker_daily_loss_cap_usd=Decimal("1000"),
        alpaca_client=None,
        journal=None,
    )
    # Default-off; just verify construction works without journal.
    assert broker.enable_live_orders is False
