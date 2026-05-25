#!/usr/bin/env python3
"""Shared local task store for Codex Feishu scheduled jobs."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import tempfile
import tomllib
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


DEFAULT_HOME = Path(os.getenv("CODEX_FEISHU_HOME", Path.home() / ".codex-feishu"))
TASK_STORE = Path(os.getenv("CODEX_FEISHU_TASK_STORE", DEFAULT_HOME / "tasks.json"))
DEFAULT_CODEX_HOME = Path(os.getenv("CODEX_HOME", Path.home() / ".codex"))
DEFAULT_TZ = os.getenv("CODEX_FEISHU_TIMEZONE", "Asia/Shanghai")
DEFAULT_NOTIFY_FEISHU_CHAT = os.getenv("CODEX_FEISHU_NOTIFY_CHAT_ID", "").strip()
WEEKDAY_MAP = {
    "mon": 0,
    "monday": 0,
    "一": 0,
    "周一": 0,
    "tue": 1,
    "tuesday": 1,
    "二": 1,
    "周二": 1,
    "wed": 2,
    "wednesday": 2,
    "三": 2,
    "周三": 2,
    "thu": 3,
    "thursday": 3,
    "四": 3,
    "周四": 3,
    "fri": 4,
    "friday": 4,
    "五": 4,
    "周五": 4,
    "sat": 5,
    "saturday": 5,
    "六": 5,
    "周六": 5,
    "sun": 6,
    "sunday": 6,
    "日": 6,
    "天": 6,
    "周日": 6,
    "周天": 6,
}
RRULE_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def now(tz_name: str = DEFAULT_TZ) -> datetime:
    return datetime.now(ZoneInfo(tz_name)).replace(microsecond=0)


def parse_dt(value: str, tz_name: str = DEFAULT_TZ) -> datetime:
    raw = value.strip().replace(" ", "T", 1)
    if len(raw) == 16:
        raw += ":00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.replace(microsecond=0)


def parse_time(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("time must be HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time must be HH:MM")
    return hour, minute


def iso(dt: datetime) -> str:
    return dt.astimezone().replace(microsecond=0).isoformat()


def epoch_ms(value: str = "", tz_name: str = DEFAULT_TZ) -> int:
    try:
        dt = parse_dt(value, tz_name) if value else now(tz_name)
    except Exception:
        dt = now(tz_name)
    return int(dt.timestamp() * 1000)


def codex_home_dir(codex_home: str | Path | None = None) -> Path:
    if codex_home:
        return Path(codex_home).expanduser().resolve()
    return DEFAULT_CODEX_HOME.expanduser().resolve()


def ensure_store() -> Path:
    TASK_STORE.parent.mkdir(parents=True, exist_ok=True)
    try:
        TASK_STORE.parent.chmod(0o700)
    except OSError:
        pass
    if not TASK_STORE.exists():
        TASK_STORE.write_text(json.dumps({"version": 1, "tasks": []}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            TASK_STORE.chmod(0o600)
        except OSError:
            pass
    return TASK_STORE


@contextmanager
def locked_store() -> Iterator[dict[str, Any]]:
    path = ensure_store()
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(data, dict):
                data = {"version": 1, "tasks": []}
            if not isinstance(data.get("tasks"), list):
                data["tasks"] = []
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


def compute_first_run(schedule: dict[str, Any], tz_name: str = DEFAULT_TZ) -> datetime:
    base = now(tz_name)
    kind = schedule["type"]
    if kind == "once":
        return parse_dt(schedule["at"], tz_name)
    if kind == "interval":
        return base + timedelta(minutes=int(schedule["minutes"]))
    if kind == "daily":
        hour, minute = parse_time(schedule["time"])
        candidate = base.replace(hour=hour, minute=minute, second=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate
    if kind == "weekly":
        hour, minute = parse_time(schedule["time"])
        weekdays = schedule["weekdays"]
        candidates = []
        for day in weekdays:
            delta = (int(day) - base.weekday()) % 7
            candidate = (base + timedelta(days=delta)).replace(hour=hour, minute=minute, second=0)
            if candidate <= base:
                candidate += timedelta(days=7)
            candidates.append(candidate)
        return min(candidates)
    raise ValueError(f"unsupported schedule type: {kind}")


def compute_next_run(task: dict[str, Any], after: datetime | None = None) -> str | None:
    schedule = task.get("schedule") or {}
    tz_name = task.get("timezone") or DEFAULT_TZ
    base = after.astimezone(ZoneInfo(tz_name)) if after else now(tz_name)
    kind = schedule.get("type")
    if kind == "once":
        return None
    if kind == "interval":
        return iso(base + timedelta(minutes=int(schedule["minutes"])))
    if kind == "daily":
        hour, minute = parse_time(schedule["time"])
        candidate = base.replace(hour=hour, minute=minute, second=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return iso(candidate)
    if kind == "weekly":
        hour, minute = parse_time(schedule["time"])
        candidates = []
        for day in schedule["weekdays"]:
            delta = (int(day) - base.weekday()) % 7
            candidate = (base + timedelta(days=delta)).replace(hour=hour, minute=minute, second=0)
            if candidate <= base:
                candidate += timedelta(days=7)
            candidates.append(candidate)
        return iso(min(candidates))
    return None


def parse_weekly(value: str) -> dict[str, Any]:
    if "@" not in value:
        raise ValueError("weekly format must be day[,day]@HH:MM")
    days_raw, time_raw = value.split("@", 1)
    weekdays = []
    for item in days_raw.split(","):
        key = item.strip().lower()
        if key.isdigit():
            day = int(key)
            if 1 <= day <= 7:
                day -= 1
            if not 0 <= day <= 6:
                raise ValueError("weekday number must be 0-6 or 1-7")
        else:
            if key not in WEEKDAY_MAP:
                raise ValueError(f"unknown weekday: {item}")
            day = WEEKDAY_MAP[key]
        weekdays.append(day)
    parse_time(time_raw)
    return {"type": "weekly", "weekdays": sorted(set(weekdays)), "time": time_raw.strip()}


def schedule_to_rrule(schedule: dict[str, Any], tz_name: str = DEFAULT_TZ) -> tuple[str, bool]:
    kind = schedule.get("type")
    if kind == "daily":
        hour, minute = parse_time(str(schedule.get("time") or "00:00"))
        return f"FREQ=WEEKLY;BYDAY={','.join(RRULE_WEEKDAYS)};BYHOUR={hour};BYMINUTE={minute}", True
    if kind == "weekly":
        hour, minute = parse_time(str(schedule.get("time") or "00:00"))
        byday = ",".join(RRULE_WEEKDAYS[int(day)] for day in schedule.get("weekdays") or [])
        return f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={hour};BYMINUTE={minute}", True
    if kind == "interval":
        minutes = int(schedule.get("minutes") or 0)
        if minutes > 0 and minutes % 60 == 0:
            return f"FREQ=HOURLY;INTERVAL={minutes // 60}", True
        return f"FREQ=MINUTELY;INTERVAL={max(1, minutes)}", False
    if kind == "once":
        dt = parse_dt(str(schedule.get("at") or ""), tz_name)
        return f"FREQ=DAILY;COUNT=1;BYHOUR={dt.hour};BYMINUTE={dt.minute}", False
    return "FREQ=DAILY;INTERVAL=1", False


def automation_id_for_task(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(task_id or "").strip()).strip("-")
    if safe.startswith("task_"):
        safe = "task-" + safe.removeprefix("task_")
    return f"codex-feishu-{safe or uuid.uuid4().hex[:12]}"


def automation_status(task: dict[str, Any]) -> str:
    return "ACTIVE" if task.get("status") in {"active", "running"} else "PAUSED"


def load_codex_runtime_defaults(codex_home: str | Path | None = None) -> dict[str, str]:
    path = codex_home_dir(codex_home) / "config.toml"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        config = {}
    return {
        "model": str(config.get("model") or ""),
        "reasoning_effort": str(config.get("model_reasoning_effort") or "medium"),
    }


def _toml_str(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(value) for value in values) + "]"


def codex_automation_path(task: dict[str, Any], codex_home: str | Path | None = None) -> Path:
    automation_id = str(task.get("codex_automation_id") or automation_id_for_task(str(task.get("id") or "")))
    return codex_home_dir(codex_home) / "automations" / automation_id / "automation.toml"


def write_codex_automation(task: dict[str, Any], codex_home: str | Path | None = None) -> str:
    if truthy_env("CODEX_FEISHU_DISABLE_CODEX_AUTOMATION_MIRROR"):
        return ""
    automation_id = str(task.get("codex_automation_id") or automation_id_for_task(str(task.get("id") or "")))
    task["codex_automation_id"] = automation_id
    path = codex_automation_path(task, codex_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    runtime = load_codex_runtime_defaults(codex_home)
    schedule = task.get("schedule") or {}
    tz_name = str(task.get("timezone") or DEFAULT_TZ)
    rrule, codex_native_schedule = schedule_to_rrule(schedule, tz_name)
    workspace = str(Path(task.get("workspace") or os.getcwd()).expanduser().resolve())
    created_at = str(task.get("created_at") or "")
    updated_at = str(task.get("updated_at") or "")
    name = str(task.get("name") or task.get("id") or "Codex Feishu task")
    display_name = name if name.startswith("Codex Feishu") else f"Codex Feishu · {name}"
    prompt = str(task.get("prompt") or "").strip()
    body_lines = [
        "version = 1",
        f"id = {_toml_str(automation_id)}",
        'kind = "cron"',
        f"name = {_toml_str(display_name)}",
        f"prompt = {_toml_str(prompt)}",
        f"status = {_toml_str(automation_status(task))}",
        f"rrule = {_toml_str(rrule)}",
        f"cwds = {_toml_array([workspace])}",
        'execution_environment = "local"',
        f"model = {_toml_str(runtime['model'])}",
        f"reasoning_effort = {_toml_str(runtime['reasoning_effort'])}",
        "local_environment_config_path = \"\"",
        f"created_at = {epoch_ms(created_at, tz_name)}",
        f"updated_at = {epoch_ms(updated_at, tz_name)}",
        "",
        "[metadata]",
        'source = "codex-feishu"',
        f"feishu_task_id = {_toml_str(task.get('id'))}",
        f"task_store = {_toml_str(TASK_STORE)}",
        f"task_status = {_toml_str(task.get('status'))}",
        f"task_next_run_at = {_toml_str(task.get('next_run_at'))}",
        f"task_timezone = {_toml_str(tz_name)}",
        f"feishu_destination_type = {_toml_str((task.get('destination') or {}).get('type'))}",
        f"feishu_chat_id = {_toml_str((task.get('destination') or {}).get('chat_id'))}",
        f"codex_native_schedule = {'true' if codex_native_schedule else 'false'}",
        'authoritative_runner = "com.codex.feishu"',
        'note = "Created from Feishu; com.codex.feishu remains the authoritative runner for Feishu notification."',
    ]
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(body_lines).rstrip() + "\n")
    os.replace(tmp_name, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return str(path)


def remove_codex_automation(task: dict[str, Any], codex_home: str | Path | None = None) -> None:
    automation_id = str(task.get("codex_automation_id") or "")
    if not automation_id.startswith("codex-feishu-"):
        return
    automations_dir = codex_home_dir(codex_home) / "automations"
    target = (automations_dir / automation_id).resolve()
    try:
        target.relative_to(automations_dir.resolve())
    except ValueError:
        return
    shutil.rmtree(target, ignore_errors=True)


def mark_task_automation_error(task_id: str, error: str) -> None:
    current = now()
    with locked_store() as data:
        for task in data["tasks"]:
            if task.get("id") == task_id:
                task["codex_automation_last_error"] = str(error)[:1000]
                task["updated_at"] = iso(current)
                return


def sync_task_automation(task: dict[str, Any], codex_home: str | Path | None = None) -> dict[str, Any]:
    try:
        path = write_codex_automation(task, codex_home)
    except Exception as exc:
        mark_task_automation_error(str(task.get("id") or ""), str(exc))
        updated = dict(task)
        updated["codex_automation_last_error"] = str(exc)[:1000]
        return updated
    if path:
        task["codex_automation_path"] = path
    return dict(task)


def _merge_automation_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    target.update(
        {
            key: value
            for key, value in source.items()
            if key.startswith("codex_automation")
        }
    )


def sync_all_task_automations(codex_home: str | Path | None = None) -> int:
    tasks_to_sync: list[dict[str, Any]] = []
    with locked_store() as data:
        for task in data["tasks"]:
            if not task.get("codex_automation_id"):
                task["codex_automation_id"] = automation_id_for_task(str(task.get("id") or ""))
            tasks_to_sync.append(dict(task))
    synced: dict[str, dict[str, Any]] = {}
    for task in tasks_to_sync:
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        synced[task_id] = sync_task_automation(task, codex_home)
    if synced:
        with locked_store() as data:
            for task in data["tasks"]:
                updated = synced.get(str(task.get("id") or ""))
                if updated:
                    _merge_automation_fields(task, updated)
    return len(synced)


def schedule_from_args(args: argparse.Namespace) -> dict[str, Any]:
    selected = [bool(args.at), bool(args.every_minutes), bool(args.daily_at), bool(args.weekly)]
    if sum(selected) != 1:
        raise ValueError("choose exactly one of --at, --every-minutes, --daily-at, --weekly")
    if args.at:
        parse_dt(args.at, args.timezone)
        return {"type": "once", "at": args.at}
    if args.every_minutes:
        minutes = int(args.every_minutes)
        if minutes <= 0:
            raise ValueError("--every-minutes must be positive")
        return {"type": "interval", "minutes": minutes}
    if args.daily_at:
        parse_time(args.daily_at)
        return {"type": "daily", "time": args.daily_at}
    return parse_weekly(args.weekly)


def add_task(args: argparse.Namespace) -> dict[str, Any]:
    schedule = schedule_from_args(args)
    task_id = "task_" + uuid.uuid4().hex[:12]
    created = now(args.timezone)
    name = args.name or args.prompt.strip().splitlines()[0][:60] or task_id
    task = {
        "id": task_id,
        "codex_automation_id": automation_id_for_task(task_id),
        "name": name,
        "prompt": args.prompt,
        "workspace": str(Path(args.workspace).expanduser().resolve()),
        "schedule": schedule,
        "timezone": args.timezone,
        "status": "active",
        "next_run_at": iso(compute_first_run(schedule, args.timezone)),
        "destination": {
            "type": "feishu" if (args.notify_feishu_chat or DEFAULT_NOTIFY_FEISHU_CHAT) else "log",
            "chat_id": args.notify_feishu_chat or DEFAULT_NOTIFY_FEISHU_CHAT,
            "reply_to": args.notify_reply_to or "",
        },
        "source": args.source or "manual",
        "created_at": iso(created),
        "updated_at": iso(created),
        "last_run_at": "",
        "last_status": "",
        "last_summary": "",
        "last_output_path": "",
    }
    with locked_store() as data:
        data["tasks"].append(task)
    updated = sync_task_automation(task)
    with locked_store() as data:
        for stored in data["tasks"]:
            if stored.get("id") == task_id:
                _merge_automation_fields(stored, updated)
                break
    return updated


def list_tasks(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    with locked_store() as data:
        tasks = list(data["tasks"])
    if include_inactive:
        return tasks
    return [t for t in tasks if t.get("status") in {"active", "running"}]


def due_tasks(*, limit: int = 20, at: datetime | None = None) -> list[dict[str, Any]]:
    at = at or now()
    result = []
    for task in list_tasks(include_inactive=False):
        if task.get("status") != "active":
            continue
        next_run = task.get("next_run_at")
        if next_run and parse_dt(next_run) <= at:
            result.append(task)
        if len(result) >= limit:
            break
    return result


def seconds_until_next_due(at: datetime | None = None) -> float | None:
    current = at or now()
    nearest: datetime | None = None
    for task in list_tasks(include_inactive=False):
        if task.get("status") != "active":
            continue
        next_run = task.get("next_run_at")
        if not next_run:
            continue
        try:
            candidate = parse_dt(str(next_run))
        except Exception:
            continue
        if nearest is None or candidate < nearest:
            nearest = candidate
    if nearest is None:
        return None
    return max(0.0, (nearest - current.astimezone(nearest.tzinfo)).total_seconds())


def claim_due_tasks(*, runner_id: str, limit: int = 5) -> list[dict[str, Any]]:
    claimed = []
    current = now()
    with locked_store() as data:
        for task in data["tasks"]:
            if task.get("status") != "active":
                continue
            next_run = task.get("next_run_at")
            if not next_run or parse_dt(next_run) > current:
                continue
            task["status"] = "running"
            task["locked_by"] = runner_id
            task["locked_at"] = iso(current)
            task["updated_at"] = iso(current)
            claimed.append(dict(task))
            if len(claimed) >= limit:
                break
    return claimed


def complete_task(task_id: str, *, success: bool, summary: str = "", output_path: str = "") -> dict[str, Any] | None:
    current = now()
    updated_task: dict[str, Any] | None = None
    with locked_store() as data:
        for task in data["tasks"]:
            if task.get("id") != task_id:
                continue
            task["last_run_at"] = iso(current)
            task["last_status"] = "success" if success else "failure"
            task["last_summary"] = summary[:2000]
            task["last_output_path"] = output_path
            next_run = compute_next_run(task, after=current)
            task["next_run_at"] = next_run or ""
            task["status"] = "active" if next_run else "completed"
            task.pop("locked_by", None)
            task.pop("locked_at", None)
            task["updated_at"] = iso(current)
            updated_task = dict(task)
            break
    if updated_task is not None:
        updated_task = sync_task_automation(updated_task)
        with locked_store() as data:
            for stored in data["tasks"]:
                if stored.get("id") == task_id:
                    _merge_automation_fields(stored, updated_task)
                    break
    return updated_task


def recover_running_tasks() -> int:
    recovered = 0
    current = now()
    with locked_store() as data:
        for task in data["tasks"]:
            if task.get("status") != "running":
                continue
            task["status"] = "active"
            task.pop("locked_by", None)
            task.pop("locked_at", None)
            task["updated_at"] = iso(current)
            recovered += 1
    return recovered


def set_task_status(task_id: str, status: str) -> bool:
    current = now()
    updated_task: dict[str, Any] | None = None
    with locked_store() as data:
        for task in data["tasks"]:
            if task.get("id") == task_id:
                task["status"] = status
                task["updated_at"] = iso(current)
                updated_task = dict(task)
                break
    if updated_task is None:
        return False
    updated_task = sync_task_automation(updated_task)
    with locked_store() as data:
        for stored in data["tasks"]:
            if stored.get("id") == task_id:
                _merge_automation_fields(stored, updated_task)
                break
    return True


def delete_task(task_id: str) -> bool:
    removed: list[dict[str, Any]] = []
    with locked_store() as data:
        before = len(data["tasks"])
        kept = []
        for task in data["tasks"]:
            if task.get("id") == task_id:
                removed.append(dict(task))
            else:
                kept.append(task)
        data["tasks"] = kept
        changed = len(data["tasks"]) != before
    for task in removed:
        remove_codex_automation(task)
    return changed


def print_result(obj: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif isinstance(obj, list):
        for task in obj:
            print(f"{task['id']} [{task.get('status')}] {task.get('name')} next={task.get('next_run_at')}")
    elif isinstance(obj, dict):
        print(f"{obj['id']} [{obj.get('status')}] {obj.get('name')} next={obj.get('next_run_at')}")
    else:
        print(obj)


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex Feishu shared task store")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("--name", default="")
    add.add_argument("--prompt", required=True)
    add.add_argument("--workspace", default=os.getcwd())
    add.add_argument("--timezone", default=DEFAULT_TZ)
    add.add_argument("--at")
    add.add_argument("--every-minutes", type=int)
    add.add_argument("--daily-at")
    add.add_argument("--weekly", help="day[,day]@HH:MM, e.g. mon,wed@08:30")
    add.add_argument("--notify-feishu-chat", default="")
    add.add_argument("--notify-reply-to", default="")
    add.add_argument("--source", default="manual")
    add.add_argument("--json", action="store_true")

    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--json", action="store_true")

    due = sub.add_parser("due")
    due.add_argument("--json", action="store_true")

    pause = sub.add_parser("pause")
    pause.add_argument("task_id")
    resume = sub.add_parser("resume")
    resume.add_argument("task_id")
    delete = sub.add_parser("delete")
    delete.add_argument("task_id")

    args = parser.parse_args()
    if args.cmd == "add":
        print_result(add_task(args), as_json=args.json)
        return 0
    if args.cmd == "list":
        print_result(list_tasks(include_inactive=args.all), as_json=args.json)
        return 0
    if args.cmd == "due":
        print_result(due_tasks(), as_json=args.json)
        return 0
    if args.cmd == "pause":
        return 0 if set_task_status(args.task_id, "paused") else 1
    if args.cmd == "resume":
        return 0 if set_task_status(args.task_id, "active") else 1
    if args.cmd == "delete":
        return 0 if delete_task(args.task_id) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
