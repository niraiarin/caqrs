"""Phase E3 reconciliation seed loaders — types and protocol.

Implementation lands in step 2; this commit ships the type contract
and the failing test list per ADR-0006.

Per :doc:`docs/research/data-integration/03-phase-e3-reconciliation`
§C, the loaders walk a source's master / list endpoint and upsert
:class:`caqrs.entities.types.Issuer` rows into an
:class:`caqrs.entities.protocol.EntityStore`. Conflicts are
collected (not raised) and surfaced via
:class:`ReconcilerResult.conflicts`; ``dry_run=True`` walks the source
without mutating the store.
"""

from __future__ import annotations

from caqrs.data.edinetdb.client import EdinetDbClient
from caqrs.data.jquants.client import JQuantsClient
from caqrs.entities.protocol import EntityStore
from caqrs.schemas.common import StrictBaseModel


class ReconcilerResult(StrictBaseModel):
    """Outcome of one reconciler run.

    The result is intentionally narrow: counts plus a conflict log,
    plus a flag that tells the caller whether the store was actually
    mutated. The detailed audit trail lives on the
    :class:`~caqrs.entities.types.Provenance` rows attached to each
    upsert. All fields are populated even when ``dry_run=True``; in
    dry-run mode :attr:`upserted` reflects planned upserts that would
    have happened.
    """

    upserted: int
    skipped: int
    conflicts: tuple[str, ...]
    dry_run: bool


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

    Implementation lands in step 2 per ADR-0006.
    """
    raise NotImplementedError("Phase E3 step 2: see ADR-0006")


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

    Implementation lands in step 2 per ADR-0006.
    """
    raise NotImplementedError("Phase E3 step 2: see ADR-0006")
