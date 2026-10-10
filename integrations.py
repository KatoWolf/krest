"""Manage only child services owned by this panel, with bounded redacted logs."""

from pathlib import Path
import os
import re
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
import logging

BASE_DIR = Path(__file__).resolve().parent


def redact(text, settings):
    values = settings.get("api_keys", {})
    for key in (
        "krest_bot_token",
        "discord_token",
        "telegram_api_hash",
        "steam_api_key",
    ):
        value = os.getenv(
            {
                "krest_bot_token": "KREST_BOT_TOKEN",
                "discord_token": "DISCORD_TOKEN",
            }.get(key, ""),
            "",
        ) or values.get(key)
        if value:
            text = text.replace(str(value), "[скрыто]")
    return re.sub(r"bot\d{5,}:[A-Za-z0-9_-]+", "bot[скрыто]", text)


class IntegrationManager:
    MODULES = {
        "tg_user": ("tg_bridge.py", "Telegram User"),
        "tgt": ("krest_bot.py", "Telegram Terminal"),
        "discord": ("ds_session.py", "Discord Bot"),
    }

    def __init__(self, app, base_dir=BASE_DIR):
        self.app = app
        self.base_dir = Path(base_dir)
        self.processes, self.errors, self.started_at, self.log_handles = {}, {}, {}, {}
        self._readers = {}
        self._lock = threading.RLock()

    def path(self, filename):
        return str(self.base_dir / filename)

    def exists(self, filename):
        return Path(self.path(filename)).is_file()

    def _module_status(self, filename):
        if not self.exists(filename):
            return "Не установлен"
        proc = self.processes.get(filename)
        if proc and proc.poll() is None:
            return "Запущен"
        return "Ошибка запуска" if filename in self.errors else "Не запущен"

    def refresh(self):
        if self._lock.acquire(blocking=False):
            try:
                for filename, proc in list(self.processes.items()):
                    code = proc.poll()
                    if code is not None:
                        if code:
                            self.errors[filename] = (
                                f"Процесс завершился с кодом {code}; подробности в журнале"
                            )
                        self.processes.pop(filename, None)
            finally:
                self._lock.release()
        return {key: self._module_status(filename) for key, (filename, _) in self.MODULES.items()}

    def _capture(self, filename, proc, settings):
        path = self.base_dir / "logs" / (filename + ".log")
        handler = RotatingFileHandler(path, maxBytes=512_000, backupCount=2, encoding="utf-8")
        try:
            for line in proc.stdout:
                record = logging.LogRecord(
                    filename,
                    logging.INFO,
                    "",
                    0,
                    redact(line.rstrip(), settings),
                    (),
                    None,
                )
                handler.emit(record)
        finally:
            proc.stdout.close()
            handler.close()

    def start(self, filename):
        with self._lock:
            if not self.exists(filename):
                raise FileNotFoundError(filename)
            proc = self.processes.get(filename)
            if proc and proc.poll() is None:
                return "Уже запущен"
            from diagnostics import validate_module

            settings = self.app.config_mgr.snapshot()
            validate_module(filename, settings)
            (self.base_dir / "logs").mkdir(exist_ok=True)
            env = dict(
                os.environ,
                PYTHONIOENCODING="utf-8",
                KREST_CONFIG_PATH=str(self.app.config_mgr.filepath),
            )
            proc = subprocess.Popen(
                [sys.executable, "-u", self.path(filename)],
                cwd=self.base_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            self.processes[filename] = proc
            self.started_at[filename] = time.time()
            self.errors.pop(filename, None)
            reader = threading.Thread(
                target=self._capture, args=(filename, proc, settings), daemon=True
            )
            self._readers[filename] = reader
            reader.start()
            return "Запущен"

    def stop(self, filename):
        with self._lock:
            proc = self.processes.pop(filename, None)
            if proc is None:
                return "Не запущен"
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            reader = self._readers.pop(filename, None)
            if reader:
                reader.join(timeout=1)
            return "Остановлен"

    def restart(self, filename):
        with self._lock:
            self.stop(filename)
            return self.start(filename)

    def stop_all(self):
        for filename in list(self.processes):
            self.stop(filename)

    def log_tail(self, filename, limit=6000):
        path = self.base_dir / "logs" / (filename + ".log")
        if not path.exists():
            return "Журнал пока пуст."
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return redact(
                stream.read().decode("utf-8", errors="replace"),
                self.app.config_mgr.snapshot(),
            )
