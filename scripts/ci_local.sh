#!/usr/bin/env bash
#
# Local equivalent of .github/workflows/ci.yml — runs the same gates
# the upstream CI runs, with the same env (HYPOTHESIS_PROFILE=ci).
# Useful when GitHub Actions is unavailable (billing limit, outage,
# etc.) or when iterating locally before pushing.
#
# Default: runs against the active Python version (whatever uv resolves).
# Pass --matrix to also run against Python 3.12 + 3.13 (each with its
# own ephemeral env so mypy / pytest see the right interpreter).
#
# Usage::
#
#     scripts/ci_local.sh             # active Python only
#     scripts/ci_local.sh --matrix    # 3.12 + 3.13
#
# Exits 0 on success; non-zero on first gate failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export HYPOTHESIS_PROFILE=ci

run_gates() {
    local label="$1"
    local python_arg="${2:-}"
    echo
    echo "════════════════════════════════════════════════════════════════"
    echo "  ${label}"
    echo "════════════════════════════════════════════════════════════════"

    if [[ -n "$python_arg" ]]; then
        local uv_run=(uv run --python "$python_arg" --frozen)
    else
        local uv_run=(uv run --frozen)
    fi

    echo "[1/6] ruff format --check"
    "${uv_run[@]}" ruff format --check .

    echo "[2/6] ruff check"
    "${uv_run[@]}" ruff check .

    echo "[3/6] mypy"
    "${uv_run[@]}" mypy src tests

    echo "[4/6] traceability"
    "${uv_run[@]}" --with pyyaml python scripts/check_traceability.py

    echo "[5/6] tos"
    python scripts/check_data_source_tos.py

    echo "[6/6] pytest (HYPOTHESIS_PROFILE=ci)"
    "${uv_run[@]}" pytest --cov=caqrs --cov-report=xml

    echo
    echo "✓ ${label}: all gates green"
}

case "${1:-}" in
    --matrix)
        run_gates "Python 3.12" "3.12"
        run_gates "Python 3.13" "3.13"
        echo
        echo "════════════════════════════════════════════════════════════════"
        echo "  ALL MATRIX COMBINATIONS GREEN"
        echo "════════════════════════════════════════════════════════════════"
        ;;
    "" | --active)
        run_gates "active Python"
        ;;
    -h | --help)
        sed -n '2,/^$/p' "$0" | sed 's/^#\s\?//'
        ;;
    *)
        echo "unknown flag: $1" >&2
        echo "usage: $0 [--matrix | --active | --help]" >&2
        exit 2
        ;;
esac
