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
from typing import Any, Awaitable, Callable, Optional


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
FEISHU_FALLBACK_MODEL_CONFIG_PATH = DEFAULT_HOME / "feishu-fallback-models.json"
SUPPORTED_FEISHU_MODELS: tuple[dict[str, str], ...] = (
    {"provider": "", "name": "gpt-5.5", "reasoning": "xhigh"},
    {"provider": "", "name": "gpt-5.4", "reasoning": "high"},
    {"provider": "", "name": "gpt-5.4", "reasoning": "medium"},
    {"provider": "", "name": "gpt-5.4-mini", "reasoning": "medium"},
)
_FEISHU_REASONING_LEVELS = {"none", "minimal", "low", "medium", "high", "xhigh"}

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
    provider = str(
        data.get("model_provider") or data.get("provider") or data.get("profile") or ""
    ).strip()
    model = str(data.get("model") or "").strip()
    reasoning = str(data.get("reasoning_effort") or data.get("reasoning") or "").strip()
    return {"model_provider": provider, "provider": provider, "model": model, "reasoning_effort": reasoning}


def _model_pref_payload(model: str, reasoning_effort: str, model_provider: str = "") -> dict[str, str]:
    return {
        "model_provider": str(model_provider or "").strip(),
        "model": str(model or "").strip(),
        "reasoning_effort": str(reasoning_effort or "").strip(),
    }


def save_feishu_model_prefs(model: str, reasoning_effort: str, model_provider: str = "") -> Path:
    ensure_dir(DEFAULT_HOME)
    FEISHU_MODEL_CONFIG_PATH.write_text(
        json.dumps(_model_pref_payload(model, reasoning_effort, model_provider), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    try:
        FEISHU_MODEL_CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return FEISHU_MODEL_CONFIG_PATH


def _fallback_model_payload(choices: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    return {
        "models": [
            _model_pref_payload(
                str(choice.get("model") or choice.get("name") or ""),
                str(choice.get("reasoning_effort") or choice.get("reasoning") or ""),
                str(choice.get("model_provider") or choice.get("provider") or ""),
            )
            for choice in choices
        ]
    }


def load_feishu_fallback_model_specs() -> Optional[list[str]]:
    try:
        data = json.loads(FEISHU_FALLBACK_MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return []
    raw_models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(raw_models, list):
        return []
    specs: list[str] = []
    for item in raw_models:
        if isinstance(item, str):
            spec = item.strip()
        elif isinstance(item, dict):
            provider = str(item.get("model_provider") or item.get("provider") or "").strip()
            model = str(item.get("model") or item.get("name") or "").strip()
            reasoning = str(item.get("reasoning_effort") or item.get("reasoning") or "").strip()
            spec = "|".join(part for part in (provider, model, reasoning) if part)
        else:
            spec = ""
        if spec:
            specs.append(spec)
    return specs


def save_feishu_fallback_models(choices: list[dict[str, str]]) -> Path:
    ensure_dir(DEFAULT_HOME)
    FEISHU_FALLBACK_MODEL_CONFIG_PATH.write_text(
        json.dumps(_fallback_model_payload(choices), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        FEISHU_FALLBACK_MODEL_CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return FEISHU_FALLBACK_MODEL_CONFIG_PATH


def _normalize_model_provider(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _provider_display_name(provider_id: str, provider_config: dict[str, Any]) -> str:
    label = str(provider_config.get("name") or "").strip()
    return label or str(provider_id or "").strip() or "default"


def _provider_matches(token: str, provider_id: str, provider_config: dict[str, Any]) -> bool:
    wanted = _normalize_model_provider(token)
    if not wanted:
        return False
    candidates = {
        _normalize_model_provider(provider_id),
        _normalize_model_provider(_provider_display_name(provider_id, provider_config)),
    }
    base_url = str(provider_config.get("base_url") or "").strip()
    if base_url:
        candidates.add(_normalize_model_provider(base_url))
    return wanted in candidates


def configured_model_providers(codex_home: Optional[str | Path] = None) -> dict[str, dict[str, Any]]:
    config, _, _ = load_codex_config(codex_home)
    providers = config.get("model_providers") or {}
    if isinstance(providers, dict):
        return {
            str(provider_id): provider_config
            for provider_id, provider_config in providers.items()
            if isinstance(provider_config, dict)
        }
    return {}


def supported_feishu_model_choices(
    codex_home: Optional[str | Path] = None,
) -> list[dict[str, str]]:
    config, _, _ = load_codex_config(codex_home)
    providers = configured_model_providers(codex_home)
    current_provider = str(config.get("model_provider") or "").strip()
    provider_ids = list(providers)
    if current_provider and current_provider not in providers:
        provider_ids.insert(0, current_provider)
    if not provider_ids:
        provider_ids = [""]

    choices: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in SUPPORTED_FEISHU_MODELS:
        item_provider = str(item.get("provider") or "").strip()
        target_providers = [item_provider] if item_provider else provider_ids
        for provider_id in target_providers:
            provider_config = providers.get(provider_id, {})
            key = (
                provider_id,
                str(item["name"]).strip().lower(),
                str(item["reasoning"]).strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            choices.append(
                {
                    "provider": provider_id,
                    "model_provider": provider_id,
                    "provider_label": _provider_display_name(provider_id, provider_config),
                    "model": str(item["name"]).strip(),
                    "reasoning_effort": str(item["reasoning"]).strip(),
                    "name": str(item["name"]).strip(),
                    "reasoning": str(item["reasoning"]).strip(),
                }
            )
    return choices


def _parse_feishu_model_tokens(raw_text: str) -> tuple[str, str, list[str]]:
    text = str(raw_text or "").strip()
    if not text:
        return "", "", []
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    tokens = [part for part in re.split(r"[\s/]+", normalized) if part]
    reasoning = ""
    model_tokens: list[str] = []
    for token in tokens:
        if token in _FEISHU_REASONING_LEVELS:
            reasoning = token
        else:
            model_tokens.append(token)
    model = " ".join(model_tokens).strip()
    model = model.replace(" ", "")
    if model.startswith("/model"):
        model = model.removeprefix("/model").strip()
    return model, reasoning, model_tokens


def resolve_feishu_model_choice(
    raw_text: str,
    *,
    codex_home: Optional[str | Path] = None,
) -> tuple[Optional[dict[str, str]], str]:
    model, reasoning, model_tokens = _parse_feishu_model_tokens(raw_text)
    if not model:
        return None, "empty"

    providers = configured_model_providers(codex_home)
    provider_filter = ""
    if len(model_tokens) >= 2:
        first_token = model_tokens[0]
        for provider_id, provider_config in providers.items():
            if _provider_matches(first_token, provider_id, provider_config):
                provider_filter = provider_id
                model = "".join(model_tokens[1:]).strip()
                break

    matches: list[dict[str, str]] = []
    for item in supported_feishu_model_choices(codex_home):
        if provider_filter and item["model_provider"] != provider_filter:
            continue
        if item["model"].strip().lower() != model.lower():
            continue
        if reasoning and item["reasoning_effort"].strip().lower() != reasoning.lower():
            continue
        matches.append(item)

    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        providers_seen = {item["model_provider"] for item in matches}
        if len(providers_seen) > 1 and not provider_filter:
            return None, "ambiguous-provider"
        if not reasoning:
            return matches[0], ""
        return None, "ambiguous-reasoning"
    provider_id = provider_filter
    if not provider_id and len(providers) == 1:
        provider_id = next(iter(providers))
    if not provider_id:
        return None, "unknown"
    if not reasoning:
        return None, "missing-reasoning"
    if reasoning.lower() not in _FEISHU_REASONING_LEVELS:
        return None, "unknown-reasoning"
    provider_config = providers.get(provider_id, {})
    return (
        {
            "provider": provider_id,
            "model_provider": provider_id,
            "provider_label": _provider_display_name(provider_id, provider_config),
            "model": model,
            "reasoning_effort": reasoning.lower(),
            "name": model,
            "reasoning": reasoning.lower(),
        },
        "",
    )


def format_supported_feishu_models(codex_home: Optional[str | Path] = None) -> str:
    grouped: dict[str, list[str]] = {}
    for item in supported_feishu_model_choices(codex_home):
        provider = item["provider_label"]
        grouped.setdefault(provider, []).append(f"{item['model']} {item['reasoning_effort']}".strip())
    lines: list[str] = []
    for provider in sorted(grouped):
        values = sorted(set(grouped[provider]))
        lines.append(f"- {provider}: " + "，".join(values))
    return "\n".join(lines)


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
    selected_provider = (
        prefs.get("model_provider") or str(config.get("model_provider") or "").strip()
    )
    selected_model = prefs.get("model") or str(config.get("model") or "").strip() or "gpt-5.4"
    reasoning = (
        prefs.get("reasoning_effort")
        or str(config.get("model_reasoning_effort") or config.get("reasoning_effort") or "").strip()
        or "high"
    )
    write_feishu_model_into_config(target_home, selected_model, reasoning, selected_provider)
    return target_home


def write_feishu_model_into_config(
    codex_home: str | Path,
    model: str,
    reasoning_effort: str,
    model_provider: str = "",
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
    rewritten_provider = False
    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("model ="):
            lines[idx] = f'model = "{model}"'
            rewritten = True
        elif stripped.startswith("model_reasoning_effort ="):
            lines[idx] = f'model_reasoning_effort = "{reasoning_effort}"'
            inserted_reasoning = True
        elif stripped.startswith("model_provider =") and model_provider:
            lines[idx] = f'model_provider = "{model_provider}"'
            rewritten_provider = True
    if not rewritten:
        lines.insert(0, f'model = "{model}"')
    if model_provider and not rewritten_provider:
        lines.insert(0, f'model_provider = "{model_provider}"')
    if not inserted_reasoning:
        insert_at = 2 if model_provider and len(lines) >= 2 else 1 if lines else 0
        lines.insert(insert_at, f'model_reasoning_effort = "{reasoning_effort}"')
    target_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        target_path.chmod(0o600)
    except OSError:
        pass
    save_feishu_model_prefs(model, reasoning_effort, model_provider)
    return target_path


def _model_choice_identity(choice: dict[str, str]) -> tuple[str, str, str]:
    return (
        str(choice.get("model_provider") or choice.get("provider") or "").strip(),
        str(choice.get("model") or choice.get("name") or "").strip(),
        str(choice.get("reasoning_effort") or choice.get("reasoning") or "").strip(),
    )


def _current_model_choice(codex_home: Optional[str | Path] = None) -> dict[str, str]:
    config, _, _ = load_codex_config(codex_home)
    return _model_pref_payload(
        str(config.get("model") or "").strip(),
        str(config.get("model_reasoning_effort") or config.get("reasoning_effort") or "").strip(),
        str(config.get("model_provider") or "").strip(),
    )


def parse_feishu_fallback_model_choices(
    value: str,
    *,
    codex_home: Optional[str | Path] = None,
) -> list[dict[str, str]]:
    """Parse fallback model specs from env.

    Supported separators:
      - newline, comma, or semicolon between choices
      - `provider|model|reasoning`, `provider/model/reasoning`, or
        the same free-form syntax accepted by `/model`
    """
    raw = str(value or "").strip()
    if not raw:
        return []
    specs = [item.strip() for item in re.split(r"[\n,;]+", raw) if item.strip()]
    choices: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for spec in specs:
        normalized = spec
        if "|" in normalized:
            parts = [part.strip() for part in normalized.split("|") if part.strip()]
            normalized = " ".join(parts)
        choice, reason = resolve_feishu_model_choice(normalized, codex_home=codex_home)
        if choice is None:
            LOG.warning("Ignoring invalid Feishu fallback model spec %r: %s", spec, reason)
            continue
        identity = _model_choice_identity(choice)
        if identity in seen:
            continue
        seen.add(identity)
        choices.append(choice)
    return choices


_FALLBACK_ERROR_RE = re.compile(
    r"(?i)("
    r"余额不足|额度不足|余额不够|账户余额|欠费|充值|"
    r"insufficient[_ -]?(?:balance|quota|credit|funds)|"
    r"quota|credit|balance|billing|payment|required|"
    r"rate[_ -]?limit|too many requests|429|"
    r"unauthorized|invalid api key|invalid_api_key|401|403|"
    r"503|502|500|upstream|overloaded|temporarily unavailable|timeout|timed out"
    r")"
)


def should_try_fallback_model(result: TurnResult) -> bool:
    if result.interrupted:
        return False
    text = "\n".join(
        part
        for part in (
            str(result.error or ""),
            str(result.final_text or ""),
        )
        if part
    )
    if not text:
        return False
    return bool(_FALLBACK_ERROR_RE.search(text))


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


def parse_fallback_model_command(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw:
        return None
    match = re.match(r"(?is)^/(?:fallback-model|fallback|backup-model)(?:\s+(.*))?$", raw)
    if not match:
        return None
    return (match.group(1) or "").strip()


def normalize_text(text: str, limit: int = 12000) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n\n...（输出过长，已截断）"


_TECHNICAL_PROGRESS_RE = re.compile(
    r"(?i)(/bin/(?:zsh|bash|sh)|\b(?:sed|rg|grep|find|mkdir|cp|python3?|node|npm|git)\b|"
    r"/Users/|\\.codex/|\\.hermes/|apply_patch|MCP|mcp|tool|工具调用|执行命令|运行代码|"
    r"等待授权|准备修改|准备调用|准备在本机执行|本机执行命令|/Applications/|/tmp/|/var/|/opt/)"
    r"|exit=\d+"
)
_STRUCTURED_BLOB_PROGRESS_RE = re.compile(
    r"(?is)(看到：\s*[\{\[]|['\"]content['\"]\s*:|['\"]type['\"]\s*:|['\"]text['\"]\s*:|"
    r"\\n|github_fetch\s*返回了结果|mcp.*返回了结果|tool.*返回了结果)"
)
_PUBLIC_PROGRESS_MARKER_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:执行进展|公开进展|进展|状态更新)\s*[:：]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_SENSITIVE_TOOL_ARG_KEY_RE = re.compile(
    r"(?i)(secret|token|password|passwd|authorization|api[_-]?key|access[_-]?key|refresh[_-]?token)"
)
_VERBOSE_TOOL_ARG_KEY_RE = re.compile(
    r"(?i)^(content|contents|text|body|file_content|data|payload|result|output|"
    r"aggregated_output|image|image_url|bytes|base64|blob|diff|patch)$"
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
        if _STRUCTURED_BLOB_PROGRESS_RE.search(line):
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
    "本机 Codex 已接收",
    "Codex 正在分析",
    "已收到请求",
    "开始分析请求",
    "形成回复草稿",
    "正在分析任务",
    "正在读取共享记忆",
    "整理需要确认的目标和步骤",
    "我在理解你的目标",
    "我在理解你的请求",
    "我会先核对已有上下文",
)
_GENERIC_TOOL_SUMMARY_PHRASES = {
    "工具步骤已完成。",
    "工具步骤未完成，已根据反馈调整路线。",
    "检查步骤已完成。",
    "本机改动已写入。",
    "验证步骤已通过。",
    "文件和目录已经确认。",
    "搜索完成，相关线索已经定位。",
    "相关文件已读到，可以继续判断和改动。",
    "文件位置已经确认，后续可以直接读取或修改目标文件。",
    "搜索结果已经回来，我会只提炼和任务有关的线索，不展开匹配原文。",
    "这一步没有按预期完成，我会根据反馈换一种方式继续确认。",
    "本机改动已经落下，接下来要验证它是否真的生效。",
}


def strip_progress_step(text: str) -> str:
    value = _PROGRESS_STEP_RE.sub("", str(text or "").strip()).strip()
    match = _PUBLIC_PROGRESS_MARKER_RE.match(value)
    if match:
        return match.group(1).strip()
    return value


def extract_public_progress_notes(text: str, *, limit: int = 6) -> list[str]:
    """Extract model-authored public progress notes.

    These are not private chain-of-thought. They are explicit, user-facing
    execution updates emitted with a marker so Feishu can display them in the
    progress area while keeping the final answer clean.
    """
    notes: list[str] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        match = _PUBLIC_PROGRESS_MARKER_RE.match(raw_line)
        if not match:
            continue
        note = compact_public_text(match.group(1), limit=260)
        if note and not is_low_value_progress(note):
            notes.append(note)
        if len(notes) >= limit:
            break
    return notes


def strip_public_progress_notes(text: str) -> str:
    kept: list[str] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if _PUBLIC_PROGRESS_MARKER_RE.match(raw_line):
            continue
        kept.append(raw_line)
    return "\n".join(kept).strip()


def is_low_value_progress(text: str) -> bool:
    body = strip_progress_step(text)
    if not body:
        return True
    if _TECHNICAL_PROGRESS_RE.search(body):
        return True
    if _STRUCTURED_BLOB_PROGRESS_RE.search(body):
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


def clean_public_progress_lines(
    lines: list[str],
    *,
    max_lines: int = 10,
) -> tuple[list[str], list[str]]:
    cleaned: list[str] = []
    keys: list[str] = []
    for raw in lines:
        line = compact_public_text(strip_progress_step(str(raw or "")), limit=260)
        if not line or is_low_value_progress(line):
            continue
        if is_similar_progress_key(line, keys):
            continue
        cleaned.append(line)
        keys.append(normalize_progress_key(line))
    if len(cleaned) > max_lines:
        cleaned = cleaned[-max_lines:]
    if len(keys) > 40:
        keys = keys[-40:]
    return cleaned, keys


def clean_tool_lines(lines: list[str], *, max_lines: int = 8) -> list[str]:
    cleaned: list[str] = []
    keys: list[str] = []
    for raw in lines:
        line = compact_public_text(redact_sensitive_text(str(raw or "")), limit=220)
        if not line:
            continue
        if line in _GENERIC_TOOL_SUMMARY_PHRASES:
            continue
        if _STRUCTURED_BLOB_PROGRESS_RE.search(line) and not line.startswith(("调用", "执行", "修改")):
            continue
        key = normalize_tool_key(line)
        if not key or key in keys:
            continue
        cleaned.append(line)
        keys.append(key)
    return cleaned[-max_lines:]


def normalize_tool_key(text: str) -> str:
    value = redact_sensitive_text(str(text or "")).strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def public_tool_name(item: dict[str, Any]) -> str:
    raw = (
        item.get("tool")
        or item.get("name")
        or item.get("server")
        or "工具"
    )
    name = compact_public_text(str(raw), limit=80)
    lowered = name.lower()
    if lowered.startswith("github_") or lowered in {"github", "github.fetch", "github_fetch"}:
        if "fetch" in lowered:
            return "GitHub 文件读取"
        if "search" in lowered:
            return "GitHub 搜索"
        if "pr" in lowered or "pull" in lowered:
            return "GitHub PR 检查"
        if "issue" in lowered:
            return "GitHub issue 检查"
        return "GitHub 查询"
    if lowered.startswith("web") or "browser" in lowered:
        return "网页查询"
    if lowered.startswith("lark") or lowered.startswith("feishu"):
        return "飞书接口调用"
    return name or "工具"


def raw_tool_name(item: dict[str, Any]) -> str:
    raw = (
        item.get("tool")
        or item.get("name")
        or item.get("server")
        or "工具"
    )
    return compact_public_text(str(raw), limit=96) or "工具"


def tool_server_name(item: dict[str, Any]) -> str:
    server = str(item.get("server") or "").strip()
    return compact_public_text(server, limit=80)


def public_tool_start_text(tool_name: str) -> str:
    if tool_name.startswith("GitHub"):
        return f"我在用{tool_name}补齐外部仓库信息。"
    if tool_name == "网页查询":
        return "我在查询网页信息，用公开资料补齐判断依据。"
    if tool_name == "飞书接口调用":
        return "我在调用飞书接口确认实际消息或配置状态。"
    return f"我在调用{tool_name}获取实际结果。"


def public_tool_done_text(tool_name: str, *, failed: bool) -> str:
    if failed:
        return f"{tool_name}没有完成，我会根据错误换一种方式继续。"
    if tool_name.startswith("GitHub"):
        return f"{tool_name}完成，我会只提炼结论，不展开文件正文。"
    if tool_name == "网页查询":
        return "网页查询完成，我会把公开资料里的关键点纳入判断。"
    if tool_name == "飞书接口调用":
        return "飞书接口调用完成，实际状态已经确认。"
    return f"{tool_name}返回了结果，我会提炼关键结论继续。"


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


def sanitize_tool_argument(value: Any, *, depth: int = 0) -> Any:
    if depth > 2:
        return "..."
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_TOOL_ARG_KEY_RE.search(key_text):
                sanitized[key_text] = "***"
            elif _VERBOSE_TOOL_ARG_KEY_RE.search(key_text):
                sanitized[key_text] = "[已省略]"
            else:
                sanitized[key_text] = sanitize_tool_argument(item, depth=depth + 1)
        return sanitized
    if isinstance(value, list):
        items = [sanitize_tool_argument(item, depth=depth + 1) for item in value[:3]]
        if len(value) > 3:
            items.append(f"...另 {len(value) - 3} 项")
        return items
    if isinstance(value, str):
        return compact_public_text(redact_sensitive_text(value), limit=120)
    return value


def compact_tool_arguments(arguments: Any, *, limit: int = 150) -> str:
    if arguments in (None, "", {}, []):
        return ""
    try:
        sanitized = sanitize_tool_argument(arguments)
        text = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(arguments)
    text = compact_public_text(redact_sensitive_text(text), limit=limit)
    if _STRUCTURED_BLOB_PROGRESS_RE.search(text):
        return ""
    return text


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


def _command_matches_any(value: str, tokens: tuple[str, ...]) -> bool:
    return any(token in value for token in tokens)


def _command_topic(command: str) -> str:
    script = shell_script_from_command(command)
    value = str(script or command or "").lower()
    if _command_matches_any(value, ("home assistant", "homeassistant", "hass", "hassos")):
        return "homeassistant"
    if _command_matches_any(value, ("卫生间", "人体", "human presence", "occupancy", "motion_sensor", "motion sensor")):
        return "homeassistant"
    if _command_matches_any(value, ("oect", "openwrt", "pve", "homenet", "home lan", "192.168.")):
        return "home_network"
    if _command_matches_any(value, ("user_memory.md", "codeX_feishu_memory.md".lower(), "shared-memory.md", "shared_memory.py")):
        return "memory"
    if _command_matches_any(value, ("agents.md", "current_task_memory.md", "current_handoff.md")):
        return "project_context"
    if _command_matches_any(value, ("codex_feishu_app.py", "conversation_memory.py", "tasks.py", ".codex-feishu/app")):
        return "bridge_code"
    if _command_matches_any(value, ("feishu.py", "single-card", "cardkit", "reasoning_text", "tool_lines")):
        return "feishu_card"
    if _command_matches_any(value, ("launchd", "launchctl", "com.codex.feishu")):
        return "service"
    if _command_matches_any(value, ("github", "git ", ".git", "origin/main", "codex-feishu-bridge-skill")):
        return "github"
    if _command_matches_any(value, ("skill.md", ".codex/skills", ".agents/skills")):
        return "skill"
    if _command_matches_any(value, ("weather", "天气", "forecast")):
        return "weather"
    if _command_matches_any(value, ("log", "launchd.err", "launchd.out")):
        return "logs"
    return ""


def _command_public_object(command: str) -> str:
    topic = _command_topic(command)
    if topic == "homeassistant":
        return "Home Assistant 和家庭传感器规则"
    if topic == "home_network":
        return "家庭内网跳板和设备连通性"
    if topic == "memory":
        return "飞书共享记忆"
    if topic == "project_context":
        return "当前项目交接上下文"
    if topic == "bridge_code":
        return "Codex-Feishu 桥接代码"
    if topic == "feishu_card":
        return "飞书卡片展示逻辑"
    if topic == "service":
        return "飞书桥接服务状态"
    if topic == "github":
        return "公开 skill 仓库同步状态"
    if topic == "skill":
        return "Codex 可见 skill"
    if topic == "weather":
        return "天气通知输出格式"
    if topic == "logs":
        return "桥接运行日志"
    return "相关本机上下文"


def _public_command_file_target(words: list[str], *, fallback: str = "相关文件") -> str:
    candidates: list[str] = []
    for word in words:
        text = str(word or "").strip()
        if not text or text.startswith("-"):
            continue
        if re.fullmatch(r"\d+(?:,\d+)?[a-z]?", text, flags=re.IGNORECASE):
            continue
        if any(ch in text for ch in ("/", ".")) or text.upper().endswith((".MD", ".PY", ".JSON", ".TOML", ".YAML", ".YML", ".SH")):
            candidates.append(text)
    return basename_list(candidates[-3:]) or fallback


def _search_pattern(words: list[str]) -> str:
    for word in words[1:]:
        if not word.startswith("-"):
            return compact_public_text(word, limit=80)
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


_PUBLIC_COMMAND_START: dict[str, str] = {
    "task_store": "我在核对本机任务库，确认定时规则、通知窗口和最近运行记录。",
    "logs": "我在读取最新桥接日志，确认飞书消息、卡片更新和任务执行状态。",
    "bridge_code": "我在检查 Codex-Feishu 桥接实现，定位这段展示行为由哪里生成。",
    "hermes_reference": "我在只读参考 Hermes 的通知格式，只借鉴样式，不修改 Hermes。",
    "service": "我在检查本机飞书桥接服务状态，确认新逻辑是否已经运行。",
    "verify": "我在做本机验证，确认改动不会导致桥接启动失败。",
    "search": "我在搜索相关线索，缩小需要检查的代码和配置范围。",
    "read": "我在读取相关文件，先看清楚现有实现再调整。",
    "files": "我在盘点相关文件和目录，确认目标是否存在。",
    "git": "我在核对 Git 状态，确认这次只涉及 Codex-Feishu 改动。",
    "modify": "我在写入本机改动，让展示和通知规则按新要求执行。",
}


_PUBLIC_COMMAND_DONE: dict[str, str] = {
    "task_store": "任务库核对完成，定时规则和运行记录已经确认。",
    "logs": "日志核对完成，已经看到消息进入、卡片更新或错误位置。",
    "bridge_code": "桥接实现位置已经确认，下一步按这个位置调整展示。",
    "hermes_reference": "Hermes 天气格式已参考完毕；这里只改 Codex-Feishu。",
    "service": "服务状态核对完成，桥接运行情况已经确认。",
    "verify": "本机验证通过，这轮改动可以正常加载。",
    "search": "搜索完成，相关线索已经定位。",
    "read": "相关文件已读到，可以继续判断和改动。",
    "files": "文件和目录已经确认。",
    "git": "Git 状态已确认。",
    "modify": "本机改动已经写入，下一步做验证和重启。",
}


def public_command_area(command: str) -> str:
    script = shell_script_from_command(command)
    words = split_shell_words(script)
    value = str(script or command or "").lower()
    if "tasks.json" in value or "/app/tasks.py" in value or "codex-feishu-task" in value:
        return "task_store"
    if "/logs/" in value or value.startswith("tail ") or " launchd." in value:
        return "logs"
    if "codex_feishu_app.py" in value or "platforms/feishu.py" in value or "/.codex-feishu/app/" in value:
        return "bridge_code"
    if "/.hermes/" in value or "hermes" in value:
        return "hermes_reference"
    if "launchctl" in value or "com.codex.feishu" in value:
        return "service"
    if "py_compile" in value or "bash -n" in value or "pytest" in value:
        return "verify"
    first = words[0] if words else ""
    if first in {"rg", "grep"} or " rg " in f" {value} " or " grep " in f" {value} ":
        return "search"
    if first in {"sed", "cat"} or "sed -n" in value:
        return "read"
    if first in {"find", "ls", "stat", "wc"}:
        return "files"
    if first == "git" or " git " in f" {value} ":
        return "git"
    if any(token in value for token in ("apply_patch", "mkdir", " cp ", " mv ", " chmod", " install ")):
        return "modify"
    return ""


def command_start_explanation(command: str) -> tuple[str, str]:
    script = shell_script_from_command(command)
    words = split_shell_words(script)
    value = str(script or command or "").lower()
    intent = classify_command_intent(command)
    first = words[0] if words else ""
    subject = _command_public_object(command)
    area = public_command_area(command)
    if first in {"rg", "grep"} or " rg " in f" {value} " or " grep " in f" {value} ":
        pattern = _search_pattern(words)
        if _command_topic(command) == "homeassistant":
            return intent, "我在本机记忆和可见 skill 里找 Home Assistant / HASS 接入规则，先确认应该怎么读家庭设备状态。"
        if _command_topic(command) == "feishu_card":
            return intent, "我在查飞书卡片展示链路，找出执行过程和工具栏分别由哪段代码生成。"
        suffix = f"“{pattern}”" if pattern else subject
        return intent, f"我在搜索{suffix}，目的是找到和这次任务直接相关的实现或配置。"
    if first in {"sed", "cat"} or "sed -n" in value:
        target = _public_command_file_target(words)
        if _command_topic(command) == "homeassistant":
            return intent, f"我在读取 {target} 里的 Home Assistant 接入说明，确认查询状态要走哪条稳定链路。"
        if _command_topic(command) == "memory":
            return intent, f"我在读取 {target}，确认飞书端每轮会带上哪些长期记忆。"
        if _command_topic(command) == "feishu_card":
            return intent, f"我在读取 {target}，看清楚卡片如何展示执行过程和工具调用。"
        return intent, f"我在读取 {target}，先理解现有内容再决定下一步。"
    if first in {"find", "ls", "stat", "wc"}:
        return intent, f"我在盘点 {subject} 的本机文件位置，确认后续读取或修改的对象。"
    if first == "ssh" or " ssh " in f" {value} ":
        if _command_topic(command) in {"homeassistant", "home_network"}:
            return intent, "我在通过已知家庭内网跳板确认链路，而不是把这台 Mac 直连失败当成结论。"
        return intent, f"我在通过 SSH 检查 {subject}，确认远端实际状态。"
    if area:
        if area == "bridge_code":
            return intent, "我在检查 Codex-Feishu 桥接实现，定位飞书端执行过程和工具栏的分流位置。"
        if area == "logs":
            return intent, "我在读取最新桥接日志，确认你刚才的飞书消息有没有进入本机 Codex，以及卡片更新是否正常。"
        if area == "service":
            return intent, "我在检查本机飞书桥接服务状态，确认新逻辑是否已经运行在当前进程里。"
        if area == "git":
            return intent, "我在核对 Git 状态，确认本机改动和公开 skill 仓库是否同步。"
        return intent, _PUBLIC_COMMAND_START.get(area, f"我在检查 {subject}，拿实际结果来决定下一步。")
    if "py_compile" in value:
        files = basename_list(words[words.index("py_compile") + 1 :] if "py_compile" in words else words)
        target = f"：{files}" if files else ""
        return intent, f"我在跑 Python 语法检查{target}，这是为了确认刚改的桥接代码能被解释器正常加载。"
    if "bash -n" in value:
        return intent, "我在检查脚本语法，先排除启动脚本层面的低级错误。"
    if "pytest" in value or "npm test" in value or "pnpm test" in value:
        return intent, "我在运行测试，用实际用例确认行为没有被改坏。"
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
    first = words[0] if words else ""
    topic = _command_topic(command)
    subject = _command_public_object(command)
    area = public_command_area(command)
    if words and words[0] in {"rg", "grep"} or " rg " in f" {value} " or " grep " in f" {value} ":
        pattern = _search_pattern(words)
        if failed and exit_code != 1:
            return intent, f"搜索 {subject} 时没有拿到可用结果，我会换路径继续定位，不把这一步当成结论。"
        if exit_code == 1:
            target = f"“{pattern}”" if pattern else subject
            return intent, f"没有搜到 {target} 的匹配项；这说明这条线索不存在或残留已经清掉，我会继续查其它来源。"
        clue = summarize_search_output(output, exit_code=exit_code)
        if clue:
            return intent, f"我已经搜到 {subject} 的线索，会从结果里提炼结论，不把匹配原文堆到主卡片里。"
        return intent, f"{subject} 的搜索已经返回，我会据此收窄下一步检查范围。"
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
        if failed:
            return intent, "读取日志失败了，我会改用别的方式确认服务状态。"
        return intent, "日志已经读到，我会根据最新记录判断服务是否正常，不把原始日志塞进执行过程。"
    if first in {"sed", "cat"} or "sed -n" in value:
        target = _public_command_file_target(words)
        if failed:
            return intent, f"{target} 没有读到，我会换另一个本机来源继续确认 {subject}。"
        if topic == "homeassistant":
            return intent, f"{target} 已经读完，Home Assistant 的跳板、认证和实体查询规则可以用于后续判断。"
        if topic == "memory":
            return intent, f"{target} 已经读完，飞书端会加载的长期记忆范围已经确认。"
        if topic == "feishu_card":
            return intent, f"{target} 已经读完，执行过程和工具栏的展示分工已经确认。"
        return intent, f"{target} 已经读完，我会把里面和当前任务有关的内容用于后续判断。"
    if first in {"find", "ls", "stat", "wc"}:
        if failed:
            return intent, f"{subject} 的文件盘点没有拿到结果，我会换一个路径确认目标位置。"
        return intent, f"{subject} 的文件位置已经确认，后续可以直接读取或修改目标文件。"
    if area:
        if failed and exit_code != 1:
            if area == "service":
                return intent, "服务状态检查没有拿到稳定结果，我会继续看日志或重启状态来确认。"
            if area == "bridge_code":
                return intent, "这条代码检查路径没有返回可用结果，我会换一个入口继续定位飞书展示逻辑。"
            return intent, f"检查 {subject} 时没有拿到预期结果，我会换路径继续确认。"
        if area == "bridge_code":
            return intent, "桥接实现的相关位置已经确认，接下来会按这里修正飞书展示行为。"
        if area == "logs":
            return intent, "日志已经核对到关键位置，可以判断消息处理或卡片更新卡在哪一步。"
        if area == "service":
            clue = summarize_launchctl_output(output)
            return intent, f"飞书桥接服务状态已经确认。{('看到：' + clue) if clue else '没有发现启动层面的异常。'}"
        if area == "read":
            target = _public_command_file_target(words)
            return intent, f"{target} 已经读完，我会把里面和当前任务有关的规则用于后续判断。"
        if area == "files":
            return intent, f"{subject} 的文件位置已经确认，后续可以直接读取或修改目标文件。"
        if area == "modify":
            return intent, f"{subject} 的本机改动已经写入，接下来要用语法检查和服务状态验证它。"
        return intent, _PUBLIC_COMMAND_DONE.get(area, f"{subject} 已经确认，我会继续推进后面的判断。")
    if "ssh" in value or "oect" in value:
        if failed:
            return intent, "这条远程连接路径没有打通，我会优先改走共享记忆里的家庭内网接入规则继续确认。"
        if topic in {"homeassistant", "home_network"}:
            return intent, "家庭内网链路已经返回结果，我会根据实际状态继续查 Home Assistant 或设备实体。"
        return intent, f"{subject} 的远程检查已经返回，我会根据实际结果继续。"
    if failed:
        return intent, f"检查 {subject} 时没有拿到预期结果，我会换一种方式继续，而不是停在这一步。"
    if intent == "inspect":
        return intent, f"{subject} 的检查已经有结果，我会据此决定下一步。"
    if intent == "modify":
        return intent, f"{subject} 的本机改动已经写入，接下来要验证它是否真的生效。"
    if intent == "verify":
        return intent, "验证通过了，说明这一轮改动至少能正常加载或运行。"
    return intent, f"{subject} 的当前步骤已经完成，我继续推进后面的判断。"


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


def command_tool_summary(command: str, *, exit_code: Any = None, output: Any = "") -> str:
    script = shell_script_from_command(command) or str(command or "").strip()
    display = compact_public_text(redact_sensitive_text(script), limit=180)
    failed = exit_code not in (None, 0)
    if not display:
        display = "本机命令"
    if exit_code is None:
        return f"执行命令：{display}"
    status = f"exit={exit_code}" if failed else "完成"
    return f"执行命令：{display}（{status}）"


def file_change_tool_summary(changes: Any) -> str:
    if not isinstance(changes, list) or not changes:
        return "修改文件：未返回文件列表"
    parts: list[str] = []
    for change in changes[:4]:
        if not isinstance(change, dict):
            continue
        kind = (change.get("kind") or {}).get("type") if isinstance(change.get("kind"), dict) else change.get("kind")
        path = str(change.get("path") or "").strip()
        if not path:
            continue
        filename = Path(path).name or compact_public_text(path, limit=60)
        parts.append(f"{kind or 'update'} {filename}")
    if len(changes) > 4:
        parts.append(f"另 {len(changes) - 4} 项")
    return f"修改文件：{compact_public_text('、'.join(parts), limit=180)}" if parts else "修改文件：未返回文件列表"


def mcp_tool_summary(item: dict[str, Any], *, dynamic: bool = False) -> str:
    tool = raw_tool_name(item)
    server = tool_server_name(item)
    failed = bool(item.get("error")) or item.get("success") is False
    args = compact_tool_arguments(item.get("arguments"))
    if dynamic:
        label = f"调用工具：{tool}"
    elif server:
        label = f"调用 MCP：{server}.{tool}"
    else:
        label = f"调用工具：{tool}"
    if args:
        label = f"{label} {args}"
    return f"{label}（{'失败' if failed else '完成'}）"


def legacy_command_tool_summary(command: str, *, exit_code: Any = None, output: Any = "") -> str:
    area = public_command_area(command)
    failed = exit_code not in (None, 0)
    if failed and exit_code != 1:
        return "工具步骤未完成，已根据反馈调整路线。"
    if area:
        return _PUBLIC_COMMAND_DONE.get(area, "工具步骤已完成。")
    intent = classify_command_intent(command)
    if intent == "inspect":
        return "检查步骤已完成。"
    if intent == "modify":
        return "本机改动已写入。"
    if intent == "verify":
        return "验证步骤已通过。"
    return "工具步骤已完成。"


def summarize_item(notification: dict[str, Any]) -> Optional[str]:
    if notification.get("method") != "item/completed":
        return None
    item = (notification.get("params") or {}).get("item") or {}
    item_type = item.get("type") or ""
    if item_type == "commandExecution":
        exit_code = item.get("exitCode")
        return command_tool_summary(
            str(item.get("command") or ""),
            exit_code=exit_code,
            output=item.get("aggregatedOutput") or item.get("output") or "",
        )
    if item_type == "fileChange":
        return file_change_tool_summary(item.get("changes") or [])
    if item_type == "mcpToolCall":
        return mcp_tool_summary(item)
    if item_type == "dynamicToolCall":
        return mcp_tool_summary(item, dynamic=True)
    if item_type == "collabAgentToolCall":
        return mcp_tool_summary(item, dynamic=True)
    return None


def summarize_public_progress(
    notification: dict[str, Any],
    *,
    step: int,
    progress: Optional["CardProgress"] = None,
) -> Optional[str]:
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
        if progress is not None and progress.has_model_progress:
            return None
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
        tool_name = public_tool_name(item)
        if started:
            return progress_explanation(
                step=step,
                text=public_tool_start_text(tool_name),
            )
        failed = bool(item.get("error")) or item.get("success") is False
        return progress_explanation(
            step=step,
            text=public_tool_done_text(tool_name, failed=failed),
        )
    if item_type == "agentMessage":
        notes = extract_public_progress_notes(item.get("text") or "")
        if notes:
            return progress_explanation(step=step, text=notes[-1])
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


def final_text_without_progress_markers(text: str) -> str:
    cleaned = strip_public_progress_notes(text)
    return normalize_text(cleaned) if cleaned else ""


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


def is_weather_task(task: dict[str, Any]) -> bool:
    value = f"{task.get('name') or ''}\n{task.get('prompt') or ''}"
    return "天气" in value and ("成都" in value or "weather" in value.lower())


def normalize_weather_notification(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return value
    start = re.search(r"\*\*成都天气\s*·\s*\d{4}-\d{2}-\d{2}\*\*", value)
    if start:
        value = value[start.start():]
    allowed_prefixes = ("**成都天气", "天气：", "温度：", "空气：", "风力：", "提醒：")
    lines: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip(" \t-•")
        if not line:
            continue
        if any(line.startswith(prefix) for prefix in allowed_prefixes):
            lines.append(line)
        if len(lines) >= 6:
            break
    if lines and lines[0].startswith("**成都天气"):
        return "\n".join(lines)
    return value


def notification_body_for_task(task: dict[str, Any], result: TurnResult) -> str:
    text = result.final_text or "定时任务已完成，但没有返回正文。"
    if result.error:
        text += f"\n\n> 执行期间提示：{result.error}"
    if is_weather_task(task):
        text = normalize_weather_notification(text)
    return text.strip()


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
    has_model_progress: bool = False
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
            self.reasoning_lines, self.reasoning_keys = clean_public_progress_lines(
                self.reasoning_lines,
                max_lines=10,
            )
        self.last_reasoning = "\n".join(f"- {item}" for item in self.reasoning_lines[-10:])
        result = await self.adapter._upsert_single_response_card(
            chat_id=self.chat_id,
            content=None,
            reply_to=self.reply_to,
            metadata=self.metadata,
            reasoning_text=self.last_reasoning,
            tool_lines=clean_tool_lines(self.tool_lines, max_lines=8) or None,
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

    async def push_model_progress(self, line: str, *, force: bool = True) -> None:
        self.has_model_progress = True
        await self.push_reasoning(line, force=force)

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
        display_tool_lines = clean_tool_lines(self.tool_lines, max_lines=8)
        result = await self.adapter._upsert_single_response_card(
            chat_id=self.chat_id,
            content=None,
            reply_to=self.reply_to,
            metadata=self.metadata,
            reasoning_text=None,
            tool_lines=display_tool_lines,
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
        tool_lines = None if self.notification_only else (clean_tool_lines(self.tool_lines, max_lines=8) or None)
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
        providers = configured_model_providers(self.codex_home)
        provider_label = _provider_display_name(
            runtime_state.model_provider,
            providers.get(runtime_state.model_provider, {}),
        )
        current = (
            f"{provider_label} / {runtime_state.model or '默认模型'} "
            f"{runtime_state.reasoning_effort or '默认'}"
        ).strip()
        return "\n".join(
            [
                f"当前飞书模型：{current}",
                "",
                "支持切换：",
                format_supported_feishu_models(self.codex_home),
                "",
                "用法：",
                "- `/model` 查看当前模型",
                "- `/model fhl gpt-5.4 high` 按供应商/API 精确切换",
                "- `/model gpt-5.4 high` 只有一个供应商匹配时可省略供应商",
                "- 以后多个供应商有同名模型时，请加供应商前缀",
                "- `/fallback-model` 查看备用模型",
                "- `/fallback-model set <provider> <model> <reasoning>` 设置备用模型列表",
                "- `/fallback-model add <provider> <model> <reasoning>` 追加备用模型",
                "- `/fallback-model clear` 清空备用模型",
                "",
                self.fallback_model_help_text(),
            ]
        ).strip()

    def switch_feishu_model(self, raw_choice: str) -> tuple[bool, str]:
        choice, reason = resolve_feishu_model_choice(raw_choice, codex_home=self.codex_home)
        if choice is None:
            if reason == "ambiguous-provider":
                prefix = "这个模型名在多个供应商/API 下都存在，请加供应商前缀。"
            elif reason == "ambiguous-reasoning":
                prefix = "这个模型有多个思考强度，请把思考程度也写上。"
            else:
                prefix = "未识别这个飞书模型。"
            return False, prefix + "\n\n" + self.model_help_text()
        config_path = write_feishu_model_into_config(
            self.codex_home or DEFAULT_FEISHU_CODEX_HOME,
            choice["model"],
            choice["reasoning_effort"],
            choice["model_provider"],
        )
        self._runtime_signature = None
        provider_label = choice.get("provider_label") or choice.get("model_provider") or "default"
        return True, "\n".join(
            [
                f"已切换飞书模型到：{provider_label} / {choice['model']} {choice['reasoning_effort']}",
                "",
                "说明：这只影响飞书机器人链路，不影响桌面 Codex 当前模型。",
                f"配置已写入：`{config_path}`",
            ]
        ).strip()

    def fallback_model_choices(self) -> list[dict[str, str]]:
        specs = load_feishu_fallback_model_specs()
        if specs is None:
            raw = (
                os.getenv("CODEX_FEISHU_FALLBACK_MODELS", "").strip()
                or os.getenv("CODEX_FEISHU_FALLBACK_MODEL", "").strip()
            )
        else:
            raw = "\n".join(specs)
        choices = parse_feishu_fallback_model_choices(raw, codex_home=self.codex_home)
        current = _model_choice_identity(_current_model_choice(self.codex_home))
        return [choice for choice in choices if _model_choice_identity(choice) != current]

    def fallback_model_help_text(self) -> str:
        choices = self.fallback_model_choices()
        if not choices:
            return "备用模型：未配置"
        lines = ["备用模型："]
        for choice in choices:
            provider_label = choice.get("provider_label") or choice.get("model_provider") or "default"
            lines.append(f"- {provider_label} / {choice['model']} {choice['reasoning_effort']}")
        return "\n".join(lines)

    def fallback_model_command_help_text(self) -> str:
        return "\n".join(
            [
                self.fallback_model_help_text(),
                "",
                "用法：",
                "- `/fallback-model` 查看当前备用模型列表",
                "- `/fallback-model set <provider> <model> <reasoning>` 替换备用模型列表",
                "- `/fallback-model add <provider> <model> <reasoning>` 追加一个备用模型",
                "- `/fallback-model clear` 清空备用模型列表",
                "",
                "说明：备用模型是通用机制，不绑定任何固定供应商；provider/model/reasoning 与 `/model` 使用同一套可选项。",
            ]
        ).strip()

    def update_fallback_models(self, raw_command: str) -> tuple[bool, str]:
        command = str(raw_command or "").strip()
        if not command or command.lower() in {"list", "show", "status", "状态", "查看"}:
            return True, self.fallback_model_command_help_text()

        head, _, tail = command.partition(" ")
        action = head.strip().lower()
        if action in {"clear", "off", "disable", "none", "关闭", "清空"}:
            path = save_feishu_fallback_models([])
            return True, "\n".join(
                [
                    "已清空飞书备用模型列表。",
                    "之后主模型失败时不会自动切换到备用模型。",
                    f"配置已写入：`{path}`",
                ]
            )

        if action not in {"set", "add", "设置", "追加"}:
            tail = command
            action = "set"
        if not tail.strip():
            return False, "缺少备用模型参数。\n\n" + self.fallback_model_command_help_text()

        parsed = parse_feishu_fallback_model_choices(tail, codex_home=self.codex_home)
        if not parsed:
            return False, "未识别这个备用模型。\n\n" + self.model_help_text()

        if action in {"add", "追加"}:
            choices = self.fallback_model_choices() + parsed
        else:
            choices = parsed

        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        current = _model_choice_identity(_current_model_choice(self.codex_home))
        for choice in choices:
            identity = _model_choice_identity(choice)
            if identity == current or identity in seen:
                continue
            seen.add(identity)
            deduped.append(choice)

        path = save_feishu_fallback_models(deduped)
        self._runtime_signature = None
        return True, "\n".join(
            [
                "已更新飞书备用模型列表。",
                "",
                self.fallback_model_help_text(),
                "",
                "说明：这只影响飞书机器人链路，不影响桌面 Codex 当前模型。",
                f"配置已写入：`{path}`",
            ]
        ).strip()

    def apply_model_choice(self, choice: dict[str, str]) -> str:
        config_path = write_feishu_model_into_config(
            self.codex_home or DEFAULT_FEISHU_CODEX_HOME,
            choice["model"],
            choice["reasoning_effort"],
            choice["model_provider"],
        )
        self._runtime_signature = None
        return str(config_path)

    async def run_codex_turn_with_fallback(
        self,
        *,
        run_once: Callable[[], Awaitable[TurnResult]],
        progress: CardProgress,
        active_turn: Optional[ActiveTurn] = None,
    ) -> TurnResult:
        attempted: set[tuple[str, str, str]] = set()
        attempted.add(_model_choice_identity(_current_model_choice(self.codex_home)))
        result = await run_once()
        if not should_try_fallback_model(result):
            return result
        fallback_choices = self.fallback_model_choices()
        if not fallback_choices:
            return result

        first_error = normalize_text(result.error or result.final_text or "", 900)
        for choice in fallback_choices:
            identity = _model_choice_identity(choice)
            if identity in attempted:
                continue
            attempted.add(identity)
            if active_turn is not None and active_turn.interrupt_requested:
                return result
            provider_label = choice.get("provider_label") or choice.get("model_provider") or "default"
            LOG.warning(
                "Codex turn failed with fallback-worthy error; retrying with fallback provider=%s model=%s reasoning=%s",
                choice.get("model_provider") or "<default>",
                choice.get("model") or "<default>",
                choice.get("reasoning_effort") or "<default>",
            )
            await progress.push_reasoning(
                f"主模型返回可切换错误，准备改用备用模型 {provider_label} / {choice['model']} {choice['reasoning_effort']} 重试本轮任务。",
                force=True,
            )
            self.apply_model_choice(choice)
            retry_result = await run_once()
            if not should_try_fallback_model(retry_result):
                if first_error and retry_result.final_text:
                    retry_result.final_text = (
                        retry_result.final_text.rstrip()
                        + "\n\n> 已从主模型失败自动切换到备用模型完成本轮任务。"
                    )
                return retry_result
            result = retry_result
        return result

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
            if chat_id and not progress.notification_only:
                await progress.seed()
                if not progress.card_created:
                    LOG.warning(
                        "scheduled task %s initial Feishu card seed failed: chat_id=%s error=%s",
                        task.get("id"),
                        chat_id,
                        progress.last_error or "unknown error",
                    )
            result = await self.run_codex_turn_with_fallback(
                run_once=lambda: self.run_oneoff_codex_turn(
                    prompt=self.build_scheduled_prompt(task),
                    session_key=session_key,
                    progress=progress,
                    chat_id=chat_id,
                    active_turn=active_turn,
                ),
                progress=progress,
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
                final_text = notification_body_for_task(task, result)
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
                "如果需要新增或维护飞书通知型定时任务，必须通过 $CODEX_FEISHU_HOME/app/tasks.py 的 add/pause/resume/delete 接口维护共享任务库，不要直接手写 tasks.json。",
                "遇到家庭设备、Home Assistant/HASS、NAS、PVE、OpenWrt、远程服务器、skill 或自动化任务问题时，先读取共享记忆和 Codex 可见 skill/脚本；不要因为默认飞书工作区没有线索就断言缺认证或不可完成。",
                "查询用户自有家庭自动化状态时，只做只读状态读取；可以读取灯、门磁、温湿度、人体/移动/存在传感器等设备状态，但不要访问摄像头/音频，也不要监控第三方隐私。",
                "如果 Mac 直连家庭内网服务失败，不要直接下结论；优先使用共享记忆里的家庭内网跳板、Home Assistant 接入规则或已有脚本继续验证。",
                "这是通知型定时任务；最终回答只输出要推送给用户的通知正文，不要写执行过程、尾注、工具调用、完成情况、验证结果、来源清单或需要用户处理的事项，除非任务正文明确要求。",
                "通知型任务不要输出“执行进展：”标记行；通知窗口只需要最终通知正文。",
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
                "最终回答只输出通知正文。",
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
        fallback_model_command_arg = parse_fallback_model_command(event.text)
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
        if fallback_model_command_arg is not None:
            await self.adapter.on_processing_start(event)
            outcome = ProcessingOutcome.SUCCESS
            progress = CardProgress(
                chat_id=chat_id,
                reply_to=reply_to,
                session_key=f"{session_key}:fallback-model:{reply_to or now_ms()}",
                card_key=f"{session_key}:fallback-model:{reply_to or now_ms()}",
                adapter=self.adapter,
                prompt=event.text,
            )
            try:
                await progress.seed()
                ok, message = self.update_fallback_models(fallback_model_command_arg)
                await progress.final(message, "飞书备用模型已更新" if ok else "飞书备用模型设置失败")
                return
            except Exception:
                outcome = ProcessingOutcome.FAILURE
                LOG.exception("fallback model command failed")
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
                result = await self.run_codex_turn_with_fallback(
                    run_once=lambda: self.run_codex_turn(event, session_key, progress, active_turn),
                    progress=progress,
                    active_turn=active_turn,
                )
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
                    cleaned_result_text = final_text_without_progress_markers(result.final_text or "")
                    final_text = cleaned_result_text or "Codex 已完成，但没有返回正文。"
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
        public_progress = summarize_public_progress(notification, step=step, progress=progress)
        if public_progress:
            item = (notification.get("params") or {}).get("item") or {}
            if item.get("type") == "agentMessage":
                asyncio.run_coroutine_threadsafe(
                    progress.push_model_progress(public_progress, force=True),
                    self.loop,
                )
            else:
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
            "- 执行过程中要像 Codex 桌面端一样给用户看得懂的公开执行进展。进展只写你正在查什么、为什么、发现了什么、哪条路失败后换哪条路、验证是否通过；不要写私密推理、不要写命令、代码、路径、JSON、工具结果原文。",
            "- 公开执行进展不是标题，也不是空泛状态。要像桌面端 commentary 一样具体，例如“我先查 Home Assistant 的实体来源，再确认卫生间人体传感器的 entity_id 和当前状态”。",
            "- 每当你开始一个新的实质步骤，或一个步骤有关键发现/失败/通过时，单独输出一行：`执行进展：...`。这行会显示在飞书卡片的执行过程区，最终正文会自动移除这些标记行。",
            "- 在第一次调用工具之前，必须先输出一条 `执行进展：...`，说明这次准备从哪里查、为什么这么查。后续每个实质阶段最多输出 1 条关键进展，避免重复刷屏。",
            "- 不要输出泛化空话，例如“文件和目录已确认”“相关线索已定位”“这一步没有按预期完成”。要说清楚对象和含义，例如“执行进展：我先查本机共享记忆里的 Home Assistant 入口，确认是否应该经 OECT 跳板访问”。",
            "- 你看到的是本机共享记忆层；不同 Codex 登录方式的云端 thread 不共享，但本机记忆和任务库共享。",
            "- 飞书对话采用独立轻量 agent 记忆：每轮都是新的本机 Codex turn，只带滚动摘要和最近几轮，避免长上下文拖慢和过度消耗 token。",
            "- 如果用户要求创建、安装或更新 skill，必须写入 Codex 可见路径，例如 $CODEX_HOME/skills 或 $HOME/.agents/skills；不要创建飞书专用 skill。",
            "- 如果用户通过飞书创建定时任务，要使用本机共享任务库；任务应能在 Codex automations 里被看到。",
            "- 创建、暂停、恢复或删除飞书通知型定时任务时，必须调用 $CODEX_FEISHU_HOME/app/tasks.py 的 add/pause/resume/delete 接口，不要直接手写 $CODEX_FEISHU_HOME/tasks.json；该接口会同步 Codex automations 镜像。",
            "- 遇到家庭设备、Home Assistant/HASS、NAS、PVE、OpenWrt、远程服务器、skill 或自动化任务问题时，先读取共享记忆和 Codex 可见 skill/脚本；不要因为默认飞书工作区没有线索就断言缺认证或不可完成。",
            "- 查询用户自有家庭自动化状态时，只做只读状态读取；可以读取灯、门磁、温湿度、人体/移动/存在传感器等设备状态，但不要访问摄像头/音频，也不要监控第三方隐私。",
            "- 如果 Mac 直连家庭内网服务失败，不要直接下结论；优先使用共享记忆里的家庭内网跳板、Home Assistant 接入规则或已有脚本继续验证。",
            "- 用户查询自己家中 Home Assistant 暴露的设备状态时，可以做只读状态读取并报告实体状态。人体/移动/存在传感器可以报告传感器自身 on/off/last_changed/friendly_name 等状态；不要访问摄像头、音频，也不要对画面或第三方隐私做额外推断。",
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
