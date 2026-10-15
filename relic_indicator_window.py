"""Per-pixel Windows presentation adapted from the supplied Relic V2 module."""

import ctypes as C
from ctypes import wintypes as W


class LayeredWindow:
    def __init__(self, root):
        import tkinter as tk

        self.root = root
        self.monitor, self.margin, self.shown = "cursor", 20, False
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.update_idletasks()
        self.u = C.WinDLL("user32", use_last_error=True)
        self.g = C.WinDLL("gdi32", use_last_error=True)
        self._configure_api()
        self.hwnd = self.u.GetParent(self.window.winfo_id()) or self.window.winfo_id()
        style = self.get_style(self.hwnd, -20)
        self.set_style(self.hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x8000000)
        self.scale = 1
        self.xy = (0, 0)

    def hide(self):
        if self.shown:
            self.u.ShowWindow(self.hwnd, 0)
            self.shown = False

    def close(self):
        self.window.destroy()

    def _configure_api(self):
        u, g = self.u, self.g

        def sig(lib, name, args, ret):
            f = getattr(lib, name)
            f.argtypes = args
            f.restype = ret
            return f

        self.get_style = sig(
            u,
            "GetWindowLongPtrW" if C.sizeof(C.c_void_p) == 8 else "GetWindowLongW",
            [W.HWND, C.c_int],
            C.c_ssize_t,
        )
        self.set_style = sig(
            u,
            "SetWindowLongPtrW" if C.sizeof(C.c_void_p) == 8 else "SetWindowLongW",
            [W.HWND, C.c_int, C.c_ssize_t],
            C.c_ssize_t,
        )
        sig(u, "GetParent", [W.HWND], W.HWND)
        sig(u, "GetDC", [W.HWND], W.HDC)
        sig(u, "ReleaseDC", [W.HWND, W.HDC], C.c_int)
        sig(u, "GetCursorPos", [C.POINTER(W.POINT)], W.BOOL)
        sig(u, "MonitorFromPoint", [W.POINT, W.DWORD], W.HANDLE)
        sig(u, "GetMonitorInfoW", [W.HANDLE, C.c_void_p], W.BOOL)
        sig(u, "ShowWindow", [W.HWND, C.c_int], W.BOOL)
        sig(u, "SetWindowPos", [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT], W.BOOL)
        sig(
            u,
            "UpdateLayeredWindow",
            [
                W.HWND,
                W.HDC,
                C.c_void_p,
                C.c_void_p,
                W.HDC,
                C.c_void_p,
                W.DWORD,
                C.c_void_p,
                W.DWORD,
            ],
            W.BOOL,
        )
        sig(g, "CreateCompatibleDC", [W.HDC], W.HDC)
        sig(g, "DeleteDC", [W.HDC], W.BOOL)
        sig(
            g,
            "CreateDIBSection",
            [W.HDC, C.c_void_p, W.UINT, C.c_void_p, W.HANDLE, W.DWORD],
            W.HBITMAP,
        )
        sig(g, "SelectObject", [W.HDC, W.HGDIOBJ], W.HGDIOBJ)
        sig(g, "DeleteObject", [W.HGDIOBJ], W.BOOL)

    def _position(self):
        class Info(C.Structure):
            _fields_ = [
                ("cbSize", W.DWORD),
                ("rcMonitor", W.RECT),
                ("rcWork", W.RECT),
                ("flags", W.DWORD),
            ]

        point = W.POINT()
        if self.monitor == "cursor":
            self.u.GetCursorPos(C.byref(point))
        handle = self.u.MonitorFromPoint(point, 1 if self.monitor == "primary" else 2)
        info = Info()
        info.cbSize = C.sizeof(info)
        if not self.u.GetMonitorInfoW(handle, C.byref(info)):
            raise C.WinError(C.get_last_error())
        scale = 1.0
        try:
            sh = C.WinDLL("shcore")
            fn = sh.GetDpiForMonitor
            fn.argtypes = [W.HANDLE, C.c_int, C.POINTER(W.UINT), C.POINTER(W.UINT)]
            fn.restype = C.c_long
            x = W.UINT()
            y = W.UINT()
            if fn(handle, 0, C.byref(x), C.byref(y)) == 0:
                scale = x.value / 96
        except (AttributeError, OSError):
            pass
        self.scale = max(0.75, min(4.0, scale))
        r = info.rcWork
        self.xy = (
            round((r.left + r.right - 170 * self.scale) / 2),
            round(r.bottom - (152 + self.margin) * self.scale),
        )

    def _present(self, im):
        class Header(C.Structure):
            _fields_ = [
                ("size", W.DWORD),
                ("width", W.LONG),
                ("height", W.LONG),
                ("planes", W.WORD),
                ("bits", W.WORD),
                ("compression", W.DWORD),
                ("imageSize", W.DWORD),
                ("x", W.LONG),
                ("y", W.LONG),
                ("used", W.DWORD),
                ("important", W.DWORD),
            ]

        class Blend(C.Structure):
            _fields_ = [
                ("op", C.c_ubyte),
                ("flags", C.c_ubyte),
                ("alpha", C.c_ubyte),
                ("format", C.c_ubyte),
            ]

        # UpdateLayeredWindow requires PREMULTIPLIED alpha (avoids black halos).
        buf = im.convert("RGBa").tobytes("raw", "BGRa")
        screen = self.u.GetDC(None)
        dc = self.g.CreateCompatibleDC(screen)
        bits = C.c_void_p()
        bitmap = None
        old = None
        try:
            header = Header(C.sizeof(Header), im.width, -im.height, 1, 32, 0, len(buf), 0, 0, 0, 0)
            bitmap = self.g.CreateDIBSection(screen, C.byref(header), 0, C.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise C.WinError(C.get_last_error())
            old = self.g.SelectObject(dc, bitmap)
            C.memmove(bits, bytes(buf), len(buf))
            dest = W.POINT(*self.xy)
            size = W.SIZE(im.width, im.height)
            src = W.POINT()
            blend = Blend(0, 0, 255, 1)
            if not self.u.UpdateLayeredWindow(
                self.hwnd,
                screen,
                C.byref(dest),
                C.byref(size),
                dc,
                C.byref(src),
                0,
                C.byref(blend),
                2,
            ):
                raise C.WinError(C.get_last_error())
        finally:
            if old:
                self.g.SelectObject(dc, old)
            if bitmap:
                self.g.DeleteObject(bitmap)
            if dc:
                self.g.DeleteDC(dc)
            if screen:
                self.u.ReleaseDC(None, screen)
        if not self.shown:
            self.u.SetWindowPos(self.hwnd, W.HWND(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
            self.u.ShowWindow(self.hwnd, 4)
            self.shown = True
