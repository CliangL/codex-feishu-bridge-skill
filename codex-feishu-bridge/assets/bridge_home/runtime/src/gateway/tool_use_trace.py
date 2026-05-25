"""Minimal tool-use trace store for Feishu single-card progress display.

The previous Hermes backup kept tool lifecycle state in a dedicated store so
Feishu could fold tool progress into one continuously-updated card instead of
spraying separate bubbles.  The fresh install lost that module entirely, which
is why tool calls regressed into standalone messages.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolUseStep:
    tool_name: str
    params: Dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_ms: int = 0
    is_error: bool = False
    result_preview: Optional[str] = None


@dataclass
class ToolUseTraceRun:
    session_key: str
    started_at: float
    steps: List[ToolUseStep] = field(default_factory=list)


_runs: Dict[str, ToolUseTraceRun] = {}
_lock = threading.Lock()

_PREVIEW_KEYS = (
    "path",
    "file_path",
    "file",
    "url",
    "query",
    "q",
    "command",
    "cmd",
    "skill",
    "name",
    "message",
    "target",
)


def start_tool_use_trace_run(session_key: str) -> None:
    with _lock:
        _runs[session_key] = ToolUseTraceRun(
            session_key=session_key,
            started_at=time.time(),
        )


def clear_tool_use_trace_run(session_key: str) -> None:
    with _lock:
        _runs.pop(session_key, None)


def get_tool_use_trace_run(session_key: str) -> Optional[ToolUseTraceRun]:
    with _lock:
        return _runs.get(session_key)


def record_tool_use_start(session_key: str, tool_name: str, params: Optional[Dict[str, Any]] = None) -> None:
    with _lock:
        run = _runs.get(session_key)
        if run is None:
            run = ToolUseTraceRun(session_key=session_key, started_at=time.time())
            _runs[session_key] = run
        run.steps.append(
            ToolUseStep(
                tool_name=tool_name or "",
                params=dict(params or {}),
                started_at=time.time(),
            )
        )


def record_tool_use_end(
    session_key: str,
    tool_name: str,
    *,
    elapsed_ms: int = 0,
    is_error: bool = False,
    result_preview: Optional[str] = None,
) -> None:
    with _lock:
        run = _runs.get(session_key)
        if not run:
            return
        for step in reversed(run.steps):
            if step.tool_name == tool_name and step.ended_at == 0:
                now = time.time()
                duration = max(0, int(elapsed_ms or 0))
                step.duration_ms = duration
                step.ended_at = now
                if duration <= 0 and step.started_at > 0:
                    step.duration_ms = int(max(0.0, now - step.started_at) * 1000)
                step.is_error = bool(is_error)
                step.result_preview = result_preview
                break


def get_tool_use_steps(session_key: str) -> List[ToolUseStep]:
    with _lock:
        run = _runs.get(session_key)
        return list(run.steps) if run else []


def get_tool_use_elapsed_ms(session_key: str) -> int:
    with _lock:
        run = _runs.get(session_key)
        if not run or not run.steps:
            return 0
        now = time.time()
        total_ms = 0.0
        for step in run.steps:
            if step.duration_ms > 0:
                total_ms += float(step.duration_ms)
                continue
            if step.started_at > 0:
                end_at = step.ended_at if step.ended_at > 0 else now
                total_ms += max(0.0, end_at - step.started_at) * 1000.0
        return int(max(0.0, total_ms))


def humanize_tool_name(name: str) -> str:
    cleaned = str(name or "").replace("_", " ").replace("-", " ").strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else "Tool"


def _format_elapsed_ms(value: int) -> str:
    seconds = max(0, int(value or 0)) / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s".replace(".0s", "s")
    minutes = int(seconds // 60)
    rem = int(seconds % 60)
    return f"{minutes}m{rem:02d}s"


def _compact_preview(value: Any, limit: int = 48) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        text = ", ".join(str(key) for key in list(value)[:3])
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _pick_preview(step: ToolUseStep) -> str:
    params = step.params or {}
    for key in _PREVIEW_KEYS:
        preview = _compact_preview(params.get(key))
        if preview:
            return preview
    if params:
        keys = list(params.keys())[:3]
        return ", ".join(str(key) for key in keys)
    return _compact_preview(step.result_preview)


def build_tool_use_steps_for_card(
    session_key: str,
    show_full_paths: bool = False,
    max_display_steps: int = 5,
) -> List[Dict[str, Any]]:
    del show_full_paths  # kept for API compatibility with the backup module
    steps = get_tool_use_steps(session_key)
    if len(steps) > max_display_steps:
        steps = steps[-max_display_steps:]
    formatted: List[Dict[str, Any]] = []
    for step in steps:
        title = humanize_tool_name(step.tool_name)
        preview = _pick_preview(step)
        duration = _format_elapsed_ms(step.duration_ms)
        suffix_parts = []
        if preview:
            suffix_parts.append(preview)
        if duration:
            suffix_parts.append(duration)
        if step.is_error:
            suffix_parts.append("failed")
        line = title
        if suffix_parts:
            line = f"{line}: {' · '.join(suffix_parts)}"
        formatted.append(
            {
                "tool_name": step.tool_name,
                "title": title,
                "preview": preview,
                "duration_ms": step.duration_ms,
                "is_error": step.is_error,
                "line": line,
            }
        )
    return formatted


class ToolUseTraceStore:
    """Small class-style facade used by the gateway and Feishu adapter."""

    @staticmethod
    def start_tool(session_key: str, tool_name: str, params: Optional[Dict[str, Any]] = None) -> None:
        if not get_tool_use_trace_run(session_key):
            start_tool_use_trace_run(session_key)
        record_tool_use_start(session_key, tool_name, params or {})

    @staticmethod
    def end_tool(
        session_key: str,
        tool_name: str,
        elapsed_ms: int = 0,
        *,
        is_error: bool = False,
        result_preview: Optional[str] = None,
    ) -> None:
        record_tool_use_end(
            session_key,
            tool_name,
            elapsed_ms=elapsed_ms,
            is_error=is_error,
            result_preview=result_preview,
        )

    @staticmethod
    def get_traces(session_key: str) -> Optional[Dict[str, Any]]:
        steps = build_tool_use_steps_for_card(session_key)
        if not steps:
            return None
        return {
            "steps": steps,
            "elapsed_ms": get_tool_use_elapsed_ms(session_key),
            "total_count": len(get_tool_use_steps(session_key)),
        }

    @staticmethod
    def clear(session_key: str) -> None:
        clear_tool_use_trace_run(session_key)
