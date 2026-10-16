"""Bounded local Warframe notebook; every edit is atomic and independent of network access."""

from storage import read_json


from contextlib import contextmanager
import copy
import json
from pathlib import Path
import threading
import time
import uuid
from storage import atomic_write_json

LIMITS = {
    "goals": 200,
    "tasks": 100,
    "foundry": 100,
    "builds": 100,
    "journal": 300,
    "trades": 300,
    "watches": 30,
}


def empty_state():
    return dict(
        version=1,
        inventory={},
        mod_ranks={},
        aliases={},
        notifications={},
        **{key: [] for key in LIMITS},
    )


def validate_state(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Неизвестный формат данных Warframe")
    credits = data.get("farm_credits", [])
    if (
        not isinstance(credits, list)
        or len(credits) > 300
        or any(
            not isinstance(r, dict)
            or any(not isinstance(r.get(k), str) or len(r[k]) > 200 for k in ("id", "name", "goal"))
            or type(r.get("quantity")) is not int
            or not 1 <= r["quantity"] <= 100
            or any(
                type(r.get(k)) not in (int, float) or not 0 <= r[k] <= 10**12
                for k in ("time", "minutes")
            )
            for r in credits
        )
    ):
        raise ValueError("Повреждены подтверждения добычи")
    for key, limit in LIMITS.items():
        rows = data.get(key)
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("Слишком большой или повреждённый раздел " + key)
        seen = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or not row["id"]
                or len(row["id"]) > 200
                or row["id"] in seen
            ):
                raise ValueError("Повреждена запись " + key)
            seen.add(row["id"])
            if (
                not isinstance(row.get("name"), str)
                or not row["name"].strip()
                or len(row["name"]) > 200
            ):
                raise ValueError("Название должно содержать 1–200 символов")
            if not isinstance(row.get("notes", ""), str) or len(row.get("notes", "")) > 8000:
                raise ValueError("Заметка слишком длинная")
            for number in (
                "created",
                "ends",
                "quantity",
                "price",
                "minutes",
                "priority",
                "progress",
                "target",
                "completed_at",
            ):
                if number in row and (
                    not isinstance(row[number], (int, float))
                    or isinstance(row[number], bool)
                    or not 0 <= row[number] <= 10**12
                ):
                    raise ValueError("Некорректное число: " + number)
            if key == "tasks" and row.get("period") not in ("once", "daily", "weekly"):
                raise ValueError("Период задачи: once, daily, weekly")
            if key == "tasks":
                for field, maximum in [("reset_hour", 23), ("reset_weekday", 6)]:
                    if type(row.get(field, 0)) is not int or not 0 <= row.get(field, 0) <= maximum:
                        raise ValueError("Некорректное время сброса задачи")
            if (
                key == "watches"
                and row.get("rank") is not None
                and (type(row["rank"]) is not int or not 0 <= row["rank"] <= 10)
            ):
                raise ValueError("Ранг: 0–10")
            for field in ("done", "claimed", "enabled"):
                if field in row and type(row[field]) is not bool:
                    raise ValueError("Некорректный переключатель " + field)
            if key == "foundry" and (
                not isinstance(row.get("ends"), (int, float))
                or row["ends"] > time.time() + 366 * 86400
            ):
                raise ValueError("Некорректное время Кузницы")
            if key == "watches" and (
                not isinstance(row.get("slug"), str)
                or not __import__("re").fullmatch(r"[a-z0-9_]{1,160}", row["slug"])
                or not 1 <= row.get("price", 0) <= 100000
            ):
                raise ValueError("Некорректное правило цены")
            if key == "builds":
                mods = row.get("mods", {})
                if (
                    not isinstance(mods, dict)
                    or len(mods) > 20
                    or any(
                        not isinstance(name, str)
                        or len(name) > 150
                        or type(rank) is not int
                        or not 0 <= rank <= 10
                        for name, rank in mods.items()
                    )
                ):
                    raise ValueError("Укажите до 20 модов с рангом от 0 до 10")
                url = row.get("url", "")
                if (
                    not isinstance(url, str)
                    or len(url) > 2000
                    or url
                    and not url.startswith("https://")
                ):
                    raise ValueError("Ссылка на билд должна начинаться с https://")
    for key in ("inventory", "mod_ranks"):
        values = data.get(key)
        if (
            not isinstance(values, dict)
            or len(values) > 2000
            or any(
                not isinstance(n, str)
                or not 1 <= len(n) <= 200
                or type(v) is not int
                or not 0 <= v <= (10 if key == "mod_ranks" else 10**9)
                for n, v in values.items()
            )
        ):
            raise ValueError("Некорректная коллекция: " + key)
    aliases = data.get("aliases")
    if (
        not isinstance(aliases, dict)
        or len(aliases) > 300
        or any(
            not isinstance(k, str)
            or not isinstance(v, str)
            or not 1 <= len(k) <= 150
            or not 1 <= len(v) <= 150
            for k, v in aliases.items()
        )
    ):
        raise ValueError("Некорректные названия предметов")
    if not isinstance(data.get("notifications"), dict) or len(data["notifications"]) > 1000:
        raise ValueError("Повреждена история оповещений")
    return data


class WarframeStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = empty_state()
        self.error = ""
        if self.path.exists():
            try:
                if self.path.stat().st_size > 4 * 1024**2:
                    raise ValueError("Данные превышают 4 МБ")
                self.data = validate_state(read_json(self.path))
            except (OSError, ValueError, TypeError) as exc:
                self.error = str(exc)

    def snapshot(self, *sections):
        """Copy only requested sections for frequent status reads, or all for editing."""
        with self.lock:
            data = (
                {key: self.data[key] for key in sections if key in self.data}
                if sections
                else self.data
            )
            return copy.deepcopy(data)

    @contextmanager
    def edit(self):
        with self.lock:
            if self.error:
                raise ValueError(
                    "Данные Warframe повреждены: "
                    + self.error
                    + ". Сохраните файл перед восстановлением."
                )
            before = copy.deepcopy(self.data)
            try:
                yield self.data
                validate_state(self.data)
                if len(json.dumps(self.data, ensure_ascii=False).encode("utf-8")) > 4 * 1024**2:
                    raise ValueError(
                        "Блокнот превышает 4 МБ. Экспортируйте и удалите старые записи."
                    )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(self.path, self.data)
            except Exception:
                self.data = before
                raise

    def add(self, section, name, **values):
        if section not in LIMITS:
            raise ValueError("Неизвестный раздел")
        row = dict(values, id=uuid.uuid4().hex, name=str(name).strip(), created=time.time())
        with self.edit() as data:
            if section in ("journal", "trades"):
                data[section] = data[section][-(LIMITS[section] - 1) :]
            data[section].append(row)
        return row

    def update(self, section, ident, **values):
        if section not in LIMITS or set(values) & {"id", "created"}:
            raise ValueError("Некорректное изменение")
        with self.edit() as data:
            row = next((r for r in data[section] if r["id"] == ident), None)
            if row is None:
                raise ValueError("Запись уже удалена")
            row.update(values)
        return copy.deepcopy(row)

    def remove(self, section, ident):
        if section not in LIMITS:
            raise ValueError("Неизвестный раздел")
        with self.edit() as data:
            data[section] = [row for row in data[section] if row["id"] != ident]

    def remember_notice(self, key, when=None, repeat_after=None):
        when = time.time() if when is None else when
        with self.lock:
            previous = self.data["notifications"].get(key)
            if previous is not None and (
                repeat_after is None
                or isinstance(previous, (int, float))
                and when - previous < repeat_after
            ):
                return False
            with self.edit() as data:
                data["notifications"] = {
                    k: v
                    for k, v in data["notifications"].items()
                    if isinstance(v, (float, int)) and when - v < 14 * 86400
                }
                if len(data["notifications"]) >= 950:
                    oldest = sorted(data["notifications"], key=data["notifications"].get)[:100]
                    for item in oldest:
                        del data["notifications"][item]
                data["notifications"][key] = when
            return True

    def export(self, path):
        atomic_write_json(Path(path), self.snapshot())
        return str(path)

    def import_file(self, path):
        path = Path(path)
        if path.stat().st_size > 4 * 1024**2:
            raise ValueError("Файл превышает 4 МБ")
        incoming = validate_state(json.loads(path.read_text(encoding="utf-8")))
        # Imported active deadlines/watchers need an explicit user review.
        incoming["notifications"] = {}
        for row in incoming["watches"]:
            row["enabled"] = False
        with self.lock:
            if self.error:
                import shutil

                backup = self.path.with_name(self.path.name + ".broken-" + str(time.time_ns()))
                shutil.copy2(self.path, backup)
                atomic_write_json(self.path, incoming)
                self.data = incoming
                self.error = ""
            else:
                with self.edit() as data:
                    data.clear()
                    data.update(incoming)
        return "Данные Warframe восстановлены. Оповещения о ценах включаются отдельно."
