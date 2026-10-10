"""User-editable local memory and transparent routine suggestions."""

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path


def briefing(state, now=None):
    now = now or datetime.now()
    data = state.snapshot()
    upcoming = sorted(
        (
            item
            for kind in ("active_timers", "active_alarms", "daily_tasks")
            for item in data[kind]
            if datetime.fromtimestamp(item["end_time"]).date() == now.date()
        ),
        key=lambda v: v["end_time"],
    )
    completed = sum(
        datetime.fromtimestamp(v["completed_at"]).date() == now.date()
        for v in data["completed_tasks"]
    )
    parts = [
        f"Дел по расписанию до конца дня: {len(upcoming)}. Непрочитанных напоминаний: {len(state.reminders())}. Выполнено сегодня: {completed}."
    ]
    parts.extend(
        datetime.fromtimestamp(v["end_time"]).strftime("%H:%M") + " — " + v["label"]
        for v in upcoming[:12]
    )
    return "\n".join(parts)


def routine_suggestions(jobs):
    """Suggest only; never schedule or replay observed commands automatically."""
    groups = defaultdict(list)
    for row in jobs:
        if row["status"] == "completed" and row["source"] in ("text", "voice"):
            groups[row["command"].strip().casefold()].append(datetime.fromtimestamp(row["created"]))
    result = []
    for command, times in groups.items():
        days = {v.date() for v in times}
        if len(days) >= 3:
            hours = [v.hour for v in times]
            if max(hours) - min(hours) <= 1:
                result.append(
                    {
                        "command": command,
                        "days": len(days),
                        "time": f"{round(sum(hours) / len(hours)):02}:00",
                    }
                )
    return sorted(result, key=lambda v: -v["days"])[:10]


def export_memory(store, path):
    from storage import atomic_write_json

    atomic_write_json(Path(path), {"version": 1, "memory": store.list()})


def import_memory(store, path):
    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ValueError("Файл памяти слишком большой")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("memory"), list)
        or len(data["memory"]) > 1000
    ):
        raise ValueError("Ожидался экспорт памяти Крест версии 1")
    # Validate the whole batch before modifying existing data.
    for item in data["memory"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("key"), str)
            or not 1 <= len(item["key"].strip()) <= 100
            or not isinstance(item.get("value"), str)
            or len(item["value"]) > 4000
            or item.get("category") not in ("preference", "note", "application", "correction")
        ):
            raise ValueError("Повреждена запись памяти")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        keys = {r[0] for r in db.execute("SELECT key FROM memory")} | {
            v["key"].strip().casefold() for v in data["memory"]
        }
        if len(keys) > 1000:
            raise ValueError("Импорт превышает лимит памяти в 1000 записей")
        import time

        for item in data["memory"]:
            db.execute(
                "INSERT INTO memory VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,category=excluded.category,updated=excluded.updated",
                (item["key"].strip().casefold(), item["value"], item["category"], time.time()),
            )
    return len(data["memory"])
