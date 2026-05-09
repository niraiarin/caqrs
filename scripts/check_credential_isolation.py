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


def main() -> int:
    raise NotImplementedError(
        "Task #88 step 1 placeholder; CLI wiring impl in step 2",
    )


if __name__ == "__main__":
    sys.exit(main())
