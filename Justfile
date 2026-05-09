# CAQRS task runner. Every recipe is a thin wrapper over `uv run`; you can
# run any of these directly without installing `just`. See README.md.

default:
    @just --list

# === Code quality ===
lint:
    uv run ruff check .

fmt:
    uv run ruff format .

fmt-check:
    uv run ruff format --check .

typecheck:
    uv run mypy src tests

# Check that registry <-> test-suite traces are consistent
traceability:
    uv run --with pyyaml python scripts/check_traceability.py

# Verify every data source has a LICENSE_AND_TOS.md entry
tos:
    python scripts/check_data_source_tos.py

# Verify NFR-LIVE-BROKER-2 credential isolation across the import graph
lint-creds:
    uv run --frozen python scripts/check_credential_isolation.py

# === Tests ===
test:
    uv run pytest

test-cov:
    uv run pytest --cov=caqrs --cov-report=term-missing

# Run performance smoke benchmarks (NFR-PERF-1, NFR-PERF-3). These are
# deselected from the default `just test` via `addopts = "-m 'not perf'"`
# in pyproject.toml, and run in a separate, non-blocking CI job (Task #89).
perf:
    uv run pytest -m perf --benchmark-only --benchmark-disable-gc

# === Aggregate ===
ci: fmt-check lint typecheck traceability tos lint-creds test
    @echo "All CI checks passed."

# === Local dev ===
sync:
    uv sync

upgrade:
    uv lock --upgrade
    uv sync
