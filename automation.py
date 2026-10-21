"""Explicit event-to-scenario rules, deduplicated and rate limited."""

from datetime import datetime
from pathlib import Path
import threading
import time

EVENTS = {
    "startup",
    "wake",
    "unlock",
    "lock",
    "device_change",
    "game_started",
    "game_stopped",
    "file_changed",
    "headphones_connected",
    "headphones_disconnected",
}


def validate_rules(rules, scenarios):
    if not isinstance(rules, list) or len(rules) > 30:
        raise ValueError("Допускается до 30 правил событий")
    identifiers = set()
    for rule in rules:
        if (
            not isinstance(rule, dict)
            or not isinstance(rule.get("id"), str)
            or not rule["id"]
            or rule["id"] in identifiers
        ):
            raise ValueError("Правилу нужен уникальный идентификатор")
        identifiers.add(rule["id"])
        if (
            type(rule.get("enabled", False)) is not bool
            or rule.get("event") not in EVENTS
            or rule.get("scenario") not in scenarios
        ):
            raise ValueError("Укажите известное событие и сохранённый сценарий")
        if type(rule.get("cooldown", 60)) is not int or not 10 <= rule.get("cooldown", 60) <= 86400:
            raise ValueError("Пауза между запусками: 10–86400 секунд")
        if rule["event"] == "file_changed" and (
            not isinstance(rule.get("path"), str) or not Path(rule["path"]).is_absolute()
        ):
            raise ValueError("Нужен абсолютный путь отслеживаемого файла")
        if not isinstance(rule.get("match", ""), str) or len(rule.get("match", "")) > 500:
            raise ValueError("Некорректный фильтр события")
        days = rule.get("weekdays", list(range(7)))
        if not isinstance(days, list) or any(type(v) is not int or v not in range(7) for v in days):
            raise ValueError("Некорректные дни недели правила")


class EventAutomation:
    def __init__(self, runtime):
        self.runtime = runtime
        self.last = {}
        self.files = {}
        self.game = ""
        self._lock = threading.RLock()
        self.last_wall = time.time()
        self._last_scan = 0
        self._started = False
        self._audio_devices = None
        self._last_audio = 0
        self._audio_error = ""

    def emit(self, kind, payload=None, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self.runtime.memory.event(kind, payload or {})
            if hasattr(self.runtime, "smart"):
                self.runtime.smart.habits.event(kind, payload)
            for rule in self.runtime.settings().get("event_rules", []):
                if not rule.get("enabled") or rule["event"] != kind:
                    continue
                if kind == "file_changed" and rule["path"] != (payload or {}).get("path"):
                    continue
                if (
                    rule.get("match")
                    and rule["match"].casefold()
                    != str(
                        (payload or {}).get("process" if kind.startswith("game_") else "device", "")
                    ).casefold()
                ):
                    continue
                if datetime.fromtimestamp(now).weekday() not in rule.get(
                    "weekdays", list(range(7))
                ):
                    continue
                ident = rule["id"]
                if now - self.last.get(ident, float("-inf")) < rule.get("cooldown", 60):
                    continue
                self.last[ident] = now
                try:
                    self.runtime.scenarios.start(rule["scenario"])
                    self.runtime.memory.event("rule_started", {"rule": ident, "event": kind})
                except ValueError as exc:
                    self.runtime.publish_notice(
                        f"Правило {ident}: {exc}", "Автоматизация", "low", speak=False
                    )

    def tick(self, now=None):
        now = time.time() if now is None else now
        if not self._started:
            self._started = True
            self.emit("startup", now=now)
        if now - self.last_wall > 90:
            self.emit("wake", {"source": "clock_gap"}, now=now)
        self.last_wall = now
        if now - self._last_scan < 5:
            return
        self._last_scan = now
        game = self.runtime.performance.game
        if game != self.game:
            if self.game:
                self.emit("game_stopped", {"process": self.game}, now)
            if game:
                self.emit("game_started", {"process": game}, now)
            self.game = game
        paths = {
            r["path"]
            for r in self.runtime.settings().get("event_rules", [])
            if r.get("enabled") and r["event"] == "file_changed"
        }
        for path in paths:
            try:
                info = Path(path).stat()
                stamp = (info.st_size, info.st_mtime_ns)
            except OSError:
                stamp = None
            if path in self.files and stamp != self.files[path]:
                self.emit("file_changed", {"path": path, "exists": stamp is not None}, now)
            self.files[path] = stamp
        self.files = {k: v for k, v in self.files.items() if k in paths}
        self._audio_tick(now)
        self._briefing(now)

    def _audio_tick(self, now):
        # The dispatcher already has a bounded device snapshot. It emits the
        # same events, so the scheduler must not enumerate COM devices again.
        if self.runtime.settings().get("sound_dispatcher", {}).get("enabled"):
            return
        rules = self.runtime.settings().get("event_rules", [])
        if (
            not any(r.get("enabled") and r["event"].startswith("headphones_") for r in rules)
            or now - self._last_audio < 15
        ):
            return
        self._last_audio = now
        try:
            from windows_audio import output_devices

            devices = {
                d["id"]: d["name"]
                for d in output_devices()
                if any(
                    token in d["name"].casefold()
                    for token in ("наушник", "головн", "headphone", "headset", "airpods")
                )
            }
            if self._audio_devices is not None:
                for ident in devices.keys() - self._audio_devices.keys():
                    self.emit("headphones_connected", {"device": devices[ident]}, now)
                for ident in self._audio_devices.keys() - devices.keys():
                    self.emit(
                        "headphones_disconnected", {"device": self._audio_devices[ident]}, now
                    )
            self._audio_devices = devices
            self._audio_error = ""
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
            if error != self._audio_error:
                self.runtime.publish_notice(
                    "События наушников недоступны: " + error, "Windows", "low", speak=False
                )
                self._audio_error = error

    def _briefing(self, now):
        from assistant_memory import briefing

        current = datetime.fromtimestamp(now)
        for label, clock in self.runtime.settings().get("briefings", {}).items():
            if not clock:
                continue
            key = "briefing." + label
            date = current.date().isoformat()
            if current.strftime("%H:%M") == clock and self.runtime.memory.recall(key) != date:
                self.runtime.memory.remember(key, date)
                self.runtime.publish_notice(briefing(self.runtime.state, current), "План дня")
