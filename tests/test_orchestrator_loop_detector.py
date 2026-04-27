"""Tests for ToolCallLoopDetector."""

from caqrs.orchestrator import LoopDetection, ToolCallLoopDetector

# === record / counters ===


def test_total_calls_increments_on_record() -> None:
    d = ToolCallLoopDetector()
    d.record("read_file", {"path": "a"})
    d.record("read_file", {"path": "b"})
    assert d.total_calls == 2


def test_record_resets_no_action_counter() -> None:
    d = ToolCallLoopDetector()
    d.record_no_action()
    d.record_no_action()
    d.record("read_file", {"path": "a"})
    assert d.record_no_action() is False


def test_no_action_caps_at_threshold() -> None:
    d = ToolCallLoopDetector()
    for _ in range(4):
        assert d.record_no_action() is False
    assert d.record_no_action() is True


# === absolute limit ===


def test_absolute_limit_total_calls() -> None:
    d = ToolCallLoopDetector()
    for i in range(24):
        d.record("read_file", {"i": i})
    assert d.detect_absolute_limit() is False
    d.record("read_file", {"i": 24})
    assert d.detect_absolute_limit() is True


def test_absolute_limit_failed_calls() -> None:
    d = ToolCallLoopDetector()
    for i in range(11):
        d.record("run_command", {"i": i}, failed=True)
    assert d.detect_absolute_limit() is False
    d.record("run_command", {"i": 11}, failed=True)
    assert d.detect_absolute_limit() is True


# === identical loop ===


def test_identical_loop_triggers_at_three() -> None:
    d = ToolCallLoopDetector()
    d.record("approve_scope", {"path": "/foo"})
    d.record("approve_scope", {"path": "/foo"})
    assert d.detect_identical() is None
    d.record("approve_scope", {"path": "/foo"})
    result = d.detect_identical()
    assert isinstance(result, LoopDetection)
    assert result.tool == "approve_scope"
    assert result.count == 3
    assert d.hard_aborted is True


def test_identical_loop_does_not_trigger_when_params_differ() -> None:
    d = ToolCallLoopDetector()
    d.record("read_file", {"path": "/a"})
    d.record("read_file", {"path": "/b"})
    d.record("read_file", {"path": "/c"})
    assert d.detect_identical() is None


def test_identical_loop_recovers_after_different_call() -> None:
    d = ToolCallLoopDetector()
    d.record("approve_scope", {"path": "/foo"})
    d.record("approve_scope", {"path": "/foo"})
    d.record("read_file", {"path": "/bar"})  # breaks the streak
    d.record("approve_scope", {"path": "/foo"})
    d.record("approve_scope", {"path": "/foo"})
    assert d.detect_identical() is None  # only 2 consecutive identical


# === similar-failing loop ===


def test_similar_failing_triggers_at_four() -> None:
    d = ToolCallLoopDetector()
    for i in range(4):
        d.record("git_push", {"i": i}, failed=True)
    result = d.detect_similar_failing()
    assert result is not None
    assert result.tool == "git_push"
    assert result.count == 4
    assert d.hard_aborted is True


def test_similar_failing_requires_failure_on_last() -> None:
    d = ToolCallLoopDetector()
    for i in range(4):
        d.record("git_push", {"i": i}, failed=True)
    d.record("git_push", {"i": 4}, failed=False)  # success breaks the streak
    assert d.detect_similar_failing() is None


def test_similar_failing_below_threshold_returns_none() -> None:
    d = ToolCallLoopDetector()
    for i in range(3):
        d.record("git_push", {"i": i}, failed=True)
    assert d.detect_similar_failing() is None


# === same-tool consecutive (soft) ===


def test_same_tool_normal_triggers_at_three() -> None:
    d = ToolCallLoopDetector()
    for i in range(3):
        d.record("write_file", {"i": i})
    result = d.detect_same_tool()
    assert result is not None
    assert result.tool == "write_file"
    assert result.count == 3


def test_same_tool_high_tolerance_triggers_at_five() -> None:
    d = ToolCallLoopDetector()
    for i in range(4):
        d.record("read_file", {"i": i})
    assert d.detect_same_tool() is None
    d.record("read_file", {"i": 4})
    result = d.detect_same_tool()
    assert result is not None
    assert result.count == 5


def test_same_tool_threshold_drops_when_three_failures() -> None:
    d = ToolCallLoopDetector()
    # 3 failures total in window → threshold drops to 3 (from 5)
    for i in range(3):
        d.record("read_file", {"i": i}, failed=True)
    result = d.detect_same_tool()
    assert result is not None
    assert result.count == 3


# === text repetition ===


def test_text_repetition_triggers_on_three_similar_steps() -> None:
    d = ToolCallLoopDetector()
    for _ in range(3):
        d.record_step_text("I tried fetching the data but encountered the same error.")
    result = d.detect_text_repetition()
    assert result is not None
    assert result.count == 3


def test_text_repetition_below_threshold_returns_none() -> None:
    d = ToolCallLoopDetector()
    d.record_step_text("First attempt at fetching the data.")
    d.record_step_text("Second attempt at fetching the data.")
    assert d.detect_text_repetition() is None


def test_text_repetition_ignores_dissimilar_texts() -> None:
    d = ToolCallLoopDetector()
    d.record_step_text("I will start by reading the source file carefully.")
    d.record_step_text("Now I will run the test suite to verify behaviour.")
    d.record_step_text("Finally I summarise findings into the report file.")
    assert d.detect_text_repetition() is None


def test_record_step_text_ignores_short_or_empty() -> None:
    d = ToolCallLoopDetector()
    d.record_step_text("")
    d.record_step_text("short")  # length < 10
    d.record_step_text("ten chars!")  # exactly 10 — kept
    for _ in range(2):
        d.record_step_text("ten chars!")
    result = d.detect_text_repetition()
    assert result is not None  # 3 identical "ten chars!" entries


# === reset ===


def test_reset_clears_all_state() -> None:
    d = ToolCallLoopDetector()
    for _ in range(3):
        d.record("approve_scope", {"path": "/foo"})
    assert d.detect_identical() is not None
    d.reset()
    assert d.total_calls == 0
    assert d.hard_aborted is False
    assert d.detect_identical() is None
    assert d.detect_absolute_limit() is False


# === sticky hard_aborted ===


def test_hard_aborted_remains_set_until_reset() -> None:
    d = ToolCallLoopDetector()
    for _ in range(3):
        d.record("approve_scope", {"path": "/foo"})
    d.detect_identical()
    assert d.hard_aborted is True

    d.record("read_file", {"path": "/other"})
    assert d.hard_aborted is True

    d.reset()
    assert d.hard_aborted is False
