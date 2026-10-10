"""Tray lifecycle: never withdraw the window before the icon is ready."""

import os
from app_icons import load_image


class DesktopShell:
    def init_desktop_shell(self):
        self.tray_icon = None
        self.tray_state = "stopped"
        self._tray_hide_requested = False
        self._tray_timeout = None

    def _new_tray_icon(self):
        import pystray

        def ui(callback):
            return lambda *args: self.post_ui(callback)

        menu = pystray.Menu(
            pystray.MenuItem("Открыть Крест", ui(self.restore_window), default=True),
            pystray.MenuItem("Уведомления", ui(lambda: self._open_from_tray("inbox"))),
            pystray.MenuItem("Включить микрофон", ui(self._start_voice)),
            pystray.MenuItem("Выключить микрофон", ui(self.runtime.stop_voice)),
            pystray.MenuItem(
                "Без озвучки",
                ui(self._tray_toggle_mute),
                checked=lambda item: self.runtime.state.is_muted,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Завершить работу", ui(self._quit_from_tray)),
        )
        return pystray.Icon("krest", load_image(64), "КРЕСТ — личный помощник", menu)

    def ensure_tray(self, hide=False):
        if hasattr(getattr(self, "runtime", None), "client"):
            tray = self.runtime.cached.get("tray", {})
            if tray.get("ready"):
                self.tray_state = "ready"
                if hide:
                    self.withdraw()
                return True
            if hide:
                self._report_error(
                    "Трей ядра ещё не готов. Окно оставлено открытым."
                    + (" " + tray["error"] if tray.get("error") else "")
                )
            return False
        self._tray_hide_requested |= hide
        if self.tray_state == "ready":
            if self._tray_hide_requested:
                self._tray_hide_requested = False
                self.withdraw()
            return True
        if self.tray_state == "starting":
            return True
        icon = None
        try:
            icon = self._new_tray_icon()
            self.tray_icon = icon
            self.tray_state = "starting"

            def ready(actual):
                try:
                    if self.closing or actual is not self.tray_icon:
                        actual.stop()
                        return
                    actual.visible = True
                    self.post_ui(lambda: self._tray_ready(actual))
                except Exception as exc:
                    self.post_ui(lambda error=str(exc): self._tray_failed(actual, error))

            self._tray_timeout = self.after(
                5000, lambda: self._tray_failed(icon, "не получено подтверждение запуска")
            )
            icon.run_detached(setup=ready)
            return True
        except Exception as exc:
            self._tray_failed(icon, str(exc))
            return False

    def _tray_ready(self, icon):
        if icon is not self.tray_icon or self.closing:
            self._background(icon.stop)
            return
        if self._tray_timeout:
            self.after_cancel(self._tray_timeout)
            self._tray_timeout = None
        self.tray_state = "ready"
        if self._tray_hide_requested:
            self._tray_hide_requested = False
            self.withdraw()
        self.status_var.set(
            "Крест доступна в трее. Если значок скрыт, откройте область ↑ рядом с часами."
        )

    def _tray_failed(self, icon, error):
        if icon is not self.tray_icon:
            return
        if self._tray_timeout:
            self.after_cancel(self._tray_timeout)
            self._tray_timeout = None
        self.tray_state = "failed"
        self.tray_icon = None
        self._tray_hide_requested = False
        if icon:
            self._background(icon.stop)
        self.restore_window()
        self._report_error("Трей недоступен, окно оставлено открытым: " + error)

    def hide_to_tray(self):
        return self.ensure_tray(hide=True)

    def restore_window(self):
        if self.closing:
            return
        self._tray_hide_requested = False
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _open_from_tray(self, page):
        self.restore_window()
        self.switch_page(page)

    def _quit_from_tray(self):
        self.restore_window()
        self._real_quit()

    def _tray_toggle_mute(self):
        self.runtime.state.is_muted = not self.runtime.state.is_muted
        self.status_var.set(
            "Озвучка выключена" if self.runtime.state.is_muted else "Озвучка включена"
        )

    def _on_minimize(self, event):
        if (
            event.widget is self
            and self.state() == "iconic"
            and self.config_mgr.config["app_settings"].get("minimize_to_tray")
        ):
            self.hide_to_tray()

    def on_close_window(self):
        if self.config_mgr.config["app_settings"].get("close_to_tray") and self.onboarding is None:
            self.hide_to_tray()
            return
        self._real_quit()

    def open_tray_settings(self):
        if os.name == "nt":
            os.startfile("ms-settings:taskbar")
