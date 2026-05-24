#!/usr/bin/env python3
"""Minimal local Codex <-> Feishu bridge template.

This template keeps Feishu as a transport layer. It receives Feishu bot
messages, invokes the local Codex CLI, sends a fresh per-turn response card
back to Feishu, and mirrors Feishu-created scheduled jobs into Codex
automations metadata.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    UpdateMessageRequest,
    UpdateMessageRequestBody,
)
from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client as FeishuWSClient


LOG = logging.getLogger("codex-feishu-bridge")

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 can still run the bridge without runtime footer metadata.
    tomllib = None  # type: ignore[assignment]

SENSITIVE_RE = re.compile(
    r"(?i)(app_secret|client_secret|tenant_access_token|app_access_token|"
    r"user_access_token|refresh_token|authorization|api[_-]?key|token)"
    r"([=:\s]+)([A-Za-z0-9._~+/=-]{8,})"
)
BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
AT_TAG_RE = re.compile(r"<at[^>]*>.*?</at>")


def redact(value: Any) -> str:
    text = str(value)
    text = SENSITIVE_RE.sub(r"\1\2***", text)
    return BEARER_RE.sub(r"\1***", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(home: Path) -> None:
    logs_dir = home / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(logs_dir / "bridge.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    log_filter = RedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(log_filter)
    logging.getLogger().addFilter(log_filter)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def truthy_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else default


def now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name)).replace(microsecond=0)


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("time must use HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("time must use HH:MM")
    return hour, minute


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def clamp_text(text: str, limit: int = 15000) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[truncated]"


def load_codex_runtime(codex_home: Path) -> dict[str, str]:
    config_path = codex_home / "config.toml"
    if tomllib is None:
        return {"model": "", "reasoning_effort": ""}
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {"model": "", "reasoning_effort": ""}
    return {
        "model": str(config.get("model") or "").strip(),
        "reasoning_effort": str(
            config.get("model_reasoning_effort") or config.get("reasoning_effort") or ""
        ).strip(),
    }


def format_elapsed(seconds: float) -> str:
    total = max(0.0, float(seconds or 0))
    if total < 60:
        text = f"{total:.1f} 秒"
        return text.replace(".0 秒", " 秒")
    minutes = int(total // 60)
    rem = int(total % 60)
    return f"{minutes} 分 {rem:02d} 秒"


def codex_footer(codex_home: Path, *, elapsed_seconds: float, tool_count: int = 0) -> str:
    runtime = load_codex_runtime(codex_home)
    return " · ".join(
        [
            runtime.get("model") or "默认模型",
            runtime.get("reasoning_effort") or "默认",
            format_elapsed(elapsed_seconds),
            f"{max(0, int(tool_count or 0))} 步工具调用",
        ]
    )


@dataclass(frozen=True)
class BridgeConfig:
    home: Path
    env_file: Path
    workspace: Path
    codex_home: Path
    codex_bin: str
    codex_args: list[str]
    app_id: str
    app_secret: str
    domain_name: str
    connection_mode: str
    verification_token: str
    encrypt_key: str
    notify_chat_id: str
    allowed_users: frozenset[str]
    require_mention: bool
    timezone: str
    max_memory_turns: int
    max_input_chars: int
    codex_timeout_seconds: int
    automations_enabled: bool
    scheduler_wake_seconds: int

    @classmethod
    def from_env(cls, home: Path, env_file: Path) -> "BridgeConfig":
        workspace = env_path("CODEX_FEISHU_WORKSPACE", home / "workspace")
        codex_home = env_path("CODEX_HOME", Path.home() / ".codex")
        allowed = frozenset(
            item.strip()
            for item in os.getenv("CODEX_FEISHU_ALLOWED_USERS", "").split(",")
            if item.strip()
        )
        codex_args = shlex.split(
            os.getenv(
                "CODEX_FEISHU_CODEX_ARGS",
                "exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox",
            )
        )
        return cls(
            home=home,
            env_file=env_file,
            workspace=workspace,
            codex_home=codex_home,
            codex_bin=os.getenv("CODEX_FEISHU_CODEX_BIN", "codex").strip() or "codex",
            codex_args=codex_args,
            app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            domain_name=os.getenv("FEISHU_DOMAIN", "feishu").strip().lower(),
            connection_mode=os.getenv("FEISHU_CONNECTION_MODE", "websocket").strip().lower(),
            verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip(),
            encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", "").strip(),
            notify_chat_id=os.getenv("CODEX_FEISHU_NOTIFY_CHAT_ID", "").strip(),
            allowed_users=allowed,
            require_mention=truthy_env("CODEX_FEISHU_REQUIRE_MENTION", True),
            timezone=os.getenv("CODEX_FEISHU_TIMEZONE", "Asia/Shanghai").strip() or "UTC",
            max_memory_turns=max(0, int(os.getenv("CODEX_FEISHU_MAX_MEMORY_TURNS", "12"))),
            max_input_chars=max(2000, int(os.getenv("CODEX_FEISHU_MAX_INPUT_CHARS", "24000"))),
            codex_timeout_seconds=max(30, int(os.getenv("CODEX_FEISHU_CODEX_TIMEOUT_SECONDS", "1800"))),
            automations_enabled=truthy_env("CODEX_FEISHU_AUTOMATIONS", True),
            scheduler_wake_seconds=max(5, int(os.getenv("CODEX_FEISHU_TASK_POLL_SECONDS", "60"))),
        )

    @property
    def domain(self) -> str:
        return LARK_DOMAIN if self.domain_name == "lark" else FEISHU_DOMAIN


class ConversationMemory:
    def __init__(self, home: Path, max_turns: int) -> None:
        self.root = home / "conversations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_turns = max_turns

    def _path(self, chat_id: str) -> Path:
        digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:24]
        return self.root / f"{digest}.json"

    def load_recent(self, chat_id: str) -> list[dict[str, str]]:
        if self.max_turns <= 0:
            return []
        path = self._path(chat_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data[-self.max_turns :]
        except Exception:
            return []
        return []

    def append(self, chat_id: str, role: str, text: str) -> None:
        if self.max_turns <= 0:
            return
        path = self._path(chat_id)
        items = self.load_recent(chat_id)
        items.append(
            {
                "role": role,
                "text": clamp_text(text, 8000),
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        path.write_text(json_dumps(items[-self.max_turns :]) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def reset(self, chat_id: str) -> None:
        path = self._path(chat_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOG.warning("Failed to reset conversation memory for chat %s", chat_id)


class TaskStore:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.path = cfg.home / "tasks.json"
        self.lock_path = cfg.home / "tasks.json.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json_dumps({"version": 1, "tasks": []}) + "\n", encoding="utf-8")

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        with self.lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                data = {"version": 1, "tasks": []}
            if not isinstance(data.get("tasks"), list):
                data["tasks"] = []
            yield data
            fd, tmp_name = tempfile.mkstemp(prefix="tasks.", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json_dumps(data))
                fh.write("\n")
            os.replace(tmp_name, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def list_active(self) -> list[dict[str, Any]]:
        with self.locked() as data:
            return [task for task in data["tasks"] if task.get("status") == "active"]

    def add_daily(self, *, prompt: str, time_text: str, chat_id: str, name: str = "") -> dict[str, Any]:
        hour, minute = parse_hhmm(time_text)
        task_id = f"task_{int(time.time() * 1000)}"
        schedule = {"type": "daily", "time": f"{hour:02d}:{minute:02d}"}
        first_run = self.compute_next_run({"schedule": schedule}, after=now(self.cfg.timezone) - timedelta(seconds=1))
        destination_chat = self.cfg.notify_chat_id or chat_id
        task = {
            "id": task_id,
            "name": name or f"Codex Feishu task {time_text}",
            "prompt": prompt.strip(),
            "schedule": schedule,
            "timezone": self.cfg.timezone,
            "status": "active",
            "next_run_at": first_run,
            "destination": {
                "type": "feishu",
                "chat_id": destination_chat,
                "reply_to": "",
            },
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.locked() as data:
            data["tasks"].append(task)
        self.write_codex_automation(task)
        return task

    def update_next_run(self, task_id: str, next_run_at: str | None) -> None:
        with self.locked() as data:
            for task in data["tasks"]:
                if task.get("id") == task_id:
                    task["next_run_at"] = next_run_at
                    if next_run_at is None:
                        task["status"] = "completed"
                    task["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    self.write_codex_automation(task)
                    break

    def due_tasks(self) -> list[dict[str, Any]]:
        current = now(self.cfg.timezone)
        due: list[dict[str, Any]] = []
        for task in self.list_active():
            try:
                run_at = datetime.fromisoformat(str(task.get("next_run_at")))
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=ZoneInfo(self.cfg.timezone))
                if run_at <= current:
                    due.append(task)
            except Exception:
                continue
        return due

    def seconds_until_next(self) -> float:
        current = now(self.cfg.timezone)
        seconds = float(self.cfg.scheduler_wake_seconds)
        for task in self.list_active():
            try:
                run_at = datetime.fromisoformat(str(task.get("next_run_at")))
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=ZoneInfo(self.cfg.timezone))
                seconds = min(seconds, max(0.0, (run_at - current).total_seconds()))
            except Exception:
                continue
        return max(1.0, seconds)

    def compute_next_run(self, task: dict[str, Any], after: datetime | None = None) -> str | None:
        schedule = task.get("schedule") or {}
        base = after or now(self.cfg.timezone)
        kind = schedule.get("type")
        if kind == "daily":
            hour, minute = parse_hhmm(str(schedule["time"]))
            candidate = base.replace(hour=hour, minute=minute, second=0)
            if candidate <= base:
                candidate += timedelta(days=1)
            return iso(candidate)
        return None

    def write_codex_automation(self, task: dict[str, Any]) -> None:
        if not self.cfg.automations_enabled:
            return
        automation_id = f"codex-feishu-{task['id']}"
        automation_dir = self.cfg.codex_home / "automations" / automation_id
        automation_dir.mkdir(parents=True, exist_ok=True)
        schedule = task.get("schedule") or {}
        rrule = ""
        if schedule.get("type") == "daily":
            hour, minute = parse_hhmm(str(schedule["time"]))
            rrule = f"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;BYHOUR={hour};BYMINUTE={minute}"
        destination = task.get("destination") or {}
        content = f'''version = 1
id = "{automation_id}"
kind = "cron"
name = "{toml_escape(str(task.get("name") or automation_id))}"
prompt = "{toml_escape(str(task.get("prompt") or ""))}"
status = "{'ACTIVE' if task.get('status') == 'active' else 'PAUSED'}"
rrule = "{rrule}"
cwds = ["{toml_escape(str(self.cfg.workspace))}"]
execution_environment = "local"
model = ""
reasoning_effort = ""
local_environment_config_path = ""

[metadata]
source = "codex-feishu"
feishu_task_id = "{toml_escape(str(task.get('id') or ''))}"
task_store = "{toml_escape(str(self.path))}"
task_status = "{toml_escape(str(task.get('status') or ''))}"
task_next_run_at = "{toml_escape(str(task.get('next_run_at') or ''))}"
task_timezone = "{toml_escape(str(task.get('timezone') or self.cfg.timezone))}"
feishu_destination_type = "feishu"
feishu_chat_id = "{toml_escape(str(destination.get('chat_id') or ''))}"
codex_native_schedule = true
authoritative_runner = "codex-feishu-bridge"
'''
        (automation_dir / "automation.toml").write_text(content, encoding="utf-8")


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class FeishuBridge:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.cfg.workspace.mkdir(parents=True, exist_ok=True)
        self.memory = ConversationMemory(cfg.home, cfg.max_memory_turns)
        self.tasks = TaskStore(cfg)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.chat_locks: dict[str, threading.Lock] = {}
        self.seen: set[str] = set()
        self.seen_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.client = self._build_lark_client()

    def _build_lark_client(self) -> Any:
        return (
            lark.Client.builder()
            .app_id(self.cfg.app_id)
            .app_secret(self.cfg.app_secret)
            .domain(self.cfg.domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def run(self) -> None:
        if not self.cfg.app_id or not self.cfg.app_secret:
            raise SystemExit("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        if self.cfg.connection_mode != "websocket":
            raise SystemExit("This template supports FEISHU_CONNECTION_MODE=websocket")
        threading.Thread(target=self.scheduler_loop, name="codex-feishu-scheduler", daemon=True).start()
        handler = (
            EventDispatcherHandler.builder(self.cfg.encrypt_key, self.cfg.verification_token)
            .register_p2_im_message_receive_v1(self.on_message_event)
            .build()
        )
        LOG.info("Starting Feishu websocket bridge app_id=%s", mask_id(self.cfg.app_id))
        ws_client = FeishuWSClient(
            app_id=self.cfg.app_id,
            app_secret=self.cfg.app_secret,
            log_level=lark.LogLevel.WARNING,
            event_handler=handler,
            domain=self.cfg.domain,
        )
        ws_client.start()

    def on_message_event(self, data: Any) -> None:
        self.executor.submit(self.handle_message_event, data)

    def handle_message_event(self, data: Any) -> None:
        try:
            event = getattr(data, "event", None)
            message = getattr(event, "message", None)
            sender = getattr(event, "sender", None)
            if not event or not message or not sender:
                return
            message_id = str(getattr(message, "message_id", "") or "")
            if not message_id or self.is_duplicate(message_id):
                return
            chat_id = str(getattr(message, "chat_id", "") or "")
            chat_type = str(getattr(message, "chat_type", "p2p") or "p2p")
            sender_id = getattr(sender, "sender_id", None)
            if not self.sender_allowed(sender_id):
                LOG.info("Dropping message from non-allowed sender")
                return
            text = self.extract_text(message)
            if chat_type != "p2p" and self.cfg.require_mention and not self.message_mentions_bot(message):
                return
            text = AT_TAG_RE.sub("", text).strip()
            if not text:
                return
            lock = self.chat_locks.setdefault(chat_id, threading.Lock())
            with lock:
                self.process_user_turn(chat_id=chat_id, message_id=message_id, text=text)
        except Exception:
            LOG.exception("Failed to process inbound Feishu message")

    def is_duplicate(self, message_id: str) -> bool:
        with self.seen_lock:
            if message_id in self.seen:
                return True
            self.seen.add(message_id)
            if len(self.seen) > 2048:
                self.seen = set(list(self.seen)[-1024:])
            return False

    def sender_allowed(self, sender_id: Any) -> bool:
        if not self.cfg.allowed_users:
            return True
        candidates = {
            str(getattr(sender_id, "open_id", "") or ""),
            str(getattr(sender_id, "user_id", "") or ""),
            str(getattr(sender_id, "union_id", "") or ""),
        }
        return "*" in self.cfg.allowed_users or bool(candidates & set(self.cfg.allowed_users))

    def message_mentions_bot(self, message: Any) -> bool:
        mentions = getattr(message, "mentions", None) or []
        return bool(mentions)

    @staticmethod
    def extract_text(message: Any) -> str:
        raw = str(getattr(message, "content", "") or "")
        try:
            content = json.loads(raw)
            return str(content.get("text") or content.get("content") or raw)
        except Exception:
            return raw

    def process_user_turn(self, *, chat_id: str, message_id: str, text: str) -> None:
        LOG.info("Inbound message chat=%s message=%s text=%r", chat_id, message_id, text[:120])
        if text.strip().lower() in {"/new", "/reset"} or text.strip() in {"新对话", "开启新对话"}:
            self.memory.reset(chat_id)
            self.send_card(
                chat_id,
                "Codex",
                "已开启新的飞书对话。当前飞书会话上下文已清空；本机 Codex 配置、skills、共享记忆和定时任务不会受影响。",
                reply_to=message_id,
            )
            return
        task = self.maybe_create_task(chat_id, text)
        if task:
            response = (
                "Scheduled task created.\n\n"
                f"Name: {task['name']}\n"
                f"Next run: {task['next_run_at']}\n"
                f"Notification chat: {(task.get('destination') or {}).get('chat_id') or chat_id}\n"
                "It has also been mirrored into Codex automations metadata."
            )
            self.send_card(chat_id, "Codex", response, reply_to=message_id)
            return

        response_id = self.send_card(
            chat_id,
            "Codex",
            "Received. Handing this to local Codex...",
            reply_to=message_id,
        )
        self.memory.append(chat_id, "user", text)
        started = time.monotonic()
        answer = self.run_codex_for_chat(chat_id, text)
        answer_with_footer = (
            answer
            + "\n\n---\n"
            + codex_footer(self.cfg.codex_home, elapsed_seconds=time.monotonic() - started, tool_count=0)
        )
        self.memory.append(chat_id, "assistant", answer)
        if response_id:
            self.update_card(response_id, "Codex", answer_with_footer)
        else:
            self.send_card(chat_id, "Codex", answer_with_footer)

    def maybe_create_task(self, chat_id: str, text: str) -> dict[str, Any] | None:
        explicit = re.match(r"^/task\s+daily\s+(\d{1,2}:\d{2})\s+(.+)$", text.strip(), re.I | re.S)
        if explicit:
            return self.tasks.add_daily(prompt=explicit.group(2), time_text=explicit.group(1), chat_id=chat_id)

        # Small Chinese convenience parser for "every day at 7:30/7点30".
        lowered = text.strip()
        if "every day" not in lowered.lower() and "daily" not in lowered.lower() and "每天" not in lowered:
            return None
        match = re.search(r"(\d{1,2})\s*[:点時时]\s*(\d{1,2})?", lowered)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        prompt = lowered
        return self.tasks.add_daily(prompt=prompt, time_text=f"{hour:02d}:{minute:02d}", chat_id=chat_id)

    def build_prompt(self, chat_id: str, text: str) -> str:
        shared_memory = ""
        shared_path = self.cfg.home / "shared-memory.md"
        if shared_path.exists():
            shared_memory = clamp_text(shared_path.read_text(encoding="utf-8", errors="replace"), 8000)
        recent = self.memory.load_recent(chat_id)
        recent_lines = "\n".join(
            f"{item.get('role', 'unknown')}: {item.get('text', '')}" for item in recent
        )
        prompt = f"""You are local Codex, invoked from a Feishu bot bridge.

Feishu is only the interface. Use the current local Codex configuration,
skills, MCP servers, auth, and workspace. Do not reveal API keys, app secrets,
tokens, passwords, or full access keys.

Shared bridge memory:
{shared_memory or "(none)"}

Recent Feishu conversation:
{recent_lines or "(none)"}

User message:
{text}
"""
        return clamp_text(prompt, self.cfg.max_input_chars)

    def run_codex_for_chat(self, chat_id: str, text: str) -> str:
        return self.invoke_codex(self.build_prompt(chat_id, text))

    def invoke_codex(self, prompt: str) -> str:
        output_file = tempfile.NamedTemporaryFile(delete=False)
        output_file.close()
        cmd = [self.cfg.codex_bin, *self.cfg.codex_args, "-C", str(self.cfg.workspace), "-o", output_file.name, prompt]
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.cfg.codex_home)
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.cfg.workspace),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.cfg.codex_timeout_seconds,
            )
            answer = Path(output_file.name).read_text(encoding="utf-8", errors="replace").strip()
            if not answer:
                answer = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                LOG.warning("Codex exited with %s: %s", result.returncode, redact(result.stderr[-1000:]))
                if not answer:
                    answer = "Codex failed before producing a final answer. Check bridge logs."
            return clamp_text(redact(answer), 15000)
        except subprocess.TimeoutExpired:
            return "Codex timed out while processing this request."
        except Exception as exc:
            LOG.exception("Codex invocation failed")
            return f"Codex invocation failed: {redact(exc)}"
        finally:
            try:
                Path(output_file.name).unlink()
            except OSError:
                pass

    def scheduler_loop(self) -> None:
        LOG.info("Task scheduler started; timer wake window <= %ss", self.cfg.scheduler_wake_seconds)
        while not self.stop_event.is_set():
            try:
                for task in self.tasks.due_tasks():
                    self.run_scheduled_task(task)
                time.sleep(self.tasks.seconds_until_next())
            except Exception:
                LOG.exception("Scheduler loop failed")
                time.sleep(float(self.cfg.scheduler_wake_seconds))

    def run_scheduled_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("id") or "")
        prompt = str(task.get("prompt") or "")
        destination = task.get("destination") or {}
        chat_id = str(destination.get("chat_id") or self.cfg.notify_chat_id or "")
        if not task_id or not prompt or not chat_id:
            return
        LOG.info("Running scheduled task %s", task_id)
        response_id = self.send_card(chat_id, "Codex scheduled task", "Running scheduled task...")
        started = time.monotonic()
        answer = self.invoke_codex(prompt)
        answer_with_footer = (
            answer
            + "\n\n---\n"
            + codex_footer(
                self.cfg.codex_home,
                elapsed_seconds=time.monotonic() - started,
                tool_count=0,
            )
        )
        if response_id:
            self.update_card(response_id, "Codex scheduled task", answer_with_footer)
        else:
            self.send_card(chat_id, "Codex scheduled task", answer_with_footer)
        next_run = self.tasks.compute_next_run(task, after=now(self.cfg.timezone))
        self.tasks.update_next_run(task_id, next_run)

    def send_card(self, chat_id: str, title: str, body: str, reply_to: str | None = None) -> str:
        content = json_dumps(build_card(title, body))
        try:
            if reply_to:
                request_body = (
                    ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type("interactive")
                    .reply_in_thread(False)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                request = ReplyMessageRequest.builder().message_id(reply_to).request_body(request_body).build()
                response = self.client.im.v1.message.reply(request)
            else:
                request_body = (
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                request = (
                    CreateMessageRequest.builder()
                    .receive_id_type("open_id" if chat_id.startswith("ou_") else "chat_id")
                    .request_body(request_body)
                    .build()
                )
                response = self.client.im.v1.message.create(request)
            if not response or not response.success():
                LOG.warning("Feishu send failed: code=%s msg=%s", getattr(response, "code", None), getattr(response, "msg", None))
                return ""
            data = getattr(response, "data", None)
            return str(getattr(data, "message_id", "") or getattr(data, "open_message_id", "") or "")
        except Exception:
            LOG.exception("Feishu send failed")
            return ""

    def update_card(self, message_id: str, title: str, body: str) -> bool:
        content = json_dumps(build_card(title, body))
        try:
            request_body = (
                UpdateMessageRequestBody.builder()
                .msg_type("interactive")
                .content(content)
                .build()
            )
            request = UpdateMessageRequest.builder().message_id(message_id).request_body(request_body).build()
            response = self.client.im.v1.message.update(request)
            if response and response.success():
                return True
            LOG.warning("Feishu update failed: code=%s msg=%s", getattr(response, "code", None), getattr(response, "msg", None))
        except Exception:
            LOG.exception("Feishu update failed")
        return False


def build_card(title: str, body: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": clamp_text(title, 80)},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": clamp_text(body, 15000) or " ",
            }
        ],
    }


def mask_id(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=os.getenv("CODEX_FEISHU_HOME", str(Path.home() / ".codex-feishu")))
    parser.add_argument("--env-file", default=os.getenv("CODEX_FEISHU_ENV_FILE", ""))
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser()
    env_file = Path(args.env_file).expanduser() if args.env_file else home / ".env"
    home.mkdir(parents=True, exist_ok=True)
    load_dotenv(env_file)
    cfg = BridgeConfig.from_env(home, env_file)
    setup_logging(cfg.home)
    LOG.info("Bridge home=%s workspace=%s codex_home=%s", cfg.home, cfg.workspace, cfg.codex_home)
    if not shutil.which(cfg.codex_bin) and not Path(cfg.codex_bin).exists():
        LOG.warning("Codex binary not found yet: %s", cfg.codex_bin)
    bridge = FeishuBridge(cfg)
    bridge.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
