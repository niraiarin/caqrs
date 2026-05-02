# Non-functional requirements catalogue

All NFR-IDs in this catalogue are also in `docs/requirements/registry.yaml`. Each
NFR has a measurable target so that compliance can be checked at CI time, at
runtime, or via a documented manual audit. NFRs without a measurable target are
marked `status: deferred` and tracked for promotion.

## Conventions

- IDs are stable: `NFR-<CATEGORY>-N`.
- Each NFR has: title, target metric, threshold, measurement method, gate
  location, and the owning task / ADR responsible for promoting the gate from
  manual to automated where applicable.
- Threshold may be `"TBD"` only when explicitly deferred to a future phase
  (the deferral rationale is recorded in the row); otherwise it must be numeric
  or a specific predicate.
- Categories follow the ISO/IEC 25010 quality model, adapted to this repo's
  research-orchestrator scope.
- Status semantics in `registry.yaml`:
  - `formalized` — measurable threshold AND an automated gate enforce it today.
  - `partial` — measurable threshold exists; gate is either manual, scoped to
    a unit test, or pending a follow-up task (typically #80 / #83 / #88 / #89).
  - `deferred` — threshold itself is not yet pinned; the row points to the
    follow-up that will pin it.
  - `tacit` — behaviour is implicit in code or in module docstrings without a
    dedicated catalogue entry; promotion path is documented in the row's notes.

## Categories

The 12 categories below cover the surface area surveyed by the Codex audit
(performance, security, reliability, maintainability, observability,
scalability, compatibility, compliance, cost, auditability, portability,
usability). Every NFR-ID present in `docs/requirements/registry.yaml` is
catalogued here.

### Performance

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-PERF-1 | Issuer lookup p95 latency | wall-clock latency of `EntityStore.lookup_issuer` | ≤ 1 ms in-memory and ≤ 5 ms DuckDB at 10k issuers | `pytest-benchmark` round under `tests/perf/` | CI `perf` job (separate from default suite) | Task #89 |
| NFR-PERF-2 | Cycle wall-clock budget (stub providers) | `CycleRunner.run()` median latency with stub agents and no real I/O | ≤ 5 s median, ≤ 10 s p95 (no LLM, no network) | `pytest-benchmark` in `tests/perf/test_cycle_latency.py` | CI `perf` job | Task #89 |
| NFR-PERF-3 | Walk-forward engine throughput | `run_walk_forward` folds/sec at universe = 200, history = 2 y | ≥ 0.5 fold/s on CI baseline runner (single-core) | `pytest-benchmark` in `tests/perf/` | CI `perf` job | Task #89 |

Notes:

- The 5 s / 10 s budgets in NFR-PERF-2 are deliberately generous because the
  CI `perf` runner is shared. They exist to catch order-of-magnitude
  regressions, not micro-optimisation. Tighter intra-platform budgets can be
  added once #89 lands and we have a local baseline distribution to compare
  against.
- Real-LLM cycles are tracked but unbounded — token + wall-clock caps are the
  job of `BudgetGuard` (see NFR-COST-2), not this NFR.

### Security

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-SEC-1 | Subscription credentials never extracted; only CLI session paths read | set of credential read sites in `src/caqrs/providers/` | sites ⊆ {`~/.claude`, `~/.codex/sessions`, macOS Keychain via `_cli_creds.real_keychain_reader`} | manual code audit + `rg` regex over `src/` | manual audit per release; promote to ruff custom rule in #80 | ADR-0002 / ADR-0003 |
| NFR-SEC-2 | FastAPI surface does not leak credentials in errors or in `openapi.json` | substring scan of `openapi.json` and 4xx/5xx response bodies for credential-shaped tokens | 0 matches against the credential regex (env-var names, token prefixes, base64 32+ chars in known fields) | `schemathesis` fuzz suite + a dedicated unit test that scans the rendered OpenAPI schema | CI (`schemathesis` step in the API job) | Task #89 |

### Reliability

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-REL-1 | Graceful degradation: provider / data / broker failure never crashes the runner | `CycleRunner.run()` outcome on every external failure mode | every external failure produces a `CycleResult` with `aborted_reason` set; 0 unhandled exceptions escape `run()` | exhaustive negative tests covering provider error, data fetch error, broker error, budget exceeded, validation error | CI (existing `pytest` suite); promote to a `@traces` cross-check in #80 | existing fallback path in `caqrs.providers` |
| NFR-REL-2 | Daily quota cap respected even after process restart | rolling 24-hour HTTP request count from `caqrs.data.edinetdb.quota` | ≤ 100 requests / 24 h (free-tier default; configurable per deployment) | `test_log_persists_across_instances` plus a future end-to-end soak in `tests/perf/` | CI (unit test today); soak in #89 | `quota.py` persistence layer |

### Maintainability

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-MAINT-1 | Strict type-checking gate on `src/` | `mypy --strict` pass on `src/` | 0 errors | `uv run --frozen mypy src` | CI (existing `Type check` step in `.github/workflows/ci.yml`) | already enforced |
| NFR-MAINT-2 | Lint + format gate | `ruff format --check .` and `ruff check .` (full ruleset incl. `PERF`, `PL`, `B`, `SIM`, `ANN`, `RUF`) | 0 violations, 0 reformat hits | `uv run --frozen ruff format --check .` + `uv run --frozen ruff check .` | CI (existing `Format check` and `Lint` steps) | already enforced |

### Observability

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-OBS-1 | Every agent invocation emits paired `AGENT_INVOKED` and `AGENT_SUCCEEDED`/`AGENT_FAILED` | ratio of `AGENT_INVOKED` to `AGENT_SUCCEEDED ∪ AGENT_FAILED` events per cycle | exactly 1:1 (no lost events, no orphan invocations) | runner-level integration test asserting the pairing on a cycle that uses every agent slot | CI (existing runner tests); promote to a `@traces` cross-check in #80 / #88 | `caqrs.orchestrator.event_log.EventLog` |
| NFR-OBS-2 | `EventLog` JSONL persistence is loss-less | round-trip equality of in-memory event sequence vs. `EventLog.load_jsonl()` of the persisted file | byte-for-byte equal sequence | `test_load_jsonl_round_trips` in `tests/test_orchestrator_event_log.py` | CI (existing unit test) | `EventLog.persist_to=` + `load_jsonl` |

### Scalability

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-SCALE-1 | DuckDB store handles representative working-set without quadratic blow-up | wall-clock for `list_all_market_points` and `filings_for` at 1 M `MarketPoint` rows / 100 k `Issuer` rows | ≤ 2 s for `list_all_market_points`, ≤ 50 ms p95 for a single `filings_for` lookup | `pytest-benchmark` against a synthetic seed under `tests/perf/` | CI `perf` job | Task #89 |

Note: the 1 M / 100 k figures are the upper end of the planned Phase E2/E3
working set. Any growth beyond that requires a fresh measurement plus an
ADR amendment to ADR-0006 and the entities spec.

### Compatibility

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-COMPAT-1 | Python and core-dep version floor | `pyproject.toml` `requires-python` and pinned dep ranges | Python ≥ 3.12 (CI matrix covers 3.12 + 3.13); pydantic ≥ 2.7 < 3; httpx ≥ 0.27 < 1 | `uv lock` + CI matrix in `.github/workflows/ci.yml` | CI (existing matrix) | already enforced |
| NFR-COMPAT-2 | Cache TTLs match upstream change cadence (EDINET DB) | `DEFAULT_*_TTL_SECONDS` constants in `src/caqrs/data/edinetdb/cache.py` | companies 7 d, financials 30 d, rankings 7 d (verbatim from the cache module's docstring rationale) | unit tests pin the defaults; any change requires an ADR-style note in the PR | CI (existing unit tests) | `cache.py` module + tests |

### Compliance

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-COMPLY-1 | ToS-compliant credential reuse (Anthropic / OpenAI subscription posture) | credential paths read by provider adapters | paths ⊆ {`~/.claude`, `~/.codex/sessions`, macOS Keychain}; **no** OAuth-token extraction, **no** browser-cookie scraping | manual audit + ADR-0002 / ADR-0003 cross-check on every provider PR | manual audit; promote to `rg` lint in #80 | ADR-0002, ADR-0003 |
| NFR-COMPLY-2 | Public, unauthenticated API usage only for Polymarket | count of auth headers / signed requests in CLOB, Gamma, Archive clients | exactly 0 | `rg -n "Authorization\|X-API-Key\|Sign-" src/caqrs/data/polymarket/` returns no hits | manual audit per release; promote to ruff custom rule in #80 | implicit in `polymarket/clob_client.py` docstring |
| NFR-COMPLY-3 | Free-tier rate limit honored (J-Quants) | `AsyncRateLimiter.min_interval_seconds` default in `caqrs.data.jquants.client` | 12.0 s (= 5 req / min) | `test_default_rate_limiter_min_interval_is_free_tier_pacing` | CI (existing unit test) | `_FREE_TIER_MIN_INTERVAL_SECONDS` constant |

Compliance scope notes:

- This repo stores **no PII**. All ingested signals are public market data,
  public filings, or public prediction-market quotes. GDPR-style review only
  re-engages when the source list grows to include user-keyed data (PRAW,
  Twitter); the placeholder entries in `LICENSE_AND_TOS.md` flag those for
  re-evaluation before integration.
- The `LICENSE_AND_TOS.md` audit ledger is the canonical compliance log.
  Promotion of "every integrated source has a non-placeholder row" to a CI
  lint is Task #83.

### Cost

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-COST-1 | LLM monthly token budget (subscription mode) | aggregate `token_in + token_out` per provider per calendar month from `RunMetadata` | TBD per subscription tier | `EventLog` rollup; future telemetry sink | manual review of cycle archive until a telemetry sink is wired | deferred — depends on production deployment shape; deferral kept explicit |
| NFR-COST-2 | Per-cycle token cap (`CycleBudget`) bounds runaway cost | `CycleBudget.token_cap` enforcement via `BudgetGuard` | caller-provided per cycle; `BudgetGuard` emits `BUDGET_EXCEEDED` and aborts the cycle on first breach | `BudgetGuard` unit tests in `tests/test_orchestrator_budget.py` | CI (existing unit tests) | already enforced |

### Auditability

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-AUDIT-1 | `RunMetadata` lineage chain is fully reconstructable | for every artifact, parent chain via `parent_id` resolves to a root with no orphan link | 0 orphan `parent_id` references in any persisted cycle | integration test that walks every artifact in a recorded cycle and asserts root-reachability | CI (Task #89 lands the integration test) | `caqrs.orchestrator` + `caqrs.memory.CycleStore` |
| NFR-AUDIT-2 | `Provenance.payload_hash` is the canonical SHA-256 of the original upstream JSON | `hashlib.sha256(canonical_json(payload)).hexdigest()` matches the stored hash | byte-for-byte equality | `test_market_point_provenance_preserves_jquants_payload_hash` (and equivalent tests on each integrated source) | CI (existing unit tests) | `caqrs.entities.types.Provenance` |

### Portability

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-PORT-1 | Tests pass on the supported OS × Python matrix | green CI on every cell of {ubuntu-latest, macos-latest} × {3.12, 3.13} | 0 platform-specific failures | `.github/workflows/ci.yml` matrix | CI (existing matrix) | already enforced |
| NFR-PORT-2 | Keychain reader degrades to `None` on non-Darwin | return value of `real_keychain_reader` outside macOS | always `None` (never raises, never blocks the loop) | `test_real_keychain_reader_returns_none_on_non_darwin` | CI (existing unit test) | `caqrs.providers._cli_creds.real_keychain_reader` |

### Usability — developer

| ID | Title | Metric | Threshold | Measurement | Gate | Enforcement |
|---|---|---|---|---|---|---|
| NFR-UX-1 | OpenAPI schema includes core artifact schemas | `openapi.json` `components.schemas` entries | covers the public artifact set: `ObserverInput`, `StrategyDecision`, `BacktestReport`, `RunMetadata`, `Provenance`, `CycleResult` (extend as the public surface grows) | `test_core_schemas_appear_in_openapi_components` in `tests/test_api_app_skeleton.py` | CI (existing unit test) | `caqrs.api` schema export |
| NFR-UX-2 | `AuthError` message names the exact renewal command | error string from `caqrs.providers.errors.AuthError` on pre-flight expiry | message contains the literal renewal command (e.g. `claude login`, `codex login`) | `test_complete_raises_when_cred_expired` for both `anthropic_cli` and `codex_cli` providers | CI (existing unit tests) | `caqrs.providers.errors` |
