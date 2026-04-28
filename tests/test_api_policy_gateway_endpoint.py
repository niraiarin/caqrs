"""P3.d-3 — `/v1/policy-gateway/apply` endpoint.

Demonstrates the full gateway projection over HTTP: a caller (a
notional supervisor process) posts a `(decision, config)` pair and
gets back the projected `FeasibleAction`. Proves the auto-doc
pipeline survives end-to-end against a real business endpoint, not
just schema-preview placeholders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from caqrs.api.app import build_app
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.decision import (
    DecisionAction,
    Side,
    StrategyDecision,
    TargetPosition,
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


def _meta() -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name="decider",
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=0,
        token_in=0,
        token_out=0,
    )


def _adopt_decision_payload() -> dict[str, object]:
    """Build a JSON-serialisable ADOPT decision matching StrategyDecision."""
    decision = StrategyDecision(
        metadata=_meta(),
        backtest_run_id=new_run_id(),
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        rationale="test",
        notional_cap_usd=Decimal("1000000"),
        max_position_weight=Decimal("0.7"),
        daily_loss_limit_usd=Decimal("10000"),
    )
    return decision.model_dump(mode="json")


def test_apply_endpoint_passes_clean_decision_through(client: TestClient) -> None:
    """Empty config → identity projection → ADOPT echoed back."""
    body = {
        "decision": _adopt_decision_payload(),
        "config": {},
    }
    resp = client.post("/v1/policy-gateway/apply", json=body)
    assert resp.status_code == 200
    fa = resp.json()
    assert fa["action"] == "adopt"
    assert fa["violations"] == []
    assert len(fa["targets"]) == 1


def test_apply_endpoint_demotes_on_loss_budget_exhaustion(client: TestClient) -> None:
    """Caller-supplied realized loss equals the limit → DEFER + violation."""
    body = {
        "decision": _adopt_decision_payload(),
        "config": {"daily_realized_loss_usd": "10000"},
    }
    resp = client.post("/v1/policy-gateway/apply", json=body)
    assert resp.status_code == 200
    fa = resp.json()
    assert fa["action"] == "defer"
    assert fa["targets"] == []
    kinds = {v["kind"] for v in fa["violations"]}
    assert "loss_budget_exhausted" in kinds


def test_apply_endpoint_demotes_on_denied_ticker(client: TestClient) -> None:
    body = {
        "decision": _adopt_decision_payload(),
        "config": {"denied_tickers": ["AAPL"]},
    }
    resp = client.post("/v1/policy-gateway/apply", json=body)
    assert resp.status_code == 200
    fa = resp.json()
    assert fa["action"] == "defer"
    kinds = {v["kind"] for v in fa["violations"]}
    assert "ticker_deny_listed" in kinds


def test_apply_endpoint_validates_request_body(client: TestClient) -> None:
    """Missing required field → 422 from FastAPI's validator."""
    resp = client.post("/v1/policy-gateway/apply", json={"config": {}})
    assert resp.status_code == 422


def test_apply_endpoint_appears_in_openapi_paths(client: TestClient) -> None:
    """The endpoint registers in the OpenAPI document with both schemas
    referenced from the request and response."""
    spec = client.get("/openapi.json").json()
    assert "/v1/policy-gateway/apply" in spec["paths"]
    operation = spec["paths"]["/v1/policy-gateway/apply"]["post"]
    # Tagged so the Swagger UI groups it under "policy-gateway".
    assert "policy-gateway" in operation["tags"]
