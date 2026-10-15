"""Integer-positioned Tk viewport, without Canvas bitmap scrolling on Windows."""

import tkinter as tk


class ScrollViewport(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.content = tk.Frame(self, bg=self.cget("bg"))
        self.content.place(x=0, y=0, relwidth=1)
        self.offset = 0
        self.total = 1
        self._command = None
        self._layout_job = None
        self._placement = None
        self._closed = False
        self._redraw_job = None
        self.bind("<Configure>", self._schedule, add="+")
        self.content.bind("<Configure>", self._schedule, add="+")
        self.bind("<Destroy>", self._destroyed, add="+")

    def set_scrollbar(self, command):
        self._command = command
        self._schedule()

    def _schedule(self, event=None):
        if self._closed or getattr(self.winfo_toplevel(), "_destroyed", False):
            return
        if self._layout_job is None:
            self._layout_job = self.after_idle(self._layout)

    def _layout(self):
        self._layout_job = None
        if self._closed:
            return
        self.total = max(1, self.content.winfo_reqheight())
        self._move(self.offset)

    def _move(self, offset):
        self.offset = max(0, min(round(offset), max(0, self.total - self.winfo_height())))
        placement = (self.offset, max(1, self.winfo_width()), self.total)
        if placement != self._placement:
            self._placement = placement
            # Move native opaque children; don't scroll/copy a Canvas surface
            # beneath them. Integer positions retain the original text raster.
            self.content.place_configure(
                x=0, y=-self.offset, width=placement[1], relwidth=0, height=self.total
            )
            # Windows can retain stale child text while moving a native window.
            # Invalidate the visible subtree once per coalesced scroll event.
            if self._redraw_job is None:
                self._redraw_job = self.after_idle(self.redraw)
        if self._command:
            self._command(*self.yview())

    def yview(self, *args):
        if args:
            if args[0] == "moveto":
                self.yview_moveto(float(args[1]))
            elif args[0] == "scroll":
                self.yview_scroll(int(args[1]), args[2])
            return None
        return (
            self.offset / self.total,
            min(1.0, (self.offset + self.winfo_height()) / self.total),
        )

    def yview_moveto(self, fraction):
        self.total = max(1, self.content.winfo_reqheight())
        self._move(float(fraction) * self.total)

    def yview_scroll(self, amount, what):
        step = max(1, round(self.winfo_height() * 0.85)) if what == "pages" else 1
        self._move(self.offset + int(amount) * step)

    def _destroyed(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._closed = True
        if self._redraw_job is not None:
            self.after_cancel(self._redraw_job)
            self._redraw_job = None
        if self._layout_job is not None:
            self.after_cancel(self._layout_job)
            self._layout_job = None

    def redraw(self):
        self._redraw_job = None
        if self._closed or not self.winfo_ismapped():
            return
        import os

        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            user = ctypes.WinDLL("user32")
            user.RedrawWindow.argtypes = [
                wintypes.HWND,
                ctypes.c_void_p,
                wintypes.HANDLE,
                wintypes.UINT,
            ]
            # Invalidate once; let Windows coalesce WM_PAINT with the next frame.
            # RDW_UPDATENOW/ERASE forced synchronous painting of every child.
            user.RedrawWindow(self.winfo_id(), None, None, 0x1 | 0x80)

    def destroy(self):
        self._destroyed()
        super().destroy()
