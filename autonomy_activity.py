"""Conservative AFK permission. No screenshots, hooks, model inference or GPU polling."""

import asyncio
import ctypes
import os
import threading
import time
from functools import lru_cache

import psutil


@lru_cache(maxsize=1)
def _input_api():
    if os.name != "nt":
        raise OSError("Автоматический простой определяется только в Windows")
    from ctypes import wintypes as w

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetLastInputInfo.argtypes = [ctypes.POINTER(LastInput)]
    kernel.GetTickCount64.restype = ctypes.c_ulonglong
    return user, kernel, LastInput


def windows_input():
    user, kernel, LastInput = _input_api()
    item = LastInput(ctypes.sizeof(LastInput), 0)
    if not user.GetLastInputInfo(ctypes.byref(item)):
        raise ctypes.WinError(ctypes.get_last_error())
    idle = ((kernel.GetTickCount64() & 0xFFFFFFFF) - item.dwTime) & 0xFFFFFFFF
    return idle / 1000


class ControllerInput:
    def __init__(self):
        self.api = None
        self.loaded = False

    def active(self):
        if os.name != "nt":
            return False
        if not self.loaded:
            self.loaded = True
            for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
                try:
                    self.api = ctypes.WinDLL(name)
                    break
                except OSError:
                    pass
        if self.api is None:
            return False

        class Pad(ctypes.Structure):
            _fields_ = [
                ("buttons", ctypes.c_ushort),
                ("left", ctypes.c_ubyte),
                ("right", ctypes.c_ubyte),
                ("lx", ctypes.c_short),
                ("ly", ctypes.c_short),
                ("rx", ctypes.c_short),
                ("ry", ctypes.c_short),
            ]

        class State(ctypes.Structure):
            _fields_ = [("packet", ctypes.c_uint32), ("pad", Pad)]

        self.api.XInputGetState.argtypes = [ctypes.c_uint32, ctypes.POINTER(State)]
        for index in range(4):
            state = State()
            if self.api.XInputGetState(index, ctypes.byref(state)) == 0:
                pad = state.pad
                if (
                    pad.buttons
                    or pad.left > 30
                    or pad.right > 30
                    or any(abs(value) > 8000 for value in (pad.lx, pad.ly, pad.rx, pad.ry))
                ):
                    return True
        return False


def fullscreen_window():
    """Covers silent/unregistered video players too. Unknown fullscreen stays busy."""
    from ctypes import wintypes as w

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = w.HWND
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
    user.MonitorFromWindow.restype = w.HANDLE

    class Monitor(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("screen", w.RECT), ("work", w.RECT), ("flags", w.DWORD)]

    user.GetMonitorInfoW.argtypes = [w.HANDLE, ctypes.POINTER(Monitor)]
    hwnd = user.GetForegroundWindow()
    if not hwnd:
        return False
    name = ctypes.create_unicode_buffer(256)
    user.GetClassNameW(hwnd, name, len(name))
    if name.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    rect = w.RECT()
    monitor = Monitor()
    monitor.size = ctypes.sizeof(Monitor)
    if not user.GetWindowRect(hwnd, ctypes.byref(rect)) or not user.GetMonitorInfoW(
        user.MonitorFromWindow(hwnd, 2), ctypes.byref(monitor)
    ):
        raise OSError("Не удалось проверить активное окно")
    screen = monitor.screen
    return (
        rect.left <= screen.left
        and rect.top <= screen.top
        and rect.right >= screen.right
        and rect.bottom >= screen.bottom
    )


def audio_activity(own_pids):
    """Query render AND capture sessions, including silent calls and muted playback."""
    import comtypes
    from pycaw.pycaw import AudioUtilities
    from pycaw.api.audiopolicy import IAudioSessionManager2, IAudioSessionControl2

    comtypes.CoInitializeEx()
    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        for flow, reason in ((0, "воспроизведение звука"), (1, "звонок или запись микрофона")):
            devices = enumerator.EnumAudioEndpoints(flow, 1)
            for i in range(devices.GetCount()):
                device = devices.Item(i)
                manager = ctypes.cast(
                    device.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None),
                    ctypes.POINTER(IAudioSessionManager2),
                )
                sessions = manager.GetSessionEnumerator()
                for j in range(sessions.GetCount()):
                    session = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
                    if session.GetState() == 1 and session.GetProcessId() not in own_pids:
                        return reason
        return ""
    finally:
        comtypes.CoUninitialize()


async def media_activity():
    from media_control import _manager_type

    manager = await _manager_type().request_async()
    return any(s.get_playback_info().playback_status == 4 for s in manager.get_sessions())


def windows_busy(own_pids):
    if os.name != "nt":
        raise OSError("Проверка медиасессий доступна в Windows")
    if asyncio.run(asyncio.wait_for(media_activity(), timeout=3)):
        return "идёт просмотр или воспроизведение"
    reason = audio_activity(own_pids)
    if reason:
        return reason
    if fullscreen_window():
        return "открыто полноэкранное приложение"
    return ""


class ActivityGate:
    def __init__(self, runtime, input_probe=windows_input, busy_probe=windows_busy):
        self.runtime = runtime
        self.input_probe, self.busy_probe = input_probe, busy_probe
        self.controller = ControllerInput()
        self.lock = threading.RLock()
        self.last_interaction = time.monotonic()
        self.last_busy_check = 0.0
        self.busy_reason = "Проверка активности ещё не выполнена"
        self.busy_at = 0.0
        self._snapshot = {"allowed": False, "reason": self.busy_reason, "idle": 0}
        self._stop = threading.Event()
        self._thread = None
        self._threshold = 20 * 60

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="krest-afk")
        self._thread.start()

    def touch(self):
        with self.lock:
            self.last_interaction = time.monotonic()
            self._snapshot = {
                "allowed": False,
                "reason": "Вы используете компьютер",
                "idle": 0,
                "checked": time.monotonic(),
            }

    def sample(self):
        now = time.monotonic()
        cfg = self.runtime.settings()
        options = cfg.get("autonomy", {})
        threshold = options.get("idle_minutes", 20) * 60
        self._threshold = threshold
        reason = ""
        idle = 0
        try:
            idle = min(self.input_probe(), max(0, now - self.last_interaction))
            # Recent keyboard/mouse input already resets the idle clock. Once
            # quiet, keep observing the controller so a brief press before the
            # AFK threshold also postpones work for a full idle interval.
            if idle >= 1 and self.controller.active():
                self.touch()
                idle = 0
            if cfg.get("safe_mode") or not options.get("enabled", True):
                reason = "Дежурство выключено"
            elif idle < threshold:
                reason = f"Ожидание простоя: {int(idle)} из {threshold} с"
            else:
                self.runtime.performance.refresh_activity()
                if self.runtime.performance.game:
                    reason = "Запущена игра: " + self.runtime.performance.game
                elif psutil.cpu_percent(interval=None) > 70:
                    reason = "Компьютер занят: высокая нагрузка на процессор"
                elif (
                    psutil.virtual_memory().available
                    < options.get("min_free_memory_mb", 1024) * 1024**2
                ):
                    reason = "Недостаточно свободной памяти"
                elif now - self.last_busy_check >= 3:
                    # All potentially slow COM/WinRT calls run on this worker,
                    # never on Tk, voice or the scheduler. Stale data fails closed.
                    self.last_busy_check = now
                    self.busy_at = 0
                    self.busy_reason = self.busy_probe(set(self.runtime.performance.processes))
                    self.busy_at = time.monotonic()
                if not reason:
                    reason = (
                        self.busy_reason
                        if now - self.busy_at < 8
                        else "Ожидаю проверку просмотра и звонков"
                    )
        except Exception as exc:
            reason = "Не удалось подтвердить простой: " + str(exc)[:180]
        with self.lock:
            # Input may have arrived while a slow media query was in flight.
            if time.monotonic() - self.last_interaction < threshold and not reason:
                reason = "Вы используете компьютер"
            self._snapshot = {
                "allowed": not bool(reason),
                "reason": reason or "Компьютер свободен",
                "idle": int(idle),
                "checked": time.monotonic(),
            }
            return dict(self._snapshot)

    def snapshot(self):
        with self.lock:
            result = dict(self._snapshot)
        if time.monotonic() - result.get("checked", 0) > 5:
            result.update(allowed=False, reason="Ожидаю свежую проверку активности")
        return result

    def permitted(self):
        if not self.snapshot()["allowed"]:
            return False
        try:
            # Cheap native call before each file/network chunk: no half-second
            # blind window when the user comes back during a large copy.
            return self.input_probe() >= self._threshold
        except Exception:
            return False

    def _run(self):
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(0.5)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
