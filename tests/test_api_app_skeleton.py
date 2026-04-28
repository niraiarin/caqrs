"""API skeleton — verifies the FastAPI app boots and exposes a
well-formed OpenAPI document with the core CAQRS schemas registered as
components.

The API surface itself is intentionally tiny in this slice: the goal is
to lock in the auto-documentation pipeline (Pydantic v2 → FastAPI →
OpenAPI 3) so downstream slices that add real endpoints inherit the
spec generation for free.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from caqrs.api.app import build_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


# === Boot + introspection ===


def test_openapi_endpoint_returns_valid_spec(client: TestClient) -> None:
    """`GET /openapi.json` returns a parseable OpenAPI 3.x document."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "CAQRS API"
    assert "paths" in spec
    assert "components" in spec


def test_swagger_ui_is_served(client: TestClient) -> None:
    """`/docs` (Swagger UI) is mounted by default."""
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert b"swagger-ui" in resp.content.lower()


def test_redoc_is_served(client: TestClient) -> None:
    """`/redoc` is mounted by default — alternative renderer."""
    resp = client.get("/redoc")
    assert resp.status_code == 200
    assert b"redoc" in resp.content.lower()


# === Schema visibility in OpenAPI components ===


def test_core_schemas_appear_in_openapi_components(client: TestClient) -> None:
    """The CAQRS Pydantic models that consumers will care about must be
    referenceable from the OpenAPI document. We don't enforce a
    specific endpoint shape here — only that the schemas are wired in."""
    spec = client.get("/openapi.json").json()
    component_names = set(spec["components"]["schemas"].keys())
    # Every schema worth API-documenting in this slice.
    expected = {
        "StrategyDecision",
        "FeasibleAction",
        "ExecutionReport",
        "PolicyGatewayConfig",
        "PolicyViolation",
    }
    missing = expected - component_names
    assert missing == set(), f"missing schemas: {sorted(missing)}"


def test_health_endpoint_returns_ok(client: TestClient) -> None:
    """A trivial health endpoint serves as the smoke check; consumers
    can poll it to verify the app is up without exercising business
    logic."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
