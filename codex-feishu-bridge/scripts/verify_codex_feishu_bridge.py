#!/usr/bin/env python3
"""Verify a local Codex Feishu bridge install without printing secrets."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


PLACEHOLDERS = {"", "FEISHU_APP_ID", "FEISHU_APP_SECRET"}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def ok(label: str, detail: str = "") -> None:
    print(f"[OK] {label}{': ' + detail if detail else ''}")


def warn(label: str, detail: str = "") -> None:
    print(f"[WARN] {label}{': ' + detail if detail else ''}")


def fail(label: str, detail: str = "") -> None:
    print(f"[FAIL] {label}{': ' + detail if detail else ''}")


def check_launch_agent(label: str, plist_path: Path) -> None:
    if not plist_path.exists():
        warn("LaunchAgent plist missing", str(plist_path))
        return
    try:
        with plist_path.open("rb") as fh:
            data = plistlib.load(fh)
        ok("LaunchAgent plist parses", str(plist_path))
        if data.get("Label") != label:
            warn("LaunchAgent label mismatch", str(data.get("Label")))
    except Exception as exc:
        fail("LaunchAgent plist parse failed", str(exc))
        return
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
        if proc.returncode == 0:
            first = next((line.strip() for line in proc.stdout.splitlines() if "state =" in line), "")
            ok("LaunchAgent loaded", first or label)
        else:
            warn("LaunchAgent not loaded", proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else label)
    except Exception as exc:
        warn("LaunchAgent status unavailable", str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(Path.home() / ".codex-feishu"))
    parser.add_argument("--launch-agent-label", default="com.codex.feishu")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser().resolve()
    env_path = home / ".env"
    app_path = home / "app" / "codex_feishu_app.py"
    env = read_env(env_path)
    failures = 0

    if home.exists():
        ok("home exists", str(home))
    else:
        fail("home missing", str(home)); failures += 1

    if app_path.exists():
        ok("bridge app exists", str(app_path))
    else:
        fail("bridge app missing", str(app_path)); failures += 1

    if env_path.exists():
        ok(".env exists", str(env_path))
    else:
        fail(".env missing", str(env_path)); failures += 1

    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET"):
        value = env.get(key, "")
        if value in PLACEHOLDERS:
            fail(f"{key} not configured")
            failures += 1
        else:
            ok(f"{key} configured", "***")

    runtime_src = home / "runtime" / "src"
    if runtime_src.exists():
        ok("runtime src exists", str(runtime_src))
    else:
        fail("runtime src missing", str(runtime_src)); failures += 1

    feishu_codex_home = Path(env.get("CODEX_FEISHU_CODEX_HOME") or home / "codex-home")
    if feishu_codex_home.exists():
        ok("Feishu codex-home exists", str(feishu_codex_home))
    else:
        fail("Feishu codex-home missing", str(feishu_codex_home)); failures += 1

    codex_bin = env.get("CODEX_FEISHU_CODEX_BIN") or shutil.which("codex") or "codex"
    if shutil.which(codex_bin) or Path(codex_bin).exists():
        ok("Codex binary found", codex_bin)
    else:
        warn("Codex binary not found", codex_bin)

    venv_python = home / "runtime" / "venv" / "bin" / "python"
    if venv_python.exists():
        ok("venv python exists", str(venv_python))
        try:
            proc = subprocess.run(
                [str(venv_python), "-c", "import lark_oapi; print('ok')"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            if proc.returncode == 0:
                ok("lark_oapi import works")
            else:
                fail("lark_oapi import failed", proc.stderr.strip())
                failures += 1
        except Exception as exc:
            fail("lark_oapi import check failed", str(exc)); failures += 1
    else:
        fail("venv python missing", str(venv_python)); failures += 1

    extra_compile_targets = [
        str(home / "app" / "conversation_memory.py"),
        str(home / "app" / "shared_memory.py"),
        str(home / "app" / "tasks.py"),
    ]
    if app_path.exists() and venv_python.exists():
        proc = subprocess.run(
            [str(venv_python), "-m", "py_compile", str(app_path), *extra_compile_targets],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode == 0:
            ok("bridge app compiles")
        else:
            fail("bridge app compile failed", proc.stderr.strip()); failures += 1

    feishu_model_state = home / "feishu-model.json"
    if feishu_model_state.exists():
        ok("Feishu model state exists", str(feishu_model_state))
    else:
        warn("Feishu model state missing", "it will be created after the first /model switch or startup seed")

    notify_chat = env.get("CODEX_FEISHU_NOTIFY_CHAT_ID", "")
    if notify_chat:
        ok("notification chat configured", mask_id(notify_chat))
    else:
        warn("notification chat empty", "scheduled tasks will use source chat")

    automations = Path(env.get("CODEX_HOME") or Path.home() / ".codex") / "automations"
    mirrors = sorted(automations.glob("codex-feishu-*/automation.toml")) if automations.exists() else []
    if mirrors:
        ok("Codex automation mirrors found", str(len(mirrors)))
    else:
        warn("no Codex automation mirrors found yet")

    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{args.launch_agent_label}.plist"
    check_launch_agent(args.launch_agent_label, plist_path)

    log_path = home / "logs" / "bridge.log"
    if log_path.exists():
        ok("bridge log exists", str(log_path))
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        for line in tail:
            print("  " + scrub(line))
    else:
        warn("bridge log missing", str(log_path))

    return 1 if failures else 0


def mask_id(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def scrub(value: str) -> str:
    for key in ("FEISHU_APP_SECRET", "app_secret", "tenant_access_token", "authorization"):
        value = value.replace(key, key)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
