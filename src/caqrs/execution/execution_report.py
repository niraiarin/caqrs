"""Typed result of a broker execution attempt.

The :class:`ExecutionReport` is what flows back to the cycle log after
the broker processes a :class:`FeasibleAction`. Carries enough detail
to reconstruct what would have happened on a real venue: per-target
fill status, fill prices, notional, and a reason string for the
non-FILLED paths so audits can answer "why was this skipped/rejected"
without joining against the broker's internal state.

Design parity with the Gateway: the report is **frozen** + **extra=forbid**;
violation-equivalent context lives in plain ``Fill.reason`` strings to
keep JSON round-trip trivial.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from caqrs.schemas.common import StrictBaseModel, Ticker
from caqrs.schemas.decision import Side


class FillStatus(StrEnum):
    """Per-target fill outcome.

    ``FILLED``: synchronous broker (PaperBroker) — qty/price are the
    executed amounts. ``REJECTED``: the broker refused this leg.
    ``SUBMITTED``: live broker only — Alpaca accepted the order but
    actual fill price + qty come later via the trade-update websocket
    (``BROKER_LIVE_FILLED``). Numeric fields on a SUBMITTED Fill are
    *anticipated* (gateway-side snapshot prices), not executed.
    """

    FILLED = "filled"
    REJECTED = "rejected"
    SUBMITTED = "submitted"


class ExecutionStatus(StrEnum):
    """Top-level execution outcome.

    Synchronous brokers (PaperBroker) are all-or-nothing: ``FILLED``
    means every target filled, ``REJECTED`` means at least one couldn't
    fill AND broker state was rolled back to before the call.

    Live brokers add ``SUBMITTED``: every requested order was accepted
    by the venue but actual fills are pending the trade-update stream.
    On mid-batch venue rejection the broker MUST attempt to cancel the
    already-submitted orders and return ``REJECTED`` with empty fills
    (per Codex audit 2026-05-10 finding 1).

    ``SKIPPED`` means the action wasn't ADOPT so no broker work was
    attempted at all.
    """

    FILLED = "filled"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    SUBMITTED = "submitted"


class Fill(StrictBaseModel):
    """One per-target fill record.

    For ``status == FILLED``, ``quantity / fill_price_usd / notional_usd``
    are the executed amounts. For ``status == REJECTED``, those fields
    record what was *attempted* (zero quantity, zero notional) and
    ``reason`` carries the human-readable cause.
    """

    ticker: Ticker
    side: Side
    status: FillStatus
    quantity: Decimal = Field(ge=0)
    fill_price_usd: Decimal = Field(ge=0)
    notional_usd: Decimal = Field(ge=0)
    reason: str | None = Field(default=None, max_length=400)


class ExecutionReport(StrictBaseModel):
    """Top-level execution report for a single :class:`FeasibleAction`.

    Always carries ``source_decision_run_id`` so downstream cycle-log
    consumers can join the report to the originating decision without
    scanning.
    """

    source_decision_run_id: str
    status: ExecutionStatus
    fills: tuple[Fill, ...] = ()
    reason: str | None = Field(default=None, max_length=400)
