#!/usr/bin/env python3
"""Install the sanitized Codex Feishu bridge template."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_DIR / "assets" / "bridge_app"
DEFAULT_HOME = Path.home() / ".codex-feishu"


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def write_text(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def copy_template(app_dir: Path, *, force: bool) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    for src in TEMPLATE_DIR.iterdir():
        if src.name == "__pycache__":
            continue
        dest = app_dir / src.name
        if dest.exists():
            if not force:
                print(f"skip existing {dest}")
                continue
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)


def build_env(args: argparse.Namespace, home: Path) -> str:
    workspace = args.workspace or str(home / "workspace")
    lines = [
        "# Local Codex Feishu bridge config. Do not commit this file.",
        f"FEISHU_APP_ID={args.app_id or 'FEISHU_APP_ID'}",
        f"FEISHU_APP_SECRET={args.app_secret or 'FEISHU_APP_SECRET'}",
        f"FEISHU_DOMAIN={args.domain}",
        "FEISHU_CONNECTION_MODE=websocket",
        "FEISHU_VERIFICATION_TOKEN=",
        "FEISHU_ENCRYPT_KEY=",
        f"CODEX_FEISHU_NOTIFY_CHAT_ID={args.notify_chat_id or ''}",
        f"CODEX_HOME={args.codex_home or ''}",
        f"CODEX_FEISHU_CODEX_BIN={args.codex_bin}",
        f"CODEX_FEISHU_WORKSPACE={workspace}",
        f"CODEX_FEISHU_TASK_POLL_SECONDS={args.wake_window}",
        f"CODEX_FEISHU_TIMEZONE={args.timezone}",
        f"CODEX_FEISHU_ALLOWED_USERS={args.allowed_users or ''}",
        f"CODEX_FEISHU_REQUIRE_MENTION={'true' if args.require_mention else 'false'}",
        "CODEX_FEISHU_MAX_MEMORY_TURNS=12",
        "CODEX_FEISHU_MAX_INPUT_CHARS=24000",
        "CODEX_FEISHU_CODEX_TIMEOUT_SECONDS=1800",
        "CODEX_FEISHU_CODEX_ARGS=exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox",
        "CODEX_FEISHU_AUTOMATIONS=true",
        "",
    ]
    return "\n".join(lines)


def create_start_script(app_dir: Path, home: Path) -> None:
    content = f"""#!/bin/sh
set -eu
HOME_DIR="{home}"
APP_DIR="$HOME_DIR/app"
VENV="$HOME_DIR/runtime/venv"
exec "$VENV/bin/python" "$APP_DIR/codex_feishu_bridge.py" --home "$HOME_DIR" --env-file "$HOME_DIR/.env"
"""
    path = app_dir / "start.sh"
    write_text(path, content, 0o755)


def create_venv(home: Path, app_dir: Path, *, skip_pip: bool) -> None:
    venv = home / "runtime" / "venv"
    if not (venv / "bin" / "python").exists():
        run([sys.executable, "-m", "venv", str(venv)])
    if not skip_pip:
        run([str(venv / "bin" / "python"), "-m", "pip", "install", "-U", "pip"])
        run([str(venv / "bin" / "python"), "-m", "pip", "install", "-r", str(app_dir / "requirements.txt")])


def launch_agent_plist(label: str, home: Path) -> str:
    start = home / "app" / "start.sh"
    out_log = home / "logs" / "launchd.out.log"
    err_log = home / "logs" / "launchd.err.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{start}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{home / "app"}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{out_log}</string>
  <key>StandardErrorPath</key>
  <string>{err_log}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CODEX_FEISHU_HOME</key>
    <string>{home}</string>
    <key>CODEX_FEISHU_ENV_FILE</key>
    <string>{home / ".env"}</string>
  </dict>
</dict>
</plist>
"""


def install_launch_agent(home: Path, label: str) -> Path:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{label}.plist"
    write_text(plist_path, launch_agent_plist(label, home), 0o644)
    return plist_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(DEFAULT_HOME))
    parser.add_argument("--app-id", default="")
    parser.add_argument("--app-secret", default="")
    parser.add_argument("--notify-chat-id", default="")
    parser.add_argument("--domain", choices=["feishu", "lark"], default="feishu")
    parser.add_argument("--codex-home", default=os.getenv("CODEX_HOME", ""))
    parser.add_argument("--codex-bin", default=shutil.which("codex") or "codex")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--allowed-users", default="")
    parser.add_argument("--wake-window", type=int, default=60)
    parser.add_argument("--require-mention", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--install-launch-agent", action="store_true")
    parser.add_argument("--launch-agent-label", default="com.codex.feishu")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-pip-install", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser().resolve()
    app_dir = home / "app"
    print(f"install home: {home}")
    if args.dry_run:
        print("dry run only; no files changed")
        return 0

    for child in ("app", "logs", "workspace", "runtime"):
        (home / child).mkdir(parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass

    copy_template(app_dir, force=args.force)
    create_start_script(app_dir, home)

    env_path = home / ".env"
    if env_path.exists() and not args.force:
        print(f"skip existing {env_path}")
    else:
        write_text(env_path, build_env(args, home), 0o600)

    example = home / ".env.example"
    if not example.exists() or args.force:
        shutil.copy2(TEMPLATE_DIR / ".env.example", example)
        example.chmod(0o600)

    create_venv(home, app_dir, skip_pip=args.skip_pip_install)

    if args.install_launch_agent:
        plist_path = install_launch_agent(home, args.launch_agent_label)
        print(f"LaunchAgent written: {plist_path}")
        print(f"reload: launchctl bootout gui/$(id -u) {plist_path} 2>/dev/null || true")
        print(f"reload: launchctl bootstrap gui/$(id -u) {plist_path}")

    print("installed. Fill .env if placeholders remain, then start app/start.sh or reload LaunchAgent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
