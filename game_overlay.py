"""Small out-of-process, click-through Windows game indicator; never hooks a game."""

from app_paths import app_command

from ui_fonts import UI_FONT, load_fonts

import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

COLORS = {
    "idle": "#75bde8",
    "learning": "#69d2d0",
    "listening": "#5ce2ad",
    "busy": "#ffc56b",
    "speaking": "#c2a0ff",
    "notice": "#ff9e60",
    "error": "#fa6379",
    "addressed": "#65efbc",
    "recognizing": "#7cc8ff",
    "queued": "#e2c488",
    "executing": "#ffc56b",
    "success": "#73ec9b",
}
LABELS = {
    "idle": "Наблюдаю",
    "learning": "Запоминаю",
    "listening": "Слушаю",
    "busy": "Выполняю",
    "speaking": "Отвечаю",
    "notice": "Есть новость",
    "error": "Нужна проверка",
    "addressed": "Услышала",
    "recognizing": "Распознаю",
    "queued": "В очереди",
    "executing": "Выполняю",
    "success": "Готово",
}
TRANSPARENT = "#010203"


def indicator_state(data, now=None):
    now = time.time() if now is None else now
    game, voice = data.get("game", {}), data.get("voice", {})
    feedback = data.get("feedback", {})
    if feedback.get("until", 0) > now and feedback.get("stage") in COLORS:
        return feedback["stage"]
    if game.get("route", {}).get("mode") in ("recording", "playing"):
        return "busy"
    if voice.get("speaking"):
        return "speaking"
    if game.get("vision", {}).get("busy") or game.get("busy") or data.get("command"):
        return "busy"
    if (
        data.get("listening")
        and voice.get("connected")
        and voice.get("rms", 0) >= voice.get("threshold", 200)
    ):
        return "listening"
    if game.get("error") or data.get("listening") and voice.get("mic_error"):
        return "error"
    if 0 < now - game.get("notice_at", 0) < 6:
        return "notice"
    if game.get("study", {}).get("active"):
        return "learning"
    return "idle"


def passthrough(window):
    """No input/focus interception. Called only for our own overlay windows."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    user = ctypes.windll.user32
    user.GetParent.argtypes = [wintypes.HWND]
    user.GetParent.restype = wintypes.HWND
    hwnd = user.GetParent(window.winfo_id()) or window.winfo_id()
    get = user.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.GetWindowLongW
    put = user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.SetWindowLongW
    get.argtypes = [wintypes.HWND, ctypes.c_int]
    get.restype = ctypes.c_ssize_t
    put.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    put.restype = ctypes.c_ssize_t
    put(hwnd, -20, get(hwnd, -20) | 0x20 | 0x80 | 0x80000 | 0x08000000)
    # Keep our own hint text out of screen OCR (Windows 10 2004+).
    user.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.SetWindowDisplayAffinity(hwnd, 0x11)
    user.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x20)


def move_overlay(window, x, y, width, height):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        user = ctypes.windll.user32
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        hwnd = user.GetParent(window.winfo_id()) or window.winfo_id()
        user.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user.SetWindowPos(hwnd, -1, int(x), int(y), int(width), int(height), 0x10)
    else:
        window.geometry(f"{int(width)}x{int(height)}{int(x):+d}{int(y):+d}")


def draw_cross(canvas, size, state="idle", level=0, phase=0, reduced=False, labels=True):
    canvas.delete("all")
    width = max(size + 14, 124 if labels else 0)
    cx, cy = width / 2, size / 2 + 5
    color = COLORS[state]
    pulse = 0 if reduced else (math.sin(phase) + 1) / 2
    amplitude = 0 if reduced else min(1, max(0, level) / 3000)
    radius = size * (0.37 + pulse * 0.035 + amplitude * 0.05)
    canvas.create_oval(
        cx - radius, cy - radius, cx + radius, cy + radius, outline=color, width=1 + amplitude * 2
    )
    # Flared arms and a long lower stem, recognisable at 32–96 px.
    points = [
        (-0.09, -0.33),
        (0.09, -0.33),
        (0.055, -0.12),
        (0.25, -0.16),
        (0.25, 0.035),
        (0.06, 0),
        (0.105, 0.34),
        (-0.105, 0.34),
        (-0.06, 0),
        (-0.25, 0.035),
        (-0.25, -0.16),
        (-0.055, -0.12),
    ]
    coords = [value for x, y in points for value in (cx + x * size, cy + y * size)]
    canvas.create_polygon(*coords, fill="#191527", outline=color, width=2)
    r = size * (0.055 + amplitude * 0.018)
    canvas.create_oval(
        cx - r, cy - size * 0.07 - r, cx + r, cy - size * 0.07 + r, fill=color, outline=""
    )
    if labels:
        canvas.create_text(cx, size + 12, text=LABELS[state], fill=color, font=(UI_FONT, 8, "bold"))


def draw_panel(canvas, panel, width, top):
    """A bounded, click-through card; no scrolling or interactive controls."""
    canvas.create_rectangle(
        1, top, width - 1, top + 258, fill="#191527", outline="#655482", width=1
    )
    canvas.create_text(
        14,
        top + 17,
        anchor="w",
        text=panel.get("title", "Сводка"),
        fill="#e5d9ff",
        font=(UI_FONT, 12, "bold"),
    )
    canvas.create_text(
        14,
        top + 38,
        anchor="w",
        text="Сохранённые данные · проверьте актуальность"
        if panel.get("stale")
        else "«Скрой сводку» — свернуть",
        fill="#b7a9cc",
        font=(UI_FONT, 8),
    )
    for i, line in enumerate(panel.get("lines", [])[:6]):
        # Cap each row to two short lines; full details remain in the menu.
        text = str(line)[:88]
        if len(str(line)) > 88:
            text += "…"
        canvas.create_text(
            14,
            top + 57 + i * 32,
            anchor="nw",
            text=text,
            width=width - 28,
            fill="#f2edf8",
            font=(UI_FONT, 9),
        )


class OverlayManager:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.process = None
        self.last_try = 0
        self.error = ""

    def tick(self, enabled):
        if os.name != "nt":
            return
        if not enabled:
            self.close()
            return
        if self.process and self.process.poll() is None:
            return
        if time.monotonic() - self.last_try < 15:
            return
        self.last_try = time.monotonic()
        try:
            self.process = subprocess.Popen(
                app_command(
                    "--game-overlay",
                    "--config",
                    str(self.config_path),
                ),
                cwd=Path(__file__).parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.error = ""
        except OSError as exc:
            self.error = str(exc)

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass
        self.process = None


def run_overlay(config_path):
    from kernel_client import KernelClient
    from process_lock import ProcessLock
    import tkinter as tk
    from game_capture import foreground_game_rect

    lock = ProcessLock(Path(config_path).parent / ".overlay.lock")
    if not lock.acquire():
        return 0
    load_fonts()
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.configure(bg=TRANSPARENT)
    root.attributes("-topmost", True)
    if os.name == "nt":
        root.attributes("-transparentcolor", TRANSPARENT)
    canvas = tk.Canvas(root, bg=TRANSPARENT, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    stopped = threading.Event()
    updates = queue.Queue(maxsize=1)
    client = KernelClient(config_path)
    data = {}
    failures = 0

    def poll():
        while not stopped.is_set():
            try:
                value = client.call("overlay")
            except Exception:
                value = None
            try:
                updates.get_nowait()
            except queue.Empty:
                pass
            updates.put_nowait(value)
            stopped.wait(0.25)

    def frame():
        nonlocal data, failures
        try:
            value = updates.get_nowait()
            if value is None:
                failures += 1
            else:
                data = value
                failures = 0
        except queue.Empty:
            pass
        cfg = data.get("settings", {})
        game = data.get("game", {})
        if failures >= 3 or data and (not game.get("running") or not cfg.get("overlay_enabled")):
            stopped.set()
            root.destroy()
            return
        try:
            rect = foreground_game_rect()
        except Exception:
            rect = None
        visible = (
            data
            and not failures
            and (rect is not None or not cfg.get("overlay_foreground_only", True))
        )
        if visible:
            size = cfg.get("overlay_size", 52)
            labels = cfg.get("overlay_labels", True)
            width = max(size + 14, 124 if labels else 0)
            cross_width = width
            height = size + (26 if labels else 12)
            cross_height = height
            panel = game.get("panel", {})
            route = game.get("route", {})
            study = game.get("study", {})
            if study.get("active") and panel.get("kind") == "study":
                seconds = study.get("seconds", 0)
                panel = {
                    "title": "Аполлон · запись показа",
                    "lines": [
                        "Валькирия Прайм · цель: Акси",
                        study.get("message", "Управление у вас"),
                        f"{seconds // 60}:{seconds % 60:02d} · кадров: {study.get('frames', 0)} · меток: {study.get('labels', 0)}",
                        study.get("last_label") or "«Варфрейм метка ключ» — отметить событие",
                        "Добыча не проверена · автопилот не обучен",
                        "«Закончи показ варфрейм» — сохранить",
                    ],
                    "until": panel.get("until", 0),
                }
            if route.get("mode") in ("recording", "playing"):
                panel = {
                    "title": "Запись маршрута"
                    if route["mode"] == "recording"
                    else "Повтор маршрута",
                    "lines": [route["message"], "Esc / F8 / «верни управление» — остановить"],
                    "until": time.time() + 2,
                }
            if panel and panel.get("until", 0) > time.time():
                width = 390
                height += 262
            else:
                panel = {}
            left, top, right, bottom = rect or (
                0,
                0,
                root.winfo_screenwidth(),
                root.winfo_screenheight(),
            )
            corner = cfg.get("overlay_corner", "top-right")
            width = min(width, max(124, right - left - 36))
            x = left + 18 if corner.endswith("left") else right - width - 18
            y = top + 24 if corner.startswith("top") else bottom - height - 24
            y = max(top, y)
            if not root.winfo_viewable():
                root.update_idletasks()
                passthrough(root)
                root.deiconify()
            move_overlay(root, x, y, width, height)
            draw_cross(
                canvas,
                size,
                indicator_state(data),
                data.get("voice", {}).get("rms", 0),
                time.monotonic() * 3,
                data.get("reduced", False),
                labels,
            )
            if panel and corner.endswith("right"):
                canvas.move("all", width - cross_width, 0)
            if panel:
                draw_panel(canvas, panel, width, cross_height)
        else:
            root.withdraw()
        root.after(125 if data.get("reduced") or not visible else 65, frame)

    threading.Thread(target=poll, daemon=True, name="krest-overlay-status").start()
    root.after(30, frame)
    try:
        root.mainloop()
    finally:
        stopped.set()
        lock.close()
    return 0
