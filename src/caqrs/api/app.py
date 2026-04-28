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

from typing import Annotated, Final

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError

from caqrs.execution.execution_report import ExecutionReport
from caqrs.policy.gateway import (
    FeasibleAction,
    PolicyGatewayConfig,
    PolicyViolation,
    apply_policy_gateway,
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


class ApplyPolicyGatewayRequest(BaseModel):
    """Body of ``POST /v1/policy-gateway/apply``.

    The caller (a supervisor process or CycleRunner) supplies a
    :class:`StrategyDecision` and the freshly-assembled
    :class:`PolicyGatewayConfig` for this projection. The
    ``daily_realized_loss_usd`` field of ``config`` is expected to come
    from a :class:`caqrs.policy.LossBudgetTracker` reading the active
    broker; this endpoint is stateless and does not run that tracker
    itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: StrategyDecision
    config: PolicyGatewayConfig


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

    # === Real projection endpoints ===

    async def _parse_apply_body(request: Request) -> ApplyPolicyGatewayRequest:
        # The internal Pydantic models (StrategyDecision,
        # PolicyGatewayConfig) use strict=True so the cycle pipeline
        # catches str-vs-Decimal bugs at LLM-output validation time.
        # JSON has no Decimal type, so the HTTP boundary must use
        # strict=False; the call-time override propagates to nested
        # validators per Pydantic v2 semantics.
        raw = await request.json()
        try:
            return ApplyPolicyGatewayRequest.model_validate(raw, strict=False)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc

    @new_app.post(
        "/v1/policy-gateway/apply",
        response_model=FeasibleAction,
        tags=["policy-gateway"],
        summary="Apply the Policy Gateway projection",
        description=(
            "Pure-function projection `Π : StrategyDecision → "
            "FeasibleAction` over caller-supplied account-level "
            "constraints. The endpoint is stateless: every request is "
            "evaluated against the supplied body alone, exactly "
            "matching the in-process `apply_policy_gateway` semantics. "
            "Per ADR-0005 §Decision 1, the projection is reproducible "
            "from the inputs alone — replaying the same body always "
            "returns the same FeasibleAction.\n\n"
            "Request body shape: "
            '`{"decision": StrategyDecision, "config": '
            "PolicyGatewayConfig}`. See the schema-preview endpoints "
            "under `/v1/schemas/` for field-level documentation."
        ),
    )
    def apply_gateway(
        body: Annotated[ApplyPolicyGatewayRequest, Depends(_parse_apply_body)],
    ) -> FeasibleAction:
        return apply_policy_gateway(decision=body.decision, config=body.config)

    return new_app


app = build_app()
