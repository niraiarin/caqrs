"""Static-analysis lints owned by the CAQRS repo.

Distinct from `scripts/check_*.py` shims: this subpackage holds the
typed, mypy-strict, importable audit functions; the scripts are thin
CLI wrappers that the Justfile / CI invoke. Tests assert against the
audit functions directly with synthetic fixtures.
"""

from __future__ import annotations
