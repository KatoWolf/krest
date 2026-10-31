"""Portable recipe checks and a real Win32/UIA round-trip in Windows builds."""


def check_recipes():
    import copy
    import queue
    import tempfile
    import threading
    from pathlib import Path
    from types import SimpleNamespace
    from learned_actions import LearnedActions
    from smart_store import SmartStore

    with tempfile.TemporaryDirectory(prefix="krest-recipe-") as folder:
        rt = SimpleNamespace(
            events=queue.Queue(), stop_event=threading.Event(), command_stop=threading.Event()
        )
        brain = LearnedActions(rt, SmartStore(Path(folder) / "recipes.sqlite3"))
        button = dict(
            exe="C:\\Demo\\app.exe",
            window_class="Demo",
            window_title="Программа",
            name="Нажать",
            auto_id="apply",
            class_name="Button",
            control_type="Button",
            parents=[],
            operation="invoke",
            value=None,
        )
        calls = []

        def native(action, **data):
            calls.append(action)
            if action == "capture":
                return {"step": copy.deepcopy(button)}
            return {"found": True, "window": 42, "status": "reported", "text": "Accepted"}

        brain.native = native
        brain.begin("Мой порядок")
        brain.capture()
        brain.capture()
        if calls != ["capture", "capture"]:
            raise RuntimeError("Обучение неожиданно выполнило действия")
        brain.save()
        result = brain.run("мой порядок")
        if not result["success"] or calls.count("perform") != 2:
            raise RuntimeError("Обученное действие потеряло шаги")
        calls.clear()

        def broken(action, **data):
            calls.append(action)
            if action == "activate":
                return {"found": True, "window": 42}
            raise TimeoutError("Unknown outcome")

        brain.native = broken
        if brain.run("мой порядок")["success"] or calls.count("perform") != 1:
            raise RuntimeError("Неподтверждённое нажатие повторилось")
        if brain.progress[1]["status"] != "not_run":
            raise RuntimeError("После неопределённого шага цепочка продолжилась")
        restored = LearnedActions(rt, brain.store)
        if restored.playing or restored.draft or restored.names() != ["мой порядок"]:
            raise RuntimeError("Восстановление действий неверно")
    print(
        "Taught actions: no recording effects, exact steps, no replay after timeout, persistence passed.",
        flush=True,
    )


def check_native_recipes(*, interactive=True):
    try:
        _check_native_recipes(interactive=interactive)
    except Exception:
        import traceback

        detail = traceback.format_exc()[-7000:]
        print(
            "::error::" + detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
            flush=True,
        )
        raise


def _focus_test_window(hwnd):
    """Interactive CI only: verify the real foreground, including Win32's code-0 error."""
    import win32gui
    import pywintypes

    def current():
        foreground = win32gui.GetForegroundWindow()
        return win32gui.GetAncestor(foreground, 2) if foreground else 0

    if current() == hwnd:
        return
    try:
        win32gui.SetForegroundWindow(hwnd)
    except pywintypes.error as exc:
        if current() != hwnd:
            raise RuntimeError(
                "Windows не дала интерактивной проверке активировать тестовое окно. "
                "Для обычной сборки не используйте --interactive-checks."
            ) from exc
    if current() != hwnd:
        raise RuntimeError("Интерактивная проверка не получила фокус тестового окна")


def _check_native_recipes(*, interactive=True):
    """Only our disposable test window is touched; no user application is opened."""
    import os
    import time
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    import win32api
    import win32con
    import win32gui
    from media_desktop import request
    from desktop_transport import reset_idle
    import ctypes

    # UIA hit testing uses physical pixels. Match the helper's DPI awareness
    # before reading native rectangles in this disposable parent process.
    ctypes.windll.user32.SetProcessDPIAware()

    clicks = []

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == win32con.WM_COMMAND and (wparam & 0xFFFF) == 101:
            clicks.append(101)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    instance = win32api.GetModuleHandle(None)
    class_name = "KrestRecipeCheck_" + uuid.uuid4().hex
    wc = win32gui.WNDCLASS()
    wc.hInstance = instance
    wc.lpszClassName = class_name
    wc.lpfnWndProc = wndproc
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindowEx(
        0 if interactive else win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
        class_name,
        "Крест · проверка обучения",
        win32con.WS_OVERLAPPEDWINDOW,
        80,
        80,
        500,
        260,
        0,
        0,
        instance,
        None,
    )
    button = win32gui.CreateWindow(
        "BUTTON",
        "Применить",
        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
        20,
        20,
        180,
        45,
        hwnd,
        101,
        instance,
        None,
    )
    check = win32gui.CreateWindow(
        "BUTTON",
        "Включить режим",
        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_AUTOCHECKBOX,
        20,
        85,
        200,
        40,
        hwnd,
        102,
        instance,
        None,
    )
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW if interactive else win32con.SW_SHOWNOACTIVATE)
        if interactive:
            _focus_test_window(hwnd)
        win32gui.UpdateWindow(hwnd)
        with ThreadPoolExecutor(max_workers=1) as worker:

            def call(action, **data):
                before = win32gui.GetForegroundWindow()
                future = worker.submit(request, "learned_action", data, action)
                deadline = time.monotonic() + 15
                while not future.done() and time.monotonic() < deadline:
                    win32gui.PumpWaitingMessages()
                    time.sleep(0.01)
                try:
                    return future.result(timeout=1)
                except Exception as exc:
                    after = win32gui.GetForegroundWindow()
                    root = win32gui.GetAncestor(after, win32con.GA_ROOT) if after else 0
                    title = win32gui.GetWindowText(after) if after else ""
                    raise RuntimeError(
                        f"{action} failed: target={hwnd}, foreground={before}->{after}, "
                        f"root={root}, title={title!r}, clicks={len(clicks)}: {exc}"
                    ) from exc

            def capture(control):
                _focus_test_window(hwnd)
                win32gui.UpdateWindow(hwnd)
                win32gui.PumpWaitingMessages()
                x1, y1, x2, y2 = win32gui.GetWindowRect(control)
                try:
                    return call("capture", point=[(x1 + x2) // 2, (y1 + y2) // 2])["step"]
                except Exception as exc:
                    raise RuntimeError(
                        f"Capture failed: target={hwnd}, foreground={win32gui.GetForegroundWindow()}, rect={(x1, y1, x2, y2)}: {exc}"
                    ) from exc

            if not interactive:
                import psutil

                # Locate by exact Unicode labels without cursor hit testing,
                # foreground changes, clicks, toggles or keyboard input.
                step = dict(
                    exe=psutil.Process(os.getpid()).exe(),
                    window_class=class_name,
                    window_title="Крест · проверка обучения",
                    name="Применить",
                    auto_id="",
                    class_name="Button",
                    control_type="Button",
                    parents=[],
                    operation="invoke",
                    value=None,
                )
                result = call("locate", step=step)
                if not result["found"] or result["window"] != hwnd or clicks:
                    raise RuntimeError("Фоновая проверка не нашла свою кнопку без нажатия")
                absent = call("locate", step=dict(step, name="Несуществующая кнопка"))
                if absent["found"] or clicks:
                    raise RuntimeError("Фоновая проверка неверно распознала отсутствующую кнопку")
                print(
                    "Native recipes: background UIA lookup passed without focus changes or input. "
                    "Interactive capture/invocation checks were not run in this mode.",
                    flush=True,
                )
                return

            step = capture(button)
            if step["operation"] != "invoke" or clicks:
                raise RuntimeError("Чтение кнопки изменило окно")
            win32gui.SetWindowPos(hwnd, 0, 180, 160, 500, 260, win32con.SWP_NOZORDER)
            call("activate", step=step)
            result = call("perform", step=step, expected_window=hwnd)
            deadline = time.monotonic() + 1
            while not clicks and time.monotonic() < deadline:
                win32gui.PumpWaitingMessages()
                time.sleep(0.01)
            if result["status"] != "reported" or clicks != [101]:
                raise RuntimeError("Кнопка не выполнена один раз после перемещения окна")
            if not call("locate", step=step)["found"] or clicks != [101]:
                raise RuntimeError("Проверка первого шага выполнила нажатие")
            toggle = capture(check)
            if toggle["operation"] != "toggle" or toggle["value"] != 1:
                raise RuntimeError("Не распознано желаемое состояние переключателя")
            for _ in range(2):
                if call("perform", step=toggle, expected_window=hwnd)["status"] != "confirmed":
                    raise RuntimeError("Переключатель не подтвердил состояние")
            if win32gui.SendMessage(check, win32con.BM_GETCHECK, 0, 0) != 1:
                raise RuntimeError("Повтор включения выключил переключатель")
            duplicate = win32gui.CreateWindow(
                "BUTTON",
                "Применить",
                win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
                240,
                20,
                180,
                45,
                hwnd,
                101,
                instance,
                None,
            )
            try:
                try:
                    call("perform", step=step, expected_window=hwnd)
                except Exception as exc:
                    if "несколько" not in str(exc):
                        raise
                else:
                    raise RuntimeError("Неоднозначная кнопка выполнена")
                if clicks != [101]:
                    raise RuntimeError("Проверка неоднозначности нажала кнопку")
            finally:
                win32gui.DestroyWindow(duplicate)
            # Same executable, class and title, but a different top-level window:
            # normalizing a focused child must not weaken the window guard.
            other = win32gui.CreateWindow(
                class_name,
                "Крест · проверка обучения",
                win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
                100,
                100,
                500,
                260,
                0,
                0,
                instance,
                None,
            )
            try:
                _focus_test_window(other)
                try:
                    call("perform", step=step, expected_window=hwnd)
                except Exception as exc:
                    if "Активное окно изменилось" not in str(exc):
                        raise
                else:
                    raise RuntimeError("Смена окна не остановила действие")
                if clicks != [101]:
                    raise RuntimeError("Проверка смены окна выполнила нажатие")
            finally:
                win32gui.DestroyWindow(other)
    finally:
        reset_idle("learned")
        if win32gui.IsWindow(hwnd):
            win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(class_name, instance)
    print(
        "Native recipes: Unicode selectors, moved window, single invocation, idempotent toggle, ambiguity and focus guard passed.",
        flush=True,
    )
