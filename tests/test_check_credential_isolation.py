"""Test list for the credential-isolation lint (Task #88).

Step 1 / Step 2 dispatch (ADR-0006):

- **Step 1** (this commit): every test below is decorated
  ``@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")``
  and exercises the public surface declared in
  :mod:`caqrs.lint.credential_isolation`. ``audit_credential_isolation``
  raises ``NotImplementedError`` so pytest reports ``8 xfailed``.
- **Step 2** (next commit): the audit's body is implemented. Every
  ``xfail`` marker is removed in the same commit that makes the
  corresponding assertion pass; ``strict=True`` would otherwise turn
  the unexpected pass into a hard failure.

Per ADR-0008 §NFR-LIVE-BROKER-2 the lint MUST catch both the direct
case (a boundary that itself reads ``os.environ["LIVE_BROKER_*"]``)
and the transitive case (a boundary that imports a helper that does).
The test list is structured to cover:

- happy path on the real codebase (regression guard)
- direct literal env-read inside the boundary
- kwarg-default env-read (the existing edinet ``from_env`` pattern)
- transitive multi-hop chains (with ``import_path`` reporting)
- non-matching env vars (no false positives)
- modules outside the boundary subgraph (no false positives)
- missing boundary module (skip-silently contract)
- multi-hop chain reporting

Per ENT-RECON-A6 etc. the lint emits violations sorted
lexicographically; tests assert against the sorted form to keep the
contract stable.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from caqrs.lint.credential_isolation import (
    CredentialBoundary,
    audit_credential_isolation,
)

# --- helpers -------------------------------------------------------------


def _write_module(root: Path, dotted: str, body: str) -> None:
    """Write a synthetic module at ``root/<dotted-as-path>.py`` with
    the given body. Empty ``__init__.py`` files are created for every
    intermediate package so the dotted path resolves on disk.
    """
    parts = dotted.split(".")
    pkg_dir = root.joinpath(*parts[:-1])
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for depth in range(1, len(parts)):
        ancestor = root.joinpath(*parts[:depth])
        init = ancestor / "__init__.py"
        if not init.exists():
            init.write_text("")
    pkg_dir.joinpath(f"{parts[-1]}.py").write_text(textwrap.dedent(body).lstrip())


# --- regression guard on the real codebase -------------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")
def test_real_codebase_passes_default_paper_broker_boundary() -> None:
    """Regression guard: today's ``src/caqrs/`` exposes no ``LIVE_BROKER_*``
    leak from PaperBroker (LiveBroker doesn't exist yet), so the audit
    MUST return an empty tuple. This is the test that catches a future
    PR that wires PaperBroker into LiveBroker credential surface by
    mistake."""
    src = Path(__file__).resolve().parent.parent / "src"
    boundaries = (
        CredentialBoundary(
            module="caqrs.execution.paper_broker",
            forbidden_prefixes=("LIVE_BROKER_",),
        ),
    )
    result = audit_credential_isolation(src_root=src, boundaries=boundaries)
    assert result == ()


# --- direct-case violations ----------------------------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")
def test_direct_literal_env_read_violates(tmp_path: Path) -> None:
    """A boundary module that itself reads ``os.environ["LIVE_BROKER_API_KEY"]``
    must produce one violation with ``import_path == (boundary,)``."""
    _write_module(
        tmp_path,
        "pkg.broker",
        """
        import os
        KEY = os.environ["LIVE_BROKER_API_KEY"]
        """,
    )
    boundaries = (CredentialBoundary(module="pkg.broker", forbidden_prefixes=("LIVE_BROKER_",)),)
    violations = audit_credential_isolation(src_root=tmp_path, boundaries=boundaries)
    assert len(violations) == 1
    v = violations[0]
    assert v.boundary == "pkg.broker"
    assert v.forbidden_prefix == "LIVE_BROKER_"
    assert v.env_var == "LIVE_BROKER_API_KEY"
    assert v.site_module == "pkg.broker"
    assert v.import_path == ("pkg.broker",)


@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")
def test_kwarg_default_env_read_violates(tmp_path: Path) -> None:
    """The existing ``from_env(env_var="EDINET_API_KEY")`` pattern in
    ``caqrs.data.edinet.client`` reads via a kwarg default — a string
    literal that lives in the function signature, not in the call site.
    The audit MUST resolve the kwarg default and report the env var
    name instead of treating it as dynamic."""
    _write_module(
        tmp_path,
        "pkg.broker",
        """
        import os

        def from_env(*, env_var: str = "LIVE_BROKER_API_KEY") -> str:
            return os.environ.get(env_var, "")
        """,
    )
    boundaries = (CredentialBoundary(module="pkg.broker", forbidden_prefixes=("LIVE_BROKER_",)),)
    violations = audit_credential_isolation(src_root=tmp_path, boundaries=boundaries)
    assert len(violations) == 1
    assert violations[0].env_var == "LIVE_BROKER_API_KEY"


# --- transitive-case violations ------------------------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")
def test_transitive_import_chain_is_reported(tmp_path: Path) -> None:
    """Boundary imports helper which reads forbidden env. Violation's
    ``import_path`` MUST include the full chain ``(boundary, helper)``
    so an operator can see which transitive dependency leaked."""
    _write_module(
        tmp_path,
        "pkg.broker",
        """
        from pkg.helper import KEY
        TOKEN = KEY
        """,
    )
    _write_module(
        tmp_path,
        "pkg.helper",
        """
        import os
        KEY = os.environ["LIVE_BROKER_API_KEY"]
        """,
    )
    boundaries = (CredentialBoundary(module="pkg.broker", forbidden_prefixes=("LIVE_BROKER_",)),)
    violations = audit_credential_isolation(src_root=tmp_path, boundaries=boundaries)
    assert len(violations) == 1
    v = violations[0]
    assert v.site_module == "pkg.helper"
    assert v.import_path == ("pkg.broker", "pkg.helper")


@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")
def test_violation_includes_full_import_chain_for_multi_hop(
    tmp_path: Path,
) -> None:
    """Three-hop chain: boundary -> helperA -> helperB -> env read.
    The reported ``import_path`` MUST contain all four entries."""
    _write_module(tmp_path, "pkg.broker", "from pkg.helperA import _\n")
    _write_module(tmp_path, "pkg.helperA", "from pkg.helperB import _\n")
    _write_module(
        tmp_path,
        "pkg.helperB",
        """
        import os
        _ = os.environ["LIVE_BROKER_TOKEN"]
        """,
    )
    boundaries = (CredentialBoundary(module="pkg.broker", forbidden_prefixes=("LIVE_BROKER_",)),)
    violations = audit_credential_isolation(src_root=tmp_path, boundaries=boundaries)
    assert len(violations) == 1
    assert violations[0].import_path == (
        "pkg.broker",
        "pkg.helperA",
        "pkg.helperB",
    )
    assert violations[0].site_module == "pkg.helperB"


# --- non-matching cases (no false positives) -----------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")
def test_unrelated_env_var_is_not_reported(tmp_path: Path) -> None:
    """A boundary that reads a perfectly-unrelated env var (no
    forbidden-prefix match) MUST not produce a violation."""
    _write_module(
        tmp_path,
        "pkg.broker",
        """
        import os
        DEBUG = os.environ.get("DEBUG", "")
        """,
    )
    boundaries = (CredentialBoundary(module="pkg.broker", forbidden_prefixes=("LIVE_BROKER_",)),)
    violations = audit_credential_isolation(src_root=tmp_path, boundaries=boundaries)
    assert violations == ()


@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")
def test_module_outside_boundary_subgraph_is_ignored(tmp_path: Path) -> None:
    """A sibling module reads ``LIVE_BROKER_*`` but is unreachable from
    the boundary's import closure. The audit MUST scope its reachability
    walk to the boundary's transitive imports — sibling reads are
    irrelevant."""
    _write_module(tmp_path, "pkg.broker", "VALUE = 42\n")
    _write_module(
        tmp_path,
        "pkg.unrelated",
        """
        import os
        KEY = os.environ["LIVE_BROKER_API_KEY"]
        """,
    )
    boundaries = (CredentialBoundary(module="pkg.broker", forbidden_prefixes=("LIVE_BROKER_",)),)
    violations = audit_credential_isolation(src_root=tmp_path, boundaries=boundaries)
    assert violations == ()


# --- skip-silently contract ----------------------------------------------


@pytest.mark.xfail(strict=True, reason="impl pending — Task #88")
def test_missing_boundary_module_skips_silently(tmp_path: Path) -> None:
    """The default boundaries include ``caqrs.execution.live_broker`` even
    though the module does not exist yet (P4 pending). The audit MUST
    skip a missing boundary silently — neither raise nor emit a
    violation — so the lint stays useful pre-P4."""
    _write_module(tmp_path, "pkg.existing", "X = 1\n")
    boundaries = (
        CredentialBoundary(
            module="pkg.does_not_exist",
            forbidden_prefixes=("LIVE_BROKER_",),
        ),
    )
    violations = audit_credential_isolation(src_root=tmp_path, boundaries=boundaries)
    assert violations == ()
