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
import json

BASE_DIR = Path(__file__).resolve().parent


def redact(text, settings):
    code = settings.get("license", {}).get("code", "")
    if code:
        text = text.replace(code, "[код скрыт]")
    for service in settings.get("services", {}).values():
        if isinstance(service, dict) and service.get("token"):
            text = text.replace(str(service["token"]), "[скрыто]")
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
        "steam": ("steam_bot.py", "Steam"),
        "presence": ("discord_presence.py", "Discord — мой статус"),
    }

    def __init__(self, app, base_dir=BASE_DIR):
        self.app = app
        self.base_dir = Path(base_dir)
        from app_paths import compiled, data_dir

        self.log_dir = (data_dir() if compiled() else self.base_dir) / "logs"
        self.processes, self.errors, self.started_at, self.log_handles = {}, {}, {}, {}
        self._readers = {}
        self._lock = threading.RLock()
        self._health_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._operations = {}
        self._closed = False
        self.bridge = {}
        self.manifests = {}
        self.restart_history = {}
        self.restart_due = {}
        self._generation = {}
        self.resources = {}
        self._last_resources = 0
        self.MODULES = dict(type(self).MODULES)
        self.reload_registry()

    def reload_registry(self):
        updated = dict(type(self).MODULES)
        builtins = {filename for filename, title in updated.values()}
        for ident, cfg in self.app.config_mgr.snapshot().get("services", {}).items():
            if ident not in builtins:
                updated[ident] = (ident, cfg.get("title", ident))
        valid = {filename for filename, title in updated.values()}
        for filename in list(self.processes):
            if filename not in valid:
                self.stop(filename)
        self.MODULES = updated

    def path(self, filename):
        custom = self.app.config_mgr.snapshot().get("services", {}).get(filename, {}).get("path")
        return str(Path(custom).expanduser().resolve()) if custom else str(self.base_dir / filename)

    def is_builtin_release(self, filename):
        from app_paths import compiled
        from service_runner import BUILTIN_SERVICES

        custom = self.app.config_mgr.snapshot().get("services", {}).get(filename, {}).get("path")
        return compiled() and filename in BUILTIN_SERVICES and not custom

    def exists(self, filename):
        return self.is_builtin_release(filename) or Path(self.path(filename)).is_file()

    def _module_status(self, filename):
        if not self.exists(filename):
            return "Не установлен"
        proc = self.processes.get(filename)
        if proc and proc.poll() is None:
            return "Запущен"
        return "Ошибка запуска" if filename in self.errors else "Не запущен"

    def details(self, filename):
        """Read-only UI snapshot; never wait for a process being stopped."""
        proc = self.processes.get(filename)
        running = proc is not None and proc.poll() is None
        restart_due = self.restart_due.get(filename)
        return {
            "running": running,
            "installed": self.exists(filename),
            "operation": self._operations.get(filename),
            "restart_in": max(0, int(restart_due - time.monotonic()) + 1)
            if restart_due is not None
            else None,
            "pid": proc.pid if running else None,
            "uptime": max(0, int(time.time() - self.started_at.get(filename, time.time())))
            if running
            else 0,
            "error": self.errors.get(filename, ""),
            "bridge": self.bridge.get(filename, {}),
            "health": "error"
            if self.errors.get(filename)
            else "ready"
            if self.bridge.get(filename, {}).get("ready") and running
            else "starting"
            if running
            else "stopped",
            "resources": self.resources.get(filename, {}),
            "manifest": self.manifests.get(filename, {}),
            "restarts": len(self.restart_history.get(filename, [])),
        }

    def perform(self, filename, action):
        """Serialize manual actions without queuing duplicate reconnects."""
        if action not in ("start", "stop", "restart"):
            raise ValueError("Неизвестное действие сервиса")
        with self._operation_lock:
            if filename in self._operations:
                return "Операция уже выполняется"
            self._operations[filename] = action
        try:
            return getattr(self, action)(filename)
        except Exception as exc:
            self.errors[filename] = redact(str(exc), self.app.config_mgr.snapshot())
            raise
        finally:
            with self._operation_lock:
                self._operations.pop(filename, None)

    def send(self, filename, command):
        from licensing import require_premium

        require_premium(self.app.config_mgr.snapshot())
        with self._lock:
            proc = self.processes.get(filename)
            capabilities = self.bridge.get(filename, {}).get("capabilities", [])
            if proc is None or proc.poll() is not None or command not in capabilities:
                raise RuntimeError("Скрипт не запущен или не объявил поддержку команды " + command)
            proc.stdin.write(json.dumps({"command": command}) + "\n")
            proc.stdin.flush()

    def autostart(self):
        for filename, cfg in self.app.config_mgr.snapshot().get("services", {}).items():
            if cfg.get("autostart"):
                try:
                    self.start(filename)
                except Exception as exc:
                    self.errors[filename] = redact(str(exc), self.app.config_mgr.snapshot())

    def _event(self, filename, line, settings):
        from bridge_sdk import PREFIX

        if not line.startswith(PREFIX) or len(line) > 8192:
            return
        try:
            value = json.loads(line[len(PREFIX) :])
        except ValueError:
            return
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        message = redact(str(value.get("text", ""))[:2000], settings)
        permissions = self.manifests.get(filename, {}).get(
            "permissions", ["notifications", "refresh"]
        )
        self.bridge.setdefault(filename, {})["last_event"] = time.time()
        if kind == "ready":
            self.errors.pop(filename, None)
            caps = value.get("capabilities", [])
            self.bridge[filename] = {
                "text": message,
                "ready": True,
                "last_event": time.time(),
                "capabilities": [c for c in caps if c in ("refresh",) and c in permissions]
                if isinstance(caps, list)
                else [],
            }
        elif kind == "status":
            self.bridge.setdefault(filename, {})["text"] = message
        elif kind == "error":
            self.errors[filename] = message
            self.bridge[filename]["ready"] = False
            caps = value.get("capabilities")
            if isinstance(caps, list):
                self.bridge[filename]["capabilities"] = [
                    c for c in caps if c == "refresh" and c in permissions
                ]
        elif (
            kind == "notification"
            and "notifications" in permissions
            and hasattr(self.app, "runtime")
        ):
            priority = value.get("priority", "normal")
            if priority not in ("low", "normal", "high"):
                priority = "normal"
            self.app.runtime.notifications.put(message, source=filename, priority=priority)

    def refresh(self):
        if not self._health_lock.acquire(blocking=False):
            return {
                key: self._module_status(filename)
                for key, (filename, _) in list(self.MODULES.items())
            }
        try:
            return self._refresh()
        finally:
            self._health_lock.release()

    def _refresh(self):
        if self._lock.acquire(blocking=False):
            try:
                for filename, proc in list(self.processes.items()):
                    code = proc.poll()
                    if code is not None:
                        if code:
                            self.errors.setdefault(
                                filename,
                                f"Процесс завершился с кодом {code}; подробности в журнале",
                            )
                        self.processes.pop(filename, None)
                        if proc.stdin:
                            proc.stdin.close()
                        cfg = self.app.config_mgr.snapshot().get("services", {}).get(filename, {})
                        if code and cfg.get("auto_restart", False) and not self._closed:
                            now = time.monotonic()
                            history = [
                                v for v in self.restart_history.get(filename, []) if now - v < 600
                            ]
                            self.restart_history[filename] = history
                            if len(history) < 3:
                                self.restart_due[filename] = now + 5 * 3 ** len(history)
            finally:
                self._lock.release()
        now = time.monotonic()
        if self._lock.acquire(blocking=False):
            try:
                for filename, deadline in list(self.restart_due.items()):
                    if now < deadline or self._closed:
                        continue
                    self.restart_due.pop(filename, None)
                    generation = self._generation.get(filename, 0)
                    self.restart_history.setdefault(filename, []).append(now)
                    threading.Thread(
                        target=self._recover_service, args=(filename, generation), daemon=True
                    ).start()
            finally:
                self._lock.release()
        if now - self._last_resources >= 10:
            self._last_resources = now
            import psutil

            for filename, proc in list(self.processes.items()):
                try:
                    process = psutil.Process(proc.pid)
                    self.resources[filename] = {
                        "memory_mb": round(process.memory_info().rss / 1024**2, 1),
                        "threads": process.num_threads(),
                    }
                except psutil.Error:
                    self.resources.pop(filename, None)
        return {
            key: self._module_status(filename) for key, (filename, _) in list(self.MODULES.items())
        }

    def _recover_service(self, filename, generation):
        with self._lock:
            if (
                self._closed
                or self._generation.get(filename, 0) != generation
                or not self.app.config_mgr.snapshot()
                .get("services", {})
                .get(filename, {})
                .get("auto_restart", False)
            ):
                return
            try:
                self.start(filename)
            except Exception as exc:
                self.errors[filename] = redact(str(exc), self.app.config_mgr.snapshot())

    def _capture(self, filename, proc, settings):
        path = self.log_dir / (filename + ".log")
        handler = RotatingFileHandler(path, maxBytes=512_000, backupCount=2, encoding="utf-8")
        try:
            while True:
                line = proc.stdout.readline(8193)
                if not line:
                    break
                if len(line) > 8192:
                    while line and not line.endswith("\n"):
                        line = proc.stdout.readline(8193)
                    line = "[слишком длинная строка сервиса пропущена]\n"
                if self.processes.get(filename) is proc:
                    self._event(filename, line, settings)
                record = logging.LogRecord(
                    filename,
                    logging.INFO,
                    "",
                    0,
                    redact(redact(line.rstrip(), settings), self.app.config_mgr.snapshot()),
                    (),
                    None,
                )
                handler.emit(record)
        finally:
            proc.stdout.close()
            handler.close()

    def start(self, filename):
        from licensing import require_premium

        require_premium(self.app.config_mgr.snapshot())
        with self._lock:
            if self._closed:
                raise RuntimeError("Крест завершает работу; новые процессы не запускаются")
            if not self.exists(filename):
                raise FileNotFoundError(filename)
            proc = self.processes.get(filename)
            if proc and proc.poll() is None:
                return "Уже запущен"
            from diagnostics import validate_module

            settings = self.app.config_mgr.snapshot()
            custom = settings.get("services", {}).get(filename, {}).get("path")
            if not custom:
                validate_module(filename, settings)
            from plugin_manifest import inspect_plugin

            self.manifests[filename] = (
                {"api_version": 1, "permissions": ["notifications", "refresh"], "manifest": False}
                if self.is_builtin_release(filename)
                else inspect_plugin(self.path(filename))
            )
            if Path(self.path(filename)).suffix.lower() != ".py":
                raise ValueError("Выберите Python-скрипт .py")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            env = dict(
                os.environ,
                PYTHONIOENCODING="utf-8",
                KREST_CONFIG_PATH=str(self.app.config_mgr.filepath),
                KREST_DATA_DIR=str(self.app.config_mgr.filepath.parent),
                KREST_SERVICE_ID=filename,
                KREST_SERVICE_TOKEN=settings.get("services", {}).get(filename, {}).get("token", ""),
                PYTHONPATH=str(self.base_dir) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            )
            from app_paths import compiled, app_command

            command = (
                app_command("--service", filename)
                if self.is_builtin_release(filename)
                else app_command("--external-script", self.path(filename))
                if compiled()
                else [sys.executable, "-u", self.path(filename)]
            )
            proc = subprocess.Popen(
                command,
                cwd=self.base_dir,
                env=env,
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            self.restart_due.pop(filename, None)
            self.processes[filename] = proc
            self.started_at[filename] = time.time()
            self.errors.pop(filename, None)
            self.bridge.pop(filename, None)
            reader = threading.Thread(
                target=self._capture, args=(filename, proc, settings), daemon=True
            )
            self._readers[filename] = reader
            reader.start()
            return "Запущен"

    def stop(self, filename):
        with self._lock:
            self._generation[filename] = self._generation.get(filename, 0) + 1
            self.restart_due.pop(filename, None)
            proc = self.processes.get(filename)
            if proc is None:
                return "Не запущен"
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired) as exc:
                if proc.poll() is None:
                    self.errors[filename] = "Не удалось остановить процесс; повторный запуск заблокирован"
                    raise RuntimeError(self.errors[filename]) from exc
            self.processes.pop(filename, None)
            reader = self._readers.pop(filename, None)
            if reader:
                reader.join(timeout=1)
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            return "Остановлен"

    def restart(self, filename):
        with self._lock:
            self.stop(filename)
            return self.start(filename)

    def enforce_license(self):
        from licensing import status

        if not status(self.app.config_mgr.snapshot())["premium"]:
            self._stop_owned()

    def _stop_owned(self):
        failures = []
        for filename in list(self.processes):
            try:
                self.stop(filename)
            except RuntimeError:
                failures.append(filename)
        if failures:
            raise RuntimeError("Не удалось остановить сервисы: " + ", ".join(failures))

    def stop_all(self):
        with self._lock:
            self._closed = True
            self._stop_owned()

    def log_tail(self, filename, limit=6000):
        path = self.log_dir / (filename + ".log")
        if not path.exists():
            return "Журнал пока пуст."
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return redact(
                stream.read().decode("utf-8", errors="replace"),
                self.app.config_mgr.snapshot(),
            )
