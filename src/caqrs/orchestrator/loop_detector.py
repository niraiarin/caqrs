"""Multi-axis loop detection for agent tool-call sequences.

Ported from Mercury ``src/core/agent.ts:34-240`` (the
``ToolCallLoopDetector`` class). See
``docs/research/mercury-survey/03-agent-harness.md`` for the named
patterns and the rationale per axis.

Six detection axes, in priority order at the call site:

1. Absolute limit — total calls or failed-call counter exceeds caps.
2. Identical loop — last N calls share tool + params (hard abort).
3. Similar failing loop — last N calls share tool, all failing
   (hard abort).
4. Same-tool consecutive — last N calls share tool (soft warn,
   threshold depends on a high-tolerance whitelist).
5. Text repetition — assistant step texts are too similar (warn).
6. No-action steps — model keeps thinking without calling any tool
   (cap).

The detector is per-cycle stateful (not persisted). The orchestrator
owns one instance per agent invocation. ``reset()`` clears state, used
when entering a sub-agent that legitimately needs many calls.
"""

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field


class LoopDetection(BaseModel):
    """Result of a positive loop-detection probe."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tool: str = Field(min_length=1)
    count: int = Field(ge=1)
    message: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class CallRecord:
    """Internal record of one tool call for sliding-window analysis."""

    tool: str
    params_key: str
    failed: bool


# Caps and thresholds — match Mercury defaults so behaviour is comparable.
_ABSOLUTE_MAX: Final[int] = 25
_FAILED_ABSOLUTE_MAX: Final[int] = 12
_NO_ACTION_MAX: Final[int] = 5
_RECENT_CALLS_WINDOW: Final[int] = 30
_RECENT_STEP_TEXTS_WINDOW: Final[int] = 12
_PARAMS_KEY_LIMIT: Final[int] = 200

_IDENTICAL_THRESHOLD: Final[int] = 3
_SIMILAR_FAILING_THRESHOLD: Final[int] = 4
_TEXT_REPEAT_THRESHOLD: Final[int] = 3
_TEXT_SIMILARITY_THRESHOLD: Final[float] = 0.7
_STEP_TEXT_NORMALISE_LIMIT: Final[int] = 200
_STEP_TEXT_MIN_LEN: Final[int] = 10

_SAME_TOOL_BASE_NORMAL: Final[int] = 3
_SAME_TOOL_BASE_HIGH: Final[int] = 5
_SAME_TOOL_FAILING_DROP_TRIGGER: Final[int] = 3
_SAME_TOOL_FAILING_NORMAL: Final[int] = 2
_SAME_TOOL_FAILING_HIGH: Final[int] = 3

_HIGH_TOLERANCE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "fetch_url",
        "read_file",
        "list_dir",
        "web_search",
        "github_api",
        # CAQRS additions: data-source readers can legitimately be hit many times.
        "fetch_prices",
        "fetch_news",
        "fetch_macro",
    },
)


class ToolCallLoopDetector:
    """Sliding-window detector for repeated / failing / no-action loops."""

    def __init__(self) -> None:
        self._recent_calls: deque[CallRecord] = deque(maxlen=_RECENT_CALLS_WINDOW)
        self._total_calls = 0
        self._recent_step_texts: deque[str] = deque(maxlen=_RECENT_STEP_TEXTS_WINDOW)
        self._consecutive_no_action = 0
        self._hard_aborted = False

    @property
    def total_calls(self) -> int:
        return self._total_calls

    @property
    def hard_aborted(self) -> bool:
        return self._hard_aborted

    def record(self, tool: str, params: dict[str, Any], *, failed: bool = False) -> None:
        """Record a tool call. Resets the no-action counter."""
        params_key = json.dumps(params, sort_keys=True, default=str)[:_PARAMS_KEY_LIMIT]
        self._recent_calls.append(CallRecord(tool=tool, params_key=params_key, failed=failed))
        self._total_calls += 1
        self._consecutive_no_action = 0

    def record_no_action(self) -> bool:
        """Increment the no-action counter. Returns True iff the cap was hit."""
        self._consecutive_no_action += 1
        return self._consecutive_no_action >= _NO_ACTION_MAX

    def record_step_text(self, text: str) -> None:
        """Append a normalised assistant text to the repetition window."""
        if not text or len(text) < _STEP_TEXT_MIN_LEN:
            return
        normalised = " ".join(text.lower().split())[:_STEP_TEXT_NORMALISE_LIMIT]
        if normalised:
            self._recent_step_texts.append(normalised)

    def detect_absolute_limit(self) -> bool:
        if self._total_calls >= _ABSOLUTE_MAX:
            return True
        return sum(1 for c in self._recent_calls if c.failed) >= _FAILED_ABSOLUTE_MAX

    def detect_identical(self) -> LoopDetection | None:
        """Last N consecutive calls share tool and params (hard abort)."""
        if len(self._recent_calls) < _IDENTICAL_THRESHOLD:
            return None
        last = self._recent_calls[-1]
        count = 0
        for record in reversed(self._recent_calls):
            if record.tool == last.tool and record.params_key == last.params_key:
                count += 1
            else:
                break
        if count >= _IDENTICAL_THRESHOLD:
            self._hard_aborted = True
            return LoopDetection(
                tool=last.tool,
                count=count,
                message=(
                    f'Tool "{last.tool}" called {count} times with identical params; '
                    "stopping to avoid budget burn."
                ),
            )
        return None

    def detect_similar_failing(self) -> LoopDetection | None:
        """Last N calls share tool, all failing (hard abort)."""
        if len(self._recent_calls) < _SIMILAR_FAILING_THRESHOLD:
            return None
        last = self._recent_calls[-1]
        if not last.failed:
            return None
        count = 0
        for record in reversed(self._recent_calls):
            if record.tool != last.tool or not record.failed:
                break
            count += 1
        if count >= _SIMILAR_FAILING_THRESHOLD:
            self._hard_aborted = True
            return LoopDetection(
                tool=last.tool,
                count=count,
                message=(
                    f'Tool "{last.tool}" called {count} times, all failing; '
                    "stopping the agent loop."
                ),
            )
        return None

    def detect_same_tool(self) -> LoopDetection | None:
        """Last N calls share tool (soft warn, threshold by whitelist)."""
        if len(self._recent_calls) < _SAME_TOOL_BASE_NORMAL:
            return None
        last = self._recent_calls[-1]
        consecutive = 0
        failing_among = 0
        for record in reversed(self._recent_calls):
            if record.tool != last.tool:
                break
            consecutive += 1
            if record.failed:
                failing_among += 1
        threshold = _resolve_same_tool_threshold(last.tool, failing_among)
        if consecutive >= threshold:
            return LoopDetection(
                tool=last.tool,
                count=consecutive,
                message=(
                    f'Tool "{last.tool}" used {consecutive} times in a row '
                    "— consider a different approach."
                ),
            )
        return None

    def detect_text_repetition(self) -> LoopDetection | None:
        """Recent step texts have high pairwise similarity."""
        if len(self._recent_step_texts) < _TEXT_REPEAT_THRESHOLD:
            return None
        texts = list(self._recent_step_texts)
        last = texts[-1]
        repeats = 0
        for text in reversed(texts):
            if _jaccard_similarity(last, text) >= _TEXT_SIMILARITY_THRESHOLD:
                repeats += 1
            else:
                break
        if repeats >= _TEXT_REPEAT_THRESHOLD:
            return LoopDetection(
                tool="(no-tool)",
                count=repeats,
                message=(
                    f"Assistant repeated near-identical text {repeats} times in a row; "
                    "stopping to break the repetition loop."
                ),
            )
        return None

    def reset(self) -> None:
        """Clear all state. Used when entering a sub-agent."""
        self._recent_calls.clear()
        self._total_calls = 0
        self._recent_step_texts.clear()
        self._consecutive_no_action = 0
        self._hard_aborted = False


def _resolve_same_tool_threshold(tool: str, failing_count: int) -> int:
    is_high_tolerance = tool in _HIGH_TOLERANCE_TOOLS
    base = _SAME_TOOL_BASE_HIGH if is_high_tolerance else _SAME_TOOL_BASE_NORMAL
    if failing_count >= _SAME_TOOL_FAILING_DROP_TRIGGER:
        floor = _SAME_TOOL_FAILING_HIGH if is_high_tolerance else _SAME_TOOL_FAILING_NORMAL
        return min(base, floor)
    return base


def _jaccard_similarity(a: str, b: str) -> float:
    """Word-set Jaccard similarity. ``1.0`` for identical strings."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    set_a = set(a.split())
    set_b = set(b.split())
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)
