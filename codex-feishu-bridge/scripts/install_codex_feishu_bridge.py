#!/usr/bin/env python3
"""Install the reusable Codex Feishu bridge home template."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_HOME = SKILL_DIR / "assets" / "bridge_home"
DEFAULT_HOME = Path.home() / ".codex-feishu"


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def write_text(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        try:
            path.chmod(mode)
        except OSError:
            pass


def copy_tree(src: Path, dest: Path, *, force: bool) -> None:
    for item in src.iterdir():
        if item.name == "__pycache__":
            continue
        target = dest / item.name
        if item.is_dir():
            if force and target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            copy_tree(item, target, force=force)
            continue
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def build_env(args: argparse.Namespace, home: Path) -> str:
    lines = [
        "# Local Codex Feishu bridge config. Do not commit this file.",
        f"CODEX_FEISHU_APP_ID={args.app_id or 'FEISHU_APP_ID'}",
        f"CODEX_FEISHU_APP_SECRET={args.app_secret or 'FEISHU_APP_SECRET'}",
        f"CODEX_FEISHU_DOMAIN={args.domain}",
        "CODEX_FEISHU_CONNECTION_MODE=websocket",
        "CODEX_FEISHU_GROUP_POLICY=allowlist",
        f"CODEX_FEISHU_ALLOWED_USERS={args.allowed_users or ''}",
        "CODEX_FEISHU_REACTIONS=true",
        f"CODEX_FEISHU_NOTIFY_CHAT_ID={args.notify_chat_id or ''}",
        f"CODEX_FEISHU_TIMEZONE={args.timezone}",
        f"CODEX_FEISHU_WORKSPACE={args.workspace or str(home / 'workspace')}",
        f"CODEX_FEISHU_CODEX_HOME={args.feishu_codex_home or str(home / 'codex-home')}",
        f"CODEX_HOME={args.codex_home or str(Path.home() / '.codex')}",
        f"CODEX_FEISHU_CODEX_BIN={args.codex_bin}",
        f"CODEX_FEISHU_TASK_POLL_SECONDS={args.wake_window}",
        f"CODEX_FEISHU_TASK_BATCH_LIMIT={args.task_batch_limit}",
        "",
    ]
    return "\n".join(lines)


def create_start_script(home: Path) -> None:
    app_dir = home / "app"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

APP_DIR="{app_dir}"
PYTHON_BIN="${{CODEX_FEISHU_PYTHON:-{home / 'runtime' / 'venv' / 'bin' / 'python'}}}"
if [[ ! -x "${{PYTHON_BIN}}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

CODEX_BIN="${{CODEX_FEISHU_CODEX_BIN:-/Applications/Codex.app/Contents/Resources/codex}}"
if [[ ! -x "${{CODEX_BIN}}" ]]; then
  CODEX_BIN="${{CODEX_FEISHU_CODEX_BIN:-codex}}"
fi

export CODEX_FEISHU_HOME="${{CODEX_FEISHU_HOME:-{home}}}"
export CODEX_FEISHU_RUNTIME_SRC="${{CODEX_FEISHU_RUNTIME_SRC:-{home / 'runtime' / 'src'}}}"
export PYTHONPATH="${{CODEX_FEISHU_RUNTIME_SRC}}${{PYTHONPATH:+:${{PYTHONPATH}}}}"

exec "${{PYTHON_BIN}}" "${{APP_DIR}}/codex_feishu_app.py" \\
  --workspace "${{CODEX_FEISHU_WORKSPACE:-{home / 'workspace'}}}" \\
  --codex-bin "${{CODEX_BIN}}" \\
  --codex-home "${{CODEX_FEISHU_CODEX_HOME:-{home / 'codex-home'}}}" \\
  --env-file "${{CODEX_FEISHU_ENV_FILE:-{home / '.env'}}}" \\
  "$@"
"""
    write_text(app_dir / "start.sh", content, 0o755)


def ensure_launchd_template(home: Path) -> None:
    launchd_dir = home / "app" / "launchd"
    launchd_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": "com.codex.feishu",
        "ProgramArguments": [str(home / "app" / "start.sh")],
        "WorkingDirectory": str(home / "app"),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(home / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(home / "logs" / "launchd.err.log"),
        "EnvironmentVariables": {
            "CODEX_FEISHU_WORKSPACE": str(home / "workspace"),
            "CODEX_FEISHU_ENV_FILE": str(home / ".env"),
            "CODEX_FEISHU_RUNTIME_SRC": str(home / "runtime" / "src"),
            "CODEX_FEISHU_TASK_POLL_SECONDS": "60",
        },
    }
    with (launchd_dir / "com.codex.feishu.plist").open("wb") as fh:
        plistlib.dump(plist, fh)


def create_venv(home: Path, *, skip_pip: bool) -> None:
    venv = home / "runtime" / "venv"
    if not (venv / "bin" / "python").exists():
        run([sys.executable, "-m", "venv", str(venv)])
    if skip_pip:
        return
    run([str(venv / "bin" / "python"), "-m", "pip", "install", "-U", "pip"])
    req = home / "app" / "requirements.txt"
    if req.exists():
        run([str(venv / "bin" / "python"), "-m", "pip", "install", "-r", str(req)])


def install_launch_agent(home: Path, label: str) -> Path:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{label}.plist"
    plist = {
        "Label": label,
        "ProgramArguments": [str(home / "app" / "start.sh")],
        "WorkingDirectory": str(home / "app"),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(home / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(home / "logs" / "launchd.err.log"),
        "EnvironmentVariables": {
            "CODEX_FEISHU_HOME": str(home),
            "CODEX_FEISHU_ENV_FILE": str(home / ".env"),
            "CODEX_FEISHU_RUNTIME_SRC": str(home / "runtime" / "src"),
            "CODEX_FEISHU_CODEX_HOME": str(home / "codex-home"),
            "CODEX_FEISHU_WORKSPACE": str(home / "workspace"),
        },
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)
    return plist_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(DEFAULT_HOME))
    parser.add_argument("--app-id", default="")
    parser.add_argument("--app-secret", default="")
    parser.add_argument("--notify-chat-id", default="")
    parser.add_argument("--domain", choices=["feishu", "lark"], default="feishu")
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--feishu-codex-home", default="")
    parser.add_argument("--codex-bin", default=os.getenv("CODEX_FEISHU_CODEX_BIN", shutil.which("codex") or "codex"))
    parser.add_argument("--workspace", default="")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--allowed-users", default="")
    parser.add_argument("--wake-window", type=int, default=60)
    parser.add_argument("--task-batch-limit", type=int, default=3)
    parser.add_argument("--install-launch-agent", action="store_true")
    parser.add_argument("--launch-agent-label", default="com.codex.feishu")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-pip-install", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser().resolve()
    print(f"install home: {home}")
    if args.dry_run:
        print("dry run only; no files changed")
        return 0

    for child in ("app", "logs", "workspace", "runtime", "conversations", "codex-home"):
        (home / child).mkdir(parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass

    copy_tree(TEMPLATE_HOME, home, force=args.force)
    create_start_script(home)
    ensure_launchd_template(home)

    env_path = home / ".env"
    if env_path.exists() and not args.force:
        print(f"skip existing {env_path}")
    else:
        write_text(env_path, build_env(args, home), 0o600)

    example_path = home / ".env.example"
    if not example_path.exists() or args.force:
        source_example = TEMPLATE_HOME / "app" / ".env.example"
        if source_example.exists():
            shutil.copy2(source_example, example_path)
            try:
                example_path.chmod(0o600)
            except OSError:
                pass

    create_venv(home, skip_pip=args.skip_pip_install)

    if args.install_launch_agent:
        plist_path = install_launch_agent(home, args.launch_agent_label)
        print(f"LaunchAgent written: {plist_path}")
        print(f"reload: launchctl bootout gui/$(id -u) {plist_path} 2>/dev/null || true")
        print(f"reload: launchctl bootstrap gui/$(id -u) {plist_path}")

    print("installed. Fill .env if placeholders remain, then run app/start.sh --check or reload LaunchAgent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
