# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- P0: Artifact schemas (`HypothesisCard`, `ResearchPlan`, `BacktestReport`,
  `StrategyDecision`) with pydantic v2 strict validation, frozen, `extra="forbid"`.
- P0: `Agent` protocol and `AgentResult` wrapper (structural typing,
  `runtime_checkable`).
- P0: Property-based round-trip tests via Hypothesis.
- P0: GitHub Actions CI matrix (Python 3.12 / 3.13 × Ubuntu / macOS).
- P0: Apache-2.0 license, NOTICE, ARCHITECTURE / lineage / ADR-0001 docs.
- P0: `LICENSE_AND_TOS.md` audit scaffold for forthcoming data sources.
