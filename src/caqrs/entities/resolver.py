"""EntityResolver — Phase E4 minimal slice.

Wires the existing :class:`~caqrs.entities.protocol.EntityStore` into
the cycle runner without changing the Observer agent's contract. Given
an :class:`~caqrs.schemas.observer.ObserverInput` whose ``universe``
holds source-specific identifiers (J-Quants codes, EDINET codes,
yFinance tickers, ...), the resolver looks each one up in the store
and emits one :class:`IdentifierResolution` per element. The cycle
runner consumes those resolutions to emit ``IDENTIFIER_RESOLVED``
events into the event log; the resolver itself never mutates the
ObserverInput.

Per Codex GPT-5.5 cross-family review (2026-05-03), Phase E4 is the
**smallest useful slice**:

- single optional callable hook on :class:`~caqrs.orchestrator.cycle_runner.CycleRunner`
- no schema changes to ``ObserverInput`` / ``ObserverArtifact``
- no fuzzy / vector / multi-source-join logic (those are Phase E5)
- the proof of value is one cycle resolving J-Quants and EDINET
  identifiers to the same canonical issuer

Step 1 / Step 2 dispatch (ADR-0006): this module's factory body
raises ``NotImplementedError`` in step 1; step 2 wires the lookup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from caqrs.entities.protocol import EntityStore
from caqrs.entities.types import IdentifierKind, IssuerId
from caqrs.schemas.observer import ObserverInput


@dataclass(frozen=True, slots=True)
class IdentifierResolution:
    """One ticker -> canonical-issuer mapping.

    ``matched_kind`` records which :class:`IdentifierKind` produced the
    hit; this is informational so callers can tell whether the same
    ticker matched as a J-Quants code or as an EDINET code without
    re-running the lookup. Both fields are ``None`` when no
    :class:`~caqrs.entities.types.Identifier` in the store matched
    the input ticker — the resolver is read-only and never invents
    issuers.
    """

    input_ticker: str
    canonical_issuer_id: IssuerId | None
    matched_kind: IdentifierKind | None


# A thin callable alias so the cycle runner's ctor can accept either a
# plain function or anything else that happens to satisfy the shape.
# Keeping this as a Callable (not a Protocol) avoids forcing the
# orchestrator to import from caqrs.entities.* — the runner already
# depends on schemas, and one extra entities import is structurally
# fine but pinning a Protocol shape would make it harder for callers
# to compose ad-hoc resolvers.
EntityResolver = Callable[[ObserverInput], tuple[IdentifierResolution, ...]]


def make_entity_resolver(*, store: EntityStore) -> EntityResolver:
    """Build a resolver that walks ``observer_input.universe`` and
    looks each ticker up in ``store`` against every
    :class:`IdentifierKind` (in declaration order) until one matches
    or all are exhausted.

    The "try every kind" strategy is acceptable for Phase E4 because
    :class:`~caqrs.entities.protocol.EntityStore` enforces ENT-A3
    (a (kind, value) pair maps to at most one issuer). If two kinds
    happen to match the same input value (e.g. ``"7203"`` could be
    a SEC_CODE for one issuer or a JQUANTS_CODE for another), the
    declared-first match wins. This is documented as a footgun-of-
    convenience; callers who need stricter resolution should pass a
    pre-classified universe (one ticker, one kind known by the
    operator) and skip the resolver.

    Phase E5 adds richer resolution (fuzzy matching, prefix-derived
    inference, bulk LEI lookups); they are explicitly out of scope.
    """
    raise NotImplementedError(
        "Phase E4 step 1 placeholder; lookup-loop impl in step 2",
    )
