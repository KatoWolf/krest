"""Explicit captures of the foreground Warframe window; never reads game memory."""

import os
from pathlib import Path
import time

GAME_PROCESSES = {"warframe.x64.exe", "warframe.exe"}


def foreground_game_rect():
    if os.name != "nt":
        raise ValueError("Захват окна игры доступен в Windows")
    import ctypes
    from ctypes import wintypes
    import psutil

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    handle = user.GetForegroundWindow()
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    if not pid.value or psutil.Process(pid.value).name().casefold() not in GAME_PROCESSES:
        raise ValueError("Сначала переключитесь на окно Warframe")
    rect = wintypes.RECT()
    if (
        not user.GetWindowRect(handle, ctypes.byref(rect))
        or rect.right <= rect.left
        or rect.bottom <= rect.top
    ):
        raise ValueError("Не удалось определить границы окна игры")
    return rect.left, rect.top, rect.right, rect.bottom


def save_screenshot(folder):
    from PIL import ImageGrab

    bounds = foreground_game_rect()
    root = Path(folder) / "captures"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (
        "warframe-" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns() % 1000000) + ".png"
    )
    with ImageGrab.grab(bbox=bounds, all_screens=True) as image:
        if foreground_game_rect() != bounds:
            raise ValueError("Окно игры изменилось; снимок не сохранён")
        image.save(path)
    return str(path)


def request_clip():
    foreground_game_rect()
    import pyautogui

    pyautogui.hotkey("win", "alt", "g")
    return "Отправлен запрос клипа Xbox Game Bar. Фоновая запись должна быть заранее включена в Windows; сохранение клипа Крест не подтверждает."
