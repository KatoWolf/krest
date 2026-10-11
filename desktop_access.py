"""Temporarily make a tray/minimized Electron window accessible without activation."""

from contextlib import contextmanager


@contextmanager
def expose_window(window):
    import win32con
    import win32gui

    handle = window.handle
    hidden = not win32gui.IsWindowVisible(handle)
    minimized = bool(win32gui.IsIconic(handle))
    try:
        if hidden or minimized:
            win32gui.ShowWindow(handle, win32con.SW_SHOWNOACTIVATE)
            # Keep games and the user's foreground window above the exposed app.
            # UI Automation can invoke controls behind other windows.
            win32gui.SetWindowPos(
                handle,
                win32con.HWND_BOTTOM,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
        yield
    finally:
        if win32gui.IsWindow(handle):
            if minimized:
                win32gui.ShowWindow(handle, win32con.SW_SHOWMINNOACTIVE)
            if hidden:
                win32gui.ShowWindow(handle, win32con.SW_HIDE)
