"""The persistent kernel owns the tray; the desktop may come and go."""

import os
import threading


class KernelTray:
    def __init__(self, kernel):
        self.kernel = kernel
        self.icon = None
        self.ready = False
        self.error = ""
        self.thread = None

    def start(self):
        if os.name != "nt" or self.thread:
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="krest-tray")
        self.thread.start()

    def _run(self):
        try:
            import pystray
            from app_icons import load_image

            def open_ui(*args):
                self.kernel.open_ui()

            def mute(*args):
                self.kernel.runtime.state.is_muted = not self.kernel.runtime.state.is_muted

            def quit_all(*args):
                self.kernel.event("shutdown", None)
                self.kernel.stop.set()

            self.icon = pystray.Icon(
                "krest",
                load_image(64),
                "КРЕСТ — ядро работает",
                pystray.Menu(
                    pystray.MenuItem("Открыть Крест", open_ui, default=True),
                    pystray.MenuItem(
                        "Быстрая панель", lambda *args: self.kernel.open_ui(quick=True)
                    ),
                    pystray.MenuItem(
                        "Без озвучки", mute, checked=lambda item: self.kernel.runtime.state.is_muted
                    ),
                    pystray.MenuItem(
                        "Выключить микрофон", lambda *args: self.kernel.runtime.stop_voice()
                    ),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Завершить работу", quit_all),
                ),
            )

            def ready(icon):
                icon.visible = True
                self.ready = True

            self.icon.run(setup=ready)
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.ready = False

    def notify(self, text, title="Крест"):
        if self.ready and self.icon:
            try:
                self.icon.notify(str(text)[:500], title)
            except Exception as exc:
                self.error = str(exc)

    def stop(self):
        if self.icon:
            self.icon.stop()
        if self.thread:
            self.thread.join(timeout=2)
