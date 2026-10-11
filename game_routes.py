"""Opt-in, bounded keyboard route recorder. Not vision-trained combat automation."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading
import time

from storage import atomic_write_json, read_json

KEYS = {"w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44, "space": 0x20, "shift": 0x10, "ctrl": 0x11}
SCANS = {"w": 0x11, "a": 0x1E, "s": 0x1F, "d": 0x20, "space": 0x39, "shift": 0x2A, "ctrl": 0x1D}


def validate_route(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Неизвестный формат маршрута")
    duration = value.get("duration")
    events = value.get("events")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (float, int))
        or not 0.5 <= duration <= 60
    ):
        raise ValueError("Маршрут должен длиться от 0,5 до 60 секунд")
    if not isinstance(events, list) or not 1 <= len(events) <= 2400:
        raise ValueError("Некорректное число событий маршрута")
    previous = -1
    for event in events:
        if not isinstance(event, list) or len(event) != 2:
            raise ValueError("Некорректное событие маршрута")
        at, keys = event
        if (
            isinstance(at, bool)
            or not isinstance(at, (float, int))
            or not previous <= at <= duration
        ):
            raise ValueError("Некорректное время маршрута")
        if (
            at < 0
            or not isinstance(keys, list)
            or len(keys) > len(KEYS)
            or any(key not in KEYS for key in keys)
        ):
            raise ValueError("Маршрут содержит неподдерживаемые клавиши")
        previous = at
    return value


class WindowsInput:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Маршруты доступны в Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short

    def foreground(self):
        from game_capture import foreground_game_rect

        foreground_game_rect()

    def pressed(self):
        return {key for key, code in KEYS.items() if self.user.GetAsyncKeyState(code) & 0x8000}

    def panic(self):
        return bool(
            self.user.GetAsyncKeyState(0x1B) & 0x8000 or self.user.GetAsyncKeyState(0x77) & 0x8000
        )

    def cursor(self):
        point = wintypes.POINT()
        if not self.user.GetCursorPos(ctypes.byref(point)):
            raise OSError("Не удалось проверить положение мыши")
        return point.x, point.y

    def send(self, key, down):
        class Keyboard(ctypes.Structure):
            _fields_ = [
                ("vk", wintypes.WORD),
                ("scan", wintypes.WORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t),
            ]

        class Mouse(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("data", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t),
            ]

        class Payload(ctypes.Union):
            _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("payload", Payload)]

        event = Input(
            type=1,
            payload=Payload(keyboard=Keyboard(0, SCANS[key], 0x8 | (0 if down else 0x2), 0, 0)),
        )
        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        if self.user.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise OSError("Windows не приняла нажатие; маршрут остановлен")


class RouteController:
    def __init__(self, root, allowed, port_factory=WindowsInput):
        self.path = Path(root) / "warframe_route.json"
        self.allowed = allowed
        self.port_factory = port_factory
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.mode = "idle"
        self.message = "Маршрут не запущен"
        self.confirm_until = 0
        self.pending_route = None
        self.closed = False

    def snapshot(self):
        with self.lock:
            return {
                "mode": self.mode,
                "message": self.message,
                "confirmation_pending": time.monotonic() < self.confirm_until,
            }

    def _guard(self, port):
        if self.closed or self.stop.is_set() or not self.allowed():
            raise ValueError("Маршрут остановлен")
        port.foreground()
        if port.panic():
            raise ValueError("Остановка: Esc / F8")

    def _start(self, mode, target, *args):
        with self.lock:
            if self.closed or not self.allowed():
                raise ValueError("Разрешите экспериментальные маршруты в настройках Warframe")
            if self.thread and self.thread.is_alive():
                raise ValueError("Сначала остановите текущую запись или маршрут")
            port = self.port_factory()
            port.foreground()
            self.stop.clear()
            self.mode = mode
            self.confirm_until = 0
            self.message = (
                "Записываю клавиши движения"
                if mode == "recording"
                else "Маршрут выполняется · Esc / F8 — остановить"
            )
            self.thread = threading.Thread(
                target=target, args=(port, *args), daemon=True, name="krest-route"
            )
            self.thread.start()

    def record(self):
        self._start("recording", self._record)
        return "Записываю до минуты: движение, прыжок, бег и приседание. F8 или «сохрани маршрут» завершит запись. Мышь и атаки не записываются."

    def _record(self, port):
        started = time.monotonic()
        events = []
        previous = None
        try:
            while time.monotonic() - started < 60:
                if self.stop.is_set() or port.panic():
                    break
                self._guard(port)
                keys = sorted(port.pressed())
                if keys != previous:
                    events.append([min(60, time.monotonic() - started), keys])
                    previous = keys
                if self.stop.wait(0.05):
                    break
            duration = min(60, time.monotonic() - started)
            route = validate_route({"version": 1, "duration": duration, "events": events})
            if not any(keys for _, keys in events):
                raise ValueError("Движений не было; прежний маршрут сохранён")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, route)
            self.message = f"Маршрут сохранён: {duration:.1f} сек. Повтор начинается из текущего положения персонажа."
        except Exception as exc:
            self.message = str(exc)
        finally:
            self.mode = "idle"

    def request(self):
        with self.lock:
            if not self.allowed():
                return "Маршруты выключены. Я могу показать сводку и сохранить заметку, пока вы отошли."
            if self.thread and self.thread.is_alive():
                raise ValueError("Сначала остановите текущий маршрут")
            if not self.path.exists() or self.path.stat().st_size > 200000:
                raise ValueError(
                    "Сначала запишите короткий маршрут командой «запиши маршрут варфрейм»"
                )
            self.pending_route = validate_route(read_json(self.path))
            self.confirm_until = time.monotonic() + 20
            return "Повторить записанный маршрут один раз? Это не автономный бой. В течение двадцати секунд скажите «подтверждаю маршрут». Esc, F8 или движение мыши сразу остановят повтор."

    def confirm(self):
        with self.lock:
            if time.monotonic() >= self.confirm_until or self.pending_route is None:
                raise ValueError("Нет актуального запроса. Скажите «я отойду от игры»")
            route = self.pending_route
            self.pending_route = None
            self.confirm_until = 0
            self._start("playing", self._play, route)
        return (
            "Повторяю маршрут один раз. Esc, F8, движение мыши или «верни управление» — остановка."
        )

    def _play(self, port, route):
        held = set()
        try:
            origin = port.cursor()
            started = time.monotonic()
            index = 0
            while time.monotonic() - started < route["duration"]:
                self._guard(port)
                cursor = port.cursor()
                if abs(cursor[0] - origin[0]) + abs(cursor[1] - origin[1]) > 8:
                    raise ValueError("Управление возвращено: движение мыши")
                elapsed = time.monotonic() - started
                while index < len(route["events"]) and route["events"][index][0] <= elapsed:
                    keys = set(route["events"][index][1])
                    for key in held - keys:
                        port.send(key, False)
                    for key in keys - held:
                        self._guard(port)
                        port.send(key, True)
                        held.add(key)
                    held = keys
                    index += 1
                if self.stop.wait(0.02):
                    break
            self.message = "Маршрут завершён. Управление у вас."
        except Exception as exc:
            self.message = str(exc)
        finally:
            for key in held:
                try:
                    port.send(key, False)
                except OSError:
                    self.message = "Проверьте клавиши движения: Windows не приняла отпускание"
            self.mode = "idle"

    def cancel(self):
        self.confirm_until = 0
        self.pending_route = None
        self.stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        return self.message if self.mode == "idle" else "Останавливаю маршрут"

    def close(self):
        self.closed = True
        self.cancel()
