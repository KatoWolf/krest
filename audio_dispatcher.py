"""Event-sized audio profiles; idle when disabled, no inference or microphone recording."""

import os
import threading
import time

PROFILE_NAMES = {"normal": "Обычный", "game": "Игра", "call": "Звонок", "headphones": "Наушники"}


def native_snapshot():
    import ctypes
    import comtypes
    from pycaw.pycaw import AudioUtilities
    from pycaw.api.audiopolicy import IAudioSessionManager2, IAudioSessionControl2
    import psutil
    from windows_audio import focus_sessions

    enumerator = AudioUtilities.GetDeviceEnumerator()
    devices = [
        {"id": d.id, "name": d.FriendlyName}
        for d in AudioUtilities.GetAllDevices()
        if getattr(d.state, "value", d.state) == 1
    ]
    call = False
    capture = enumerator.EnumAudioEndpoints(1, 1)
    for i in range(capture.GetCount()):
        manager = ctypes.cast(
            capture.Item(i).Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None),
            ctypes.POINTER(IAudioSessionManager2),
        )
        sessions = manager.GetSessionEnumerator()
        for j in range(sessions.GetCount()):
            session = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
            if session.GetState() != 1:
                continue
            try:
                name = psutil.Process(session.GetProcessId()).name().casefold()
            except psutil.Error:
                continue
            if name in {
                "discord.exe",
                "discordcanary.exe",
                "discordptb.exe",
                "zoom.exe",
                "ms-teams.exe",
                "teams.exe",
                "skype.exe",
                "telegram.exe",
            }:
                call = True
    defaults = {}
    for flow, key in ((0, "output"), (1, "input")):
        try:
            defaults[key] = enumerator.GetDefaultAudioEndpoint(flow, 0).GetId()
        except Exception:
            defaults[key] = ""
    return {
        "devices": devices,
        "sessions": focus_sessions() if defaults.get("output") else [],
        "call": call,
        **defaults,
    }


class AudioDispatcher:
    def __init__(self, runtime, store, backend=None):
        self.runtime, self.store, self.backend = runtime, store, backend
        self.lock = threading.RLock()
        self.last_tick = 0
        self.signature = None
        self.last_devices = None
        self.seen_sessions = set()
        self.status = "Звуковой диспетчер выключен"
        self.profile = "normal"
        self.call_streak = 0
        self.no_call_streak = 0

    def port(self, action, data=None):
        if self.backend:
            return self.backend(action, data or {})
        if os.name != "nt":
            raise ValueError("Звуковой диспетчер доступен в Windows")
        from desktop_transport import request

        result = request(
            {"operation": "audio_dispatch", "action": action, "data": data or {}}, timeout=6
        )
        if result.get("error"):
            raise ValueError(result["error"])
        return result["result"]

    def capture(self, profile):
        if profile not in PROFILE_NAMES:
            raise ValueError("Неизвестный звуковой профиль")
        with self.lock, self.runtime.audio_focus.lock:
            if not self.runtime.audio_focus.recovered:
                self.runtime.audio_focus._apply()
            current = self.port("snapshot")
            if not current["devices"]:
                raise ValueError("Windows не обнаружила доступных устройств звука")
            names = {}
            for row in current["sessions"]:
                name = row["name"].casefold()
                if name in {"python.exe", "pythonw.exe", "krest.exe"}:
                    continue
                names.setdefault(name, []).append(row)
            levels = {}
            for name, rows in names.items():
                if len(rows) == 1:
                    row = rows[0]
                    levels[name] = self.runtime.audio_focus.owned.get(row["id"], {}).get(
                        "before", row["volume"]
                    )
            self.store.put(
                "audio_profiles",
                {
                    "id": profile,
                    "levels": levels,
                    "output": current.get("output", ""),
                    "input": current.get("input", ""),
                    "devices": current["devices"],
                },
                limit=4,
            )
            self.signature = None
            return (
                "Запомнила звук: "
                + PROFILE_NAMES[profile]
                + f". Приложений с однозначной громкостью: {len(levels)}."
            )

    def apply(self, profile, current=None, *, only_new=False):
        row = self.store.get("audio_profiles", profile)
        if not row:
            self.status = PROFILE_NAMES[profile] + ": сохранённого профиля пока нет"
            return self.status
        current = current or self.port("snapshot")
        focus = self.runtime.audio_focus
        with focus.lock:
            if not focus.recovered:
                focus._apply()
            groups = {}
            for session in current["sessions"]:
                groups.setdefault(session["name"].casefold(), []).append(session)
            changes = [
                {"id": sessions[0]["id"], "volume": volume}
                for name, volume in row["levels"].items()
                if len(sessions := groups.get(name, [])) == 1
                and (not only_new or sessions[0]["id"] not in self.seen_sessions)
                and abs(
                    focus.owned.get(sessions[0]["id"], {}).get("before", sessions[0]["volume"])
                    - volume
                )
                > 0.005
            ]
            available = {d["id"] for d in current["devices"]}
            cfg = self.runtime.settings().get("sound_dispatcher", {})
            devices = [
                row[k]
                for k in ("output", "input")
                if not only_new
                and cfg.get("restore_devices", True)
                and row.get(k) in available
                and current.get(k) != row[k]
            ]
            if changes or devices:
                focus.release_sessions(None if devices else [s["id"] for s in changes])
                try:
                    self.port("apply", {"sessions": changes, "devices": devices})
                finally:
                    focus._apply()
        self.seen_sessions = {s["id"] for s in current["sessions"]}
        self.status = "Звук: " + PROFILE_NAMES[profile]
        return self.status

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last_tick < 5:
            return
        self.last_tick = now
        cfg = self.runtime.settings().get("sound_dispatcher", {})
        focus = self.runtime.audio_focus
        if not cfg.get("enabled"):
            focus.call_active = False
            self.status = "Звуковой диспетчер выключен"
            self.signature = None
            return
        with self.lock:
            try:
                current = self.port("snapshot")
                was_call = focus.call_active
                self.call_streak = self.call_streak + 1 if current["call"] else 0
                self.no_call_streak = self.no_call_streak + 1 if not current["call"] else 0
                # End-of-call hysteresis prevents volume pumping between capture gaps.
                if cfg.get("auto_calls", True) and self.call_streak:
                    focus.call_active = True
                elif not cfg.get("auto_calls", True) or self.no_call_streak >= 2:
                    focus.call_active = False
                headphones = {
                    d["id"]: d["name"]
                    for d in current["devices"]
                    if any(
                        w in d["name"].casefold()
                        for w in ("наушник", "головн", "headphone", "headset", "airpods")
                    )
                }
                connected = bool(headphones.keys() - (self.last_devices or {}).keys())
                if self.last_devices is not None and hasattr(self.runtime, "automation"):
                    for ident in headphones.keys() - self.last_devices.keys():
                        self.runtime.automation.emit(
                            "headphones_connected", {"device": headphones[ident]}
                        )
                    for ident in self.last_devices.keys() - headphones.keys():
                        self.runtime.automation.emit(
                            "headphones_disconnected", {"device": self.last_devices[ident]}
                        )
                self.last_devices = headphones
                self.profile = (
                    "call"
                    if focus.call_active
                    else "game"
                    if self.runtime.performance.game
                    else "headphones"
                    if (connected or current.get("output") in headphones)
                    else "normal"
                )
                signature = (self.profile, tuple(sorted(d["id"] for d in current["devices"])))
                if focus.manual_except is not None:
                    self.status = "Остальные приложения приглушены; возврат: «верни обычный звук»"
                    continue_profile = False
                else:
                    continue_profile = True
                if continue_profile and signature != self.signature:
                    self.apply(self.profile, current)
                    self.signature = signature
                elif (
                    continue_profile
                    and self.store.get("audio_profiles", self.profile)
                    and any(s["id"] not in self.seen_sessions for s in current["sessions"])
                ):
                    self.apply(self.profile, current, only_new=True)
                focus.tick(bool(self.runtime.performance.game), force=was_call != focus.call_active)
            except Exception as exc:
                self.status = "Звуковой диспетчер: " + str(exc)[:200]


def native_apply(data):
    from windows_audio import focus_set_many, set_default_device

    for device in data.get("devices", []):
        set_default_device(device)
    focus_set_many(data.get("sessions", []))
    return {"confirmed": True}
