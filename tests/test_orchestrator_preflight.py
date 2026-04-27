"""Tests for the pre-LLM warning preflight scanner."""

import pytest

from caqrs.orchestrator import (
    PreflightWarning,
    PreflightWarningKind,
    compose_preflight_message,
    scan_text_repetition,
    scan_tool_repetition,
)

# === scan_tool_repetition ===


def test_scan_tool_repetition_triggers_at_threshold() -> None:
    history = ["read_file", "read_file", "read_file"]
    warning = scan_tool_repetition(history)
    assert warning is not None
    assert warning.kind is PreflightWarningKind.REPEATED_TOOL
    assert "read_file" in warning.detail
    assert "3" in warning.detail


def test_scan_tool_repetition_below_threshold_returns_none() -> None:
    history = ["read_file", "read_file"]
    assert scan_tool_repetition(history) is None


def test_scan_tool_repetition_streak_must_be_consecutive() -> None:
    history = ["read_file", "read_file", "list_dir", "read_file"]
    # only 1 trailing read_file
    assert scan_tool_repetition(history) is None


def test_scan_tool_repetition_custom_threshold() -> None:
    history = ["search", "search"]
    warning = scan_tool_repetition(history, threshold=2)
    assert warning is not None
    assert warning.kind is PreflightWarningKind.REPEATED_TOOL


def test_scan_tool_repetition_rejects_non_positive_threshold() -> None:
    with pytest.raises(ValueError, match="positive"):
        scan_tool_repetition(["x"], threshold=0)


def test_scan_tool_repetition_handles_empty_history() -> None:
    assert scan_tool_repetition([]) is None


# === scan_text_repetition ===


def test_scan_text_repetition_triggers_on_three_similar_texts() -> None:
    msg = "I tried fetching the data but encountered the same error code as before."
    warning = scan_text_repetition([msg, msg, msg])
    assert warning is not None
    assert warning.kind is PreflightWarningKind.REPEATED_TEXT
    assert "3" in warning.detail


def test_scan_text_repetition_below_threshold_returns_none() -> None:
    msg = "I tried fetching the data but encountered the same error code as before."
    assert scan_text_repetition([msg, msg]) is None


def test_scan_text_repetition_ignores_dissimilar_texts() -> None:
    history = [
        "I will start by reading the source file carefully.",
        "Now I will run the test suite to verify expected behaviour.",
        "Finally I summarise findings into the report file.",
    ]
    assert scan_text_repetition(history) is None


def test_scan_text_repetition_filters_short_texts() -> None:
    # All three are below the 20-char minimum → eligible list is empty
    history = ["short one", "short two", "short three"]
    assert scan_text_repetition(history) is None


def test_scan_text_repetition_with_mixed_short_and_long() -> None:
    long = "I tried fetching the data but encountered the same error code as before."
    history = ["tiny", long, long, long]
    warning = scan_text_repetition(history)
    assert warning is not None  # short text filtered, three longs remain similar


def test_scan_text_repetition_streak_must_be_recent() -> None:
    similar = "I tried fetching the data but encountered the same error code as before."
    other = "Now I will run the test suite to verify expected behaviour fully."
    history = [similar, similar, similar, other]
    # most recent is `other`, which is not similar to its predecessor
    assert scan_text_repetition(history) is None


def test_scan_text_repetition_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="positive"):
        scan_text_repetition(["x"], threshold=0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        scan_text_repetition(["x"], similarity_threshold=1.5)


# === compose_preflight_message ===


def test_compose_returns_empty_when_no_warnings() -> None:
    assert compose_preflight_message([]) == ""


def test_compose_concatenates_with_double_newline() -> None:
    a = PreflightWarning(kind=PreflightWarningKind.REPEATED_TOOL, detail="A")
    b = PreflightWarning(kind=PreflightWarningKind.REPEATED_TEXT, detail="B")
    result = compose_preflight_message([a, b])
    assert result == "[PREFLIGHT WARNING] A\n\n[PREFLIGHT WARNING] B"


def test_warning_to_system_message_prefixes() -> None:
    w = PreflightWarning(kind=PreflightWarningKind.REPEATED_TOOL, detail="pattern detected")
    assert w.to_system_message() == "[PREFLIGHT WARNING] pattern detected"
