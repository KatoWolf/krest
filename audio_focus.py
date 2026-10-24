"""Reversible per-session ducking; other applications and manual changes win."""

from contextlib import contextmanager
import threading
import time
from storage import atomic_write_json, read_json


class AudioFocus:
    def __init__(self, settings, path, backend=None):
        self.settings, self.path = settings, path
        self.backend = backend
        self.lock = threading.RLock()
        self.mode = "auto"
        self.reasons = {}
        self.owned = {}
        self.ignored = set()
        self.last_tick = 0
        self.error = ""
        self.recovered = False
        self.call_active = False
        self.manual_except = None
        self._saved_ledger = None

    def _persist(self, ledger):
        # Keep write-ahead recovery, but do not replace/fsync an unchanged file
        # every time a game or a call is polled.
        snapshot = {key: dict(row) for key, row in ledger.items()}
        if snapshot != self._saved_ledger:
            atomic_write_json(self.path, snapshot)
            self._saved_ledger = snapshot

    def release_sessions(self, identifiers=None):
        """Restore only profile targets; leave other playing sessions ducked."""
        with self.lock:
            keys = set(self.owned) if identifiers is None else set(identifiers)
            if keys & self.owned.keys():
                read, write = self.port()
                current = {row["id"]: row for row in read()}
                for key in keys & self.owned.keys():
                    row = self.owned[key]
                    if key in current and self._matches(current[key]["volume"], row):
                        write(key, row["before"])
                    self.owned.pop(key)
                    self._persist(self.owned)
            self.ignored.difference_update(keys)

    def port(self):
        if self.backend is None:
            from windows_audio import focus_sessions, focus_set

            self.backend = (focus_sessions, focus_set)
        return self.backend

    def _apply(self):
        cfg = self.settings().get("audio_focus", {})
        factor = (
            min(self.reasons.values(), default=1)
            if cfg.get("enabled") or self.call_active or self.manual_except is not None
            else 1
        )
        if factor == 1 and not self.owned and (self.recovered or not self.path.exists()):
            # No pending restore and no ducking: avoid COM enumeration and an
            # empty journal write on every tick, even with the mixer enabled.
            self.recovered = True
            self.ignored.clear()
            self.error = ""
            return
        read, write = self.port()
        sessions = {r["id"]: r for r in read()}
        if not self.recovered:
            old = read_json(self.path) if self.path.exists() else {}
            if not isinstance(old, dict) or any(
                not isinstance(row, dict)
                or not {"before", "applied"} <= row.keys()
                or any(
                    type(value) not in (int, float) or not 0 <= value <= 1 for value in row.values()
                )
                for row in old.values()
            ):
                raise ValueError("Повреждён журнал восстановления громкости")
            for key, row in old.items():
                current = sessions.get(key)
                if current and self._matches(current["volume"], row):
                    write(key, row["before"])
                    current["volume"] = row["before"]
            # Leave the previous ledger intact if even one restore fails.
            self._persist({})
            self.recovered = True
        names = {v.strip().casefold() for v in cfg.get("applications", "").split(",") if v.strip()}
        for key, row in list(self.owned.items()):
            current = sessions.get(key)
            if current is None or not self._matches(current["volume"], row):
                self.owned.pop(key, None)
                self.ignored.add(key)
        for key, current in sessions.items():
            row = self.owned.get(key)
            selected = (
                current["name"].casefold() in names
                if self.manual_except is None
                else current["name"].casefold() not in self.manual_except
            )
            if not row and (factor == 1 or not selected or key in self.ignored):
                continue
            row = row or {"before": current["volume"], "applied": current["volume"]}
            target = row["before"] * factor if selected else row["before"]
            if abs(current["volume"] - target) > 0.005:
                # Write ahead: either the old or new value may be live if the
                # process/backend fails halfway through SetMasterVolume.
                row = {"before": row["before"], "applied": target, "previous": current["volume"]}
                self.owned[key] = row
                self._persist(self.owned)
                write(key, target)
            row.pop("previous", None)
            row["applied"] = target
            if factor == 1 or not selected:
                self.owned.pop(key, None)
            else:
                self.owned[key] = row
        if factor == 1:
            self.ignored.clear()
        self._persist(self.owned)
        self.error = ""

    @staticmethod
    def _matches(volume, row):
        return any(abs(volume - row[key]) < 0.011 for key in ("applied", "previous") if key in row)

    def tick(self, gaming=False, force=False):
        if not force and time.monotonic() - self.last_tick < 10:
            return
        with self.lock:
            self.last_tick = time.monotonic()
            cfg = self.settings().get("audio_focus", {})
            self.reasons.pop("profile", None)
            mode = (
                ("call" if self.call_active else "game" if gaming else "normal")
                if self.mode == "auto"
                else self.mode
            )
            if mode in ("game", "call"):
                self.reasons["profile"] = cfg.get(mode + "_percent", 35) / 100
            try:
                self._apply()
            except Exception:
                self.error = "Микшер недоступен; проверьте аудиосессии приложений"

    def set_mode(self, mode, gaming=False):
        if mode not in ("auto", "normal", "game", "call"):
            raise ValueError("Неизвестный профиль звука")
        with self.lock:
            restoring = mode == "normal" and bool(self.owned or self.manual_except)
            self.mode = mode
            if mode == "normal":
                self.manual_except = None
                self.reasons.pop("except", None)
            self.tick(gaming, True)
            if self.error:
                raise ValueError(self.error)
            if restoring:
                return "Обычная громкость восстановлена; ручные изменения сохранены."
            if not self.settings().get("audio_focus", {}).get("enabled") and not self.call_active:
                return "Включите автоматический микшер в «Программы и звук» и выберите приложения."
        return (
            "Профиль звука: "
            + {"auto": "автоматический", "normal": "обычный", "game": "игра", "call": "звонок"}[
                mode
            ]
        )

    @contextmanager
    def speech(self):
        cfg = self.settings().get("audio_focus", {})
        enabled = cfg.get("enabled") and cfg.get("duck_speech")
        try:
            if enabled:
                with self.lock:
                    self.reasons["speech"] = cfg.get("speech_percent", 25) / 100
                    try:
                        self._apply()
                    except Exception:
                        self.error = "Приглушение на время речи недоступно"
            yield
        finally:
            if enabled:
                with self.lock:
                    self.reasons.pop("speech", None)
                    try:
                        self._apply()
                    except Exception:
                        self.error = "Верните громкость через микшер Windows; автоматическое восстановление не удалось"

    def close(self):
        with self.lock:
            self.manual_except = None
            self.reasons.clear()
            try:
                self._apply()
            except Exception:
                pass  # Recovery ledger is retained for the next launch.
