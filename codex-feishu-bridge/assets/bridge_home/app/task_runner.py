#!/usr/bin/env python3
"""Run due Codex Feishu shared tasks without requiring Feishu connectivity."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from shared_memory import DEFAULT_HOME, build_shared_context
import tasks


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = Path(os.getenv("CODEX_FEISHU_WORKSPACE", DEFAULT_HOME / "workspace"))
RUNTIME_SRC_DIR = Path(os.getenv("CODEX_FEISHU_RUNTIME_SRC", DEFAULT_HOME / "runtime" / "src"))
DEFAULT_CODEX_BIN = os.getenv("CODEX_FEISHU_CODEX_BIN") or os.getenv("CODEX_BIN") or "codex"

if str(RUNTIME_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC_DIR))

from agent.transports.codex_app_server_session import (  # noqa: E402
    CodexAppServerSession,
    TurnResult,
    _ServerRequestRouting,
)


def build_task_prompt(task: dict, *, workspace: Path) -> str:
    shared_context = build_shared_context(workspace)
    return "\n".join(
        [
            "你是本机 Codex 共享定时任务执行器。",
            "执行时必须读取并遵守下面的共享本机记忆；不要泄露 API key、token、密码或完整 access key。",
            "",
            "## 共享本机记忆",
            shared_context,
            "",
            "## 本次定时任务",
            f"任务 ID：{task.get('id')}",
            f"任务名称：{task.get('name')}",
            f"工作目录：{workspace}",
            "",
            str(task.get("prompt") or "").strip(),
            "",
            "这是通知型定时任务；最终回答只输出要推送给用户的通知正文。",
            "不要写执行过程、尾注、工具调用、完成情况、关键结果、验证结果、来源清单或额外说明，除非任务正文明确要求。",
        ]
    )


def write_run_output(task: dict, result: TurnResult) -> Path:
    runs_dir = DEFAULT_HOME / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = runs_dir / f"{stamp}_{task.get('id')}.md"
    body = result.final_text or ""
    if result.error:
        body += f"\n\n## Error\n\n```text\n{result.error}\n```"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def run_task(task: dict, args: argparse.Namespace) -> tuple[bool, str, str]:
    workspace = Path(task.get("workspace") or args.workspace or PROJECT_ROOT).expanduser().resolve()
    routing = _ServerRequestRouting(
        auto_approve_exec=args.auto_approve_exec,
        auto_approve_apply_patch=args.auto_approve_apply_patch,
    )
    session = CodexAppServerSession(
        cwd=str(workspace),
        codex_bin=args.codex_bin,
        codex_home=args.codex_home or None,
        request_routing=routing,
    )
    try:
        result = session.run_turn(
            build_task_prompt(task, workspace=workspace),
            turn_timeout=args.turn_timeout,
        )
    finally:
        session.close()
    output_path = write_run_output(task, result)
    success = not bool(result.error)
    summary = (result.final_text or result.error or "").strip().splitlines()[0:6]
    return success, "\n".join(summary), str(output_path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run due Codex Feishu shared tasks")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    parser.add_argument("--codex-bin", default=DEFAULT_CODEX_BIN)
    parser.add_argument("--codex-home", default=os.getenv("CODEX_HOME", ""))
    parser.add_argument("--turn-timeout", type=float, default=float(os.getenv("CODEX_FEISHU_TASK_TIMEOUT", "1800")))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-approve-exec", action="store_true")
    parser.add_argument("--auto-approve-apply-patch", action="store_true")
    args = parser.parse_args(argv)

    runner_id = "local-runner-" + uuid.uuid4().hex[:8]
    if args.dry_run:
        due = tasks.due_tasks(limit=args.limit)
        for task in due:
            print(f"{task['id']} {task.get('name')} next={task.get('next_run_at')}")
        return 0

    claimed = tasks.claim_due_tasks(runner_id=runner_id, limit=args.limit)
    if not claimed:
        print("no due tasks")
        return 0
    exit_code = 0
    for task in claimed:
        print(f"running {task['id']} {task.get('name')}")
        try:
            success, summary, output_path = run_task(task, args)
            tasks.complete_task(task["id"], success=success, summary=summary, output_path=output_path)
            print(f"done {task['id']} success={success} output={output_path}")
            if not success:
                exit_code = 1
        except Exception as exc:
            tasks.complete_task(task["id"], success=False, summary=str(exc), output_path="")
            print(f"failed {task['id']}: {exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
