"""Typed failures for entity-store operations."""

from caqrs.entities.types import IdentifierKind, IssuerId


class EntityStoreError(Exception):
    """Base for typed integration-layer failures."""


class IdentifierConflictError(EntityStoreError):
    """Raised when an identifier is already associated with another issuer."""

    def __init__(
        self,
        *,
        kind: IdentifierKind,
        value: str,
        existing_issuer_id: IssuerId,
        proposed_issuer_id: IssuerId,
    ) -> None:
        self.kind = kind
        self.value = value
        self.existing_issuer_id = existing_issuer_id
        self.proposed_issuer_id = proposed_issuer_id
        super().__init__(
            f"identifier {kind.value}:{value} is already assigned to {existing_issuer_id}"
        )


class UnknownIssuerError(EntityStoreError):
    """Raised when an operation references an issuer missing from the store."""

    def __init__(self, *, issuer_id: IssuerId) -> None:
        self.issuer_id = issuer_id
        super().__init__(f"unknown issuer_id: {issuer_id}")
