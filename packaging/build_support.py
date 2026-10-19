"""Standard-library build logging and a lock shared by the launcher and CI."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
import os
import sys
import threading


class Tee:
    def __init__(self, console, log):
        self.console, self.log = console, log
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.log.write(text)
            self.log.flush()
            if self.console is not None:
                self.console.write(text)
                self.console.flush()
        return len(text)

    def flush(self):
        self.log.flush()
        if self.console is not None:
            self.console.flush()


class BuildSession:
    """Keep one writer, including dependency setup; child packaging inherits it."""

    def __init__(self, root):
        self.root = Path(root)
        self.stack = ExitStack()
        self.inherited = os.environ.get("KREST_BUILD_PARENT") == str(os.getppid())

    def __enter__(self):
        if self.inherited:
            return self
        sys.path.insert(0, str(self.root))
        from process_lock import ProcessLock

        lock = ProcessLock(self.root / "build" / ".builder.lock")
        if not lock.acquire():
            raise RuntimeError("Сборка уже выполняется. Дождитесь её завершения.")
        self.stack.callback(lock.close)
        try:
            log = self.root / "build" / "build-last.log"
            if log.exists():
                log.replace(log.with_name("build-previous.log"))
            output = self.stack.enter_context(log.open("w", encoding="utf-8"))
            self.stack.enter_context(redirect_stdout(Tee(sys.stdout, output)))
            self.stack.enter_context(redirect_stderr(Tee(sys.stderr, output)))
            print("Крест — сборка", datetime.now().isoformat(timespec="seconds"))
            print("Python:", sys.version.split()[0], "·", sys.executable)
            print("Исходники:", self.root)
            print("Полный журнал:", log, flush=True)
            return self
        except BaseException:
            self.stack.close()
            raise

    def environment(self):
        return dict(
            os.environ,
            KREST_BUILD_PARENT=str(os.getpid()),
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            PYTHONUNBUFFERED="1",
        )

    def __exit__(self, kind, error, trace):
        if error is not None:
            import traceback

            traceback.print_exception(kind, error, trace)
            print("Сборка остановлена. Подробности: build/build-last.log", flush=True)
        self.stack.close()
        return False
