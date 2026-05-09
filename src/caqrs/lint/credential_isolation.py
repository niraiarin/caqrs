"""Credential-isolation static-graph audit (Task #88, NFR-LIVE-BROKER-2).

Builds a module-level import graph over a ``src/`` tree and verifies
that declared boundary modules cannot transitively reach an env-var
read whose name matches a forbidden prefix. The lint catches both the
direct case (``os.environ["LIVE_BROKER_API_KEY"]`` inside the boundary
itself) and the transitive case (the boundary imports a helper that
does).

Default boundaries (per ADR-0008 §NFR-LIVE-BROKER-2):

- ``caqrs.execution.paper_broker`` MUST NOT reach any ``LIVE_BROKER_*``
  env var read; PaperBroker is paper-only and any live-credential read
  in its transitive closure is a contract violation.
- ``caqrs.execution.live_broker`` (when it lands) MUST NOT reach any
  data-side env var (``JQUANTS_*``, ``EDINET_*``, ``EDINETDB_*``,
  ``POLYMARKET_*``, ``YFINANCE_*``); live broker credential surface is
  exclusively ``LIVE_BROKER_*``.

The audit is a pure function: parse AST, build graph, query
reachability, return violations. No side effects, no I/O beyond
reading source files. CLI entry: ``scripts/check_credential_isolation.py``.

Step 1 / Step 2 dispatch (ADR-0006): this module's bodies are filled in
during step 2; step 1 leaves ``NotImplementedError`` so the test list
runs xfailed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CredentialBoundary:
    """One boundary module + the env-var prefixes it must not reach.

    ``module`` is a dotted path (e.g. ``"caqrs.execution.paper_broker"``)
    that MUST exist within the audited tree; missing boundaries are
    skipped silently so the lint stays useful before the LiveBroker
    module lands.

    ``forbidden_prefixes`` is matched as a string-prefix (Python
    ``str.startswith``) against every env var name discovered in the
    boundary's transitive closure. Empty prefix is rejected at
    construction time to avoid an "everything is forbidden" footgun.
    """

    module: str
    forbidden_prefixes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.module:
            raise ValueError("CredentialBoundary.module must be non-empty")
        if not self.forbidden_prefixes:
            raise ValueError(
                "CredentialBoundary.forbidden_prefixes must be non-empty",
            )
        for prefix in self.forbidden_prefixes:
            if not prefix:
                raise ValueError(
                    "CredentialBoundary.forbidden_prefixes entries must be non-empty",
                )


@dataclass(frozen=True, slots=True)
class CredentialViolation:
    """One reachability violation.

    ``import_path`` always starts with the boundary itself and ends
    with the module that performs the env read; intermediate entries
    are the transitive helpers.
    """

    boundary: str
    forbidden_prefix: str
    env_var: str
    site_module: str
    import_path: tuple[str, ...]


# Default boundaries applied by the CLI when no explicit config is given.
# ADR-0008 §NFR-LIVE-BROKER-2 is the upstream spec; new entries land here
# when a venue ADR (ADR-0009, ADR-0010, ...) introduces a fresh credential
# surface.
_DATA_SIDE_PREFIXES: tuple[str, ...] = (
    "JQUANTS_",
    "EDINET_",
    "EDINETDB_",
    "POLYMARKET_",
    "YFINANCE_",
)

DEFAULT_BOUNDARIES: tuple[CredentialBoundary, ...] = (
    CredentialBoundary(
        module="caqrs.execution.paper_broker",
        forbidden_prefixes=("LIVE_BROKER_",),
    ),
    CredentialBoundary(
        module="caqrs.execution.live_broker",
        forbidden_prefixes=_DATA_SIDE_PREFIXES,
    ),
)


def audit_credential_isolation(
    *,
    src_root: Path,
    boundaries: tuple[CredentialBoundary, ...],
) -> tuple[CredentialViolation, ...]:
    """Audit credential isolation across the module graph rooted at ``src_root``.

    For each boundary:

    1. Resolve the boundary's source file under ``src_root`` (treating
       dotted paths as filesystem paths). If absent, skip silently.
    2. BFS the import graph starting at the boundary, following only
       edges to other modules whose dotted prefix appears under
       ``src_root`` (third-party imports are out of scope for the
       reachability check; the lint only owns first-party code).
    3. For every reachable module, collect env-var read sites whose
       statically resolvable name matches one of the boundary's
       forbidden prefixes; emit a :class:`CredentialViolation` per hit.

    Returns violations sorted lexicographically by
    ``(boundary, env_var, site_module)`` so the output is stable across
    invocations. An empty tuple means the audit passed.
    """
    raise NotImplementedError(
        "Task #88 step 1 placeholder; static graph reachability impl in step 2",
    )
