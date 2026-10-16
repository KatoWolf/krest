"""A listening indicator driven by real wake/command state, outside the menu."""

import time
from game_overlay import OverlayManager


class VoiceIndicatorManager(OverlayManager):
    FLAG = "--voice-indicator"


class ListeningAnimation:
    STAGES = {"addressed", "recognizing", "listening"}

    def __init__(self):
        self.started = None
        self.ended = None
        self.turn = 0
        self.accepted = 0
        self.visible = False

    def sync(self, feedback, enabled=True, now=None, wall=None):
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        turn = int(feedback.get("turn", 0))
        stage = feedback.get("stage")
        if feedback.get("accepted") or stage in ("queued", "executing", "success", "error"):
            self.accepted = max(self.accepted, turn)
        listening = (
            enabled
            and feedback.get("source", "voice") == "voice"
            and feedback.get("until", 0) > wall
            and stage in self.STAGES
            and turn > self.accepted
        )
        if listening:
            if not self.visible or self.ended is not None or turn != self.turn:
                self.started, self.ended, self.turn = now, None, turn
            self.visible = True
        elif self.visible and self.ended is None:
            self.ended = now
        if not enabled or self.ended is not None and now - self.ended >= 0.65:
            self.visible = False
        return self.visible

    def frame(self, now=None, reduced=False):
        from relic_indicator_visual import animation_frame

        now = time.monotonic() if now is None else now
        if self.ended is not None:
            return animation_frame(
                "leave",
                min(13, int((now - self.ended) * 20)),
                reduced,
                min(22, max(1, int((self.ended - self.started) * 20))),
            )
        age = max(0, now - self.started)
        if reduced:
            return animation_frame("idle", 0, True)
        if age < 1.1:
            return animation_frame("assemble", int(age * 20))
        return animation_frame("idle", int((age - 1.1) * 20) % 54)


def run_indicator(config_path):
    """Only this small process owns the overlay Tk loop; menu stalls cannot block it."""
    import os
    import queue
    import threading
    import tkinter as tk
    from pathlib import Path
    from kernel_client import KernelClient
    from process_lock import ProcessLock
    from relic_indicator_window import LayeredWindow
    from PIL import Image

    if os.name != "nt":
        return 0
    lock = ProcessLock(Path(config_path).parent / ".voice-indicator.lock")
    if not lock.acquire():
        return 0
    from ui_fonts import enable_dpi_awareness

    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    display = LayeredWindow(root)
    animation = ListeningAnimation()
    client = KernelClient(config_path)
    stopped = threading.Event()
    updates = queue.Queue(maxsize=1)
    data, received, failures = {}, time.monotonic(), 0
    positioned_turn = 0

    last_image, last_scaled = None, None

    def poll():
        revision = -1
        while not stopped.is_set():
            try:
                value = client.call("voice.indicator", after=revision, _timeout=2)
                revision = value.get("revision", -1)
            except Exception:
                value = None
            try:
                updates.get_nowait()
            except queue.Empty:
                pass
            updates.put_nowait(value)
            # The kernel waits on feedback changes for up to one second. Wake
            # words notify it immediately; only errors/old kernels need a delay.
            if value is None or revision == -1:
                stopped.wait(0.25)

    def tick():
        nonlocal data, received, failures, positioned_turn, last_image, last_scaled
        try:
            value = updates.get_nowait()
            if value is None:
                failures += 1
            else:
                data, received, failures = value, time.monotonic(), 0
        except queue.Empty:
            pass
        if failures >= 3 or time.monotonic() - received > 3:
            root.quit()
            return
        enabled = data.get("enabled", False)
        visible = animation.sync(data.get("feedback", {}), enabled=enabled)
        if visible:
            if positioned_turn != animation.turn:
                display._position()
                positioned_turn = animation.turn
                last_image = None
            im = animation.frame(reduced=data.get("reduced", False))
            if im is not last_image:
                last_image = im
                last_scaled = (
                    im
                    if display.scale == 1
                    else im.resize(
                        (round(170 * display.scale), round(152 * display.scale)),
                        Image.Resampling.LANCZOS,
                    )
                )
                display._present(last_scaled)
        else:
            display.hide()
            last_image = None
        root.after(50 if visible else 80, tick)

    # Failures terminate this owned process; the kernel supervisor can restart it.
    errors = []

    def failed(*args):
        errors.append(str(args[1]))
        root.quit()

    root.report_callback_exception = failed
    threading.Thread(target=poll, name="krest-indicator-link", daemon=True).start()
    root.after(0, tick)
    try:
        root.mainloop()
    finally:
        stopped.set()
        display.close()
        root.destroy()
        lock.close()
    return 1 if errors else 0
