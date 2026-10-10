"""Small authenticated JSON client. Desktop state is cached off the Tk thread."""

from pathlib import Path
from types import SimpleNamespace
import http.client
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

from secret_store import protect_bootstrap


class KernelClient:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()

    def call(self, method, **args):
        endpoint = json.loads((self.config_path.parent / "kernel_endpoint.json").read_text())
        if endpoint.get("protocol") != 1:
            raise RuntimeError("Несовместимая версия ядра. Перезапустите Крест.")
        token = protect_bootstrap(endpoint["token"], decrypt=True)
        connection = http.client.HTTPConnection("127.0.0.1", int(endpoint["port"]), timeout=8)
        try:
            body = json.dumps({"method": method, "args": args}, ensure_ascii=False).encode()
            connection.request(
                "POST",
                "/rpc",
                body,
                {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("Ядро отклонило локальное подключение")
            data = response.read(4_000_001)
            if len(data) > 4_000_000:
                raise RuntimeError("Ответ ядра превышает допустимый размер")
            payload = json.loads(data)
            if not payload["ok"]:
                raise RuntimeError(payload["error"])
            return payload["result"]
        finally:
            connection.close()

    def ensure_started(self, safe=False):
        try:
            status = self.call("ping")
            if safe and not status["safe_mode"]:
                raise RuntimeError("Сначала завершите обычное ядро: python main.py --stop-kernel")
            return self
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            pass
        args = [
            sys.executable,
            str(Path(__file__).with_name("main.py")),
            "--kernel",
            "--config",
            str(self.config_path),
        ]
        if safe:
            args.append("--safe-mode")
        environment = dict(os.environ, KREST_CONFIG_PATH=str(self.config_path))
        kwargs = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        subprocess.Popen(
            args,
            cwd=Path(__file__).parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                self.call("ping")
                return self
            except (OSError, ValueError, KeyError, http.client.HTTPException):
                time.sleep(0.15)
        raise RuntimeError(
            "Не удалось запустить ядро. Запустите python main.py --kernel для диагностики."
        )


class StateProxy:
    def __init__(self, remote):
        object.__setattr__(self, "remote", remote)

    def snapshot(self):
        return self.remote.cached["state"]

    def reminders(self):
        return self.remote.cached["reminders"]

    def __getattr__(self, name):
        if name in ("is_muted", "is_sleeping", "dota_guide_active", "load_error"):
            return self.remote.cached["flags"][name]
        if name in (
            "add_timer",
            "add_alarm",
            "cancel",
            "snooze",
            "complete",
            "acknowledge",
            "extend_timer",
            "move_occurrence",
            "skip_occurrence",
        ):

            def invoke(*args, **kwargs):
                result = self.remote.client.call("state." + name, args=args, kwargs=kwargs)
                self.remote.refresh()
                return result

            return invoke
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name not in ("is_muted", "is_sleeping"):
            raise AttributeError(name)
        self.remote.client.call("state_set", key=name, value=value)
        self.remote.cached["flags"][name] = value


class ServiceProxy:
    from integrations import IntegrationManager

    MODULES = dict(IntegrationManager.MODULES)

    def __init__(self, remote):
        self.remote = remote
        self.errors = {}
        self.MODULES = dict(remote.cached["modules"])

    @property
    def processes(self):
        return {k: v for k, v in self.remote.cached["services"].items() if v["running"]}

    def details(self, filename):
        return self.remote.cached["services"].get(
            filename,
            {
                "running": False,
                "pid": None,
                "uptime": 0,
                "error": self.errors.get(filename, ""),
                "bridge": {},
            },
        )

    def refresh(self):
        return self.remote.cached["service_statuses"]

    def path(self, filename):
        value = self.remote.settings().get("services", {}).get(filename, {}).get("path")
        return (
            str(Path(value).expanduser().resolve())
            if value
            else str(Path(__file__).parent / filename)
        )

    def exists(self, filename):
        return Path(self.path(filename)).is_file()

    def __getattr__(self, name):
        if name in (
            "start",
            "stop",
            "restart",
            "send",
            "log_tail",
            "autostart",
            "reload_registry",
            "enforce_license",
            "stop_all",
        ):

            def invoke(filename=None, command=None):
                result = self.remote.client.call(
                    "service." + name, filename=filename, command=command
                )
                self.remote.refresh()
                self.MODULES = dict(self.remote.cached["modules"])
                return result

            return invoke
        raise AttributeError(name)


class PerformanceProxy:
    def __init__(self, remote):
        self.remote = remote

    def __getattr__(self, name):
        return self.remote.cached["performance"][name]

    def set_mode(self, mode):
        return self.remote.client.call("performance", mode=mode)


class ScenarioProxy:
    def __init__(self, remote):
        self.remote = remote

    @property
    def status(self):
        return self.remote.cached["scenario"]

    def start(self, name):
        return self.remote.client.call("scenario.start", name=name)

    def cancel(self):
        return self.remote.client.call("scenario.cancel")

    def preview(self, name):
        return self.remote.client.call("scenario.preview", name=name)

    def retry(self):
        return self.remote.client.call("scenario.retry")

    def skip_wait(self):
        return self.remote.client.call("scenario.skip_wait")


class CatalogProxy:
    def __init__(self, client):
        self.client = client
        self.errors = {}
        self.entries = []
        self.refresh()

    def refresh(self):
        data = self.client.call("catalog")
        self.errors = data["errors"]
        self.entries = [
            SimpleNamespace(
                **dict(item, patterns=[SimpleNamespace(pattern=p) for p in item["patterns"]])
            )
            for item in data["entries"]
        ]

    def catalog(self):
        return self.entries

    def availability(self, entry):
        return entry.availability

    def match(self, text):
        ident = self.client.call("match", text=text)
        return (next((v for v in self.entries if v.id == ident), None), {}) if ident else None


class RemoteRuntime:
    def __init__(self, client, settings):
        from notification_center import NotificationCenter
        from platform_store import JobStore, MemoryStore

        self.client, self.settings = client, settings
        self.events = queue.Queue(maxsize=1000)
        self.stop_event = threading.Event()
        self._refresh_lock = threading.Lock()
        self.cached = client.call("snapshot")
        self.sequence = self.cached["sequence"]
        self.state = StateProxy(self)
        self.integrations = ServiceProxy(self)
        self.performance = PerformanceProxy(self)
        self.scenarios = ScenarioProxy(self)
        root = client.config_path.parent
        self.center = NotificationCenter(root / "inbox.sqlite3")
        self.jobs = JobStore(root / "jobs.sqlite3", recover=False)
        self.memory = MemoryStore(root / "memory.sqlite3")
        self.notifications = SimpleNamespace(put=lambda text: client.call("notice", text=text))
        self.manager = CatalogProxy(client)
        self._voice_thread = SimpleNamespace(is_alive=lambda: self.cached["voice"]["listening"])
        self._poller = threading.Thread(target=self._poll, daemon=True, name="krest-desktop-link")
        self._poller.start()

    def refresh(self):
        from skill_manager import CommandResult

        with self._refresh_lock:
            data = self.client.call("snapshot", after=self.sequence)
            self.cached = data
            for item in data["events"]:
                value = (
                    CommandResult(**item["value"]) if item["kind"] == "result" else item["value"]
                )
                if item["kind"] == "result" and value.skill_id == "skills.meta.reload_skills":
                    self.manager.refresh()
                try:
                    self.events.put_nowait((item["kind"], value))
                except queue.Full:
                    self.events.get_nowait()
                    self.events.put_nowait((item["kind"], value))
            self.sequence = data["sequence"]

    def _poll(self):
        reported = False
        while not self.stop_event.wait(0.5):
            try:
                self.refresh()
                reported = False
            except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                if not reported:
                    try:
                        self.events.put_nowait(
                            (
                                "error",
                                "Связь с ядром потеряна. Перезапустите Крест; журнал заданий сохранён.",
                            )
                        )
                    except queue.Full:
                        pass
                    reported = True
                self.stop_event.wait(2)

    @property
    def download_status(self):
        return self.cached["downloads"]

    @property
    def notification_mode(self):
        return self.cached["notifications_mode"]

    @notification_mode.setter
    def notification_mode(self, value):
        self.client.call("notification_mode", value=value)

    def start(self):
        return self.client.call("activate")

    def submit(self, text, **kwargs):
        return self.client.call("submit", text=text, request_key=uuid.uuid4().hex)

    def cancel_job(self, ident):
        return self.client.call("job_cancel", id=ident)

    def clear_pending(self):
        return self.client.call("clear_pending")

    def command_status(self):
        return self.cached["command"]

    def start_voice(self):
        return self.client.call("voice.start")

    def stop_voice(self):
        return self.client.call("voice.stop")

    def arm_voice(self):
        return self.client.call("voice.arm")

    def detach(self):
        self.stop_event.set()
        self._poller.join(timeout=1)
        import voice

        voice.set_remote(None)

    def close(self):
        try:
            self.client.call("shutdown")
        finally:
            self.detach()
