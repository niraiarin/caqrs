"""Registry <-> test-suite traceability consistency checker.

Reads ``docs/requirements/registry.yaml`` and the ``tests/`` tree, and exits
non-zero if any of these inconsistencies are found:

  1. A REQ-ID's ``tests[].path`` does not exist on disk, or one of its
     ``tests[].names[i]`` does not resolve to a test function or test class
     (``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef``) inside that file.
  2. A REQ-ID has ``status: formalized`` but ``tests: []``.
  3. A test marked ``@pytest.mark.traces("REQ-ID")`` references a REQ-ID that
     is **not** present in the registry.
  4. The same REQ-ID appears more than once in the registry (preserves the
     duplicate-ID check originally introduced in Task #78).

What this checker does NOT enforce yet (deliberate, deferred):

  * Bidirectional completeness ("every formalized REQ-ID has at least one
    test physically marked with ``@pytest.mark.traces``").  Retrofitting is
    incremental; gating bidirectional completeness now would block Task #80
    itself.  Follow-up work will tighten this.
  * NFR target threshold validation (still placeholders pre-Task #79).
  * ``tests/test_yfinance_schemas.py`` uses ``unittest.TestCase``-style
    classes (``TestYFinancePrice`` etc.).  ``DATA-YF-A5`` cites the class
    names; that resolution path *is* covered here, but adding
    ``@pytest.mark.traces`` to bound methods of ``TestCase`` subclasses is
    deferred (a single class-level decorator would be the natural place,
    but pytest collects the methods individually -- left as future work).

Run via the project's task runner::

    just traceability

Or directly::

    uv run --with pyyaml python scripts/check_traceability.py
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - bootstrapping guidance
    sys.stderr.write(
        "error: pyyaml is required.  Run via:\n"
        "  uv run --with pyyaml python scripts/check_traceability.py\n"
        "  (or `just traceability`)\n"
    )
    raise SystemExit(2) from None


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "requirements" / "registry.yaml"
TESTS_ROOT = REPO_ROOT / "tests"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Findings:
    """Categorised collector for traceability inconsistencies."""

    missing_files: list[str] = field(default_factory=list)
    missing_names: list[str] = field(default_factory=list)
    orphan_traces: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    formalized_without_tests: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.missing_files)
            + len(self.missing_names)
            + len(self.orphan_traces)
            + len(self.duplicate_ids)
            + len(self.formalized_without_tests)
        )

    def report(self) -> str:
        lines: list[str] = []

        def section(title: str, entries: list[str]) -> None:
            if not entries:
                return
            lines.append(f"\n[{title}] ({len(entries)})")
            lines.extend(f"  - {entry}" for entry in entries)

        section("duplicate REQ-IDs", self.duplicate_ids)
        section("missing test files", self.missing_files)
        section("missing test names", self.missing_names)
        section("orphan @pytest.mark.traces (not in registry)", self.orphan_traces)
        section("formalized REQ-IDs without tests", self.formalized_without_tests)

        if self.total == 0:
            lines.append("OK  registry <-> test-suite trace is consistent.")
        else:
            lines.append(f"\nFAIL  {self.total} traceability finding(s).")
        return "\n".join(lines).lstrip("\n")


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------


def load_registry(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "requirements" not in data:
        raise SystemExit(f"error: {path} has no top-level 'requirements' key")
    return data


def iter_requirements(registry: dict[str, Any]) -> Iterator[dict[str, Any]]:
    requirements = registry.get("requirements", [])
    if not isinstance(requirements, list):
        raise SystemExit("error: registry.requirements is not a list")
    for entry in requirements:
        if isinstance(entry, dict):
            yield entry


# ---------------------------------------------------------------------------
# AST-driven test name resolution
# ---------------------------------------------------------------------------


def _collect_top_level_names(tree: ast.Module) -> set[str]:
    """Return names of top-level test functions and test classes in ``tree``."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _parse_test_file(path: Path) -> ast.Module | None:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - tests should always parse
        sys.stderr.write(f"error: failed to parse {path}: {exc}\n")
        return None


# ---------------------------------------------------------------------------
# Forward direction: registry -> tests
# ---------------------------------------------------------------------------


def _resolve_available_names(
    file_path: Path,
    cache: dict[Path, set[str] | None],
) -> set[str] | None:
    if file_path not in cache:
        tree = _parse_test_file(file_path)
        cache[file_path] = _collect_top_level_names(tree) if tree is not None else None
    return cache[file_path]


def _check_test_block(
    req_id: str,
    test_block: object,
    findings: Findings,
    name_cache: dict[Path, set[str] | None],
) -> None:
    if not isinstance(test_block, dict):
        findings.missing_names.append(f"{req_id}: tests[] entry is not a mapping")
        return
    rel_path = test_block.get("path")
    names = test_block.get("names") or []
    if not isinstance(rel_path, str):
        findings.missing_names.append(f"{req_id}: tests[].path missing or not a string")
        return
    file_path = REPO_ROOT / rel_path
    if not file_path.is_file():
        findings.missing_files.append(f"{req_id}: {rel_path} (file not found)")
        return
    available = _resolve_available_names(file_path, name_cache)
    if available is None:
        findings.missing_names.append(f"{req_id}: {rel_path} (failed to parse)")
        return
    if not isinstance(names, list):
        findings.missing_names.append(f"{req_id}: tests[].names is not a list ({rel_path})")
        return
    for name in names:
        if not isinstance(name, str):
            findings.missing_names.append(
                f"{req_id}: tests[].names contains non-string ({rel_path})"
            )
            continue
        if name not in available:
            findings.missing_names.append(
                f"{req_id}: {rel_path}::{name} (not a top-level "
                "FunctionDef / AsyncFunctionDef / ClassDef)"
            )


def check_forward(
    registry: dict[str, Any],
    findings: Findings,
) -> set[str]:
    """Validate every entry's tests[].path / names; return the set of valid REQ-IDs."""
    seen: dict[str, int] = {}
    valid_ids: set[str] = set()
    name_cache: dict[Path, set[str] | None] = {}

    for index, entry in enumerate(iter_requirements(registry), start=1):
        req_id = entry.get("id")
        if not isinstance(req_id, str):
            findings.missing_files.append(f"requirement #{index}: missing or non-string 'id' field")
            continue

        if req_id in seen:
            findings.duplicate_ids.append(
                f"{req_id}: appears at requirement #{seen[req_id]} and #{index}"
            )
            continue
        seen[req_id] = index
        valid_ids.add(req_id)

        tests = entry.get("tests") or []
        if not isinstance(tests, list):
            findings.missing_names.append(f"{req_id}: 'tests' field is not a list")
            continue

        if entry.get("status") == "formalized" and not tests:
            findings.formalized_without_tests.append(f"{req_id}: status=formalized but tests: []")

        for test_block in tests:
            _check_test_block(req_id, test_block, findings, name_cache)

    return valid_ids


# ---------------------------------------------------------------------------
# Reverse direction: tests -> registry (orphan @traces)
# ---------------------------------------------------------------------------


def _extract_traces_args(decorator: ast.expr) -> Iterable[tuple[str, int]]:
    """Yield (req_id, lineno) for any ``@pytest.mark.traces("X", ...)`` decorator."""
    if not isinstance(decorator, ast.Call):
        return
    func = decorator.func
    # Match pytest.mark.traces:
    #   Attribute('traces').value -> Attribute('mark').value -> Name('pytest')
    if not isinstance(func, ast.Attribute) or func.attr != "traces":
        return
    mark_node = func.value
    if not isinstance(mark_node, ast.Attribute) or mark_node.attr != "mark":
        return
    pytest_node = mark_node.value
    if not isinstance(pytest_node, ast.Name) or pytest_node.id != "pytest":
        return
    for arg in decorator.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            yield arg.value, decorator.lineno


_DecoratedNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _walk_decorated(tree: ast.Module) -> Iterator[_DecoratedNode]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node


def check_reverse(valid_ids: set[str], findings: Findings) -> dict[str, list[str]]:
    """Scan tests/ for @pytest.mark.traces decorators; flag orphans.

    Returns a {req_id: [locator, ...]} map of every traces marker seen,
    primarily for diagnostic purposes.
    """
    seen_markers: dict[str, list[str]] = defaultdict(list)
    if not TESTS_ROOT.is_dir():
        return seen_markers
    for test_file in sorted(TESTS_ROOT.rglob("*.py")):
        tree = _parse_test_file(test_file)
        if tree is None:
            continue
        rel = test_file.relative_to(REPO_ROOT)
        for node in _walk_decorated(tree):
            for decorator in node.decorator_list:
                for req_id, lineno in _extract_traces_args(decorator):
                    locator = f"{rel}:{lineno} ({node.name})"
                    seen_markers[req_id].append(locator)
                    if req_id not in valid_ids:
                        findings.orphan_traces.append(
                            f"{req_id} marked at {locator} not in registry"
                        )
    return seen_markers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    if not REGISTRY_PATH.is_file():
        sys.stderr.write(f"error: {REGISTRY_PATH} not found\n")
        return 2
    registry = load_registry(REGISTRY_PATH)
    findings = Findings()
    valid_ids = check_forward(registry, findings)
    seen_markers = check_reverse(valid_ids, findings)

    print(f"registry: {len(valid_ids)} unique REQ-IDs")
    print(
        f"@pytest.mark.traces: {sum(len(v) for v in seen_markers.values())} marker(s) "
        f"covering {len(seen_markers)} REQ-ID(s)"
    )
    print(findings.report())
    return 0 if findings.total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
