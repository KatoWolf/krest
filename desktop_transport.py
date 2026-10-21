"""One serial pipe per desktop adapter; timeouts terminate without replaying clicks."""

import atexit
import json
import queue
import subprocess
import threading

PREFIX = "KREST_DESKTOP_RESULT:"
_clients = {}
_lock = threading.Lock()


class DesktopPipe:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.sequence = 0

    def close(self):
        process, self.process = self.process, None
        if process:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    pipe.close()

    def _start(self):
        from app_paths import app_command

        self.close()
        self.process = subprocess.Popen(
            app_command("--desktop-service"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            bufsize=1,
        )
        self.responses = queue.Queue(maxsize=2)
        process, responses = self.process, self.responses

        def read():
            try:
                while line := process.stdout.readline(65537):
                    if len(line) > 65536:
                        break
                    if line.startswith(PREFIX):
                        responses.put_nowait(json.loads(line[len(PREFIX) :]))
            except (OSError, ValueError, queue.Full):
                pass
            finally:
                try:
                    responses.put_nowait(None)
                except queue.Full:
                    pass

        threading.Thread(target=read, daemon=True, name="krest-desktop-pipe").start()

    def call(self, payload, timeout=12):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            self.sequence += 1
            request = dict(payload, request_id=self.sequence)
            try:
                self.process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
                self.process.stdin.flush()
                result = self.responses.get(timeout=timeout)
                if not isinstance(result, dict) or result.get("request_id") != self.sequence:
                    raise OSError("Помощник приложения завершился без подтверждения")
                return result
            except (queue.Empty, OSError, ValueError) as exc:
                self.close()
                raise TimeoutError(
                    "Не получено подтверждение от приложения. Проверьте состояние перед повтором."
                ) from exc


def request(payload, timeout=12):
    # Separate Discord from music: a stalled player must not block the microphone.
    domain = {"discord_voice": "discord", "file_context": "files", "audio_dispatch": "audio"}.get(
        payload["operation"], "music"
    )
    with _lock:
        client = _clients.setdefault(domain, DesktopPipe())
    return client.call(payload, timeout=timeout)


def close():
    with _lock:
        for client in _clients.values():
            with client.lock:
                client.close()
        _clients.clear()


def reset_idle(domain):
    """Discard only the named helper and never interrupt a live click/request."""
    with _lock:
        client = _clients.get(domain)
    if client is None:
        return True
    if not client.lock.acquire(blocking=False):
        return False
    try:
        client.close()
        return True
    finally:
        client.lock.release()


atexit.register(close)


def serve(handle, idle_seconds=120):
    """UIA runs only on this process's main thread, never on the pipe reader."""
    import sys

    inbox = queue.Queue(maxsize=1)

    def read():
        try:
            while line := sys.stdin.readline(32770):
                inbox.put(line)
        finally:
            inbox.put(None)

    threading.Thread(target=read, daemon=True).start()
    while True:
        try:
            raw = inbox.get(timeout=idle_seconds)
        except queue.Empty:
            return 0
        if raw is None:
            return 0
        handle(raw)
