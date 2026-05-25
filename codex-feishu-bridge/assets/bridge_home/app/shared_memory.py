#!/usr/bin/env python3
"""Independent shared local memory for the Codex Feishu bridge."""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_HOME = Path(os.getenv("CODEX_FEISHU_HOME", Path.home() / ".codex-feishu"))
GLOBAL_MEMORY_FILE = DEFAULT_HOME / "shared-memory.md"
DEFAULT_WORKSPACE_MEMORY_FILES = (
    "CODEX_FEISHU_MEMORY.md",
    "AGENTS.md",
)


def ensure_home() -> Path:
    DEFAULT_HOME.mkdir(parents=True, exist_ok=True)
    try:
        DEFAULT_HOME.chmod(0o700)
    except OSError:
        pass
    return DEFAULT_HOME


def _read_limited(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return ""
    if len(text) <= limit:
        return text
    head = max(1000, limit // 3)
    tail = max(1000, limit - head - 80)
    return text[:head].rstrip() + "\n\n...（中间内容已截断）...\n\n" + text[-tail:].lstrip()


def _memory_paths(workspace: Path) -> Iterable[tuple[str, Path, int]]:
    yield "Codex 飞书共享记忆", GLOBAL_MEMORY_FILE, 10000
    for name in DEFAULT_WORKSPACE_MEMORY_FILES:
        yield name, workspace / name, 8000


def build_shared_context(workspace: str | Path, *, max_chars: int = 26000) -> str:
    workspace_path = Path(workspace).expanduser().resolve()
    blocks: list[str] = []
    for title, path, limit in _memory_paths(workspace_path):
        text = _read_limited(path, limit)
        if not text:
            continue
        blocks.append(f"## {title}\n来源：{path}\n\n{text}")
    if not blocks:
        return "暂无共享本机记忆。"
    merged = "\n\n---\n\n".join(blocks).strip()
    if len(merged) <= max_chars:
        return merged
    return merged[: max_chars - 80].rstrip() + "\n\n...（共享记忆总长度已截断）"


def append_memory_note(note: str, *, source: str = "manual") -> Path:
    ensure_home()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    clean = str(note or "").strip()
    if not clean:
        raise ValueError("memory note is empty")
    existing = ""
    if GLOBAL_MEMORY_FILE.exists():
        existing = GLOBAL_MEMORY_FILE.read_text(encoding="utf-8", errors="replace").rstrip()
    prefix = existing + "\n\n" if existing else "# Codex Feishu 共享记忆\n\n"
    GLOBAL_MEMORY_FILE.write_text(
        prefix + f"## {now} · {source}\n\n{clean}\n",
        encoding="utf-8",
    )
    try:
        GLOBAL_MEMORY_FILE.chmod(0o600)
    except OSError:
        pass
    return GLOBAL_MEMORY_FILE


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex Feishu shared memory helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show")
    show.add_argument("--workspace", default=os.getcwd())
    show.add_argument("--max-chars", type=int, default=26000)
    add = sub.add_parser("append")
    add.add_argument("note")
    add.add_argument("--source", default="manual")
    args = parser.parse_args()
    if args.cmd == "show":
        print(build_shared_context(args.workspace, max_chars=args.max_chars))
        return 0
    if args.cmd == "append":
        path = append_memory_note(args.note, source=args.source)
        print(path)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
