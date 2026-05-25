#!/usr/bin/env python3
"""Bounded per-Feishu conversation memory for the Codex bridge."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


DEFAULT_HOME = Path(os.getenv("CODEX_FEISHU_HOME", Path.home() / ".codex-feishu"))
CONVERSATIONS_DIR = DEFAULT_HOME / "conversations"
MAX_SUMMARY_CHARS = int(os.getenv("CODEX_FEISHU_CONVERSATION_SUMMARY_CHARS", "6000"))
MAX_RECENT_TURNS = int(os.getenv("CODEX_FEISHU_CONVERSATION_RECENT_TURNS", "8"))
MAX_TURN_TEXT_CHARS = int(os.getenv("CODEX_FEISHU_CONVERSATION_TURN_CHARS", "1400"))


def _ensure_dir() -> Path:
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONVERSATIONS_DIR.chmod(0o700)
    except OSError:
        pass
    return CONVERSATIONS_DIR


def conversation_id(session_key: str) -> str:
    digest = hashlib.sha256(str(session_key or "").encode("utf-8")).hexdigest()
    return f"conv_{digest[:24]}"


def conversation_path(session_key: str) -> Path:
    return _ensure_dir() / f"{conversation_id(session_key)}.json"


def _truncate(text: str, limit: int) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    head = max(200, limit // 2)
    tail = max(200, limit - head - 32)
    return value[:head].rstrip() + "\n...（已压缩）...\n" + value[-tail:].lstrip()


def _default_state(session_key: str) -> dict[str, Any]:
    return {
        "version": 1,
        "conversation_id": conversation_id(session_key),
        "summary": "",
        "recent": [],
        "turn_count": 0,
        "updated_at": "",
    }


@contextmanager
def locked_state(session_key: str) -> Iterator[dict[str, Any]]:
    path = conversation_path(session_key)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except (FileNotFoundError, json.JSONDecodeError):
                data = _default_state(session_key)
            if not isinstance(data, dict):
                data = _default_state(session_key)
            if not isinstance(data.get("recent"), list):
                data["recent"] = []
            yield data
            fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp_name, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_state(session_key: str) -> dict[str, Any]:
    path = conversation_path(session_key)
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (FileNotFoundError, json.JSONDecodeError):
        return _default_state(session_key)
    if not isinstance(data, dict):
        return _default_state(session_key)
    if not isinstance(data.get("recent"), list):
        data["recent"] = []
    return data


def reset_state(session_key: str, *, source: str = "", reason: str = "") -> dict[str, Any]:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with locked_state(session_key) as data:
        data.clear()
        data.update(_default_state(session_key))
        data["reset_at"] = stamp
        data["reset_source"] = source
        data["reset_reason"] = reason
        data["updated_at"] = stamp
        return dict(data)


def build_context(session_key: str, *, max_chars: int = 12000) -> str:
    state = load_state(session_key)
    summary = str(state.get("summary") or "").strip()
    recent = state.get("recent") or []
    blocks: list[str] = []
    if summary:
        blocks.append("## 飞书会话滚动摘要\n" + summary)
    if recent:
        lines = ["## 最近几轮飞书对话"]
        for item in recent[-MAX_RECENT_TURNS:]:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("at") or "")
            user_text = _truncate(str(item.get("user") or ""), MAX_TURN_TEXT_CHARS)
            assistant_text = _truncate(str(item.get("assistant") or ""), MAX_TURN_TEXT_CHARS)
            lines.append(f"\n### {ts}".rstrip())
            lines.append(f"用户：\n{user_text or '（空）'}")
            lines.append(f"Codex：\n{assistant_text or '（空）'}")
        blocks.append("\n".join(lines).strip())
    if not blocks:
        return "暂无飞书轻量会话记忆。"
    text = "\n\n".join(blocks).strip()
    if len(text) <= max_chars:
        return text
    head = max(1000, max_chars // 3)
    tail = max(1000, max_chars - head - 80)
    return text[:head].rstrip() + "\n\n...（飞书会话记忆中段已截断）...\n\n" + text[-tail:].lstrip()


def update_state(
    session_key: str,
    *,
    user_text: str,
    assistant_text: str,
    source: str = "",
) -> dict[str, Any]:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    user_compact = _truncate(user_text, MAX_TURN_TEXT_CHARS)
    assistant_compact = _truncate(assistant_text, MAX_TURN_TEXT_CHARS)
    summary_line = (
        f"- {stamp}"
        f"{' · ' + source if source else ''}: "
        f"用户：{_truncate(user_text, 420)}；Codex：{_truncate(assistant_text, 720)}"
    )
    with locked_state(session_key) as data:
        existing_summary = str(data.get("summary") or "").strip()
        merged_summary = (existing_summary + "\n" + summary_line).strip()
        if len(merged_summary) > MAX_SUMMARY_CHARS:
            merged_summary = "（较早飞书对话已滚动压缩，仅保留近期摘要）\n" + merged_summary[-MAX_SUMMARY_CHARS:].lstrip()
        recent = list(data.get("recent") or [])
        recent.append(
            {
                "at": stamp,
                "source": source,
                "user": user_compact,
                "assistant": assistant_compact,
            }
        )
        data["version"] = 1
        data["conversation_id"] = conversation_id(session_key)
        data["summary"] = merged_summary
        data["recent"] = recent[-MAX_RECENT_TURNS:]
        data["turn_count"] = int(data.get("turn_count") or 0) + 1
        data["updated_at"] = stamp
        return dict(data)
