"""Optional Windows message loop. No polling while waiting for OS events."""

import os
import threading


class WindowsEvents:
    HOTKEYS = {
        1: (0x20, "quick_panel", "Ctrl+Alt+Space"),
        2: (0x56, "voice_arm", "Ctrl+Alt+V"),
        3: (0x78, "learn_button", "Ctrl+Alt+F9"),
    }

    def __init__(self, callback):
        self.callback = callback
        self.thread = None
        self.hwnd = None
        self.error = ""

    def start(self):
        if os.name != "nt" or self.thread:
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="krest-windows-events")
        self.thread.start()

    def stop(self):
        if self.hwnd and os.name == "nt":
            import ctypes
            from ctypes import wintypes

            api = ctypes.WinDLL("user32", use_last_error=True)
            api.PostMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            api.PostMessageW(self.hwnd, 0x0010, 0, 0)
        if self.thread:
            self.thread.join(timeout=2)

    def hotkey(self, ident):
        if ident in self.HOTKEYS:
            self.callback(self.HOTKEYS[ident][1], {})

    def _run(self):
        import ctypes as c
        from ctypes import wintypes as w

        user = c.WinDLL("user32", use_last_error=True)
        kernel = c.WinDLL("kernel32", use_last_error=True)
        wts = c.WinDLL("wtsapi32", use_last_error=True)
        callback_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WindowClass(c.Structure):
            _fields_ = [
                ("style", w.UINT),
                ("proc", callback_type),
                ("class_extra", c.c_int),
                ("window_extra", c.c_int),
                ("instance", w.HINSTANCE),
                ("icon", w.HICON),
                ("cursor", w.HANDLE),
                ("background", w.HBRUSH),
                ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR),
            ]

        user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.DefWindowProcW.restype = c.c_ssize_t
        user.CreateWindowExW.argtypes = [
            w.DWORD,
            w.LPCWSTR,
            w.LPCWSTR,
            w.DWORD,
            c.c_int,
            c.c_int,
            c.c_int,
            c.c_int,
            w.HWND,
            w.HMENU,
            w.HINSTANCE,
            c.c_void_p,
        ]
        user.CreateWindowExW.restype = w.HWND
        user.RegisterClassW.argtypes = [c.POINTER(WindowClass)]
        user.RegisterHotKey.argtypes = [w.HWND, c.c_int, w.UINT, w.UINT]
        user.UnregisterHotKey.argtypes = [w.HWND, c.c_int]
        user.DestroyWindow.argtypes = [w.HWND]
        user.GetMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user.DispatchMessageW.argtypes = [c.POINTER(w.MSG)]
        user.DispatchMessageW.restype = c.c_ssize_t
        user.TranslateMessage.argtypes = [c.POINTER(w.MSG)]
        wts.WTSRegisterSessionNotification.argtypes = [w.HWND, w.DWORD]
        wts.WTSUnRegisterSessionNotification.argtypes = [w.HWND]
        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        kernel.GetModuleHandleW.restype = w.HMODULE

        def procedure(hwnd, msg, wp, lp):
            try:
                if msg == 0x0312:
                    self.hotkey(wp)
                elif msg == 0x0218 and wp in (7, 18):
                    self.callback("wake", {"source": "windows"})
                elif msg == 0x02B1 and wp in (7, 8):
                    self.callback("lock" if wp == 7 else "unlock", {})
                elif msg == 0x0219 and wp == 7:
                    self.callback("device_change", {})
                elif msg == 0x0010:
                    user.DestroyWindow(hwnd)
                    return 0
                elif msg == 0x0002:
                    user.PostQuitMessage(0)
                    return 0
            except Exception as exc:
                self.error = str(exc)
            return user.DefWindowProcW(hwnd, msg, wp, lp)

        proc = callback_type(procedure)
        instance = kernel.GetModuleHandleW(None)
        name = "KrestEvents_" + str(os.getpid())
        wc = WindowClass(proc=proc, instance=instance, name=name)
        try:
            if not user.RegisterClassW(c.byref(wc)):
                raise c.WinError(c.get_last_error())
            # Hidden top-level window receives power/device broadcasts.
            self.hwnd = user.CreateWindowExW(
                0, name, name, 0, 0, 0, 0, 0, None, None, instance, None
            )
            if not self.hwnd:
                raise c.WinError(c.get_last_error())
            wts.WTSRegisterSessionNotification(self.hwnd, 0)
            failed = []
            for ident, (key, _, caption) in self.HOTKEYS.items():
                if not user.RegisterHotKey(self.hwnd, ident, 0x4000 | 0x0002 | 0x0001, key):
                    failed.append(caption + " занято другим приложением")
            self.error = "; ".join(failed)
            message = w.MSG()
            while user.GetMessageW(c.byref(message), None, 0, 0) > 0:
                user.TranslateMessage(c.byref(message))
                user.DispatchMessageW(c.byref(message))
        except OSError as exc:
            self.error = str(exc)
        finally:
            if self.hwnd:
                for ident in self.HOTKEYS:
                    user.UnregisterHotKey(self.hwnd, ident)
                wts.WTSUnRegisterSessionNotification(self.hwnd)
            self.hwnd = None
