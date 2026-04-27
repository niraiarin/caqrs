"""Round-trip + validator tests for agent-output artifact schemas.

Covers ``ObserverInput``, ``ObserverArtifact``, ``SkepticReport``,
``AuditReport`` — the schemas added in P1.2.d-1 to complete the
agent-pipeline I/O contract.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from caqrs.schemas import (
    AcceptanceCheck,
    AssetSnapshot,
    AuditReport,
    AuditVerdict,
    DataDimension,
    FalsificationPath,
    ObserverArtifact,
    ObserverInput,
    RunMetadata,
    Severity,
    SkepticReport,
    SkepticVerdict,
    StrictBaseModel,
    new_run_id,
    utc_now,
)


def assert_roundtrip(model: StrictBaseModel) -> None:
    js = model.model_dump_json()
    restored = type(model).model_validate_json(js)
    assert restored == model


def make_meta(now: datetime, agent: str = "test-agent") -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name=agent,
        model_id="test",
        created_at=now,
    )


# === ObserverInput ===


def test_observer_input_roundtrip() -> None:
    obs_input = ObserverInput(
        universe=("AAPL", "MSFT"),
        as_of=utc_now(),
        horizon_days=30,
        dimensions=(DataDimension.PRICES, DataDimension.NEWS),
    )
    assert_roundtrip(obs_input)


def test_observer_input_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        ObserverInput(
            universe=("AAPL",),
            as_of=datetime(2026, 1, 1),  # naive
            horizon_days=30,
            dimensions=(DataDimension.PRICES,),
        )


def test_observer_input_rejects_duplicate_universe() -> None:
    with pytest.raises(ValidationError):
        ObserverInput(
            universe=("AAPL", "AAPL"),
            as_of=utc_now(),
            horizon_days=30,
            dimensions=(DataDimension.PRICES,),
        )


def test_observer_input_rejects_duplicate_dimensions() -> None:
    with pytest.raises(ValidationError):
        ObserverInput(
            universe=("AAPL",),
            as_of=utc_now(),
            horizon_days=30,
            dimensions=(DataDimension.PRICES, DataDimension.PRICES),
        )


# === ObserverArtifact ===


def test_observer_artifact_roundtrip_minimal() -> None:
    now = utc_now()
    artifact = ObserverArtifact(
        metadata=make_meta(now, "observer"),
        universe=("AAPL", "MSFT"),
        as_of=now,
        regime_summary="Low-vol bullish; SPX +2% MTD with realized vol below long-run mean.",
    )
    assert_roundtrip(artifact)


def test_observer_artifact_with_full_payload() -> None:
    now = utc_now()
    artifact = ObserverArtifact(
        metadata=make_meta(now, "observer"),
        universe=("AAPL", "MSFT"),
        as_of=now,
        regime_summary="Mixed-vol; momentum factor leading.",
        asset_snapshots=(
            AssetSnapshot(
                ticker="AAPL",
                return_1m=Decimal("0.04"),
                return_12m=Decimal("0.18"),
                volatility_30d=Decimal("0.22"),
                last_close=Decimal("182.45"),
            ),
            AssetSnapshot(ticker="MSFT", note="data delayed"),
        ),
        news_themes=("AAPL Q1 earnings beat", "MSFT Azure outage"),
        macro_notes="VIX 14, 10Y yield 4.2%, USDJPY 152.",
        data_quality_notes=("MSFT realtime feed lagging by ~5min",),
    )
    assert_roundtrip(artifact)


def test_observer_artifact_rejects_snapshot_outside_universe() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="not in universe"):
        ObserverArtifact(
            metadata=make_meta(now, "observer"),
            universe=("AAPL",),
            as_of=now,
            regime_summary="ok",
            asset_snapshots=(AssetSnapshot(ticker="MSFT"),),
        )


def test_observer_artifact_rejects_duplicate_snapshot_tickers() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="duplicate tickers"):
        ObserverArtifact(
            metadata=make_meta(now, "observer"),
            universe=("AAPL", "MSFT"),
            as_of=now,
            regime_summary="ok",
            asset_snapshots=(
                AssetSnapshot(ticker="AAPL"),
                AssetSnapshot(ticker="AAPL"),
            ),
        )


def test_observer_artifact_rejects_duplicate_universe() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="duplicates"):
        ObserverArtifact(
            metadata=make_meta(now, "observer"),
            universe=("AAPL", "AAPL"),
            as_of=now,
            regime_summary="ok",
        )


# === SkepticReport ===


def _ok_falsification_path(severity: Severity = Severity.MEDIUM) -> FalsificationPath:
    return FalsificationPath(
        description="Strategy may break down in high-vol regimes.",
        severity=severity,
        evidence_marker="aggregate.median_sharpe drops below threshold when VIX>30",
    )


def test_skeptic_report_proceed_roundtrip() -> None:
    now = utc_now()
    report = SkepticReport(
        metadata=make_meta(now, "skeptic"),
        hypothesis_run_id=new_run_id(),
        verdict=SkepticVerdict.PROCEED,
        falsification_paths=(_ok_falsification_path(Severity.LOW),),
        concerns=("Backtest window is short.",),
        summary="No fatal flaws. Recommend backtest.",
    )
    assert_roundtrip(report)


def test_skeptic_report_kill_with_fatal_path_roundtrip() -> None:
    now = utc_now()
    report = SkepticReport(
        metadata=make_meta(now, "skeptic"),
        hypothesis_run_id=new_run_id(),
        verdict=SkepticVerdict.KILL,
        falsification_paths=(_ok_falsification_path(Severity.FATAL),),
        summary="Fatal: signal is unavailable in real-time.",
    )
    assert_roundtrip(report)


def test_skeptic_report_kill_requires_fatal_or_concern() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="FATAL"):
        SkepticReport(
            metadata=make_meta(now, "skeptic"),
            hypothesis_run_id=new_run_id(),
            verdict=SkepticVerdict.KILL,
            falsification_paths=(_ok_falsification_path(Severity.LOW),),
            concerns=(),
            summary="Killed.",
        )


def test_skeptic_report_proceed_rejects_fatal_path() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="proceed"):
        SkepticReport(
            metadata=make_meta(now, "skeptic"),
            hypothesis_run_id=new_run_id(),
            verdict=SkepticVerdict.PROCEED,
            falsification_paths=(_ok_falsification_path(Severity.FATAL),),
            summary="Inconsistent.",
        )


# === AuditReport ===


def _passing_check() -> AcceptanceCheck:
    return AcceptanceCheck(
        metric_path="aggregate.median_sharpe",
        op=">=",
        threshold=Decimal("0.5"),
        actual=Decimal("0.7"),
        passed=True,
    )


def _failing_check() -> AcceptanceCheck:
    return AcceptanceCheck(
        metric_path="aggregate.worst_fold_sharpe",
        op=">=",
        threshold=Decimal("0.0"),
        actual=Decimal("-0.3"),
        passed=False,
    )


def test_audit_report_pass_roundtrip() -> None:
    now = utc_now()
    report = AuditReport(
        metadata=make_meta(now, "auditor"),
        hypothesis_run_id=new_run_id(),
        backtest_run_id=new_run_id(),
        verdict=AuditVerdict.PASS,
        checks=(_passing_check(),),
        rationale="All acceptance criteria cleared.",
    )
    assert_roundtrip(report)


def test_audit_report_fail_roundtrip() -> None:
    now = utc_now()
    report = AuditReport(
        metadata=make_meta(now, "auditor"),
        hypothesis_run_id=new_run_id(),
        backtest_run_id=new_run_id(),
        verdict=AuditVerdict.FAIL,
        checks=(_passing_check(), _failing_check()),
        rationale="Worst-fold Sharpe negative; rejecting.",
    )
    assert_roundtrip(report)


def test_audit_report_pass_requires_all_checks_pass() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="pass requires all"):
        AuditReport(
            metadata=make_meta(now, "auditor"),
            hypothesis_run_id=new_run_id(),
            backtest_run_id=new_run_id(),
            verdict=AuditVerdict.PASS,
            checks=(_passing_check(), _failing_check()),
            rationale="x",
        )


def test_audit_report_fail_requires_at_least_one_check_fail() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="fail requires"):
        AuditReport(
            metadata=make_meta(now, "auditor"),
            hypothesis_run_id=new_run_id(),
            backtest_run_id=new_run_id(),
            verdict=AuditVerdict.FAIL,
            checks=(_passing_check(),),
            rationale="x",
        )


def test_audit_report_requires_at_least_one_check() -> None:
    now = utc_now()
    with pytest.raises(ValidationError):
        AuditReport(
            metadata=make_meta(now, "auditor"),
            hypothesis_run_id=new_run_id(),
            backtest_run_id=new_run_id(),
            verdict=AuditVerdict.PASS,
            checks=(),
            rationale="x",
        )


# === Cross-cutting: horizon-window correctness across multiple schemas ===


def test_observer_input_horizon_within_bounds() -> None:
    now = utc_now()
    obs_input = ObserverInput(
        universe=("AAPL",),
        as_of=now,
        horizon_days=3650,  # max
        dimensions=(DataDimension.PRICES,),
    )
    assert obs_input.horizon_days == 3650
    assert obs_input.as_of + timedelta(days=obs_input.horizon_days) > now
