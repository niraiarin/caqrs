"""Validation tests for caqrs.entities core types."""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from caqrs.entities import Identifier, IdentifierKind, Issuer, IssuerId, Provenance, Source


def test_issuer_id_accepts_canonical_16_hex() -> None:
    assert TypeAdapter(IssuerId).validate_python("I0123456789abcdef") == "I0123456789abcdef"


@pytest.mark.parametrize("value", ["0123456789abcdef", "I0123456789abcdeg", "I123"])
def test_issuer_id_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(IssuerId).validate_python(value)


def test_issuer_rejects_lowercase_lei() -> None:
    with pytest.raises(ValidationError):
        Issuer(
            id="I0123456789abcdef",
            lei="5493006z4dxp3jncay09",
            jcn=None,
            display_name="Toyota Motor Corporation",
            identifiers=(),
        )


def test_issuer_rejects_malformed_jcn() -> None:
    with pytest.raises(ValidationError):
        Issuer(
            id="I0123456789abcdef",
            lei=None,
            jcn="118030101877",
            display_name="Toyota Motor Corporation",
            identifiers=(),
        )


def test_pydantic_models_are_frozen() -> None:
    issuer = Issuer(
        id="I0123456789abcdef",
        lei=None,
        jcn=None,
        display_name="Toyota Motor Corporation",
        identifiers=(),
    )

    with pytest.raises(ValidationError):
        # setattr bypasses mypy's read-only enforcement so the test can
        # exercise the runtime frozen-model guard. Direct assignment
        # would type-error on the frozen field; B010 wants direct
        # assignment back, so the suppression is local and intentional.
        setattr(issuer, "display_name", "Toyota Motor")  # noqa: B010


def test_provenance_rejects_non_sha256_payload_hash() -> None:
    with pytest.raises(ValidationError):
        Provenance(
            source=Source.JQUANTS,
            fetched_at=datetime(2025, 6, 1, tzinfo=UTC),
            payload_hash="not-a-sha256",
        )


@pytest.mark.parametrize("value", ["", "x" * 65])
def test_identifier_value_bounds(value: str) -> None:
    with pytest.raises(ValidationError):
        Identifier(kind=IdentifierKind.JQUANTS_CODE, value=value)
