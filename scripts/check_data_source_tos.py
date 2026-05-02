#!/usr/bin/env python
"""Verify every src/caqrs/data/<source>/ package has a LICENSE_AND_TOS.md row.

Enforces the promise stated in LICENSE_AND_TOS.md and the compliance
expectations of NFR-COMPLY-1 / NFR-COMPLY-2 in
``docs/requirements/non-functional.md``: a data source must not land in
``src/`` without a corresponding row in the audit table.

The check is intentionally narrow:

* Existence only — every package directory under ``src/caqrs/data/`` whose
  name is not in :data:`EXCLUDED` must show up as a row in the
  ``## Currently integrated`` markdown table.
* No semantic validation — the lint does not verify URL targets, license
  wording, or rate-limit numbers. Those remain human-review gates marked
  ``# review`` in the document itself.
* Forward references are allowed — sources documented in the table but not
  yet present under ``src/caqrs/data/`` produce a NOTE only, not a
  failure. This is how the ``## Deferred`` section coexists with the lint.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "src" / "caqrs" / "data"
TOS_DOC = REPO_ROOT / "LICENSE_AND_TOS.md"
# Shared helpers (e.g. AsyncRateLimiter), not a data source themselves.
EXCLUDED = {"_common"}

_TABLE_RE = re.compile(
    r"\| Source\s+\|.*?\n\|[-| ]+\|\n((?:\|.*?\n)+)",
    re.DOTALL,
)
# Extract the bare package identifier from a markdown cell that may wrap
# the name in ``**bold**``, ``[link](url)``, or backticks.
_NAME_RE = re.compile(r"[a-z0-9_]+")

# A markdown table row has at least three pipes: the leading pipe, the
# pipe after the first cell, and the trailing pipe. ``str.split("|")``
# therefore yields at least three segments for a real row.
_MIN_ROW_PARTS = 3


def integrated_sources() -> set[str]:
    """Return the set of data-source package names found on disk."""
    if not DATA_DIR.is_dir():
        raise SystemExit(f"data directory not found: {DATA_DIR}")
    return {
        p.name
        for p in DATA_DIR.iterdir()
        if p.is_dir() and p.name not in EXCLUDED and not p.name.startswith("__")
    }


def documented_sources() -> set[str]:
    """Return the set of source names listed in the LICENSE_AND_TOS.md table."""
    if not TOS_DOC.is_file():
        raise SystemExit(f"LICENSE_AND_TOS.md not found: {TOS_DOC}")
    text = TOS_DOC.read_text(encoding="utf-8")
    table_match = _TABLE_RE.search(text)
    if table_match is None:
        raise SystemExit(
            "Could not find source table in LICENSE_AND_TOS.md "
            "(expected a row starting with '| Source ... |')."
        )
    documented: set[str] = set()
    for row in table_match.group(1).splitlines():
        # row looks like ``| **edinet** | ... | ... |``; the first cell is
        # everything between the first and second pipe.
        parts = row.split("|")
        if len(parts) < _MIN_ROW_PARTS:
            continue
        first_cell = parts[1].strip().lower()
        name_match = _NAME_RE.search(first_cell)
        if name_match:
            documented.add(name_match.group(0))
    return documented


def main() -> int:
    integrated = integrated_sources()
    documented = documented_sources()
    missing = integrated - documented
    extra = documented - integrated

    if missing:
        print(f"FAIL: integrated source(s) missing from LICENSE_AND_TOS.md: {sorted(missing)}")
        print("Add a row under '## Currently integrated' before merging.")
    if extra:
        # Forward references (e.g. FRED in ## Deferred) are legitimate.
        # Surface them as a note so reviewers can sanity-check spelling
        # without failing the build.
        print(
            "NOTE: documented but not under src/caqrs/data/: "
            f"{sorted(extra)} (forward references OK)"
        )
    if not missing and not extra:
        print(f"OK: {len(integrated)} integrated source(s), all present in LICENSE_AND_TOS.md.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
