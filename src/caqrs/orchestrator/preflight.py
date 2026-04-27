"""Pre-LLM warning injection — pattern detection on recent agent activity.

Ported from Mercury ``src/core/agent.ts:414-458``. Mercury scans the
last 6 turns of short-term memory before sending a new prompt; if
the assistant has called the same tool 3+ times in a row, or the
last 3 responses have >0.75 text similarity, a `[SYSTEM WARNING]` is
injected as a fake user/assistant turn pair so the model sees the
loop pattern in conversation history.

CAQRS adapts this to its agent-pipeline model: the orchestrator
calls the scanner with sequences of "recent tool names" and "recent
assistant texts", receives zero or more warnings, and prepends them
to the next agent's system prompt. The dual-loop discipline (this
*pre-flight* scanner + the reactive ``ToolCallLoopDetector``) catches
both pre-emptive and in-flight loops.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class PreflightWarningKind(StrEnum):
    REPEATED_TOOL = "repeated_tool"
    REPEATED_TEXT = "repeated_text"


class PreflightWarning(BaseModel):
    """A single loop pattern detected in recent agent activity."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: PreflightWarningKind
    detail: str = Field(min_length=1)

    def to_system_message(self) -> str:
        return f"[PREFLIGHT WARNING] {self.detail}"


_DEFAULT_TOOL_REPEAT_THRESHOLD: Final[int] = 3
_DEFAULT_TEXT_REPEAT_THRESHOLD: Final[int] = 3
_DEFAULT_TEXT_SIMILARITY_THRESHOLD: Final[float] = 0.75
_MIN_TEXT_LENGTH: Final[int] = 20


def scan_tool_repetition(
    tool_history: Sequence[str],
    *,
    threshold: int = _DEFAULT_TOOL_REPEAT_THRESHOLD,
) -> PreflightWarning | None:
    """Return a warning iff the last ``threshold`` calls share a tool name."""
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if len(tool_history) < threshold:
        return None
    last = tool_history[-1]
    streak = 0
    for name in reversed(tool_history):
        if name == last:
            streak += 1
        else:
            break
    if streak < threshold:
        return None
    return PreflightWarning(
        kind=PreflightWarningKind.REPEATED_TOOL,
        detail=(
            f'Tool "{last}" was used {streak} times in a row. '
            "Consider a different approach for the next call."
        ),
    )


def scan_text_repetition(
    text_history: Sequence[str],
    *,
    threshold: int = _DEFAULT_TEXT_REPEAT_THRESHOLD,
    similarity_threshold: float = _DEFAULT_TEXT_SIMILARITY_THRESHOLD,
) -> PreflightWarning | None:
    """Return a warning iff the last ``threshold`` texts have high mutual similarity.

    Texts shorter than ``_MIN_TEXT_LENGTH`` are ignored — they do not
    carry enough signal for a Jaccard-on-words measure to be reliable.
    """
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be in [0, 1]")
    eligible = [t for t in text_history if len(t) >= _MIN_TEXT_LENGTH]
    if len(eligible) < threshold:
        return None
    last_norm = _normalise_for_jaccard(eligible[-1])
    streak = 0
    for text in reversed(eligible):
        if (
            _jaccard_word_similarity(last_norm, _normalise_for_jaccard(text))
            >= similarity_threshold
        ):
            streak += 1
        else:
            break
    if streak < threshold:
        return None
    return PreflightWarning(
        kind=PreflightWarningKind.REPEATED_TEXT,
        detail=(
            f"Your last {streak} responses are near-identical. Stop "
            "repeating; either give a substantively different response "
            "or clearly state that the task cannot be completed."
        ),
    )


def compose_preflight_message(warnings: Sequence[PreflightWarning]) -> str:
    """Combine zero or more warnings into a single system-message block.

    Returns the empty string when there are no warnings, so callers can
    unconditionally concatenate the result onto the system prompt.
    """
    if not warnings:
        return ""
    return "\n\n".join(w.to_system_message() for w in warnings)


def _normalise_for_jaccard(text: str) -> str:
    """Lowercase + collapse whitespace for token-set similarity."""
    return " ".join(text.lower().split())


def _jaccard_word_similarity(a: str, b: str) -> float:
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
