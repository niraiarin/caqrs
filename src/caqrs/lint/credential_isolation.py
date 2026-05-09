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

import ast
from collections import deque
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
    graph = _walk_modules(src_root)
    violations: list[CredentialViolation] = []
    for boundary in boundaries:
        if boundary.module not in graph:
            # Skip silently — the lint stays useful before LiveBroker lands.
            continue
        reachable = _bfs_reachable(graph=graph, root=boundary.module)
        for module_name, import_path in reachable.items():
            module_info = graph[module_name]
            for read in module_info.env_reads:
                if read.name is None:
                    # Dynamic read; cannot statically classify. Out of scope
                    # for this lint — caught by NFR-SEC-1's manual audit.
                    continue
                for prefix in boundary.forbidden_prefixes:
                    if read.name.startswith(prefix):
                        violations.append(
                            CredentialViolation(
                                boundary=boundary.module,
                                forbidden_prefix=prefix,
                                env_var=read.name,
                                site_module=module_name,
                                import_path=import_path,
                            ),
                        )
                        break  # one violation per (read, boundary)
    violations.sort(key=lambda v: (v.boundary, v.env_var, v.site_module))
    return tuple(violations)


# ---------------------------------------------------------------------------
# Internal: module discovery + AST extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EnvRead:
    """One env-var read inside a module. ``name`` is ``None`` when the
    arg could not be statically resolved (dynamic computation, attribute
    access on a non-trivial expression, etc.); such reads are not
    flagged because the lint cannot prove they violate a prefix."""

    name: str | None
    line: int


@dataclass(frozen=True, slots=True)
class _ModuleInfo:
    name: str
    path: Path
    imports: frozenset[str]
    env_reads: tuple[_EnvRead, ...]


def _walk_modules(src_root: Path) -> dict[str, _ModuleInfo]:
    """Build the module graph by walking ``*.py`` files under ``src_root``.

    Dotted name resolution: ``<src_root>/<a>/<b>/<c>.py`` becomes
    ``a.b.c``. ``__init__.py`` files represent the enclosing package
    (``<src_root>/<a>/<b>/__init__.py`` becomes ``a.b``). Files whose
    parsing fails (syntax error / encoding issue) are skipped — the
    mypy-strict CI gate catches those independently.
    """
    modules: dict[str, _ModuleInfo] = {}
    if not src_root.is_dir():
        return modules
    for path in sorted(src_root.rglob("*.py")):
        rel_parts = list(path.relative_to(src_root).parts)
        if rel_parts[-1] == "__init__.py":
            rel_parts = rel_parts[:-1]
        else:
            rel_parts[-1] = rel_parts[-1][:-3]
        if not rel_parts:
            continue
        dotted = ".".join(rel_parts)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        modules[dotted] = _ModuleInfo(
            name=dotted,
            path=path,
            imports=_extract_imports(tree=tree, current=dotted),
            env_reads=_extract_env_reads(tree),
        )
    return modules


def _extract_imports(*, tree: ast.Module, current: str) -> frozenset[str]:
    """Collect import edges from one module's AST.

    For ``from a.b import c, d``, three edges are emitted: ``a.b``,
    ``a.b.c``, ``a.b.d``. The downstream BFS prunes edges whose target
    is not in the audited graph, so over-reporting at this stage is
    safe and avoids the "is c a submodule or a name?" disambiguation.

    Relative imports (``from . import x`` / ``from ..pkg import y``)
    are resolved against ``current``'s dotted path.
    """
    edges: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                base_parts = current.split(".")[: -node.level] if current else []
                base = ".".join(base_parts)
            else:
                base = ""
            module_part = node.module or ""
            if base and module_part:
                full = f"{base}.{module_part}"
            elif base:
                full = base
            elif module_part:
                full = module_part
            else:
                continue  # unreachable in well-formed code
            edges.add(full)
            for alias in node.names:
                if alias.name == "*":
                    continue
                edges.add(f"{full}.{alias.name}")
    return frozenset(edges)


def _extract_env_reads(tree: ast.Module) -> tuple[_EnvRead, ...]:
    """Collect every ``os.environ[...]`` / ``os.environ.get(...)`` /
    ``os.getenv(...)`` site, resolving the env var name where possible.

    Resolution order for the var-name argument:

    1. ``ast.Constant(str)`` literal — name is the literal string.
    2. ``ast.Name`` referring to a function-parameter with a string
       default — name is that default. This catches the prevailing
       ``def from_env(*, env_var: str = "EDINET_API_KEY"): os.environ.get(env_var, "")``
       pattern in :mod:`caqrs.data.edinet.client`.
    3. Anything else — recorded as ``name=None`` (dynamic). The audit
       skips dynamic reads because static analysis cannot prove a
       prefix match.
    """
    collector = _EnvReadCollector()
    collector.visit(tree)
    return tuple(collector.reads)


class _EnvReadCollector(ast.NodeVisitor):
    """Scope-aware AST visitor for env-var reads."""

    def __init__(self) -> None:
        self.scopes: list[dict[str, str]] = [{}]
        self.reads: list[_EnvRead] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scopes.append(_collect_param_defaults(node.args))
        self.generic_visit(node)
        self.scopes.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scopes.append(_collect_param_defaults(node.args))
        self.generic_visit(node)
        self.scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if _is_env_read_call(node) and node.args:
            name = self._resolve(node.args[0])
            self.reads.append(_EnvRead(name=name, line=node.lineno))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_environ_subscript(node):
            name = self._resolve(node.slice)
            self.reads.append(_EnvRead(name=name, line=node.lineno))
        self.generic_visit(node)

    def _resolve(self, expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        if isinstance(expr, ast.Name):
            for scope in reversed(self.scopes):
                if expr.id in scope:
                    return scope[expr.id]
        return None


def _collect_param_defaults(args: ast.arguments) -> dict[str, str]:
    """Map each parameter with a string-literal default to that string.

    Covers positional args (``args.args`` aligned with the trailing
    portion of ``args.defaults``) and keyword-only args
    (``args.kwonlyargs`` aligned 1:1 with ``args.kw_defaults``, where
    ``None`` means "no default").
    """
    defaults: dict[str, str] = {}
    pos_args = list(args.args)
    pos_defaults = list(args.defaults)
    if pos_defaults:
        offset = len(pos_args) - len(pos_defaults)
        for arg, default in zip(pos_args[offset:], pos_defaults, strict=True):
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                defaults[arg.arg] = default.value
    for arg, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if (
            kw_default is not None
            and isinstance(kw_default, ast.Constant)
            and isinstance(kw_default.value, str)
        ):
            defaults[arg.arg] = kw_default.value
    return defaults


def _is_env_read_call(node: ast.Call) -> bool:
    """True iff ``node`` is ``os.environ.get(...)`` or ``os.getenv(...)``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "get":
        value = func.value
        if isinstance(value, ast.Attribute) and value.attr == "environ":
            inner = value.value
            return isinstance(inner, ast.Name) and inner.id == "os"
        return False
    if func.attr == "getenv":
        inner = func.value
        return isinstance(inner, ast.Name) and inner.id == "os"
    return False


def _is_environ_subscript(node: ast.Subscript) -> bool:
    """True iff ``node`` is ``os.environ[...]``."""
    value = node.value
    if not isinstance(value, ast.Attribute):
        return False
    if value.attr != "environ":
        return False
    inner = value.value
    return isinstance(inner, ast.Name) and inner.id == "os"


# ---------------------------------------------------------------------------
# Internal helpers: reachability BFS
# ---------------------------------------------------------------------------


def _bfs_reachable(
    *,
    graph: dict[str, _ModuleInfo],
    root: str,
) -> dict[str, tuple[str, ...]]:
    """BFS over the module graph starting at ``root``. Returns
    ``{module_name: (root, ..., module_name)}`` for every first-party
    module reachable from ``root``. Edges leaving the graph (third-party
    imports) are pruned silently.
    """
    paths: dict[str, tuple[str, ...]] = {root: (root,)}
    queue: deque[str] = deque([root])
    while queue:
        current = queue.popleft()
        info = graph.get(current)
        if info is None:
            continue
        for target in sorted(info.imports):
            if target in paths or target not in graph:
                continue
            paths[target] = (*paths[current], target)
            queue.append(target)
    return paths
