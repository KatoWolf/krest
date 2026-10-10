"""Explicitly saved favourites and bounded, session-only input history."""

from storage import read_json


import json
from pathlib import Path
import shutil
import time

from storage import atomic_write_json


class CommandBook:
    def __init__(self, path):
        self.path = Path(path)
        self.favorites = []
        self.history = []
        self.position = 0
        self.draft = ""
        self.load_error = ""
        if self.path.exists():
            try:
                values = read_json(self.path)
                if not isinstance(values, list) or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in values
                ):
                    raise ValueError("Некорректный список избранных команд")
                self.favorites = list(dict.fromkeys(values))[:50]
            except (OSError, ValueError) as exc:
                self.load_error = str(exc)

    def _save(self, values):
        if self.load_error and self.path.exists():
            shutil.copy2(
                self.path, self.path.with_name(self.path.name + f".broken-{time.time_ns()}")
            )
        atomic_write_json(self.path, values)
        self.favorites = values
        self.load_error = ""

    def add(self, command):
        command = command.strip()
        if not command or len(command) > 2000:
            raise ValueError("Введите команду длиной от 1 до 2000 символов")
        if command in self.favorites:
            return
        if len(self.favorites) >= 50:
            raise ValueError("В избранном уже 50 команд. Удалите ненужные.")
        self._save([*self.favorites, command])

    def remove(self, command):
        self._save([v for v in self.favorites if v != command])

    def remember(self, command):
        if not self.history or self.history[-1] != command:
            self.history.append(command)
            self.history = self.history[-100:]
        self.position = len(self.history)
        self.draft = ""

    def navigate(self, offset, current):
        if self.position == len(self.history):
            self.draft = current
        self.position = max(0, min(len(self.history), self.position + offset))
        return self.history[self.position] if self.position < len(self.history) else self.draft

    def clear_history(self):
        self.history.clear()
        self.position = 0
        self.draft = ""
