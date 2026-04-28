"""Order execution layer.

Sits between the Policy Gateway (P3.a-c) and any venue. Phase ownership:

- **P3.d-1 (this slice)** — Paper broker skeleton: ``BrokerProtocol``,
  ``ExecutionReport`` schema, in-memory mid-price full-fill model.
  All-or-nothing fills (mirrors Gateway's demote-whole).
- **P3.d-2 (next)** — Rebalance + realized-PnL tracking; feeds the
  loss-budget input back to ``PolicyGatewayConfig.daily_realized_loss_usd``.
- **P4** — Live broker adapter implementing the same ``BrokerProtocol``.
"""

from caqrs.execution.execution_report import (
    ExecutionReport,
    ExecutionStatus,
    Fill,
    FillStatus,
)
from caqrs.execution.paper_broker import PaperBroker, Position
from caqrs.execution.protocol import BrokerProtocol

__all__ = [
    "BrokerProtocol",
    "ExecutionReport",
    "ExecutionStatus",
    "Fill",
    "FillStatus",
    "PaperBroker",
    "Position",
]
