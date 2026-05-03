"""Phase E3 reconciliation seed loaders — types and implementation.

Per :doc:`docs/research/data-integration/03-phase-e3-reconciliation`
§C, the loaders walk a source's master / list endpoint and upsert
:class:`caqrs.entities.types.Issuer` rows into an
:class:`caqrs.entities.protocol.EntityStore`. Conflicts are collected
(not raised) and surfaced via :attr:`ReconcilerResult.conflicts`;
``dry_run=True`` walks the source without mutating the store.

The :attr:`ReconcilerResult.provenance` tuple gives one
:class:`~caqrs.entities.types.Provenance` per upserted record (in
input order). It is empty for skipped / conflicting records and for
``dry_run`` runs (since nothing was committed). All upserts within a
single call share the same ``fetched_at`` timestamp captured at the
top of the call (see ENT-RECON-A3, ENT-RECON-T24).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic_core import to_json

from caqrs.data.edinetdb.client import EdinetDbClient
from caqrs.data.edinetdb.schemas import EdinetDbCompany
from caqrs.data.jquants.client import JQuantsClient
from caqrs.data.jquants.schemas import JQuantsListedStock
from caqrs.entities.errors import IdentifierConflictError
from caqrs.entities.protocol import EntityStore
from caqrs.entities.types import (
    Identifier,
    IdentifierKind,
    Issuer,
    IssuerId,
    Provenance,
    Source,
    new_issuer_id,
)
from caqrs.schemas.common import StrictBaseModel

_EDB_PAGE_SIZE = 500
_JQUANTS_FULL_CODE_LEN = 5  # 5-digit J-Quants code; 4-digit SEC_CODE = code[:-1].


class ReconcilerResult(StrictBaseModel):
    """Outcome of one reconciler run.

    The result is intentionally narrow: counts, a conflict log, a
    dry-run flag, and a per-upsert :class:`~caqrs.entities.types.Provenance`
    tuple. The Provenance entries are the only place the per-record
    audit trail lives — :class:`~caqrs.entities.types.Issuer` carries no
    provenance field of its own (see spec §A.3 footnote).

    All fields are populated even when ``dry_run=True``; in dry-run
    mode :attr:`upserted` reflects planned upserts that would have
    happened, and :attr:`provenance` is empty (nothing was committed).
    """

    upserted: int
    skipped: int
    conflicts: tuple[str, ...]
    dry_run: bool
    provenance: tuple[Provenance, ...] = ()


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


async def reconcile_from_jquants_master(
    *,
    client: JQuantsClient,
    store: EntityStore,
    dry_run: bool = False,
) -> ReconcilerResult:
    """Walk ``client.list_master()`` and upsert one Issuer per row.

    Per-row identifier set: ``(JQUANTS_CODE, <5-digit code>)`` plus
    ``(SEC_CODE, <4-digit code>)`` derived by stripping the trailing
    check digit. ``display_name`` is taken from the source's
    ``CompanyName`` (``CoName``) field. See
    :doc:`docs/research/data-integration/03-phase-e3-reconciliation`
    §D for the full per-record loop contract.
    """
    fetched_at = datetime.now(UTC)
    rows = await client.list_master()

    upserted = 0
    skipped = 0
    conflicts: list[str] = []
    provenance_log: list[Provenance] = []

    for row in rows:
        identifiers = _jquants_identifiers(row)
        if not identifiers:
            skipped += 1
            continue

        raw = _jquants_raw(row)
        provenance = _build_provenance(source=Source.JQUANTS, fetched_at=fetched_at, raw=raw)
        outcome = _try_upsert(
            store=store,
            display_name=row.company_name,
            identifiers=identifiers,
            dry_run=dry_run,
        )
        upserted, skipped, conflicts, provenance_log = _apply_outcome(
            outcome=outcome,
            upserted=upserted,
            skipped=skipped,
            conflicts=conflicts,
            provenance_log=provenance_log,
            provenance=provenance,
        )

    return ReconcilerResult(
        upserted=upserted,
        skipped=skipped,
        conflicts=tuple(conflicts),
        dry_run=dry_run,
        provenance=tuple(provenance_log),
    )


async def reconcile_from_edinetdb_companies(
    *,
    client: EdinetDbClient,
    store: EntityStore,
    dry_run: bool = False,
) -> ReconcilerResult:
    """Paginate ``client.list_companies(per_page=500)`` to exhaustion
    and upsert one Issuer per row.

    Per-row identifier set: ``(EDINET_CODE, <code>)`` always; plus
    ``(JCN, <13-digit>)`` when the source row carries a non-empty JCN;
    plus ``(SEC_CODE, <4-digit>)`` when the source row carries a
    sec_code (typically only listed companies). See
    :doc:`docs/research/data-integration/03-phase-e3-reconciliation`
    §D for the full per-record loop contract.
    """
    fetched_at = datetime.now(UTC)

    upserted = 0
    skipped = 0
    conflicts: list[str] = []
    provenance_log: list[Provenance] = []

    page = 1
    seen_so_far = 0
    while True:
        listing = await client.list_companies(page=page, per_page=_EDB_PAGE_SIZE)
        if not listing.data:
            break

        for company in listing.data:
            identifiers = _edinetdb_identifiers(company)
            if not identifiers:
                skipped += 1
                continue

            raw = _edinetdb_raw(company)
            provenance = _build_provenance(source=Source.EDINETDB, fetched_at=fetched_at, raw=raw)
            outcome = _try_upsert(
                store=store,
                display_name=company.name,
                identifiers=identifiers,
                dry_run=dry_run,
            )
            upserted, skipped, conflicts, provenance_log = _apply_outcome(
                outcome=outcome,
                upserted=upserted,
                skipped=skipped,
                conflicts=conflicts,
                provenance_log=provenance_log,
                provenance=provenance,
            )

        seen_so_far += len(listing.data)
        # End-of-stream detection: prefer pagination.total when present
        # (the upstream tells us exactly how many rows exist), else fall
        # back to "less than per_page" (last page heuristic). The
        # heuristic mishandles the case where the final page is exactly
        # full; total is the canonical stop signal.
        total = listing.meta.pagination.total
        if total is not None and seen_so_far >= total:
            break
        if total is None and len(listing.data) < _EDB_PAGE_SIZE:
            break
        page += 1

    return ReconcilerResult(
        upserted=upserted,
        skipped=skipped,
        conflicts=tuple(conflicts),
        dry_run=dry_run,
        provenance=tuple(provenance_log),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _jquants_identifiers(row: JQuantsListedStock) -> tuple[Identifier, ...]:
    """Build the identifier set for a J-Quants master row.

    A 5-digit J-Quants code yields both ``(JQUANTS_CODE, <5-digit>)``
    and ``(SEC_CODE, <4-digit>)`` (4-digit derived by dropping the
    trailing check digit per ENT-RECON-A6 / T27). A 4-digit code is
    accepted in JQuantsListedStock but does not produce a SEC_CODE
    in this loader (it is itself already SEC-shaped); we still emit a
    JQUANTS_CODE for traceability.
    """
    code = row.code
    if not code:
        return ()
    identifiers: list[Identifier] = [
        Identifier(kind=IdentifierKind.JQUANTS_CODE, value=code),
    ]
    if len(code) == _JQUANTS_FULL_CODE_LEN:
        identifiers.append(Identifier(kind=IdentifierKind.SEC_CODE, value=code[:-1]))
    return tuple(identifiers)


def _edinetdb_identifiers(company: EdinetDbCompany) -> tuple[Identifier, ...]:
    """Build the identifier set for an EDINET DB company row.

    Always emits ``(EDINET_CODE, ...)``. Conditionally emits
    ``(JCN, ...)`` when the source row carries a non-empty JCN, and
    ``(SEC_CODE, ...)`` when the source row carries a non-empty
    sec_code (per ENT-RECON-A6 / T28 / T29).
    """
    if not company.edinet_code:
        return ()
    identifiers: list[Identifier] = [
        Identifier(kind=IdentifierKind.EDINET_CODE, value=company.edinet_code),
    ]
    if company.jcn:
        identifiers.append(Identifier(kind=IdentifierKind.JCN, value=company.jcn))
    if company.sec_code:
        identifiers.append(Identifier(kind=IdentifierKind.SEC_CODE, value=company.sec_code))
    return tuple(identifiers)


def _jquants_raw(row: JQuantsListedStock) -> dict[str, object]:
    """Return the wire-format dict the loader hashes for provenance.

    Reconstructs the original row shape (alias-keyed) so the
    payload_hash is stable across deployments — a row with the same
    upstream payload always hashes to the same value.
    """
    raw: dict[str, object] = {
        "Date": row.date.isoformat(),
        "Code": row.code,
        "CoName": row.company_name,
    }
    if row.company_name_en is not None:
        raw["CoNameEn"] = row.company_name_en
    return raw


def _edinetdb_raw(company: EdinetDbCompany) -> dict[str, object]:
    """Return the wire-format dict the loader hashes for provenance."""
    raw: dict[str, object] = {
        "edinet_code": company.edinet_code,
        "name": company.name,
        "name_ja": company.name_ja,
        "industry": company.industry,
        "accounting_standard": company.accounting_standard,
    }
    if company.sec_code is not None:
        raw["sec_code"] = company.sec_code
    if company.jcn is not None:
        raw["jcn"] = company.jcn
    if company.name_en is not None:
        raw["name_en"] = company.name_en
    return raw


def _build_provenance(
    *, source: Source, fetched_at: datetime, raw: dict[str, object]
) -> Provenance:
    payload_hash = hashlib.sha256(to_json(raw)).hexdigest()
    return Provenance(source=source, fetched_at=fetched_at, payload_hash=payload_hash)


# Sentinel outcome tags returned by _try_upsert. The loader collapses
# these into the (upserted/skipped/conflicts, provenance_log) ledger.
# Tags:
#   "upserted"        - first-time write (or display_name changed)
#   "skipped:no-op"   - idempotent re-run, nothing changed
#   "skipped:dry-run" - dry_run=True, would-have-upserted
#   "conflict"        - IdentifierConflictError; second tuple element is the message
_UpsertOutcome = tuple[str, str | None]


def _try_upsert(
    *,
    store: EntityStore,
    display_name: str,
    identifiers: tuple[Identifier, ...],
    dry_run: bool,
) -> _UpsertOutcome:
    """Build the Issuer, decide upserted vs skipped vs conflict.

    Reuses an existing Issuer.id when one of the new identifiers
    already lives in the store (per spec §D step 4); otherwise mints a
    fresh id. A display_name mismatch on an existing-id reuse is
    treated as a conflict per ENT-RECON-T25 / A.4 ("different legal
    entity"). In ``dry_run`` mode no store mutation happens; the
    return value still distinguishes "would-have-upserted" from
    "would-have-conflicted".
    """
    owner_id, owner_kind, owner_value = _find_existing_owner(store=store, identifiers=identifiers)
    if owner_id is not None:
        owner_issuer = store.get_issuer(issuer_id=owner_id)
        if owner_issuer is not None and owner_issuer.display_name != display_name:
            return (
                "conflict",
                _format_conflict(
                    kind=owner_kind,
                    value=owner_value,
                    existing_issuer_id=owner_id,
                    proposed_issuer_id=owner_id,  # would have been reused
                ),
            )
        issuer_id = owner_id
    else:
        issuer_id = new_issuer_id()

    issuer = Issuer(
        id=issuer_id,
        lei=None,
        jcn=None,
        display_name=display_name,
        identifiers=identifiers,
    )

    existing = store.get_issuer(issuer_id=issuer_id)
    is_no_op = existing is not None and _issuers_equivalent(existing, issuer)

    if dry_run:
        return ("skipped:no-op", None) if is_no_op else ("skipped:dry-run", None)

    if is_no_op:
        return ("skipped:no-op", None)

    try:
        store.upsert_issuer(issuer=issuer)
    except IdentifierConflictError as exc:
        return (
            "conflict",
            _format_conflict(
                kind=exc.kind,
                value=exc.value,
                existing_issuer_id=exc.existing_issuer_id,
                proposed_issuer_id=exc.proposed_issuer_id,
            ),
        )
    return ("upserted", None)


def _apply_outcome(
    *,
    outcome: _UpsertOutcome,
    upserted: int,
    skipped: int,
    conflicts: list[str],
    provenance_log: list[Provenance],
    provenance: Provenance,
) -> tuple[int, int, list[str], list[Provenance]]:
    kind, message = outcome
    if kind == "upserted":
        upserted += 1
        provenance_log.append(provenance)
    elif kind == "skipped:dry-run":
        # Dry-run treats "would-have-upserted" as upserted in the count
        # (see spec §A.5: result.upserted reflects planned upserts).
        upserted += 1
    elif kind == "skipped:no-op":
        skipped += 1
    elif kind == "conflict":
        skipped += 1
        if message is not None:
            conflicts.append(message)
    return upserted, skipped, conflicts, provenance_log


def _find_existing_owner(
    *,
    store: EntityStore,
    identifiers: tuple[Identifier, ...],
) -> tuple[IssuerId | None, IdentifierKind, str]:
    """Look for an existing Issuer that already binds any of the new
    identifiers. Returns ``(issuer_id, matching_kind, matching_value)``
    on the first hit, or ``(None, first_kind, first_value)`` when no
    overlap exists. The kind+value pair is used to format conflict
    messages downstream.
    """
    first_kind = identifiers[0].kind
    first_value = identifiers[0].value
    for identifier in identifiers:
        existing = store.lookup_issuer(kind=identifier.kind, value=identifier.value)
        if existing is not None:
            return existing.id, identifier.kind, identifier.value
    return None, first_kind, first_value


def _issuers_equivalent(left: Issuer, right: Issuer) -> bool:
    """Equivalence relation that treats two Issuers as a no-op upsert
    pair when they share an id, display_name, and identifier set."""
    if left.id != right.id:
        return False
    if left.display_name != right.display_name:
        return False
    return set(left.identifiers) == set(right.identifiers)


def _format_conflict(
    *,
    kind: IdentifierKind,
    value: str,
    existing_issuer_id: IssuerId,
    proposed_issuer_id: IssuerId,
) -> str:
    """Format a conflict message per spec §D step 6.

    Stable string form; embedded in :attr:`ReconcilerResult.conflicts`
    and exercised by ENT-RECON-T25 / ENT-RECON-P9.
    """
    return (
        f"{kind.value} '{value}' already bound to {existing_issuer_id}, "
        f"refused {proposed_issuer_id}"
    )
