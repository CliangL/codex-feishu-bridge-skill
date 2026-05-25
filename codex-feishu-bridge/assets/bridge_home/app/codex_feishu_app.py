#!/usr/bin/env python3
"""
Local Codex <-> Feishu bridge.

This standalone app connects a dedicated Feishu bot to the local Codex
app-server session. It keeps a single-card Feishu UX: public progress first,
tool calls in a collapsed panel, then the final answer and runtime footer in
the same card.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import logging
import os
import queue
import re
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = Path(os.getenv("CODEX_FEISHU_HOME", Path.home() / ".codex-feishu"))
RUNTIME_SRC_DIR = Path(os.getenv("CODEX_FEISHU_RUNTIME_SRC", DEFAULT_HOME / "runtime" / "src"))
DEFAULT_ENV_FILE = Path(
    os.getenv("CODEX_FEISHU_ENV_FILE", Path.home() / ".codex-feishu" / ".env")
)
DEFAULT_WORKSPACE = Path(os.getenv("CODEX_FEISHU_WORKSPACE", DEFAULT_HOME / "workspace"))
DEFAULT_FEISHU_CODEX_HOME = Path(
    os.getenv("CODEX_FEISHU_CODEX_HOME", DEFAULT_HOME / "codex-home")
)
DEFAULT_CODEX_BIN = os.getenv("CODEX_FEISHU_CODEX_BIN") or os.getenv("CODEX_BIN") or "codex"
DEFAULT_SHARED_CODEX_HOME = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
FEISHU_MODEL_CONFIG_PATH = DEFAULT_HOME / "feishu-model.json"
SUPPORTED_FEISHU_MODELS: tuple[dict[str, str], ...] = (
    {"name": "gpt-5.5", "reasoning": "xhigh"},
    {"name": "gpt-5.4", "reasoning": "high"},
    {"name": "gpt-5.4", "reasoning": "medium"},
    {"name": "gpt-5.4-mini", "reasoning": "medium"},
)
_SUPPORTED_FEISHU_MODEL_KEYS = {
    f'{item["name"].strip().lower()}::{item["reasoning"].strip().lower()}': item
    for item in SUPPORTED_FEISHU_MODELS
}
_SUPPORTED_FEISHU_MODEL_NAMES = {item["name"].strip().lower() for item in SUPPORTED_FEISHU_MODELS}

if str(RUNTIME_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC_DIR))

from agent.transports.codex_app_server import CodexAppServerError  # noqa: E402
from agent.transports.codex_app_server_session import (  # noqa: E402
    CodexAppServerSession,
    TurnResult,
    _ServerRequestRouting,
)
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms import feishu as feishu_mod  # noqa: E402
from gateway.platforms.base import MessageEvent, ProcessingOutcome  # noqa: E402
from gateway.platforms.feishu import FeishuAdapter, check_feishu_requirements  # noqa: E402
from gateway.session import build_session_key  # noqa: E402

import conversation_memory  # noqa: E402
from shared_memory import build_shared_context  # noqa: E402
import tasks as shared_tasks  # noqa: E402


LOG = logging.getLogger("codex-feishu")
_ORIGINAL_LOG_RECORD_FACTORY = logging.getLogRecordFactory()
_MANAGED_CODEX_ENV_VALUES: dict[str, str] = {}


_SENSITIVE_LOG_VALUE_RE = re.compile(
    r"(?i)(\b(?:access_key|tenant_access_token|app_access_token|user_access_token|"
    r"refresh_token|client_secret|app_secret|secret|token|authorization)=)"
    r"(?:bearer|basic)?\s*([^&\s]+)"
)
_SENSITIVE_HEADER_RE = re.compile(
    r"(?i)(\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+"
)


def redact_sensitive_text(value: Any) -> str:
    text = str(value)
    text = _SENSITIVE_LOG_VALUE_RE.sub(r"\1***", text)
    text = _SENSITIVE_HEADER_RE.sub(r"\1***", text)
    return text


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_sensitive_text(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def install_secret_log_filter() -> None:
    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = _ORIGINAL_LOG_RECORD_FACTORY(*args, **kwargs)
        try:
            record.msg = redact_sensitive_text(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return record

    logging.setLogRecordFactory(record_factory)
    log_filter = SecretRedactingFilter()
    candidate_loggers = [
        logging.getLogger(),
        logging.getLogger("Lark"),
        logging.getLogger("lark_oapi"),
        logging.getLogger("codex-feishu"),
    ]
    for logger_obj in candidate_loggers:
        if not any(isinstance(existing, SecretRedactingFilter) for existing in logger_obj.filters):
            logger_obj.addFilter(log_filter)
        for handler in logger_obj.handlers:
            if not any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
                handler.addFilter(log_filter)


def load_dotenv(path: Path, *, override: bool = False) -> None:
    """Load a simple KEY=VALUE env file without printing secret values."""
    for key, value in read_env_file(path).items():
        if key and (override or key not in os.environ):
            os.environ[key] = value


def truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_env_value(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    try:
        parts = shlex.split(value, comments=False, posix=True)
    except ValueError:
        parts = []
    if parts:
        return parts[0]
    return value.strip().strip('"').strip("'")


def _parse_env_assignment(raw_line: str, key: str) -> Optional[str]:
    line = str(raw_line or "").strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    for prefix in ("export ", "declare -x "):
        if line.startswith(prefix):
            line = line[len(prefix) :].strip()
            break
    name, raw_value = line.split("=", 1)
    if name.strip() != key:
        return None
    return _parse_env_value(raw_value)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        for prefix in ("export ", "declare -x "):
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = _parse_env_value(raw_value)
    return values


def read_env_value_from_file(path: Path, key: str) -> Optional[str]:
    if not key:
        return None
    return read_env_file(path).get(key)


def ensure_dir(path: Path, *, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:
        pass
    return path


def ensure_file_parent(path: Path) -> Path:
    ensure_dir(path.parent)
    return path.parent


def symlink_or_copy(source: Path, target: Path) -> None:
    ensure_file_parent(target)
    if target.exists() or target.is_symlink():
        return
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
        return
    except OSError:
        pass
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    elif source.exists():
        shutil.copy2(source, target)


def load_feishu_model_prefs() -> dict[str, str]:
    try:
        data = json.loads(FEISHU_MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    model = str(data.get("model") or "").strip()
    reasoning = str(data.get("reasoning_effort") or data.get("reasoning") or "").strip()
    return {"model": model, "reasoning_effort": reasoning}


def save_feishu_model_prefs(model: str, reasoning_effort: str) -> Path:
    ensure_dir(DEFAULT_HOME)
    FEISHU_MODEL_CONFIG_PATH.write_text(
        json.dumps(
            {
                "model": str(model or "").strip(),
                "reasoning_effort": str(reasoning_effort or "").strip(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        FEISHU_MODEL_CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return FEISHU_MODEL_CONFIG_PATH


def resolve_feishu_model_choice(raw_text: str) -> Optional[dict[str, str]]:
    text = str(raw_text or "").strip()
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if normalized in _SUPPORTED_FEISHU_MODEL_NAMES:
        for item in SUPPORTED_FEISHU_MODELS:
            if item["name"].strip().lower() == normalized:
                return {
                    "model": item["name"],
                    "reasoning_effort": item["reasoning"],
                    "name": item["name"],
                    "reasoning": item["reasoning"],
                }
    compact = normalized.replace(" ", "")
    if compact in _SUPPORTED_FEISHU_MODEL_NAMES:
        for item in SUPPORTED_FEISHU_MODELS:
            if item["name"].strip().lower() == compact:
                return {
                    "model": item["name"],
                    "reasoning_effort": item["reasoning"],
                    "name": item["name"],
                    "reasoning": item["reasoning"],
                }
    tokens = [part for part in re.split(r"[\s/]+", normalized) if part]
    reasoning = ""
    model_tokens: list[str] = []
    for token in tokens:
        if token in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            reasoning = token
        else:
            model_tokens.append(token)
    model = " ".join(model_tokens).strip() or compact
    model = model.replace(" ", "")
    if model.startswith("/model"):
        model = model.removeprefix("/model").strip()
    key = f"{model.lower()}::{reasoning.lower()}"
    if reasoning and key in _SUPPORTED_FEISHU_MODEL_KEYS:
        item = _SUPPORTED_FEISHU_MODEL_KEYS[key]
        return {
            "model": item["name"],
            "reasoning_effort": item["reasoning"],
            "name": item["name"],
            "reasoning": item["reasoning"],
        }
    for item in SUPPORTED_FEISHU_MODELS:
        if item["name"].strip().lower() == model.lower():
            if not reasoning or item["reasoning"].strip().lower() == reasoning.lower():
                return {
                    "model": item["name"],
                    "reasoning_effort": item["reasoning"],
                    "name": item["name"],
                    "reasoning": item["reasoning"],
                }
    return None


def format_supported_feishu_models() -> str:
    return "\n".join(
        f"- {item['name']} {item['reasoning']}".strip() for item in SUPPORTED_FEISHU_MODELS
    )


def ensure_feishu_codex_home(
    codex_home: Optional[str | Path] = None,
    *,
    shared_codex_home: Optional[str | Path] = None,
) -> Path:
    target_home = (
        Path(codex_home).expanduser().resolve()
        if codex_home
        else DEFAULT_FEISHU_CODEX_HOME.expanduser().resolve()
    )
    shared_home = (
        Path(shared_codex_home).expanduser().resolve()
        if shared_codex_home
        else DEFAULT_SHARED_CODEX_HOME
    )
    ensure_dir(target_home)
    ensure_dir(target_home / "log")
    ensure_dir(target_home / "tmp")

    # Keep Feishu runtime isolated for model config, but continue to share the
    # user-visible Codex ecosystem pieces the user asked to keep in sync.
    shared_entries = (
        "skills",
        "automations",
        "memories",
        "sessions",
        "plugins",
        "browser",
        "shell_snapshots",
        "sqlite",
        "vendor_imports",
        "rules",
        "node_repl",
        "computer-use",
        "ambient-suggestions",
        ".tmp",
    )
    for name in shared_entries:
        source = shared_home / name
        if source.exists():
            symlink_or_copy(source, target_home / name)

    shared_files = (
        "auth.json",
        "installation_id",
        "version.json",
        ".codex-global-state.json",
        "models_cache.json",
    )
    for name in shared_files:
        source = shared_home / name
        if source.exists():
            symlink_or_copy(source, target_home / name)

    source_config = shared_home / "config.toml"
    target_config = target_home / "config.toml"
    if source_config.exists() and not target_config.exists():
        shutil.copy2(source_config, target_config)
        try:
            target_config.chmod(0o600)
        except OSError:
            pass

    prefs = load_feishu_model_prefs()
    config, _, _ = load_codex_config(target_home)
    selected_model = prefs.get("model") or str(config.get("model") or "").strip() or "gpt-5.4"
    reasoning = (
        prefs.get("reasoning_effort")
        or str(config.get("model_reasoning_effort") or config.get("reasoning_effort") or "").strip()
        or "high"
    )
    write_feishu_model_into_config(target_home, selected_model, reasoning)
    return target_home


def write_feishu_model_into_config(
    codex_home: str | Path,
    model: str,
    reasoning_effort: str,
) -> Path:
    home = Path(codex_home).expanduser().resolve()
    ensure_dir(home)
    source_path = DEFAULT_SHARED_CODEX_HOME / "config.toml"
    target_path = home / "config.toml"
    base_text = ""
    if target_path.exists():
        base_text = target_path.read_text(encoding="utf-8", errors="replace")
    elif source_path.exists():
        base_text = source_path.read_text(encoding="utf-8", errors="replace")

    lines = base_text.splitlines()
    rewritten = False
    inserted_reasoning = False
    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("model ="):
            lines[idx] = f'model = "{model}"'
            rewritten = True
        elif stripped.startswith("model_reasoning_effort ="):
            lines[idx] = f'model_reasoning_effort = "{reasoning_effort}"'
            inserted_reasoning = True
    if not rewritten:
        lines.insert(0, f'model = "{model}"')
    if not inserted_reasoning:
        insert_at = 1 if lines else 0
        lines.insert(insert_at, f'model_reasoning_effort = "{reasoning_effort}"')
    target_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        target_path.chmod(0o600)
    except OSError:
        pass
    save_feishu_model_prefs(model, reasoning_effort)
    return target_path


def codex_home_dir(codex_home: Optional[str | Path] = None) -> Path:
    if codex_home:
        return Path(codex_home).expanduser().resolve()
    return Path(os.getenv("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def load_codex_config(codex_home: Optional[str | Path] = None) -> tuple[dict[str, Any], Path, int]:
    config_path = codex_home_dir(codex_home) / "config.toml"
    try:
        stat = config_path.stat()
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        return config, config_path, int(stat.st_mtime_ns)
    except Exception:
        return {}, config_path, 0


def load_env_value_from_shell_snapshots(
    key: str,
    *,
    codex_home: Optional[str | Path] = None,
) -> tuple[Optional[str], str]:
    if not key:
        return None, ""
    snapshots_dir = codex_home_dir(codex_home) / "shell_snapshots"
    try:
        snapshots = sorted(
            snapshots_dir.glob("*.sh"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return None, ""
    for path in snapshots:
        value = read_env_value_from_file(path, key)
        if value:
            return value, path.name
    return None, ""


@dataclass(frozen=True)
class CodexRuntimeState:
    model_provider: str = ""
    model: str = ""
    reasoning_effort: str = ""
    env_key: str = ""
    env_present: bool = False
    env_source: str = "not required"
    env_hash: str = ""
    config_path: str = ""
    config_mtime_ns: int = 0

    @property
    def signature(self) -> tuple[str, str, str, str, str, int]:
        return (
            self.model_provider,
            self.model,
            self.reasoning_effort,
            self.env_key,
            self.env_hash,
            self.config_mtime_ns,
        )


def current_codex_runtime_state(
    *,
    codex_home: Optional[str | Path] = None,
    env_file: Optional[Path] = None,
) -> CodexRuntimeState:
    config, config_path, config_mtime_ns = load_codex_config(codex_home)
    provider = str(config.get("model_provider") or "").strip()
    model = str(config.get("model") or "").strip()
    reasoning_effort = str(
        config.get("model_reasoning_effort") or config.get("reasoning_effort") or ""
    ).strip()
    provider_config = ((config.get("model_providers") or {}).get(provider) or {})
    env_key = str(provider_config.get("env_key") or "").strip()
    if not env_key:
        return CodexRuntimeState(
            model_provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            config_path=str(config_path),
            config_mtime_ns=config_mtime_ns,
        )

    process_value = os.getenv(env_key, "").strip()
    managed_value = _MANAGED_CODEX_ENV_VALUES.get(env_key, "")
    env_value = ""
    env_source = "missing"

    if process_value and process_value != managed_value:
        env_value = process_value
        env_source = "process"
    elif env_file is not None:
        env_value = (read_env_value_from_file(env_file, env_key) or "").strip()
        if env_value:
            os.environ[env_key] = env_value
            _MANAGED_CODEX_ENV_VALUES[env_key] = env_value
            env_source = f"env-file:{env_file}"
    if not env_value:
        env_value, snapshot_name = load_env_value_from_shell_snapshots(env_key, codex_home=codex_home)
        env_value = (env_value or "").strip()
        if env_value:
            os.environ[env_key] = env_value
            _MANAGED_CODEX_ENV_VALUES[env_key] = env_value
            env_source = f"shell-snapshot:{snapshot_name}"
    if not env_value and process_value and process_value == managed_value:
        os.environ.pop(env_key, None)
        _MANAGED_CODEX_ENV_VALUES.pop(env_key, None)

    env_hash = hashlib.sha256(env_value.encode("utf-8")).hexdigest() if env_value else ""
    return CodexRuntimeState(
        model_provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        env_key=env_key,
        env_present=bool(env_value),
        env_source=env_source,
        env_hash=env_hash,
        config_path=str(config_path),
        config_mtime_ns=config_mtime_ns,
    )


def apply_codex_feishu_env(env_file: Path) -> dict[str, str]:
    """Load bridge-specific settings and map them for the Feishu adapter."""
    dotenv_values = read_env_file(env_file)
    for key, value in dotenv_values.items():
        if key.startswith("CODEX_FEISHU_") or key.startswith("FEISHU_"):
            os.environ[key] = value

    app_id = os.getenv("CODEX_FEISHU_APP_ID", "")
    app_secret = os.getenv("CODEX_FEISHU_APP_SECRET", "")

    mapping = {
        "FEISHU_GROUP_POLICY": os.getenv("CODEX_FEISHU_GROUP_POLICY", "allowlist"),
        "FEISHU_ALLOWED_USERS": os.getenv("CODEX_FEISHU_ALLOWED_USERS", ""),
        "FEISHU_CONNECTION_MODE": os.getenv("CODEX_FEISHU_CONNECTION_MODE", "websocket"),
        "FEISHU_DOMAIN": os.getenv("CODEX_FEISHU_DOMAIN", "feishu"),
        "FEISHU_REACTIONS": os.getenv("CODEX_FEISHU_REACTIONS", "true"),
        "FEISHU_BOT_OPEN_ID": os.getenv("CODEX_FEISHU_BOT_OPEN_ID", ""),
        "FEISHU_BOT_USER_ID": os.getenv("CODEX_FEISHU_BOT_USER_ID", ""),
        "FEISHU_BOT_NAME": os.getenv("CODEX_FEISHU_BOT_NAME", ""),
        "FEISHU_ENCRYPT_KEY": os.getenv("CODEX_FEISHU_ENCRYPT_KEY", ""),
        "FEISHU_VERIFICATION_TOKEN": os.getenv("CODEX_FEISHU_VERIFICATION_TOKEN", ""),
        "FEISHU_REQUIRE_MENTION": os.getenv("CODEX_FEISHU_REQUIRE_MENTION", ""),
        "FEISHU_ALLOW_BOTS": os.getenv("CODEX_FEISHU_ALLOW_BOTS", ""),
    }
    for key, value in mapping.items():
        if value != "":
            os.environ[key] = value

    return {
        "app_id": app_id.strip(),
        "app_secret": app_secret.strip(),
        "domain": os.getenv("CODEX_FEISHU_DOMAIN", os.getenv("FEISHU_DOMAIN", "feishu")).strip(),
        "connection_mode": os.getenv(
            "CODEX_FEISHU_CONNECTION_MODE",
            os.getenv("FEISHU_CONNECTION_MODE", "websocket"),
        ).strip(),
        "env_file": str(env_file),
    }


def redact(value: str, keep: int = 4) -> str:
    text = str(value or "")
    if len(text) <= keep * 2:
        return "***" if text else ""
    return f"{text[:keep]}...{text[-keep:]}"


def current_codex_env_key() -> str:
    return current_codex_runtime_state(env_file=DEFAULT_ENV_FILE).env_key


def now_ms() -> int:
    return int(time.time() * 1000)


def format_elapsed(ms: int) -> str:
    seconds = max(0, int(ms)) / 1000.0
    if seconds < 60:
        text = f"{seconds:.1f} 秒"
        return text.replace(".0 秒", " 秒")
    minutes = int(seconds // 60)
    rem = int(seconds % 60)
    return f"{minutes} 分 {rem:02d} 秒"


def format_codex_footer(
    state: CodexRuntimeState,
    *,
    elapsed_ms: int,
    tool_count: int,
) -> str:
    model = state.model or "默认模型"
    reasoning = state.reasoning_effort or "默认"
    return " · ".join(
        [
        model,
        reasoning,
        format_elapsed(elapsed_ms),
        f"{max(0, int(tool_count or 0))} 步工具调用",
        ]
    )


def parse_model_command(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw:
        return None
    match = re.match(r"(?is)^/model(?:\s+(.*))?$", raw)
    if not match:
        return None
    return (match.group(1) or "").strip()


def normalize_text(text: str, limit: int = 12000) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n\n...（输出过长，已截断）"


_TECHNICAL_PROGRESS_RE = re.compile(
    r"(?i)(/bin/(?:zsh|bash|sh)|\\b(?:sed|rg|grep|find|mkdir|cp|python3?|node|npm|git)\\b|"
    r"/Users/|\\.codex/|\\.hermes/|apply_patch|MCP|mcp|tool|工具调用|执行命令|运行代码|等待授权|准备修改|准备调用)"
)


def sanitize_thought_summary(text: str, *, limit: int = 900) -> Optional[str]:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return None
    kept: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _TECHNICAL_PROGRESS_RE.search(line):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    if not cleaned:
        return None
    return normalize_text(cleaned, limit)


def compact_public_text(text: str, *, limit: int = 220) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"`+", "", value)
    value = re.sub(r"(?:/Users|/Applications|/tmp|/var|/opt)/[^\s,，。；;]+", _compact_path_match, value)
    value = re.sub(r"(?<!:)(?:^|\s)/(?:[^\s,，。；;]+/)+[^\s,，。；;]+", _compact_path_match, value)
    value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "邮箱", value)
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


_PROGRESS_STEP_RE = re.compile(r"^\s*(?:[-*]\s*)?\d+[.、)]\s*")
_LOW_VALUE_PROGRESS_PHRASES = (
    "开始处理这条飞书消息",
    "结合本机共享记忆",
    "当前工作区判断需要做什么",
    "用户目标、本机记忆",
    "判断下一步应该查、改还是验证",
    "整理处理顺序",
    "避免误动无关功能",
    "处理顺序已经明确",
    "按检查、改动、验证的顺序推进",
    "已经形成可以给你的结论",
    "整理成正文",
    "整理最终正文",
)


def strip_progress_step(text: str) -> str:
    return _PROGRESS_STEP_RE.sub("", str(text or "").strip()).strip()


def is_low_value_progress(text: str) -> bool:
    body = strip_progress_step(text)
    if not body:
        return True
    return any(phrase in body for phrase in _LOW_VALUE_PROGRESS_PHRASES)


def normalize_progress_key(text: str) -> str:
    value = strip_progress_step(text)
    value = re.sub(r"[`*_~#>\-•·\s，。；：:,.!！?？（）()\[\]【】“”\"'、]+", "", value)
    value = re.sub(r"\bexit=\d+\b", "exit", value, flags=re.IGNORECASE)
    value = re.sub(r"\d+", "#", value)
    return value.lower()


def is_similar_progress_key(candidate: str, existing: list[str]) -> bool:
    key = normalize_progress_key(candidate)
    if not key:
        return True
    for previous in existing[-16:]:
        if key == previous:
            return True
        if len(key) >= 18 and len(previous) >= 18 and (key in previous or previous in key):
            return True
        if len(key) >= 24 and len(previous) >= 24:
            if difflib.SequenceMatcher(None, key, previous).ratio() >= 0.82:
                return True
    return False


def _compact_path_match(match: re.Match[str]) -> str:
    token = match.group(0)
    prefix = " " if token.startswith(" ") else ""
    raw = token.strip()
    suffix = ""
    path_part = raw
    colon_match = re.match(r"^(.+?)(:\d+(?::\d+)?)$", raw)
    if colon_match:
        path_part = colon_match.group(1)
        suffix = colon_match.group(2)
    name = Path(path_part).name or "本机文件"
    return f"{prefix}{name}{suffix}"


def shell_script_from_command(command: str) -> str:
    value = str(command or "").strip()
    if not value:
        return ""
    try:
        parts = shlex.split(value)
    except ValueError:
        return value
    for index, part in enumerate(parts[:-1]):
        if part == "-lc" and parts[index + 1]:
            return parts[index + 1]
    return value


def split_shell_words(script: str) -> list[str]:
    try:
        return shlex.split(script)
    except ValueError:
        return str(script or "").split()


def basename_list(values: list[str], *, limit: int = 3) -> str:
    names: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text.startswith("-"):
            continue
        names.append(Path(text).name or compact_public_text(text, limit=60))
        if len(names) >= limit:
            break
    return "、".join(names)


def extract_output_clue(output: Any, *, limit: int = 180) -> str:
    text = str(output or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SENSITIVE_HEADER_RE.search(line) or _SENSITIVE_LOG_VALUE_RE.search(line):
            continue
        if len(line) > 220:
            line = line[:217].rstrip() + "..."
        lines.append(line)
        if len(lines) >= 2:
            break
    if not lines:
        return ""
    clue = "；".join(lines)
    return compact_public_text(clue, limit=limit)


def summarize_search_output(output: Any, *, exit_code: Any = None) -> str:
    text = str(output or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        first = compact_public_text(lines[0], limit=150)
        return f"找到 {len(lines)} 条线索，第一条是 {first}"
    if exit_code == 1:
        return "没有找到匹配项；如果这一步是在查残留文案，说明残留已经清掉。"
    return ""


def summarize_launchctl_output(output: Any) -> str:
    text = str(output or "")
    state_match = re.search(r"\bstate\s*=\s*([A-Za-z_ -]+)", text)
    pid_match = re.search(r"\bpid\s*=\s*(\d+)", text)
    pieces: list[str] = []
    if state_match:
        pieces.append(f"服务状态是 {state_match.group(1).strip()}")
    if pid_match:
        pieces.append(f"进程号 {pid_match.group(1)}")
    return "，".join(pieces)


def classify_command_intent(command: str) -> str:
    value = str(command or "").lower()
    if any(token in value for token in ("py_compile", "bash -n", "pytest", "npm test", " pnpm test", " verify", " check")):
        return "verify"
    if any(token in value for token in ("mkdir", "cp ", "mv ", "rm ", "chmod", "apply_patch", "tee ", "install ")):
        return "modify"
    if any(token in value for token in ("find ", "rg ", "grep ", "sed -n", "cat ", "ls ", "stat ", "git status", "git diff")):
        return "inspect"
    return "operate"


def command_start_explanation(command: str) -> tuple[str, str]:
    script = shell_script_from_command(command)
    words = split_shell_words(script)
    value = str(script or command or "").lower()
    intent = classify_command_intent(command)
    first = words[0] if words else ""
    if "py_compile" in value:
        files = basename_list(words[words.index("py_compile") + 1 :] if "py_compile" in words else words)
        target = f"：{files}" if files else ""
        return intent, f"我在跑 Python 语法检查{target}，这是为了确认刚改的桥接代码能被解释器正常加载。"
    if "bash -n" in value:
        return intent, "我在检查脚本语法，先排除启动脚本层面的低级错误。"
    if "pytest" in value or "npm test" in value or "pnpm test" in value:
        return intent, "我在运行测试，用实际用例确认行为没有被改坏。"
    if first in {"rg", "grep"} or "rg " in value or "grep " in value:
        pattern = ""
        for word in words[1:]:
            if not word.startswith("-"):
                pattern = compact_public_text(word, limit=80)
                break
        suffix = f"“{pattern}”" if pattern else "相关关键词"
        return intent, f"我在搜索{suffix}，目的是找到这段行为到底由哪处代码或配置产生。"
    if first == "find" or "find " in value or first in {"ls", "stat"}:
        target = basename_list(words[1:]) or "相关目录"
        return intent, f"我在盘点 {target}，确认目标文件和目录是否真的存在。"
    if first == "sed" or "sed -n" in value or first == "cat":
        target = basename_list(words[-2:] if len(words) > 1 else words) or "相关文件"
        return intent, f"我在打开 {target}，先看清楚现有实现，再决定怎么改。"
    if "git diff" in value or "git status" in value:
        return intent, "我在核对本机改动状态，确认这次只改 Codex 飞书桥接，不碰无关内容。"
    if "mkdir" in value:
        return intent, "我在创建缺失目录，让后续文件写入有落点。"
    if "cp " in value or "mv " in value:
        return intent, "我在迁移或整理本机文件，让 Codex 自己能直接使用。"
    if "chmod" in value:
        return intent, "我在更新文件权限，避免后续执行时被权限挡住。"
    if "launchctl" in value:
        if "print" in value:
            return "verify", "我在读取 LaunchAgent 状态，确认飞书桥接是不是已经按新代码运行。"
        return "operate", "我在重启本机飞书桥接服务，让刚才的代码改动真正生效。"
    if "tail " in value or "log " in value:
        return "inspect", "我在看最新运行日志，确认服务启动、飞书连接和消息处理有没有报错。"
    return intent, "我在执行当前必要的本机步骤，拿实际结果来决定下一步。"


def command_complete_explanation(
    command: str,
    *,
    exit_code: Any = None,
    output: Any = "",
) -> tuple[str, str]:
    script = shell_script_from_command(command)
    words = split_shell_words(script)
    value = str(script or command or "").lower()
    intent = classify_command_intent(command)
    failed = exit_code not in (None, 0)
    if words and words[0] in {"rg", "grep"}:
        clue = summarize_search_output(output, exit_code=exit_code)
        if failed and exit_code != 1:
            return intent, f"搜索命令失败了，返回 exit={exit_code}；我会换一种方式定位。{(' ' + clue) if clue else ''}"
        return intent, f"搜索结果已经回来。{clue or '这次没有额外输出。'}"
    if "py_compile" in value:
        if failed:
            clue = extract_output_clue(output)
            return intent, f"语法检查没通过，说明刚才改动里还有语法问题；我会按报错位置继续修。{(' 报错：' + clue) if clue else ''}"
        return intent, "语法检查没有报错，说明这几个 Python 文件至少能正常加载。"
    if "launchctl" in value:
        clue = summarize_launchctl_output(output) or extract_output_clue(output)
        if failed:
            return intent, f"服务检查或重启没有成功，我需要继续看 launchd 返回的信息。{(' 看到：' + clue) if clue else ''}"
        return intent, f"服务检查或重启完成。{('看到：' + clue) if clue else '没有出现错误输出。'}"
    if "tail " in value or "log " in value:
        clue = extract_output_clue(output)
        if failed:
            return intent, f"读取日志失败了，我会改用别的方式确认服务状态。{(' 反馈：' + clue) if clue else ''}"
        return intent, f"日志已经读到，我会根据最新几行判断服务是否正常。{(' 看到：' + clue) if clue else ''}"
    clue = extract_output_clue(output)
    if failed:
        return intent, f"这一步返回 exit={exit_code}，没有按预期完成；我会根据反馈调整路线。{(' 反馈：' + clue) if clue else ''}"
    if intent == "inspect":
        return intent, f"检查有结果了，我会根据拿到的线索决定下一步。{(' 看到：' + clue) if clue else ''}"
    if intent == "modify":
        return intent, f"本机改动已经落下，接下来要验证它是否真的生效。{(' 反馈：' + clue) if clue else ''}"
    if intent == "verify":
        return intent, f"验证通过了，说明这一轮改动至少能正常加载或运行。{(' 输出：' + clue) if clue else ''}"
    return intent, f"这一步已经完成，我继续推进后面的判断。{(' 反馈：' + clue) if clue else ''}"


def progress_explanation(
    *,
    step: int,
    text: str,
    detail: str = "",
) -> str:
    body = compact_public_text(text, limit=260)
    if detail:
        body = f"{body}；{compact_public_text(detail, limit=160)}"
    return f"{step}. {body}"


def summarize_file_changes(changes: Any, *, limit: int = 160) -> str:
    if not isinstance(changes, list) or not changes:
        return ""
    parts: list[str] = []
    for change in changes[:3]:
        if not isinstance(change, dict):
            continue
        kind = (change.get("kind") or {}).get("type") if isinstance(change.get("kind"), dict) else change.get("kind")
        path = str(change.get("path") or "").strip()
        if not path:
            continue
        path = Path(path).name or compact_public_text(path, limit=48)
        parts.append(f"{kind or 'update'} {path}")
    if len(changes) > 3:
        parts.append(f"另 {len(changes) - 3} 项")
    return compact_public_text("、".join(parts), limit=limit)


def command_progress_line(
    command: str,
    *,
    step: int,
    started: bool,
    exit_code: Any = None,
    output: Any = "",
) -> str:
    if started:
        _, text = command_start_explanation(command)
        return progress_explanation(step=step, text=text)
    _, text = command_complete_explanation(command, exit_code=exit_code, output=output)
    return progress_explanation(step=step, text=text)


def summarize_item(notification: dict[str, Any]) -> Optional[str]:
    if notification.get("method") != "item/completed":
        return None
    item = (notification.get("params") or {}).get("item") or {}
    item_type = item.get("type") or ""
    if item_type == "commandExecution":
        command = str(item.get("command") or "").strip().replace("\n", " ")
        if len(command) > 120:
            command = command[:117] + "..."
        exit_code = item.get("exitCode")
        suffix = f" exit={exit_code}" if exit_code not in (None, 0) else ""
        return f"执行命令：`{command}`{suffix}" if command else "执行命令"
    if item_type == "fileChange":
        changes = item.get("changes") or []
        paths = []
        for change in changes[:3]:
            if isinstance(change, dict) and change.get("path"):
                paths.append(str(change["path"]))
        suffix = f"：{', '.join(paths)}" if paths else ""
        if len(changes) > 3:
            suffix += f"，另 {len(changes) - 3} 项"
        return f"修改文件{suffix}"
    if item_type == "mcpToolCall":
        server = item.get("server") or "mcp"
        tool = item.get("tool") or "tool"
        return f"调用 MCP：{server}.{tool}"
    if item_type == "dynamicToolCall":
        return f"调用工具：{item.get('name') or item.get('tool') or 'dynamic'}"
    return None


def summarize_public_progress(notification: dict[str, Any], *, step: int) -> Optional[str]:
    method = notification.get("method", "")
    if method == "turn/started":
        return None
    if method == "item/commandExecution/requestApproval":
        return progress_explanation(
            step=step,
            text="这里需要你授权一个本机操作。授权后我会继续执行；如果拒绝，我会换不需要该权限的办法。",
        )
    if method == "item/fileChange/requestApproval":
        return progress_explanation(
            step=step,
            text="这里需要你授权文件改动。授权后我会应用改动并继续验证。",
        )
    if method == "item/permissions/requestApproval":
        return progress_explanation(
            step=step,
            text="Codex 请求调整权限，我会按本机既定权限策略处理，不会静默扩大权限。",
        )
    if method == "mcpServer/elicitation/request":
        return progress_explanation(
            step=step,
            text="外部工具需要补充信息或授权，我会等这个条件满足后再继续。",
        )
    if method not in {"item/started", "item/completed"}:
        return None
    item = (notification.get("params") or {}).get("item") or {}
    item_type = item.get("type") or ""
    started = method == "item/started"
    if item_type == "reasoning":
        return None
    if item_type in {"plan", "hookPrompt"}:
        return None
    if item_type == "commandExecution":
        return command_progress_line(
            str(item.get("command") or ""),
            step=step,
            started=started,
            exit_code=item.get("exitCode"),
            output=item.get("aggregatedOutput") or item.get("output") or "",
        )
    if item_type == "fileChange":
        changes = item.get("changes") or []
        change_summary = summarize_file_changes(changes)
        if started:
            return progress_explanation(
                step=step,
                text="我准备修改文件，因为前面的检查已经定位到需要调整的实现位置。",
                detail=f"涉及：{change_summary}" if change_summary else "",
            )
        status = str(item.get("status") or "").lower()
        failed = status in {"failed", "error", "cancelled", "canceled"} or bool(item.get("error"))
        return progress_explanation(
            step=step,
            text="文件改动没有成功，我会根据失败原因调整后再试。"
            if failed
            else "文件改动已应用，下一步要检查语法、服务状态或实际效果。",
            detail=f"涉及：{change_summary}" if change_summary else "",
        )
    if item_type in {"mcpToolCall", "dynamicToolCall", "collabAgentToolCall"}:
        tool_name = item.get("tool") or item.get("name") or item.get("server") or "工具"
        tool_name = compact_public_text(str(tool_name), limit=80)
        if started:
            return progress_explanation(
                step=step,
                text=f"我调用 {tool_name} 来完成当前步骤，因为这类信息不能只靠猜，需要让工具给出实际结果。",
            )
        failed = bool(item.get("error")) or item.get("success") is False
        clue = extract_output_clue(
            item.get("result")
            or item.get("output")
            or item.get("error")
            or item.get("content")
            or ""
        )
        return progress_explanation(
            step=step,
            text=f"{tool_name} 没有完成，我会根据反馈换一种方式。"
            if failed
            else f"{tool_name} 返回了结果，我会把这个结果纳入下一步判断。",
            detail=f"看到：{clue}" if clue else "",
        )
    if item_type == "agentMessage":
        return None
    return None


def summarize_reasoning(notification: dict[str, Any], *, limit: int = 1200) -> Optional[str]:
    if notification.get("method") != "item/completed":
        return None
    item = (notification.get("params") or {}).get("item") or {}
    if item.get("type") != "reasoning":
        return None
    parts: list[str] = []
    for fragment in item.get("summary") or []:
        if isinstance(fragment, str):
            parts.append(fragment)
        elif isinstance(fragment, dict):
            text = fragment.get("text") or fragment.get("summary") or fragment.get("content")
            if text:
                parts.append(str(text))
    text = "\n".join(part.strip() for part in parts if part and part.strip())
    if not text:
        return None
    return sanitize_thought_summary(text, limit=limit)


def parse_task_request(text: str) -> Optional[dict[str, Any]]:
    """Parse a compact Feishu task command.

    Supported examples:
    - /task daily 08:30 检查今天日程
    - 定时 daily 08:30 检查今天日程
    - /task every 30m 检查下载状态
    - /task at 2026-05-25T09:00 提醒我...
    - /task weekly mon,wed@08:30 生成周报
    """
    raw = str(text or "").strip()
    prefixes = ("/task ", "task ", "定时 ", "计划任务 ")
    prefix = next((p for p in prefixes if raw.lower().startswith(p) or raw.startswith(p)), "")
    if not prefix:
        return None
    rest = raw[len(prefix):].strip()
    parts = rest.split(maxsplit=2)
    if len(parts) < 3:
        raise ValueError("格式：/task daily 08:30 任务内容；或 /task every 30m 任务内容")
    mode, value, prompt = parts[0].lower(), parts[1], parts[2].strip()
    if not prompt:
        raise ValueError("任务内容不能为空")
    if mode in {"daily", "每天"}:
        shared_tasks.parse_time(value)
        return {"kind": "daily", "value": value, "prompt": prompt}
    if mode in {"every", "interval", "每隔"}:
        minutes_text = value.lower().removesuffix("minutes").removesuffix("minute").removesuffix("mins").removesuffix("min")
        if minutes_text.endswith("m"):
            minutes_text = minutes_text[:-1]
        minutes = int(minutes_text)
        if minutes <= 0:
            raise ValueError("间隔分钟必须大于 0")
        return {"kind": "interval", "value": minutes, "prompt": prompt}
    if mode in {"at", "once", "一次"}:
        return {"kind": "once", "value": value, "prompt": prompt}
    if mode in {"weekly", "每周"}:
        shared_tasks.parse_weekly(value)
        return {"kind": "weekly", "value": value, "prompt": prompt}
    raise ValueError("支持 daily/every/at/weekly")


def create_task_from_request(
    request: dict[str, Any],
    *,
    workspace: Path,
    source: str,
    chat_id: str,
    reply_to: Optional[str],
) -> dict[str, Any]:
    class Args:
        pass

    args = Args()
    args.name = request["prompt"].splitlines()[0][:60]
    args.prompt = request["prompt"]
    args.workspace = str(workspace)
    args.timezone = shared_tasks.DEFAULT_TZ
    args.at = request["value"] if request["kind"] == "once" else None
    args.every_minutes = request["value"] if request["kind"] == "interval" else None
    args.daily_at = request["value"] if request["kind"] == "daily" else None
    args.weekly = request["value"] if request["kind"] == "weekly" else None
    args.notify_feishu_chat = os.getenv("CODEX_FEISHU_NOTIFY_CHAT_ID", "").strip() or chat_id
    args.notify_reply_to = ""
    args.source = source
    return shared_tasks.add_task(args)


def summarize_result(result: TurnResult, *, limit: int = 2000) -> str:
    text = (result.final_text or result.error or "").strip()
    if not text:
        return ""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    summary = "\n".join(lines[:8])
    if len(summary) > limit:
        summary = summary[:limit].rstrip() + "\n..."
    return summary


@dataclass(frozen=True)
class BusyMessageDecision:
    action: str
    reason: str


_STOP_COMMANDS = {"/stop", "stop", "停止", "终止", "中止"}
_NEW_SESSION_COMMANDS = {"/new", "/reset", "新对话", "开启新对话"}
_NO_INTERRUPT_RE = re.compile(r"(不(?:要|用)?中断|别中断|不中断|不(?:要|用)?打断|别打断|继续执行|当前任务继续)")
_BUSY_STATUS_RE = re.compile(
    r"(进度|到哪|哪一步|执行到|处理到|跑到|状态|现在.*(?:做|干嘛|执行|处理)|"
    r"你在干嘛|还要多久|多久能|卡住|完成了?吗|通过了?没|失败了吗|成功了吗)"
)
_BUSY_CORRECTION_RE = re.compile(
    r"(不对|错了|搞错|理解错|你理解|跑偏|偏了|我说的是|我的意思是|不是.*(?:而是|是|这个|那个|要|让你)|"
    r"这不是|不是这个|不是那个|要的是|需要的是|应该|改成|换成|调整为|改一下|纠正|重新|重来|"
    r"(?:别|不要|先别)(?:再|继续)?(?:改|动|删|写|执行|下载|安装|创建|处理|查|跑|弄|碰|操作|做)|"
    r"取消当前|停一下|暂停一下|和.*没关系|另一个任务)"
)
_BUSY_SUPPLEMENT_RE = re.compile(r"(补充|另外|还有|顺便|注意|加上|再加|漏了|忘了说|刚才|前面|上面|同时|附加)")
_BUSY_SUPPLEMENT_CHANGES_RE = re.compile(
    r"(当前|这次|这个任务|刚才|前面|上面|正在|那一步|这个|这条|要求|条件|限制|"
    r"一起|也要|也帮|也做|顺便.*(?:做|改|处理|查|看)|补充.*(?:要求|条件|限制))"
)
_BUSY_ACK_RE = re.compile(r"^(好|好的|收到|嗯|嗯嗯|行|可以|ok|okay|辛苦了|谢谢|谢了)[。.!！\s]*$", re.IGNORECASE)


def classify_busy_message(text: str) -> BusyMessageDecision:
    raw = str(text or "").strip()
    lowered = raw.lower()
    compact = re.sub(r"\s+", "", lowered)
    if not compact:
        return BusyMessageDecision("ack", "empty")
    if compact in _STOP_COMMANDS:
        return BusyMessageDecision("stop", "stop_command")
    if compact in _NEW_SESSION_COMMANDS:
        return BusyMessageDecision("interrupt", "new_session_command")

    no_interrupt_requested = bool(_NO_INTERRUPT_RE.search(compact))
    if no_interrupt_requested:
        if _BUSY_STATUS_RE.search(compact):
            return BusyMessageDecision("status", "status_question_without_interrupt")
        return BusyMessageDecision("queue", "explicit_no_interrupt")

    if _BUSY_CORRECTION_RE.search(compact):
        return BusyMessageDecision("interrupt", "correction_or_change")
    if _BUSY_STATUS_RE.search(compact):
        return BusyMessageDecision("status", "status_question")
    if _BUSY_ACK_RE.match(lowered.strip()):
        return BusyMessageDecision("ack", "short_ack")
    if _BUSY_SUPPLEMENT_RE.search(compact):
        if _BUSY_SUPPLEMENT_CHANGES_RE.search(compact) and not no_interrupt_requested:
            return BusyMessageDecision("interrupt", "supplement_changes_current_task")
        return BusyMessageDecision("queue", "supplement_without_interrupt")
    return BusyMessageDecision("queue", "separate_message")


@dataclass
class CardProgress:
    chat_id: str
    reply_to: Optional[str]
    session_key: str
    card_key: str
    adapter: FeishuAdapter
    prompt: str
    started_ms: int = field(default_factory=now_ms)
    tool_lines: list[str] = field(default_factory=list)
    tool_total: int = 0
    last_push_ms: int = 0
    last_reasoning: str = ""
    reasoning_lines: list[str] = field(default_factory=list)
    reasoning_keys: list[str] = field(default_factory=list)
    progress_step: int = 0
    active_item_steps: dict[str, int] = field(default_factory=dict)
    card_created: bool = False
    completed: bool = False
    last_error: str = ""
    notification_only: bool = False

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "_feishu_single_card": True,
            "_feishu_card_session_key": self.session_key,
            "_feishu_card_key": self.card_key,
            "_feishu_card_brand": "Codex",
            "reply_to_message_id": self.reply_to,
            "notify": True,
        }

    async def seed(self) -> None:
        if self.notification_only:
            return
        if self.completed:
            return
        result = await self.adapter._upsert_single_response_card(
            chat_id=self.chat_id,
            content=None,
            reply_to=self.reply_to,
            metadata=self.metadata,
            finalize=False,
        )
        if result.success:
            self.card_created = True
            self.last_error = ""
        else:
            self.last_error = str(result.error or "unknown send error")
        self.last_push_ms = now_ms()

    async def push_reasoning(self, line: str, *, force: bool = False) -> None:
        if self.notification_only:
            return
        if self.completed:
            return
        line = compact_public_text(line, limit=260)
        if not line:
            return
        if is_low_value_progress(line):
            return
        if is_similar_progress_key(line, self.reasoning_keys):
            return
        line = strip_progress_step(line)
        key = normalize_progress_key(line)
        current = now_ms()
        if not force and current - self.last_push_ms < 1500:
            return
        if not self.reasoning_lines or self.reasoning_lines[-1] != line:
            self.reasoning_lines.append(line)
            self.reasoning_keys.append(key)
            if len(self.reasoning_lines) > 12:
                self.reasoning_lines = self.reasoning_lines[-12:]
            if len(self.reasoning_keys) > 40:
                self.reasoning_keys = self.reasoning_keys[-40:]
        self.last_reasoning = "\n".join(f"- {item}" for item in self.reasoning_lines[-10:])
        result = await self.adapter._upsert_single_response_card(
            chat_id=self.chat_id,
            content=None,
            reply_to=self.reply_to,
            metadata=self.metadata,
            reasoning_text=line,
            tool_lines=self.tool_lines[-8:] or None,
            tool_total_count=self.tool_total or None,
            tool_elapsed_ms=current - self.started_ms,
            finalize=False,
        )
        if result.success:
            self.card_created = True
            self.last_error = ""
        else:
            self.last_error = str(result.error or "unknown send error")
        self.last_push_ms = current

    def step_for_event(self, notification: dict[str, Any]) -> int:
        method = str(notification.get("method") or "")
        item = (notification.get("params") or {}).get("item") or {}
        item_id = str(item.get("id") or "")
        if item_id:
            if item_id not in self.active_item_steps:
                self.progress_step += 1
                self.active_item_steps[item_id] = self.progress_step
            elif method == "item/completed":
                self.progress_step += 1
                self.active_item_steps[item_id] = self.progress_step
            step = self.active_item_steps.get(item_id) or self.progress_step
            if method == "item/completed":
                self.active_item_steps.pop(item_id, None)
            return step
        self.progress_step += 1
        return self.progress_step

    async def push_tool(self, line: str) -> None:
        if self.notification_only:
            return
        if self.completed:
            return
        line = str(line or "").strip()
        if not line:
            return
        self.tool_total += 1
        self.tool_lines.append(line)
        if len(self.tool_lines) > 20:
            self.tool_lines = self.tool_lines[-20:]
        result = await self.adapter._upsert_single_response_card(
            chat_id=self.chat_id,
            content=None,
            reply_to=self.reply_to,
            metadata=self.metadata,
            reasoning_text=None,
            tool_lines=self.tool_lines[-8:],
            tool_total_count=self.tool_total,
            tool_elapsed_ms=now_ms() - self.started_ms,
            finalize=False,
        )
        if result.success:
            self.card_created = True
            self.last_error = ""
        else:
            self.last_error = str(result.error or "unknown send error")
        self.last_push_ms = now_ms()

    async def final(self, text: str, footer: str) -> bool:
        if self.completed:
            return True
        tool_lines = None if self.notification_only else (self.tool_lines[-8:] or None)
        tool_total_count = None if self.notification_only else (self.tool_total or None)
        footer_text = "" if self.notification_only else footer
        result = await self.adapter._upsert_single_response_card(
            chat_id=self.chat_id,
            content=normalize_text(text) or "Codex 没有返回正文。",
            reply_to=self.reply_to,
            metadata=self.metadata,
            tool_lines=tool_lines,
            tool_total_count=tool_total_count,
            tool_elapsed_ms=now_ms() - self.started_ms,
            footer_text=footer_text,
            finalize=True,
        )
        if result.success:
            self.card_created = True
            self.completed = True
            self.last_error = ""
            return True
        self.last_error = str(result.error or "unknown send error")
        return False


@dataclass
class ActiveTurn:
    session_key: str
    chat_id: str
    message_id: Optional[str]
    progress: CardProgress
    kind: str = "message"
    session: Optional[CodexAppServerSession] = None
    interrupt_requested: bool = False
    interrupt_reason: str = ""
    queued_messages: list[dict[str, str]] = field(default_factory=list)
    queued_events: list[MessageEvent] = field(default_factory=list)

    def request_interrupt(self, reason: str) -> None:
        self.interrupt_requested = True
        self.interrupt_reason = reason
        if self.session is not None:
            self.session.request_interrupt()

    def queue_message(self, text: str, *, kind: str, reason: str) -> None:
        value = normalize_text(text, 1400)
        if not value:
            return
        self.queued_messages.append(
            {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "kind": kind,
                "reason": reason,
                "text": value,
            }
        )
        if len(self.queued_messages) > 20:
            self.queued_messages = self.queued_messages[-20:]

    def queue_event(self, event: MessageEvent, *, kind: str, reason: str) -> None:
        self.queue_message(event.text, kind=kind, reason=reason)
        self.queued_events.append(event)
        if len(self.queued_events) > 20:
            self.queued_events = self.queued_events[-20:]

    def pop_queued_events(self) -> list[MessageEvent]:
        queued = list(self.queued_events)
        self.queued_events.clear()
        return queued


@dataclass
class ApprovalWaiter:
    event: threading.Event = field(default_factory=threading.Event)
    choice: str = "deny"
    user_name: str = ""


class CodexFeishuAdapter(FeishuAdapter):
    """Feishu adapter with local Codex approval waiters.

    The reused Feishu adapter resolves approval buttons through its original
    approval registry. This bridge runs Codex directly, so it keeps a tiny
    in-process waiter table keyed by Codex session instead.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        self._codex_approval_waiters: dict[str, ApprovalWaiter] = {}
        self._codex_approval_lock = threading.Lock()

    async def _connect_websocket(self) -> None:
        """Use the SDK websocket setup, but keep connection URLs out of logs."""
        if not getattr(feishu_mod, "FEISHU_WEBSOCKET_AVAILABLE", False):
            raise RuntimeError("websockets not installed; websocket mode unavailable")

        lark_mod = getattr(feishu_mod, "lark")
        domain = feishu_mod.FEISHU_DOMAIN if self._domain_name != "lark" else feishu_mod.LARK_DOMAIN
        self._client = self._build_lark_client(domain)
        self._event_handler = self._build_event_handler()
        if self._event_handler is None:
            raise RuntimeError("failed to build Feishu event handler")
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("adapter loop is not ready")
        await self._hydrate_bot_identity()
        self._ws_client = feishu_mod.FeishuWSClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            log_level=lark_mod.LogLevel.WARNING,
            event_handler=self._event_handler,
            domain=domain,
        )
        self._ws_future = loop.run_in_executor(
            None,
            feishu_mod._run_official_feishu_ws_client,
            self._ws_client,
            self,
        )

    def register_codex_approval_waiter(self, session_key: str) -> ApprovalWaiter:
        waiter = ApprovalWaiter()
        with self._codex_approval_lock:
            self._codex_approval_waiters[session_key] = waiter
        return waiter

    def unregister_codex_approval_waiter(self, session_key: str, waiter: ApprovalWaiter) -> None:
        with self._codex_approval_lock:
            if self._codex_approval_waiters.get(session_key) is waiter:
                self._codex_approval_waiters.pop(session_key, None)

    def _handle_approval_card_action(self, *, event: Any, action_value: dict[str, Any], loop: Any) -> Any:
        approval_id = action_value.get("approval_id")
        response_cls = getattr(feishu_mod, "P2CardActionTriggerResponse", None)
        if approval_id is None:
            LOG.debug("Feishu card action missing approval_id")
            return response_cls() if response_cls else None

        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        if not self._is_interactive_operator_authorized(open_id):
            LOG.warning("Unauthorized Feishu approval click by %s", open_id or "<unknown>")
            return response_cls() if response_cls else None

        choice_map = getattr(feishu_mod, "_APPROVAL_CHOICE_MAP", {})
        choice = choice_map.get(action_value.get("hermes_action"), "deny")
        user_name = self._get_cached_sender_name(open_id) or open_id

        if not self._submit_on_loop(loop, self._resolve_approval(approval_id, choice, user_name)):
            return response_cls() if response_cls else None

        if response_cls is None:
            return None
        response = response_cls()
        callback_card_cls = getattr(feishu_mod, "CallBackCard", None)
        if callback_card_cls is not None:
            card = callback_card_cls()
            card.type = "raw"
            card.data = self._build_resolved_approval_card(choice=choice, user_name=user_name)
            response.card = card
        return response

    async def _resolve_approval(self, approval_id: Any, choice: str, user_name: str) -> None:
        state = self._approval_state.pop(approval_id, None)
        if not state:
            LOG.debug("Feishu approval %s already resolved or unknown", approval_id)
            return
        session_key = state.get("session_key", "")
        with self._codex_approval_lock:
            waiter = self._codex_approval_waiters.pop(session_key, None)
        if waiter is None:
            LOG.warning("No Codex approval waiter for session %s", session_key)
            return
        waiter.choice = choice or "deny"
        waiter.user_name = user_name or ""
        waiter.event.set()
        LOG.info("Feishu button resolved Codex approval for %s (choice=%s)", session_key, choice)


class FeishuApprovalBridge:
    """Synchronous approval callback backed by Feishu interactive cards."""

    def __init__(
        self,
        *,
        adapter: FeishuAdapter,
        chat_id: str,
        session_key: str,
        metadata: dict[str, Any],
        loop: asyncio.AbstractEventLoop,
        timeout_seconds: int,
    ) -> None:
        self.adapter = adapter
        self.chat_id = chat_id
        self.session_key = session_key
        self.metadata = metadata
        self.loop = loop
        self.timeout_seconds = timeout_seconds

    def __call__(self, command: str, description: str, *, allow_permanent: bool = False) -> str:
        if not isinstance(self.adapter, CodexFeishuAdapter):
            LOG.warning("approval bridge requires CodexFeishuAdapter; denying request")
            return "deny"

        waiter = self.adapter.register_codex_approval_waiter(self.session_key)

        async def _send() -> Any:
            return await self.adapter.send_exec_approval(
                chat_id=self.chat_id,
                command=command,
                session_key=self.session_key,
                description=description,
                metadata=self.metadata,
            )

        try:
            future = asyncio.run_coroutine_threadsafe(_send(), self.loop)
            send_result = future.result(timeout=30)
            if not send_result.success:
                LOG.warning("approval card send failed: %s", send_result.error)
                self.adapter.unregister_codex_approval_waiter(self.session_key, waiter)
                return "deny"
            if not waiter.event.wait(self.timeout_seconds):
                LOG.warning("approval timed out after %ss", self.timeout_seconds)
                self.adapter.unregister_codex_approval_waiter(self.session_key, waiter)
                return "deny"
        except Exception as exc:
            LOG.warning("approval flow failed: %s", exc)
            self.adapter.unregister_codex_approval_waiter(self.session_key, waiter)
            return "deny"

        choice = waiter.choice or "deny"
        if not allow_permanent and choice == "always":
            return "session"
        return choice


class CodexFeishuService:
    def __init__(
        self,
        *,
        adapter: FeishuAdapter,
        workspace: Path,
        codex_bin: str,
        codex_home: Optional[Path],
        env_file: Path,
        turn_timeout: float,
        approval_timeout: int,
        auto_approve_exec: bool,
        auto_approve_apply_patch: bool,
        permission_profile: str,
        task_poll_seconds: int,
        task_batch_limit: int,
    ) -> None:
        self.adapter = adapter
        self.workspace = workspace
        self.codex_bin = codex_bin
        self.codex_home = str(codex_home) if codex_home else None
        self.env_file = env_file
        self.turn_timeout = turn_timeout
        self.approval_timeout = approval_timeout
        self.auto_approve_exec = auto_approve_exec
        self.auto_approve_apply_patch = auto_approve_apply_patch
        self.permission_profile = str(permission_profile or "").strip() or None
        self.task_poll_seconds = max(5, int(task_poll_seconds))
        self.task_batch_limit = max(1, int(task_batch_limit))
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._task_runner_id = f"codex-feishu-app-{os.getpid()}"
        self._task_runner_stop: Optional[asyncio.Event] = None
        self._task_runner_wake: Optional[asyncio.Event] = None
        self._task_runner_task: Optional[asyncio.Task] = None
        self._runtime_signature: Optional[tuple[str, str, str, str, str, int]] = None
        self._turn_gate: Optional[asyncio.Lock] = None
        self._active_progress: dict[int, CardProgress] = {}
        self._active_turns: dict[str, ActiveTurn] = {}
        self._stopping = asyncio.Event()

    def model_help_text(self) -> str:
        runtime_state = current_codex_runtime_state(codex_home=self.codex_home, env_file=self.env_file)
        current = f"{runtime_state.model or '默认模型'} {runtime_state.reasoning_effort or '默认'}".strip()
        return "\n".join(
            [
                f"当前飞书模型：{current}",
                "",
                "支持切换：",
                format_supported_feishu_models(),
                "",
                "用法：",
                "- `/model` 查看当前模型",
                "- `/model gpt-5.4` 切到 gpt-5.4 high",
                "- `/model gpt-5.5` 切到 gpt-5.5 xhigh",
                "- `/model gpt-5.4 medium` 按指定思考强度切换",
            ]
        ).strip()

    def switch_feishu_model(self, raw_choice: str) -> tuple[bool, str]:
        choice = resolve_feishu_model_choice(raw_choice)
        if choice is None:
            return False, (
                "未识别这个飞书模型。\n\n"
                + self.model_help_text()
            )
        config_path = write_feishu_model_into_config(
            self.codex_home or DEFAULT_FEISHU_CODEX_HOME,
            choice["model"],
            choice["reasoning_effort"],
        )
        self._runtime_signature = None
        return True, "\n".join(
            [
                f"已切换飞书模型到：{choice['model']} {choice['reasoning_effort']}",
                "",
                "说明：这只影响飞书机器人链路，不影响桌面 Codex 当前模型。",
                f"配置已写入：`{config_path}`",
            ]
        ).strip()

    def session_key_for(self, event: MessageEvent) -> str:
        return "codex-feishu:" + build_session_key(
            event.source,
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
        )

    def get_session_lock(self, key: str) -> asyncio.Lock:
        lock = self.session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.session_locks[key] = lock
        return lock

    def get_turn_gate(self) -> asyncio.Lock:
        if self._turn_gate is None:
            self._turn_gate = asyncio.Lock()
        return self._turn_gate

    def close_sessions(self) -> None:
        return None

    def register_progress(self, progress: CardProgress) -> None:
        self._active_progress[id(progress)] = progress

    def unregister_progress(self, progress: CardProgress) -> None:
        self._active_progress.pop(id(progress), None)

    def active_turn_for(self, session_key: str) -> Optional[ActiveTurn]:
        return self._active_turns.get(session_key)

    def active_turn_for_chat(self, chat_id: str) -> Optional[ActiveTurn]:
        for active_turn in reversed(list(self._active_turns.values())):
            if active_turn.chat_id == chat_id:
                return active_turn
        return None

    def register_active_turn(self, active_turn: ActiveTurn) -> None:
        self._active_turns[active_turn.session_key] = active_turn

    def unregister_active_turn(self, active_turn: ActiveTurn) -> None:
        current = self._active_turns.get(active_turn.session_key)
        if current is active_turn:
            self._active_turns.pop(active_turn.session_key, None)

    def request_session_interrupt(self, session_key: str, reason: str) -> Optional[ActiveTurn]:
        active_turn = self._active_turns.get(session_key)
        if active_turn is not None:
            active_turn.request_interrupt(reason)
        return active_turn

    def request_chat_interrupt(self, chat_id: str, reason: str) -> Optional[ActiveTurn]:
        active_turn = self.active_turn_for_chat(chat_id)
        if active_turn is not None:
            active_turn.request_interrupt(reason)
        return active_turn

    def summarize_active_turn(self, active_turn: ActiveTurn) -> str:
        progress = active_turn.progress
        label = "定时任务" if active_turn.kind == "scheduled" else "飞书任务"
        prompt_preview = compact_public_text(progress.prompt, limit=180)
        lines = [
            f"当前有一个{label}正在执行，我没有中断它。",
            f"- 已运行：{format_elapsed(now_ms() - progress.started_ms)}",
            f"- 工具调用：{progress.tool_total} 步",
        ]
        if prompt_preview:
            lines.append(f"- 任务：{prompt_preview}")
        if active_turn.interrupt_requested:
            lines.append(f"- 状态：已收到中断请求，正在让本机 Codex 收尾。")
        elif progress.reasoning_lines:
            lines.append(f"- 状态：{progress.reasoning_lines[-1]}")
        else:
            lines.append("- 状态：本机 Codex 已接收任务，正在等待下一条执行进展。")
        if progress.reasoning_lines:
            lines.extend(["", "最近执行过程："])
            lines.extend(f"- {item}" for item in progress.reasoning_lines[-5:])
        if progress.tool_lines:
            lines.extend(["", "最近工具结果："])
            lines.extend(f"- {compact_public_text(item, limit=220)}" for item in progress.tool_lines[-3:])
        if active_turn.queued_messages:
            lines.append("")
            lines.append(f"运行中已收到 {len(active_turn.queued_messages)} 条未中断消息。")
        return "\n".join(lines).strip()

    async def reply_to_busy_message(
        self,
        event: MessageEvent,
        session_key: str,
        active_turn: ActiveTurn,
        decision: BusyMessageDecision,
    ) -> None:
        reply_key = f"{session_key}:busy:{event.message_id or now_ms()}"
        progress = CardProgress(
            chat_id=event.source.chat_id,
            reply_to=event.message_id,
            session_key=reply_key,
            card_key=reply_key,
            adapter=self.adapter,
            prompt=event.text,
        )
        if decision.action == "status":
            final_text = self.summarize_active_turn(active_turn)
        elif decision.action == "ack":
            final_text = "收到。当前任务继续执行，没有中断。"
        else:
            active_turn.queue_event(event, kind=decision.action, reason=decision.reason)
            if decision.reason == "supplement_without_interrupt":
                final_text = (
                    "收到补充。我先不打断当前任务；当前任务结束后会继续处理这条消息。"
                    "如果它需要立刻改变正在执行的方向，请直接说“改成…”或发 /stop。"
                )
            else:
                final_text = (
                    "收到这条消息。我判断它不像是在修改当前任务，所以当前任务继续执行；"
                    "当前任务结束后会继续处理这条消息。"
                )
            try:
                await active_turn.progress.push_reasoning(
                    "收到一条不中断当前任务的新消息，已排队，当前执行继续。",
                    force=True,
                )
            except Exception:
                LOG.debug("failed to append busy-message note to active card", exc_info=True)
        await progress.final(final_text, "运行中消息 · 未中断当前任务")
        conversation_memory.update_state(
            session_key,
            user_text=event.text,
            assistant_text=final_text,
            source=event.source.description,
        )

    async def finalize_active_progress(self, message: str) -> None:
        if not self._active_progress:
            return
        runtime_state = current_codex_runtime_state(codex_home=self.codex_home, env_file=self.env_file)
        for progress in list(self._active_progress.values()):
            if progress.completed or not progress.card_created:
                continue
            try:
                await progress.final(
                    message,
                    format_codex_footer(
                        runtime_state,
                        elapsed_ms=now_ms() - progress.started_ms,
                        tool_count=progress.tool_total,
                    ),
                )
            except Exception:
                LOG.debug("failed to finalize interrupted Feishu card", exc_info=True)

    def prepare_codex_runtime(self) -> CodexRuntimeState:
        state = current_codex_runtime_state(codex_home=self.codex_home, env_file=self.env_file)
        if state.env_key and not state.env_present:
            LOG.warning(
                "Current Codex provider requires env_key=%s but no value is available",
                state.env_key,
            )
        if self._runtime_signature is None:
            self._runtime_signature = state.signature
            LOG.info(
                "Codex runtime ready: provider=%s model=%s env_key=%s env_source=%s",
                state.model_provider or "<default>",
                state.model or "<default>",
                state.env_key or "not required",
                state.env_source,
            )
            return state
        if state.signature != self._runtime_signature:
            LOG.info(
                "Codex runtime changed; future Feishu turns will use the new runtime "
                "(provider=%s model=%s env_key=%s env_source=%s)",
                state.model_provider or "<default>",
                state.model or "<default>",
                state.env_key or "not required",
                state.env_source,
            )
            self._runtime_signature = state.signature
        return state

    def start_task_runner(self) -> None:
        if self._task_runner_task is not None and not self._task_runner_task.done():
            return
        self._task_runner_stop = asyncio.Event()
        self._task_runner_wake = asyncio.Event()
        self._task_runner_task = asyncio.create_task(self.task_runner_loop())

    async def stop_task_runner(self) -> None:
        if self._task_runner_stop is not None:
            self._task_runner_stop.set()
        if self._task_runner_wake is not None:
            self._task_runner_wake.set()
        task = self._task_runner_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task_runner_task = None
        self._task_runner_stop = None
        self._task_runner_wake = None

    def wake_task_runner(self) -> None:
        if self._task_runner_wake is not None:
            self._task_runner_wake.set()

    async def wait_for_task_due_or_wake(self, timeout: float) -> None:
        stop_event = self._task_runner_stop
        wake_event = self._task_runner_wake
        if stop_event is None:
            await asyncio.sleep(max(1.0, timeout))
            return
        waiters = [asyncio.create_task(stop_event.wait())]
        if wake_event is not None:
            waiters.append(asyncio.create_task(wake_event.wait()))
        try:
            done, pending = await asyncio.wait(waiters, timeout=max(1.0, timeout), return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if wake_event is not None and wake_event.is_set():
                wake_event.clear()
            for task in done:
                try:
                    task.result()
                except Exception:
                    pass
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()

    async def task_runner_loop(self) -> None:
        assert self.loop is not None
        try:
            synced_count = shared_tasks.sync_all_task_automations(codex_home=self.codex_home)
            if synced_count:
                LOG.info("Synced %s Feishu task(s) to Codex automations", synced_count)
        except Exception:
            LOG.exception("failed to sync Feishu tasks to Codex automations")
        LOG.info("Shared task runner started; timer-based scheduler")
        while self._task_runner_stop is not None and not self._task_runner_stop.is_set():
            try:
                claimed = shared_tasks.claim_due_tasks(
                    runner_id=self._task_runner_id,
                    limit=self.task_batch_limit,
                )
                for task in claimed:
                    await self.run_scheduled_task(task)
            except Exception:
                LOG.exception("shared task runner tick failed")
            wait_seconds = float(self.task_poll_seconds)
            try:
                seconds_until_due = shared_tasks.seconds_until_next_due()
                if seconds_until_due is not None:
                    wait_seconds = min(wait_seconds, max(1.0, float(seconds_until_due)))
            except Exception:
                LOG.debug("failed to compute next task wake time", exc_info=True)
            await self.wait_for_task_due_or_wake(wait_seconds)

    async def run_scheduled_task(self, task: dict[str, Any]) -> None:
        destination = task.get("destination") or {}
        chat_id = str(destination.get("chat_id") or "").strip()
        reply_to = str(destination.get("reply_to") or "").strip() or None
        session_key = f"codex-task:{task.get('id')}:{now_ms()}"
        progress = CardProgress(
            chat_id=chat_id,
            reply_to=reply_to,
            session_key=session_key,
            card_key=session_key,
            adapter=self.adapter,
            prompt=str(task.get("prompt") or ""),
            notification_only=True,
        )
        self.register_progress(progress)
        LOG.info("Running scheduled task %s (%s)", task.get("id"), task.get("name"))
        result: Optional[TurnResult] = None
        output_path = ""
        runtime_state = current_codex_runtime_state(codex_home=self.codex_home, env_file=self.env_file)
        final_delivery_ok = True
        final_delivery_error = ""
        active_turn = ActiveTurn(
            session_key=session_key,
            chat_id=chat_id,
            message_id=reply_to,
            progress=progress,
            kind="scheduled",
        )
        self.register_active_turn(active_turn)
        try:
            if chat_id:
                await progress.seed()
                if not progress.card_created:
                    LOG.warning(
                        "scheduled task %s initial Feishu card seed failed: chat_id=%s error=%s",
                        task.get("id"),
                        chat_id,
                        progress.last_error or "unknown error",
                    )
            result = await self.run_oneoff_codex_turn(
                prompt=self.build_scheduled_prompt(task),
                session_key=session_key,
                progress=progress,
                chat_id=chat_id,
                active_turn=active_turn,
            )
            output_path = self.write_task_output(task, result)
            success = not bool(result.error)
            summary = summarize_result(result)
            shared_tasks.complete_task(
                str(task["id"]),
                success=success,
                summary=summary,
                output_path=output_path,
            )
            if chat_id:
                final_text = result.final_text or "定时任务已完成，但没有返回正文。"
                if result.error:
                    final_text += f"\n\n> 执行期间提示：{result.error}"
                delivered = await progress.final(
                    final_text,
                    format_codex_footer(
                        runtime_state,
                        elapsed_ms=now_ms() - progress.started_ms,
                        tool_count=result.tool_iterations or progress.tool_total,
                    ),
                )
                if not delivered or not progress.card_created or not progress.completed:
                    final_delivery_ok = False
                    final_delivery_error = progress.last_error or "飞书通知卡片未成功送达指定会话。"
                    fallback_text = final_text
                    footer = format_codex_footer(
                        runtime_state,
                        elapsed_ms=now_ms() - progress.started_ms,
                        tool_count=result.tool_iterations or progress.tool_total,
                    )
                    if footer:
                        fallback_text = f"{final_text}\n\n{footer}"
                    if progress.notification_only:
                        fallback_text = final_text
                    fallback_result = await self.adapter.send(
                        chat_id=chat_id,
                        content=fallback_text,
                        reply_to=reply_to,
                        metadata={"notify": True},
                    )
                    if fallback_result.success:
                        final_delivery_ok = True
                        final_delivery_error = ""
                        LOG.warning(
                            "scheduled task %s single-card delivery failed; plain message fallback sent to chat_id=%s",
                            task.get("id"),
                            chat_id,
                        )
                    else:
                        final_delivery_error = str(
                            fallback_result.error or final_delivery_error or "fallback send failed"
                        )
            if not final_delivery_ok:
                summary = f"{summary}\n通知状态：{final_delivery_error}".strip()
                shared_tasks.complete_task(
                    str(task["id"]),
                    success=False,
                    summary=summary,
                    output_path=output_path,
                )
                LOG.error(
                    "scheduled task %s completed locally but Feishu delivery failed: chat_id=%s",
                    task.get("id"),
                    chat_id,
                )
        except Exception as exc:
            LOG.exception("scheduled task failed")
            shared_tasks.complete_task(str(task.get("id", "")), success=False, summary=str(exc), output_path=output_path)
            if chat_id:
                await progress.final(
                    f"定时任务执行异常：\n\n```text\n{normalize_text(str(exc), 4000)}\n```",
                    format_codex_footer(
                        runtime_state,
                        elapsed_ms=now_ms() - progress.started_ms,
                        tool_count=progress.tool_total,
                    ),
                )
        finally:
            self.unregister_active_turn(active_turn)
            self.unregister_progress(progress)

    async def run_oneoff_codex_turn(
        self,
        *,
        prompt: str,
        session_key: str,
        progress: CardProgress,
        chat_id: str,
        active_turn: Optional[ActiveTurn] = None,
    ) -> TurnResult:
        async with self.get_turn_gate():
            self.prepare_codex_runtime()
            routing = _ServerRequestRouting(
                auto_approve_exec=self.auto_approve_exec,
                auto_approve_apply_patch=self.auto_approve_apply_patch,
            )
            approval = None
            if chat_id:
                approval = FeishuApprovalBridge(
                    adapter=self.adapter,
                    chat_id=chat_id,
                    session_key=session_key,
                    metadata=progress.metadata,
                    loop=self.loop or asyncio.get_running_loop(),
                    timeout_seconds=self.approval_timeout,
                )
            session = CodexAppServerSession(
                cwd=str(self.workspace),
                codex_bin=self.codex_bin,
                codex_home=self.codex_home,
                permission_profile=self.permission_profile,
                approval_callback=approval,
                on_event=lambda note: self.on_codex_event(progress, note),
                request_routing=routing,
            )
            if active_turn is not None:
                active_turn.session = session
            result_queue: queue.Queue[TurnResult] = queue.Queue(maxsize=1)

            def worker() -> None:
                try:
                    result_queue.put(session.run_turn(prompt, turn_timeout=self.turn_timeout))
                except Exception as exc:
                    LOG.exception("scheduled codex turn crashed")
                    result_queue.put(TurnResult(error=str(exc), should_retire=True))
                finally:
                    try:
                        session.close()
                    except Exception:
                        LOG.debug("failed to close scheduled codex session", exc_info=True)

            thread = threading.Thread(target=worker, name=f"codex-scheduled-{hash(session_key) & 0xffff:x}", daemon=True)
            thread.start()
            while thread.is_alive():
                if self._stopping.is_set():
                    session.request_interrupt()
                    return TurnResult(error="本机 Codex-飞书桥接正在重启，本轮定时任务已中断。", should_retire=True)
                if active_turn is not None and active_turn.interrupt_requested:
                    session.request_interrupt()
                await asyncio.sleep(0.3)
            result = result_queue.get()
            if active_turn is not None and active_turn.interrupt_requested:
                return TurnResult(
                    error="已终止当前定时任务执行。",
                    interrupted=True,
                    should_retire=True,
                )
            return result

    def build_scheduled_prompt(self, task: dict[str, Any]) -> str:
        shared_context = build_shared_context(task.get("workspace") or self.workspace)
        return "\n".join(
            [
                "你是本机 Codex 共享定时任务执行器。",
                "不同 Codex 登录方式的云端 thread 不共享；当前任务必须依赖下面的本机共享记忆和当前工作区状态。",
                "不要泄露 API key、token、密码或完整 access key。",
                "飞书只是任务入口；实际执行必须使用本机当前 Codex 配置、skill 和工作区。",
                "如果任务需要创建或安装 skill，必须写入 Codex 可见路径，例如 $CODEX_HOME/skills 或 $HOME/.agents/skills；不要创建飞书专用 skill。",
                "如果需要新增或维护飞书通知型定时任务，必须通过 $HOME/.codex-feishu/app/tasks.py 的 add/pause/resume/delete 接口维护共享任务库，不要直接手写 tasks.json。",
                "",
                "## 共享本机记忆",
                shared_context,
                "",
                "## 定时任务",
                f"任务 ID：{task.get('id')}",
                f"任务名称：{task.get('name')}",
                f"工作目录：{task.get('workspace') or self.workspace}",
                "",
                str(task.get("prompt") or "").strip(),
                "",
                "最终回答请包含：完成情况、关键结果、验证结果，以及需要用户处理的事项。",
            ]
        )

    async def drain_queued_busy_events(
        self,
        queued_events: list[MessageEvent],
        *,
        original_session_key: str,
    ) -> None:
        if not queued_events:
            return
        for queued_event in queued_events:
            if not (queued_event.text or "").strip():
                continue
            try:
                await self.handle(queued_event)
            except Exception:
                LOG.exception(
                    "queued busy Feishu message failed session=%s message_id=%s",
                    original_session_key,
                    queued_event.message_id,
                )

    @staticmethod
    def write_task_output(task: dict[str, Any], result: TurnResult) -> str:
        runs_dir = Path(os.getenv("CODEX_FEISHU_HOME", Path.home() / ".codex-feishu")) / "runs"
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
        return str(path)

    async def handle(self, event: MessageEvent) -> None:
        if not (event.text or "").strip():
            return
        assert self.loop is not None
        chat_id = event.source.chat_id
        reply_to = event.message_id
        session_key = self.session_key_for(event)
        command_text = event.text.strip().lower()
        model_command_arg = parse_model_command(event.text)
        if command_text in {"/stop", "stop", "停止", "终止", "中止"}:
            await self.adapter.on_processing_start(event)
            outcome = ProcessingOutcome.SUCCESS
            try:
                interrupted = (
                    self.request_session_interrupt(session_key, "user_stop")
                    or self.request_chat_interrupt(chat_id, "user_stop")
                )
                progress = CardProgress(
                    chat_id=chat_id,
                    reply_to=reply_to,
                    session_key=f"{session_key}:stop:{reply_to or now_ms()}",
                    card_key=f"{session_key}:stop:{reply_to or now_ms()}",
                    adapter=self.adapter,
                    prompt=event.text,
                )
                if interrupted is None:
                    await progress.final("当前没有正在执行的 Codex 任务。", "Codex 本机执行 · stop")
                else:
                    await interrupted.progress.push_reasoning("收到 /stop，正在中断当前本机 Codex 任务。", force=True)
                    await progress.final("已请求终止当前 Codex 任务。正在执行中的卡片会收尾为已中断。", "Codex 本机执行 · stop")
                return
            except Exception:
                outcome = ProcessingOutcome.FAILURE
                LOG.exception("stop command failed")
                raise
            finally:
                await self.adapter.on_processing_complete(event, outcome)
        if model_command_arg is not None:
            await self.adapter.on_processing_start(event)
            outcome = ProcessingOutcome.SUCCESS
            progress = CardProgress(
                chat_id=chat_id,
                reply_to=reply_to,
                session_key=f"{session_key}:model:{reply_to or now_ms()}",
                card_key=f"{session_key}:model:{reply_to or now_ms()}",
                adapter=self.adapter,
                prompt=event.text,
            )
            try:
                await progress.seed()
                if not model_command_arg:
                    await progress.final(self.model_help_text(), "飞书模型状态")
                else:
                    ok, message = self.switch_feishu_model(model_command_arg)
                    await progress.final(message, "飞书模型已更新" if ok else "飞书模型切换失败")
                return
            except Exception:
                outcome = ProcessingOutcome.FAILURE
                LOG.exception("model command failed")
                raise
            finally:
                await self.adapter.on_processing_complete(event, outcome)
        running_turn = self.active_turn_for(session_key) or self.active_turn_for_chat(chat_id)
        if running_turn is not None:
            decision = classify_busy_message(event.text)
            LOG.info(
                "Busy Feishu message classified action=%s reason=%s session=%s kind=%s",
                decision.action,
                decision.reason,
                session_key,
                running_turn.kind,
            )
            if decision.action in {"status", "ack", "queue"}:
                await self.adapter.on_processing_start(event)
                outcome = ProcessingOutcome.SUCCESS
                try:
                    await self.reply_to_busy_message(event, session_key, running_turn, decision)
                    return
                except Exception:
                    outcome = ProcessingOutcome.FAILURE
                    LOG.exception("busy message reply failed")
                    raise
                finally:
                    await self.adapter.on_processing_complete(event, outcome)
            running_turn.request_interrupt("new_message")
            try:
                await running_turn.progress.push_reasoning("收到修正指令，正在中断当前任务并切换到新指令。", force=True)
            except Exception:
                LOG.debug("failed to update interrupted progress card", exc_info=True)
        lock = self.get_session_lock(session_key)
        async with lock:
            await self.adapter.on_processing_start(event)
            outcome = ProcessingOutcome.SUCCESS
            queued_events_to_drain: list[MessageEvent] = []
            progress = CardProgress(
                chat_id=chat_id,
                reply_to=reply_to,
                session_key=session_key,
                card_key=f"{session_key}:message:{reply_to or now_ms()}",
                adapter=self.adapter,
                prompt=event.text,
            )
            self.register_progress(progress)
            active_turn = ActiveTurn(
                session_key=session_key,
                chat_id=chat_id,
                message_id=reply_to,
                progress=progress,
            )
            self.register_active_turn(active_turn)
            try:
                await progress.seed()
                if command_text in {"/new", "/reset", "新对话", "开启新对话"}:
                    conversation_memory.reset_state(
                        session_key,
                        source=event.source.description,
                        reason=event.text.strip(),
                    )
                    await progress.final(
                        "已开启新的飞书对话。当前飞书会话的轻量上下文已清空；本机共享记忆、skills、自动化任务和 Codex 登录/API 配置不会受影响。",
                        "新对话已开启",
                    )
                    return
                task_request = parse_task_request(event.text)
                if task_request is not None:
                    task = create_task_from_request(
                        task_request,
                        workspace=self.workspace,
                        source=f"feishu:{event.source.description}",
                        chat_id=chat_id,
                        reply_to=reply_to,
                    )
                    self.wake_task_runner()
                    await progress.final(
                        "\n".join(
                            [
                                "已创建本机共享定时任务。",
                                "",
                                f"- 任务：{task.get('name')}",
                                f"- ID：`{task.get('id')}`",
                                f"- 下次执行：{task.get('next_run_at')}",
                                f"- Codex 自动化：`{task.get('codex_automation_id') or '未写入'}`",
                                f"- 通知窗口：`{(task.get('destination') or {}).get('chat_id') or '未配置'}`",
                                "",
                                "它会由本机统一任务 runner 按 next_run_at 到点执行，不是每 60 秒执行任务；飞书新建任务会主动唤醒调度器重新计算。执行时会读取当前 Codex 登录/API 状态和共享记忆，并已镜像到 Codex automations 便于查看。",
                            ]
                        ),
                        "Codex 本机任务库 · 已写入",
                    )
                    return
                result = await self.run_codex_turn(event, session_key, progress, active_turn)
                if result.error and self._stopping.is_set() and not progress.card_created:
                    outcome = ProcessingOutcome.FAILURE
                    LOG.info(
                        "Dropping interrupted Feishu turn before response card was created: message_id=%s",
                        event.message_id,
                    )
                    return
                if result.interrupted and result.error and not result.final_text:
                    final_text = normalize_text(result.error, 4000)
                elif result.error and not result.final_text:
                    outcome = ProcessingOutcome.FAILURE
                    final_text = f"Codex 执行失败：\n\n```text\n{normalize_text(result.error, 4000)}\n```"
                else:
                    final_text = result.final_text or "Codex 已完成，但没有返回正文。"
                    if result.error:
                        final_text += f"\n\n> 执行期间提示：{result.error}"
                if active_turn.queued_messages:
                    queued_events_to_drain = active_turn.pop_queued_events()
                    final_text += (
                        f"\n\n> 任务执行期间另收到 {len(active_turn.queued_messages)} 条未中断消息；"
                        "当前任务没有被这些消息打断，我会在本轮收尾后继续处理。"
                    )
                runtime_state = current_codex_runtime_state(
                    codex_home=self.codex_home,
                    env_file=self.env_file,
                )
                footer = format_codex_footer(
                    runtime_state,
                    elapsed_ms=now_ms() - progress.started_ms,
                    tool_count=result.tool_iterations or progress.tool_total,
                )
                await progress.final(final_text, footer)
                conversation_memory.update_state(
                    session_key,
                    user_text=event.text,
                    assistant_text=final_text,
                    source=event.source.description,
                )
                self.wake_task_runner()
            except Exception as exc:
                outcome = ProcessingOutcome.FAILURE
                LOG.exception("message handling failed")
                runtime_state = current_codex_runtime_state(
                    codex_home=self.codex_home,
                    env_file=self.env_file,
                )
                await progress.final(
                    f"本机 Codex 桥接异常：\n\n```text\n{normalize_text(str(exc), 4000)}\n```",
                    format_codex_footer(
                        runtime_state,
                        elapsed_ms=now_ms() - progress.started_ms,
                        tool_count=progress.tool_total,
                    ),
                )
            finally:
                self.unregister_active_turn(active_turn)
                self.unregister_progress(progress)
                await self.adapter.on_processing_complete(event, outcome)
        if queued_events_to_drain:
            await self.drain_queued_busy_events(queued_events_to_drain, original_session_key=session_key)

    async def run_codex_turn(
        self,
        event: MessageEvent,
        session_key: str,
        progress: CardProgress,
        active_turn: Optional[ActiveTurn] = None,
    ) -> TurnResult:
        async with self.get_turn_gate():
            self.prepare_codex_runtime()
            approval = FeishuApprovalBridge(
                adapter=self.adapter,
                chat_id=event.source.chat_id,
                session_key=session_key,
                metadata=progress.metadata,
                loop=self.loop or asyncio.get_running_loop(),
                timeout_seconds=self.approval_timeout,
            )
            routing = _ServerRequestRouting(
                auto_approve_exec=self.auto_approve_exec,
                auto_approve_apply_patch=self.auto_approve_apply_patch,
            )
            session = CodexAppServerSession(
                cwd=str(self.workspace),
                codex_bin=self.codex_bin,
                codex_home=self.codex_home,
                permission_profile=self.permission_profile,
                approval_callback=approval,
                on_event=lambda note: self.on_codex_event(progress, note),
                request_routing=routing,
            )
            if active_turn is not None:
                active_turn.session = session
            prompt = self.build_prompt(event)
            result_queue: queue.Queue[TurnResult] = queue.Queue(maxsize=1)

            def worker() -> None:
                try:
                    result_queue.put(
                        session.run_turn(
                            prompt,
                            turn_timeout=self.turn_timeout,
                        )
                    )
                except Exception as exc:
                    LOG.exception("codex turn crashed")
                    result = TurnResult(error=str(exc), should_retire=True)
                    result_queue.put(result)
                finally:
                    try:
                        session.close()
                    except Exception:
                        LOG.debug("failed to close one-shot codex session", exc_info=True)

            thread = threading.Thread(target=worker, name=f"codex-turn-{hash(session_key) & 0xffff:x}", daemon=True)
            thread.start()
            while thread.is_alive():
                if self._stopping.is_set():
                    session.request_interrupt()
                    return TurnResult(error="本机 Codex-飞书桥接正在重启，本轮消息未完成处理。", should_retire=True)
                if active_turn is not None and active_turn.interrupt_requested:
                    session.request_interrupt()
                await asyncio.sleep(0.3)
            result = result_queue.get()
            if active_turn is not None and active_turn.interrupt_requested:
                reason = active_turn.interrupt_reason
                if reason == "new_message":
                    return TurnResult(
                        error="已收到修正或改方向的飞书消息，本轮任务已中断并切换处理新指令。",
                        interrupted=True,
                        should_retire=True,
                    )
                if reason == "user_stop":
                    return TurnResult(
                        error="已按 /stop 终止当前 Codex 任务。",
                        interrupted=True,
                        should_retire=True,
                    )
            return result

    def on_codex_event(self, progress: CardProgress, notification: dict[str, Any]) -> None:
        if self.loop is None:
            return
        method = str(notification.get("method") or "")
        display_methods = {
            "turn/started",
            "item/started",
            "item/completed",
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "mcpServer/elicitation/request",
        }
        if method not in display_methods:
            return
        step = progress.step_for_event(notification)
        public_progress = summarize_public_progress(notification, step=step)
        if public_progress:
            asyncio.run_coroutine_threadsafe(
                progress.push_reasoning(public_progress, force=True),
                self.loop,
            )
        line = summarize_item(notification)
        if line:
            asyncio.run_coroutine_threadsafe(progress.push_tool(line), self.loop)
            return
        if public_progress:
            return
        reasoning = summarize_reasoning(notification)
        if reasoning:
            asyncio.run_coroutine_threadsafe(
                progress.push_reasoning(reasoning, force=True),
                self.loop,
            )

    def build_prompt(self, event: MessageEvent) -> str:
        shared_context = build_shared_context(self.workspace)
        session_key = self.session_key_for(event)
        feishu_context = conversation_memory.build_context(session_key)
        lines = [
            "你是通过飞书机器人接入的本机 Codex。飞书只是轻量接口层，实际执行必须使用本机当前 Codex。",
            "重要要求：",
            "- 可在当前工作区内读取、编辑、运行测试；涉及危险命令或越权操作会通过飞书按钮请求用户确认。",
            "- 不要泄露 API key、token、密码或完整 access key。",
            "- 最终回答要简洁说明做了什么、验证结果和需要用户知道的限制。",
            "- 你看到的是本机共享记忆层；不同 Codex 登录方式的云端 thread 不共享，但本机记忆和任务库共享。",
            "- 飞书对话采用独立轻量 agent 记忆：每轮都是新的本机 Codex turn，只带滚动摘要和最近几轮，避免长上下文拖慢和过度消耗 token。",
            "- 如果用户要求创建、安装或更新 skill，必须写入 Codex 可见路径，例如 $CODEX_HOME/skills 或 $HOME/.agents/skills；不要创建飞书专用 skill。",
            "- 如果用户通过飞书创建定时任务，要使用本机共享任务库；任务应能在 Codex automations 里被看到。",
            "- 创建、暂停、恢复或删除飞书通知型定时任务时，必须调用 $HOME/.codex-feishu/app/tasks.py 的 add/pause/resume/delete 接口，不要直接手写 ~/.codex-feishu/tasks.json；该接口会同步 Codex automations 镜像。",
            "",
            f"飞书来源：{event.source.description}",
            f"工作目录：{self.workspace}",
            "",
            "## 共享本机记忆",
            shared_context,
            "",
            "## 飞书轻量会话记忆",
            feishu_context,
        ]
        if event.reply_to_text:
            lines += ["", "用户回复的上文：", event.reply_to_text.strip()]
        if event.media_urls:
            lines += ["", "用户附件（本地缓存路径）："]
            lines.extend(f"- {path}" for path in event.media_urls)
        lines += ["", "用户请求：", event.text.strip()]
        return "\n".join(lines)


async def run_app(args: argparse.Namespace) -> None:
    env_file = Path(args.env_file).expanduser().resolve()
    feishu_settings = apply_codex_feishu_env(env_file)

    if args.allow_all_users:
        os.environ["FEISHU_GROUP_POLICY"] = "open"

    app_id = feishu_settings["app_id"]
    app_secret = feishu_settings["app_secret"]
    if not app_id or not app_secret:
        raise SystemExit(
            "缺少 CODEX_FEISHU_APP_ID / CODEX_FEISHU_APP_SECRET。请先运行 "
            "~/.codex-feishu/app/configure.sh 写入 Codex 专用飞书机器人凭据。"
        )

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        workspace.chmod(0o700)
    except OSError:
        pass
    codex_home = ensure_feishu_codex_home(args.codex_home or None)
    LOG.info(
        "Feishu app_id=%s, mode=%s, env=%s",
        redact(app_id),
        feishu_settings["connection_mode"],
        env_file,
    )
    LOG.info("Codex workspace=%s, codex_bin=%s, codex_home=%s", workspace, args.codex_bin, codex_home)

    config = PlatformConfig(
        enabled=True,
        extra={
            "app_id": app_id,
            "app_secret": app_secret,
            "domain": feishu_settings["domain"],
            "connection_mode": feishu_settings["connection_mode"],
            "group_sessions_per_user": True,
        },
    )
    adapter = CodexFeishuAdapter(config)
    service = CodexFeishuService(
        adapter=adapter,
        workspace=workspace,
        codex_bin=args.codex_bin,
        codex_home=codex_home,
        env_file=env_file,
        turn_timeout=args.turn_timeout,
        approval_timeout=args.approval_timeout,
        auto_approve_exec=args.auto_approve_exec,
        auto_approve_apply_patch=args.auto_approve_apply_patch,
        permission_profile=args.permission_profile,
        task_poll_seconds=args.task_poll_seconds,
        task_batch_limit=args.task_batch_limit,
    )
    service.loop = asyncio.get_running_loop()

    async def on_message(event: MessageEvent) -> None:
        await service.handle(event)
        return None

    adapter.set_message_handler(on_message)

    connected = await adapter.connect()
    if not connected:
        raise SystemExit(f"飞书连接失败：{adapter.fatal_error_message or 'unknown error'}")

    if args.connect_check:
        wait_seconds = max(1, int(args.connect_check_seconds))
        LOG.info("Feishu connection check passed; keeping connection for %ss.", wait_seconds)
        try:
            await asyncio.sleep(wait_seconds)
        finally:
            await adapter.disconnect()
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    LOG.info("Codex Feishu bridge is running. Send a DM to the bot or @ it in a group.")
    try:
        if not args.disable_task_runner:
            recovered = shared_tasks.recover_running_tasks()
            if recovered:
                LOG.warning("Recovered %s task(s) left in running state after restart", recovered)
            service.start_task_runner()
        await stop_event.wait()
    finally:
        LOG.info("Stopping...")
        service._stopping.set()
        await service.finalize_active_progress("本机 Codex-飞书桥接正在重启，本轮任务已中断。请重新发送这条消息。")
        await service.stop_task_runner()
        service.close_sessions()
        await adapter.disconnect()


def run_check(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser().resolve()
    feishu_settings = apply_codex_feishu_env(env_file)
    ok = True

    workspace = Path(args.workspace).expanduser().resolve()
    codex_bin = args.codex_bin
    codex_path = codex_bin if Path(codex_bin).expanduser().exists() else shutil.which(codex_bin)
    codex_home = ensure_feishu_codex_home(args.codex_home or None)
    codex_state = current_codex_runtime_state(codex_home=codex_home, env_file=env_file)
    env_check_detail = codex_state.env_key or "not required"
    if codex_state.env_key:
        env_check_detail += f" ({codex_state.env_source})"

    checks = [
        ("Adapter runtime", RUNTIME_SRC_DIR.exists(), str(RUNTIME_SRC_DIR)),
        ("Codex Feishu env file", env_file.exists(), str(env_file)),
        ("Workspace", workspace.exists(), str(workspace)),
        ("Codex CLI", bool(codex_path), str(codex_path or codex_bin)),
        (
            "Codex provider env_key",
            not codex_state.env_key or codex_state.env_present,
            env_check_detail,
        ),
        ("Feishu SDK", check_feishu_requirements(), "lark-oapi"),
        ("CODEX_FEISHU_APP_ID", bool(feishu_settings["app_id"]), redact(feishu_settings["app_id"])),
        ("CODEX_FEISHU_APP_SECRET", bool(feishu_settings["app_secret"]), "***" if feishu_settings["app_secret"] else ""),
    ]
    for name, passed, detail in checks:
        status = "OK" if passed else "MISSING"
        print(f"{status:7} {name}: {detail}")
        ok = ok and bool(passed)

    print(f"INFO    Feishu mode: {feishu_settings['connection_mode']}")
    print(f"INFO    Feishu domain: {feishu_settings['domain']}")
    print(f"INFO    Feishu codex home: {codex_home}")
    print(f"INFO    Codex provider: {codex_state.model_provider or '<default>'}")
    print(f"INFO    Codex model: {codex_state.model or '<default>'}")
    print(f"INFO    Codex reasoning: {codex_state.reasoning_effort or '<default>'}")
    print("INFO    Secret values are intentionally redacted.")
    return 0 if ok else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Codex <-> Feishu bridge")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Codex working directory")
    parser.add_argument("--codex-bin", default=DEFAULT_CODEX_BIN, help="Path/name of codex CLI")
    parser.add_argument("--codex-home", default=os.getenv("CODEX_HOME", ""), help="Optional CODEX_HOME")
    parser.add_argument("--turn-timeout", type=float, default=float(os.getenv("CODEX_FEISHU_TURN_TIMEOUT", "1800")))
    parser.add_argument("--approval-timeout", type=int, default=int(os.getenv("CODEX_FEISHU_APPROVAL_TIMEOUT", "300")))
    parser.add_argument(
        "--auto-approve-exec",
        action="store_true",
        default=truthy_env("CODEX_FEISHU_AUTO_APPROVE_EXEC"),
        help="Auto-approve Codex command execution requests",
    )
    parser.add_argument(
        "--auto-approve-apply-patch",
        action="store_true",
        default=truthy_env("CODEX_FEISHU_AUTO_APPROVE_APPLY_PATCH"),
        help="Auto-approve Codex file-change requests",
    )
    parser.add_argument(
        "--permission-profile",
        default=os.getenv("CODEX_FEISHU_PERMISSION_PROFILE", ""),
        help="Codex permission profile hint, e.g. workspace-write or full-access",
    )
    parser.add_argument("--allow-all-users", action="store_true", help="Open group access; otherwise FEISHU_ALLOWED_USERS applies")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="Codex Feishu env file")
    parser.add_argument("--connect-check", action="store_true", help="Connect to Feishu briefly and exit")
    parser.add_argument("--connect-check-seconds", type=int, default=10, help="Seconds to keep a --connect-check connection open")
    parser.add_argument("--disable-task-runner", action="store_true", help="Do not run due shared scheduled tasks inside this bridge")
    parser.add_argument("--task-poll-seconds", type=int, default=int(os.getenv("CODEX_FEISHU_TASK_POLL_SECONDS", "60")))
    parser.add_argument("--task-batch-limit", type=int, default=int(os.getenv("CODEX_FEISHU_TASK_BATCH_LIMIT", "3")))
    parser.add_argument("--check", action="store_true", help="Check local configuration without connecting to Feishu")
    parser.add_argument("--log-level", default=os.getenv("CODEX_FEISHU_LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The Feishu SDK logs websocket URLs at INFO, including short-lived
    # access_key query params. Keep those out of local bridge logs.
    install_secret_log_filter()
    logging.getLogger("Lark").setLevel(logging.WARNING)
    if args.check:
        return run_check(args)
    try:
        asyncio.run(run_app(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except CodexAppServerError as exc:
        LOG.error("Codex app-server error: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
