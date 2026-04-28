"""FastAPI application factory + module-level app instance.

This slice ships only **introspection** endpoints — the schemas and a
trivial healthcheck. Subsequent slices (P3.d-3, P3.d-4) layer real
projection endpoints on top of the same `build_app` factory so the
OpenAPI document grows organically as the typed pipeline gains
HTTP-callable shape.

The factory pattern (`build_app()` returns a fresh `FastAPI` instance)
is preferred over a top-level mutable app for testability: every
test can spin up an isolated app without inheriting state from a
prior run. The module-level `app` is convenient for `uvicorn
caqrs.api.app:app --reload`.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from caqrs.execution.execution_report import ExecutionReport
from caqrs.policy.gateway import (
    FeasibleAction,
    PolicyGatewayConfig,
    PolicyViolation,
)
from caqrs.schemas.decision import StrategyDecision

API_TITLE: Final[str] = "CAQRS API"
API_VERSION: Final[str] = "0.0.1"
API_DESCRIPTION: Final[str] = (
    "Constrained Agentic Quant Research System — HTTP surface over the "
    "existing typed pipeline. Schemas are auto-generated from the "
    "Pydantic models the cycle runner already enforces."
)


class HealthResponse(BaseModel):
    """Minimal liveness signal returned by ``GET /healthz``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str


def build_app() -> FastAPI:
    """Construct a fresh FastAPI app instance.

    Tests use this to spin up isolated apps; production deployments
    use the module-level ``app`` symbol below (built once at import
    time).
    """
    new_app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        # Default OpenAPI / docs / redoc paths; surfaced explicitly so
        # tests don't drift if FastAPI changes its defaults.
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    @new_app.get(
        "/healthz",
        response_model=HealthResponse,
        tags=["meta"],
        summary="Liveness check",
    )
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    # === Schema-introspection endpoints ===
    # These exist solely so the schemas register as OpenAPI components
    # before any business endpoint is added. They are not load-bearing
    # API contracts; downstream slices replace them with real projection
    # endpoints that consume / produce these schemas.

    @new_app.get(
        "/v1/schemas/strategy-decision",
        response_model=StrategyDecision,
        tags=["schemas"],
        summary="StrategyDecision schema preview",
        description=(
            "Returns a 422 — this endpoint exists to register the "
            "schema in the OpenAPI components, not to serve data. "
            "Swagger UI will render the full schema in the response "
            "section."
        ),
        status_code=501,
    )
    def _strategy_decision_preview() -> StrategyDecision:
        # Never executed; FastAPI's response_model is what registers
        # the schema with the OpenAPI generator.
        raise NotImplementedError

    @new_app.get(
        "/v1/schemas/feasible-action",
        response_model=FeasibleAction,
        tags=["schemas"],
        summary="FeasibleAction schema preview",
        status_code=501,
    )
    def _feasible_action_preview() -> FeasibleAction:
        raise NotImplementedError

    @new_app.get(
        "/v1/schemas/execution-report",
        response_model=ExecutionReport,
        tags=["schemas"],
        summary="ExecutionReport schema preview",
        status_code=501,
    )
    def _execution_report_preview() -> ExecutionReport:
        raise NotImplementedError

    @new_app.get(
        "/v1/schemas/policy-gateway-config",
        response_model=PolicyGatewayConfig,
        tags=["schemas"],
        summary="PolicyGatewayConfig schema preview",
        status_code=501,
    )
    def _policy_gateway_config_preview() -> PolicyGatewayConfig:
        raise NotImplementedError

    @new_app.get(
        "/v1/schemas/policy-violation",
        response_model=PolicyViolation,
        tags=["schemas"],
        summary="PolicyViolation schema preview",
        status_code=501,
    )
    def _policy_violation_preview() -> PolicyViolation:
        raise NotImplementedError

    return new_app


app = build_app()
