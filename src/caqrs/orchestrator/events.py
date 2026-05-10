"""Cycle event types and constructors.

Every meaningful step of a research cycle emits a typed
:class:`CycleEvent`. The orchestrator consumes events for visibility
(structured logging, debugging, regret analysis, post-hoc audit) and
the state machine, loop detector, and budget guards all surface their
decisions through events.

The event shape is deliberately denormalised: ``payload`` is a
free-form dict so adding a new event kind never requires extending
existing payload schemas. Callers should use the typed builders below
(``cycle_started_event``, ``agent_succeeded_event``, …) rather than
constructing :class:`CycleEvent` directly so the payload key set
stays consistent.
"""

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

_CYCLE_ID_BYTES: Final[int] = 8


class CycleEventKind(StrEnum):
    """Closed enum of orchestrator event kinds."""

    CYCLE_STARTED = "cycle_started"
    CYCLE_COMPLETED = "cycle_completed"
    CYCLE_ABORTED = "cycle_aborted"
    AGENT_INVOKED = "agent_invoked"
    AGENT_SUCCEEDED = "agent_succeeded"
    AGENT_FAILED = "agent_failed"
    STATE_TRANSITION = "state_transition"
    LOOP_DETECTED = "loop_detected"
    BUDGET_EXCEEDED = "budget_exceeded"
    POLICY_GATEWAY_APPLIED = "policy_gateway_applied"
    BROKER_EXECUTED = "broker_executed"
    BROKER_LIVE_SUBMITTED = "broker_live_submitted"
    BROKER_LIVE_FILLED = "broker_live_filled"
    BROKER_LIVE_REJECTED = "broker_live_rejected"
    BROKER_LIVE_CANCELLED = "broker_live_cancelled"
    BROKER_LIVE_KILL_SWITCH = "broker_live_kill_switch"
    IDENTIFIER_RESOLVED = "identifier_resolved"


class CycleEvent(BaseModel):
    """One typed event in a cycle's event log."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    event_id: str = Field(min_length=1, max_length=32)
    cycle_id: str = Field(min_length=1, max_length=32)
    kind: CycleEventKind
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


def new_cycle_id() -> str:
    """Generate a fresh 16-char hex cycle id (64 bits of entropy)."""
    return secrets.token_hex(_CYCLE_ID_BYTES)


def new_event_id() -> str:
    """Generate a fresh 16-char hex event id."""
    return secrets.token_hex(_CYCLE_ID_BYTES)


def _build_event(
    *,
    cycle_id: str,
    kind: CycleEventKind,
    payload: dict[str, Any] | None = None,
) -> CycleEvent:
    return CycleEvent(
        event_id=new_event_id(),
        cycle_id=cycle_id,
        kind=kind,
        timestamp=datetime.now(UTC),
        payload=payload or {},
    )


# === Typed constructors ===


def cycle_started_event(*, cycle_id: str, observer_input_run_id: str | None = None) -> CycleEvent:
    payload: dict[str, Any] = {}
    if observer_input_run_id is not None:
        payload["observer_input_run_id"] = observer_input_run_id
    return _build_event(cycle_id=cycle_id, kind=CycleEventKind.CYCLE_STARTED, payload=payload)


def cycle_completed_event(
    *,
    cycle_id: str,
    terminal_state: str,
    artifacts_emitted: int,
    total_token_in: int = 0,
    total_token_out: int = 0,
) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.CYCLE_COMPLETED,
        payload={
            "terminal_state": terminal_state,
            "artifacts_emitted": artifacts_emitted,
            "total_token_in": total_token_in,
            "total_token_out": total_token_out,
        },
    )


def cycle_aborted_event(*, cycle_id: str, reason: str, at_state: str) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.CYCLE_ABORTED,
        payload={"reason": reason, "at_state": at_state},
    )


def agent_invoked_event(*, cycle_id: str, agent_name: str, run_id: str) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.AGENT_INVOKED,
        payload={"agent_name": agent_name, "run_id": run_id},
    )


def agent_succeeded_event(
    *,
    cycle_id: str,
    agent_name: str,
    run_id: str,
    output_schema: str,
    token_in: int,
    token_out: int,
    latency_ms: int,
) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.AGENT_SUCCEEDED,
        payload={
            "agent_name": agent_name,
            "run_id": run_id,
            "output_schema": output_schema,
            "token_in": token_in,
            "token_out": token_out,
            "latency_ms": latency_ms,
        },
    )


def agent_failed_event(
    *,
    cycle_id: str,
    agent_name: str,
    run_id: str,
    error: str,
) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.AGENT_FAILED,
        payload={
            "agent_name": agent_name,
            "run_id": run_id,
            "error": error,
        },
    )


def state_transition_event(*, cycle_id: str, src: str, dst: str) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.STATE_TRANSITION,
        payload={"src": src, "dst": dst},
    )


def loop_detected_event(
    *,
    cycle_id: str,
    rule: str,
    tool: str,
    count: int,
    message: str,
) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.LOOP_DETECTED,
        payload={
            "rule": rule,
            "tool": tool,
            "count": count,
            "message": message,
        },
    )


def budget_exceeded_event(
    *,
    cycle_id: str,
    budget_kind: str,
    consumed: int,
    cap: int,
) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.BUDGET_EXCEEDED,
        payload={
            "budget_kind": budget_kind,
            "consumed": consumed,
            "cap": cap,
        },
    )


def policy_gateway_applied_event(
    *,
    cycle_id: str,
    decision_run_id: str,
    action: str,
    violation_count: int,
) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.POLICY_GATEWAY_APPLIED,
        payload={
            "decision_run_id": decision_run_id,
            "action": action,
            "violation_count": violation_count,
        },
    )


def broker_executed_event(
    *,
    cycle_id: str,
    decision_run_id: str,
    status: str,
    fill_count: int,
    reason: str | None,
) -> CycleEvent:
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.BROKER_EXECUTED,
        payload={
            "decision_run_id": decision_run_id,
            "status": status,
            "fill_count": fill_count,
            "reason": reason,
        },
    )


def identifier_resolved_event(
    *,
    cycle_id: str,
    input_ticker: str,
    canonical_issuer_id: str | None,
    matched_kind: str | None,
) -> CycleEvent:
    """One identifier-resolution event from the Phase E4 EntityResolver.

    ``canonical_issuer_id`` and ``matched_kind`` are both ``None`` when
    no :class:`~caqrs.entities.types.Identifier` in the resolver's
    backing store matched the input ticker. Downstream consumers
    (audit, regret analysis) treat a null ``canonical_issuer_id`` as
    "the operator passed an identifier the entity layer doesn't yet
    know about" — distinct from "the operator passed a typo".
    """
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.IDENTIFIER_RESOLVED,
        payload={
            "input_ticker": input_ticker,
            "canonical_issuer_id": canonical_issuer_id,
            "matched_kind": matched_kind,
        },
    )


def broker_live_submitted_event(
    *,
    cycle_id: str,
    decision_run_id: str,
    order_id: str,
    client_order_id: str,
    idempotency_key: str,
    symbol: str,
    qty: str,
    side: str,
) -> CycleEvent:
    """ADR-0008 NFR-LIVE-BROKER-7: live broker emits this on every
    successful Alpaca order submission. ``order_id`` is the
    venue-assigned UUID; ``idempotency_key`` is the full 64-char
    sha256 digest (per ADR-0008 §NFR-LIVE-BROKER-4) so replay
    disambiguation is recoverable from the event log alone, even
    after the venue's ``client_order_id`` (48-char truncation per
    ADR-0009) has been forgotten."""
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.BROKER_LIVE_SUBMITTED,
        payload={
            "decision_run_id": decision_run_id,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "idempotency_key": idempotency_key,
            "symbol": symbol,
            "qty": qty,
            "side": side,
        },
    )


def broker_live_rejected_event(
    *,
    cycle_id: str,
    decision_run_id: str,
    reason: str,
    idempotency_key: str | None = None,
) -> CycleEvent:
    """ADR-0008 NFR-LIVE-BROKER-7: live broker emits this on every
    short-circuit path (kill-switch engaged / live orders disabled /
    paper pre-flight rejected). NEVER ``BROKER_EXECUTED`` (paper-only).
    """
    payload: dict[str, Any] = {
        "decision_run_id": decision_run_id,
        "reason": reason,
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.BROKER_LIVE_REJECTED,
        payload=payload,
    )


def broker_live_filled_event(
    *,
    cycle_id: str,
    decision_run_id: str,
    order_id: str,
    client_order_id: str,
    symbol: str,
    side: str,
    filled_qty: str,
    filled_avg_price_usd: str,
    is_partial: bool,
) -> CycleEvent:
    """ADR-0008 NFR-LIVE-BROKER-7: live broker emits this when the
    venue confirms a fill (full or partial). ``is_partial`` is the
    boolean reflecting Alpaca's ``event=partial_fill`` distinction;
    downstream consumers can deduplicate fills against
    ``order_id + filled_qty`` since at-least-once webhook delivery
    means the same fill may arrive twice."""
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.BROKER_LIVE_FILLED,
        payload={
            "decision_run_id": decision_run_id,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "filled_qty": filled_qty,
            "filled_avg_price_usd": filled_avg_price_usd,
            "is_partial": is_partial,
        },
    )


def broker_live_cancelled_event(
    *,
    cycle_id: str,
    decision_run_id: str,
    order_id: str,
    client_order_id: str,
    symbol: str,
    side: str,
    reason: str | None = None,
) -> CycleEvent:
    """ADR-0008 NFR-LIVE-BROKER-7: live broker emits this when the
    venue cancels an order (operator-initiated, kill-switch-initiated,
    or venue-side rejection after acceptance). ``reason`` is the
    venue's stated cause verbatim when available."""
    payload: dict[str, Any] = {
        "decision_run_id": decision_run_id,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
    }
    if reason is not None:
        payload["reason"] = reason
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.BROKER_LIVE_CANCELLED,
        payload=payload,
    )


def broker_live_kill_switch_event(
    *,
    cycle_id: str,
    reason: str,
) -> CycleEvent:
    """Emitted when the kill switch engages — manual via
    :meth:`LiveBrokerAlpaca.kill_switch` or auto via
    NFR-LIVE-BROKER-6 cap breach. The ``reason`` distinguishes the
    two ('manual' / 'cap_breach') for post-hoc audit."""
    return _build_event(
        cycle_id=cycle_id,
        kind=CycleEventKind.BROKER_LIVE_KILL_SWITCH,
        payload={"reason": reason},
    )
