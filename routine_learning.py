"""Learn only successful, explicitly addressed commands; activation is reviewable."""

import hashlib
import json
import threading
import time
import uuid

TRIGGERS = {
    "startup": "После запуска Крест",
    "game_started": "После запуска игры",
    "headphones_connected": "При подключении наушников",
}
SAFE_SKILLS = {
    "skills.workflows.launch_catalog_app",
    "skills.workflows.app_volume",
    "skills.media.open_yandex_music",
    "skills.media.open_spotify",
    "skills.media.set_exact_volume",
    "skills.assistant_tools.call_audio",
    "skills.assistant_tools.normal_audio",
    "skills.assistant_tools.auto_audio",
}


class RoutineLearning:
    def __init__(self, runtime, store):
        self.runtime, self.store = runtime, store
        self.lock = threading.RLock()
        self.active = None
        self.pending = []

    def enabled(self):
        return self.runtime.settings().get("smart_tools", {}).get("learn_routines", True)

    def event(self, kind, payload=None, now=None):
        if kind not in TRIGGERS or not self.enabled():
            return
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.active and len(self.active["steps"]) >= 2:
                self.pending.append(self.active)
                self.pending = self.pending[-20:]
            self.active = {
                "event": kind,
                "match": str((payload or {}).get("process", ""))
                if kind == "game_started"
                else str((payload or {}).get("device", ""))
                if kind == "headphones_connected"
                else "",
                "started": now,
                "session": uuid.uuid4().hex,
                "steps": [],
                "wall": time.time(),
            }

    def observe(self, command, skill_id, source):
        if (
            source not in {"voice", "text"}
            or skill_id not in SAFE_SKILLS
            or not command
            or not self.enabled()
        ):
            return
        with self.lock:
            if not self.active or time.monotonic() - self.active["started"] > 180:
                return
            steps = self.active["steps"]
            if len(steps) < 5 and (not steps or steps[-1] != command):
                steps.append(command)

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.enabled():
                self.active = None
                self.pending.clear()
                return
            if self.active and now - self.active["started"] >= 180:
                if len(self.active["steps"]) >= 2:
                    self.pending.append(self.active)
                self.active = None
            rows, self.pending = self.pending, []
        for sample in rows:
            signature = json.dumps(
                [sample["event"], sample["match"], sample["steps"]], ensure_ascii=False
            )
            ident = hashlib.sha256(signature.encode()).hexdigest()[:24]
            row = self.store.get("habits", ident) or {
                "id": ident,
                "event": sample["event"],
                "match": sample["match"],
                "steps": sample["steps"],
                "count": 0,
                "last": 0,
                "status": "learning",
            }
            # Restarting a game/assistant rapidly is one observation, not evidence
            # of a daily routine. Exact order and parameters must repeat.
            if sample["wall"] - row["last"] < 600:
                continue
            row["count"] += 1
            row["last"] = sample["wall"]
            if row["count"] >= 3 and row["status"] == "learning":
                row["status"] = "suggested"
                self.runtime.publish_notice(
                    "Повторяется привычка: "
                    + TRIGGERS[row["event"]]
                    + (" · " + row["match"] if row["match"] else "")
                    + ". Проверьте шаги в меню «Привычки» перед включением.",
                    "Автоматизация",
                    "low",
                    speak=False,
                )
            self.store.put("habits", row, limit=100)

    def activate(self, ident):
        with self.lock, self.runtime.config_manager._lock:
            row = self.store.get("habits", ident)
            if row is None or row["status"] != "suggested":
                raise ValueError("Выберите готовое предложение автоматизации")
            cfg = self.runtime.config_manager.snapshot()
            name = "Привычка " + ident[:8]
            if name in cfg["scenarios"] or any(
                r["id"] == "habit-" + ident for r in cfg["event_rules"]
            ):
                raise ValueError("Сценарий с этим именем уже существует; проверьте раздел событий")
            # Revalidate commands against the currently installed catalog before
            # storing anything. Changes to aliases/plugins cannot widen the rule.
            for command in row["steps"]:
                match = self.runtime.manager.match(command)
                entry = match[0] if isinstance(match, tuple) else match
                if not entry or entry.id not in SAFE_SKILLS:
                    raise ValueError("Одна из команд изменилась; предложение нужно изучить заново")
            cfg["scenarios"][name] = [
                {"action": "command", "value": command} for command in row["steps"]
            ]
            cfg["event_rules"].append(
                {
                    "id": "habit-" + ident,
                    "event": row["event"],
                    "match": row["match"],
                    "scenario": name,
                    "enabled": True,
                    "cooldown": 600,
                }
            )
            self.runtime.config_manager.save_config(cfg)
            row.update(status="enabled", scenario=name)
            self.store.put("habits", row, limit=100)
            return (
                "Автоматизация включена: " + name + ". Отключить её можно здесь или в «Событиях»."
            )

    def dismiss(self, ident):
        with self.lock, self.runtime.config_manager._lock:
            row = self.store.get("habits", ident)
            if not row:
                raise ValueError("Предложение не найдено")
            cfg = self.runtime.config_manager.snapshot()
            for rule in cfg["event_rules"]:
                if rule["id"] == "habit-" + ident:
                    rule["enabled"] = False
            self.runtime.config_manager.save_config(cfg)
            row["status"] = "dismissed"
            self.store.put("habits", row, limit=100)
            return "Предложение отключено; автоматически включаться не будет"
