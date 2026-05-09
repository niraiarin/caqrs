"""Credential-isolation lint CLI (Task #88).

Thin shim over :mod:`caqrs.lint.credential_isolation`. Run via the
project's task runner::

    just lint-creds

Or directly::

    uv run --frozen python scripts/check_credential_isolation.py

Exits 0 on a clean audit; non-zero with a structured violation report
on any forbidden-prefix reachability hit. See
:mod:`caqrs.lint.credential_isolation` and ADR-0008 §NFR-LIVE-BROKER-2.

Step 1 / Step 2 dispatch (ADR-0006): the entry point raises
``NotImplementedError`` in step 1; step 2 wires it through to the
audit function and formats the report.
"""

from __future__ import annotations

import sys
from pathlib import Path

from caqrs.lint.credential_isolation import (
    DEFAULT_BOUNDARIES,
    audit_credential_isolation,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src"


def main() -> int:
    violations = audit_credential_isolation(
        src_root=_SRC_ROOT,
        boundaries=DEFAULT_BOUNDARIES,
    )
    if not violations:
        print(
            f"OK  no credential-isolation violations across "
            f"{len(DEFAULT_BOUNDARIES)} boundary(ies).",
        )
        return 0
    for v in violations:
        chain = " -> ".join(v.import_path)
        print(
            f"FAIL  {v.boundary} reaches forbidden env "
            f"{v.env_var!r} (prefix {v.forbidden_prefix!r}) "
            f"via {chain} (site: {v.site_module})",
            file=sys.stderr,
        )
    print(
        f"FAIL  {len(violations)} credential-isolation violation(s) "
        "— see ADR-0008 §NFR-LIVE-BROKER-2",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
