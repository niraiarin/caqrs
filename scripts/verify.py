"""Pre-merge local-preconditions checklist.

Per Codex's 2026-05-03 strategic review, ``just verify`` enforces only
local preconditions (not the cross-family review itself, which stays
orchestrator-driven manual until a reliable Codex-invocation channel
exists). This script:

1. Computes the diff vs ``main`` (changed files, LOC excluding
   tests/docs/lockfiles).
2. Detects ADR-0007 triggers heuristically:

   - **Trigger 2**: new module addition (new ``__init__.py`` outside
     a package that already had one)
   - **Trigger 3**: type-contract changes (``Protocol`` /
     ``BaseModel`` / ``StrictBaseModel`` subclass edits)
   - **Trigger 4**: ADR introduction (new file under
     ``docs/decisions/``)
   - **Trigger 5**: ≥ 200 LOC excluding ``tests/`` and ``docs/``
   - **Trigger 6**: live-broker (any change under
     ``src/caqrs/execution/live_broker*`` or
     ``src/caqrs/execution/protocol.py``)

   Trigger 1 (risk classification ≥ medium) is human judgement and is
   not auto-detected here.

3. Looks for ``docs/reviews/PR-<n>.md`` when ``--pr-number=N`` is
   passed (or detected from ``gh pr view``). If a triggering PR
   lacks the report, exit non-zero.
4. Prints the gate command list a verifier should run.

Run via the project's task runner::

    just verify                  # auto-detect via gh
    just verify 89               # explicit PR number

Or directly::

    uv run --frozen python scripts/verify.py --pr-number 89

The script is intentionally heuristic — it surfaces the work, it
does not perform the work.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAIN_BRANCH = "main"
_LOC_LIMIT_FOR_VERIFIER_TRIGGER = 200


@dataclass(frozen=True, slots=True)
class _Diff:
    added_paths: tuple[Path, ...]
    modified_paths: tuple[Path, ...]
    deleted_paths: tuple[Path, ...]
    insertions_excluding_tests_docs: int


@dataclass(frozen=True, slots=True)
class _TriggerHits:
    trigger2_new_module: tuple[Path, ...]
    trigger3_type_contract: tuple[Path, ...]
    trigger4_adr: tuple[Path, ...]
    trigger5_loc_threshold: bool
    trigger6_live_broker: tuple[Path, ...]

    def any_match(self) -> bool:
        return bool(
            self.trigger2_new_module
            or self.trigger3_type_contract
            or self.trigger4_adr
            or self.trigger5_loc_threshold
            or self.trigger6_live_broker,
        )


_NUMSTAT_COL_COUNT = 3


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return out.stdout


def _diff_vs_main() -> _Diff:
    """Compute file-level + LOC stats relative to ``main``."""
    name_status = _git("diff", "--name-status", f"{_MAIN_BRANCH}...HEAD")
    added: list[Path] = []
    modified: list[Path] = []
    deleted: list[Path] = []
    for line in name_status.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        target = Path(path)
        if status == "A":
            added.append(target)
        elif status == "M":
            modified.append(target)
        elif status == "D":
            deleted.append(target)
        # rename / copy modes (R/C): treat as modification on the new path
        elif status.startswith(("R", "C")):
            modified.append(target)

    numstat = _git("diff", "--numstat", f"{_MAIN_BRANCH}...HEAD")
    insertions = 0
    for line in numstat.splitlines():
        cols = line.split("\t")
        if len(cols) < _NUMSTAT_COL_COUNT:
            continue
        added_str, _deleted_str, path_str = cols[0], cols[1], cols[2]
        if added_str == "-":
            continue  # binary
        path = Path(path_str)
        if any(part in {"tests", "docs"} for part in path.parts):
            continue
        if path.name in {"uv.lock", "package-lock.json"}:
            continue
        insertions += int(added_str)
    return _Diff(
        added_paths=tuple(added),
        modified_paths=tuple(modified),
        deleted_paths=tuple(deleted),
        insertions_excluding_tests_docs=insertions,
    )


def _detect_triggers(diff: _Diff) -> _TriggerHits:
    new_modules: list[Path] = []
    type_contract: list[Path] = []
    adrs: list[Path] = []
    live_broker: list[Path] = []
    for path in diff.added_paths:
        parts = path.parts
        if path.name == "__init__.py" and "src" in parts:
            new_modules.append(path)
        if "decisions" in parts and parts[0] == "docs":
            adrs.append(path)
    # Type-contract: any modified file under src/ that contains a
    # Protocol / BaseModel / StrictBaseModel class. Heuristic: grep
    # the file content as it stands on HEAD.
    for path in (*diff.added_paths, *diff.modified_paths):
        if not (path.parts[:1] == ("src",) and path.suffix == ".py"):
            continue
        full = _REPO_ROOT / path
        if not full.is_file():
            continue
        text = full.read_text(encoding="utf-8")
        for marker in ("Protocol):", "(BaseModel)", "(StrictBaseModel)"):
            if marker in text:
                type_contract.append(path)
                break
    # Live broker: any change under src/caqrs/execution/live_broker*
    # or to the BrokerProtocol surface.
    for path in (*diff.added_paths, *diff.modified_paths):
        if any(p.startswith("live_broker") for p in path.parts):
            live_broker.append(path)
        if path == Path("src/caqrs/execution/protocol.py"):
            live_broker.append(path)
    return _TriggerHits(
        trigger2_new_module=tuple(new_modules),
        trigger3_type_contract=tuple(type_contract),
        trigger4_adr=tuple(adrs),
        trigger5_loc_threshold=(
            diff.insertions_excluding_tests_docs >= _LOC_LIMIT_FOR_VERIFIER_TRIGGER
        ),
        trigger6_live_broker=tuple(live_broker),
    )


def _resolve_pr_number(argv_pr: int | None) -> int | None:
    if argv_pr is not None:
        return argv_pr
    # Try gh pr view --json number to auto-detect.
    try:
        out = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    out_text = out.stdout.strip()
    if not out_text:
        return None
    try:
        return int(out_text)
    except ValueError:
        return None


def _verifier_report_path(pr_number: int) -> Path:
    return _REPO_ROOT / "docs" / "reviews" / f"PR-{pr_number}.md"


def _emit_section(title: str, lines: Iterable[str]) -> None:
    print(f"\n=== {title} ===")
    for line in lines:
        print(f"  {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-merge verification checklist")
    parser.add_argument(
        "--pr-number",
        type=int,
        default=None,
        help="PR number to look up the verifier report (auto-detected via gh if omitted)",
    )
    args = parser.parse_args()

    diff = _diff_vs_main()
    triggers = _detect_triggers(diff)

    _emit_section(
        "Diff stats vs main",
        [
            f"added:    {len(diff.added_paths)} file(s)",
            f"modified: {len(diff.modified_paths)} file(s)",
            f"deleted:  {len(diff.deleted_paths)} file(s)",
            f"insertions excluding tests/docs/lockfiles: {diff.insertions_excluding_tests_docs}",
        ],
    )

    trigger_lines: list[str] = []
    if triggers.trigger2_new_module:
        trigger_lines.append(
            f"trigger 2 (new module): {', '.join(str(p) for p in triggers.trigger2_new_module)}",
        )
    if triggers.trigger3_type_contract:
        trigger_lines.append(
            f"trigger 3 (type-contract): "
            f"{', '.join(str(p) for p in triggers.trigger3_type_contract)}",
        )
    if triggers.trigger4_adr:
        trigger_lines.append(
            f"trigger 4 (ADR): {', '.join(str(p) for p in triggers.trigger4_adr)}",
        )
    if triggers.trigger5_loc_threshold:
        trigger_lines.append(
            f"trigger 5 (LOC ≥ {_LOC_LIMIT_FOR_VERIFIER_TRIGGER}): "
            f"{diff.insertions_excluding_tests_docs} added (excl. tests/docs)",
        )
    if triggers.trigger6_live_broker:
        trigger_lines.append(
            f"trigger 6 (live-broker P4+): "
            f"{', '.join(str(p) for p in triggers.trigger6_live_broker)}",
        )
    if not trigger_lines:
        trigger_lines.append("none auto-detected (trigger 1 risk≥medium is human judgement)")
    _emit_section("ADR-0007 triggers (auto-detected)", trigger_lines)

    pr_number = _resolve_pr_number(args.pr_number)
    report_lines: list[str] = []
    report_missing = False
    if pr_number is None:
        report_lines.append(
            "PR number not specified and gh pr view did not return one — "
            "skipping verifier-report check (run after `gh pr create`).",
        )
    else:
        report_path = _verifier_report_path(pr_number)
        rel = report_path.relative_to(_REPO_ROOT)
        if report_path.is_file():
            report_lines.append(f"OK  {rel} present ({report_path.stat().st_size} bytes)")
        else:
            report_lines.append(f"MISSING  {rel}")
            if triggers.any_match():
                report_missing = True
                report_lines.append(
                    "    ADR-0007 trigger matched: this PR REQUIRES the verifier report. "
                    "See docs/reviews/_template.md.",
                )
            else:
                report_lines.append(
                    "    No auto-detected trigger matched; the report is optional but "
                    "still recommended if the diff is judgment-laden.",
                )
    _emit_section("Verifier report (ADR-0007)", report_lines)

    _emit_section(
        "Gate commands a verifier should run",
        [
            "just ci                                                  # all four gates + tests",
            "uv run --frozen ruff format --check .                    # format gate",
            "uv run --frozen ruff check .                             # lint gate",
            "uv run --frozen mypy src tests                           # type gate",
            "uv run --with pyyaml python scripts/check_traceability.py  # registry / tests trace",
            "python scripts/check_data_source_tos.py                  # TOS coverage",
            "uv run --frozen python scripts/check_credential_isolation.py  # NFR-LIVE-BROKER-2",
            "uv run --frozen pytest -q                                # full test suite",
        ],
    )

    print()
    if report_missing:
        print("FAIL  verifier report required by an ADR-0007 trigger is missing.")
        return 1
    print("OK  pre-merge preconditions look reasonable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
