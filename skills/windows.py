import os
import sys
import ctypes
import time
import logging
import subprocess
import psutil
import voice


from skill_manager import skill  # type: ignore # pyright: ignore

logger = logging.getLogger("SkillWindows")

# ==========================================
# 1. КОНСТАНТЫ ВИРТУАЛЬНЫХ КЛАВИШ И WIN32 API
# ==========================================

VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B  # Left Windows key

VK_LEFT = 0x25  # Left Arrow
VK_UP = 0x26  # Up Arrow
VK_RIGHT = 0x27  # Right Arrow
VK_DOWN = 0x28  # Down Arrow

VK_A = 0x41
VK_D = 0x44
VK_E = 0x45
VK_I = 0x49
VK_L = 0x4C
VK_M = 0x4D
VK_N = 0x4E
VK_R = 0x52
VK_S = 0x53
VK_V = 0x56
VK_F4 = 0x73

KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
SW_MINIMIZE = 6


def press_key_combination(keys: list):
    """Отправляет комбинацию клавиш (одновременное нажатие и отпускание в обратном порядке)."""
    pressed = []
    try:
        for key in keys:
            ctypes.windll.user32.keybd_event(key, 0, 0, 0)
            pressed.append(key)
        time.sleep(0.05)
    finally:
        for key in reversed(pressed):
            ctypes.windll.user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)


def force_set_foreground_window(hwnd: int):
    """
    Надёжно выводит окно на передний план, обходя ограничения безопасности Windows
    с помощью имитации нажатия клавиши Alt.
    """
    try:
        ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    except Exception as e:
        logger.error(f"Ошибка вывода окна {hwnd} на передний план: {e}")


# ==========================================
# 2. УПРАВЛЕНИЕ ОКНАМИ И РАБОЧИМ СТОЛОМ
# ==========================================


@skill(
    keywords=["сверни все", "свернуть все", "покажи рабочий стол", "рабочий стол"],
    description="Свернуть все окна и показать рабочий стол (Win + M)",
    category="Windows",
)
def minimize_all_windows(text: str = ""):
    press_key_combination([VK_LWIN, VK_M])
    voice.speak("Свернула все окна.")


@skill(
    keywords=["разверни окно", "развернуть окно", "на весь экран"],
    description="Развернуть текущее окно на весь экран (Win + Up)",
    category="Windows",
)
def maximize_current_window(text: str = ""):
    press_key_combination([VK_LWIN, VK_UP])
    voice.speak("Развернула текущее окно.")


@skill(
    keywords=["сверни окно", "свернуть окно", "минимизируй окно"],
    description="Свернуть активное окно (Win + Down)",
    category="Windows",
)
def minimize_current_window(text: str = ""):
    press_key_combination([VK_LWIN, VK_DOWN])
    voice.speak("Свернула окно.")


@skill(
    keywords=["окно влево", "заполни левую половину", "окно налево"],
    description="Прикрепить окно к левой половине экрана (Win + Left)",
    category="Windows",
)
def snap_window_left(text: str = ""):
    press_key_combination([VK_LWIN, VK_LEFT])
    voice.speak("Прикрепила окно влево.")


@skill(
    keywords=["окно вправо", "заполни правую половину", "окно направо"],
    description="Прикрепить окно к правой половине экрана (Win + Right)",
    category="Windows",
)
def snap_window_right(text: str = ""):
    press_key_combination([VK_LWIN, VK_RIGHT])
    voice.speak("Прикрепила окно вправо.")


@skill(
    keywords=["переключи окно", "следующее окно", "альт таб", "переключи приложение"],
    description="Быстрое переключение между окнами (Alt + Tab)",
    category="Windows",
)
def switch_application(text: str = ""):
    press_key_combination([VK_MENU, VK_TAB])
    voice.speak("Переключила.")


@skill(
    patterns=[
        r"переключи на (?P<target>.+)",
        r"фокус на (?P<target>.+)",
        r"открой окно (?P<target>.+)",
        r"покажи окно (?P<target>.+)",
    ],
    description="Переключение на конкретное окно по его названию",
    examples=["переключи на блокнот"],
    category="Windows",
)
def focus_specific_app(text: str = "", target: str = ""):
    if not target:
        voice.speak("Укажите название приложения.")
        return

    target_clean = target.lower().strip()
    found_hwnd = None
    found_title = ""

    def enum_windows_callback(hwnd, extra):
        nonlocal found_hwnd, found_title
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.lower()
                if target_clean in title:
                    found_hwnd = hwnd
                    found_title = buff.value
                    return False  # Остановка перечисления
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_windows_callback), 0)

    if found_hwnd:
        force_set_foreground_window(found_hwnd)
        voice.speak(f"Переключила на окно {found_title}.")
    else:
        voice.speak(f"Не нашла открытое окно с названием {target}.")


@skill(
    keywords=["закрой окно", "закрой текущее окно", "закрой программу"],
    description="Закрыть активное окно (Alt + F4)",
    category="Windows",
)
def close_active_window(text: str = ""):
    press_key_combination([VK_MENU, VK_F4])
    voice.speak("Закрыла активное окно.")


# ==========================================
# 3. УПРАВЛЕНИЕ ПРОЦЕССАМИ И ЗАВИСШИМИ ОКНАМИ
# ==========================================


@skill(
    patterns=[
        r"закрой приложение (?P<target>.+)",
        r"закрой процесс (?P<target>.+)",
        r"заверши процесс (?P<target>.+)",
        r"убей процесс (?P<target>.+)",
        r"закрой (?P<target>.+)",
    ],
    keywords=["закрой приложение", "заверши программу", "убей процесс"],
    description="Завершение процесса по точному имени исполняемого файла",
    category="Windows",
)
def kill_process_by_name(text: str = "", target: str = ""):
    if not target:
        # Извлекаем название из текста, если регулярка не сработала
        target = (
            text.lower().replace("закрой", "").replace("заверши", "").replace("процесс", "").strip()
        )

    if not target or len(target) < 2:
        voice.speak("Укажите имя приложения для закрытия.")
        return

    target_clean = target.lower().strip()
    killed = False

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = proc.info["name"].lower()
            if (
                pname
                and target_clean.removesuffix(".exe") == pname.removesuffix(".exe")
                and proc.pid != os.getpid()
            ):
                proc.kill()
                killed = True
                logger.info(f"Завершён процесс {proc.info['name']} (PID: {proc.info['pid']})")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if killed:
        voice.speak(f"Приложение {target} успешно закрыто.")
    else:
        voice.speak(f"Не удалось завершить процесс {target}. Проверьте точное имя и права доступа.")
        return False


def find_hung_processes():
    found = {}

    def inspect_window(hwnd, extra):
        if ctypes.windll.user32.IsWindowVisible(hwnd) and ctypes.windll.user32.IsHungAppWindow(
            hwnd
        ):
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value and pid.value != os.getpid():
                try:
                    found[pid.value] = psutil.Process(pid.value)
                except psutil.Error:
                    pass
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    ctypes.windll.user32.EnumWindows(callback(inspect_window), 0)
    return list(found.values())


@skill(
    keywords=["проверь зависшие", "зависшие программы"],
    description="Показать зависшие программы, не завершая их",
    category="Windows",
)
def report_frozen_processes(text=""):
    names = []
    for proc in find_hung_processes():
        try:
            names.append(proc.name())
        except psutil.Error:
            continue
    return "Не отвечают: " + ", ".join(names) if names else "Зависших окон не обнаружено."


@skill(
    keywords=["закрой зависшие", "убей зависшие"],
    description="Принудительно завершить процессы с зависшими окнами",
    category="Windows",
)
def kill_frozen_processes(text=""):
    closed, errors = [], []
    for proc in find_hung_processes():
        try:
            name = proc.name()
            proc.kill()
            closed.append(name)
        except psutil.Error:
            errors.append(str(proc.pid))
    message = "Завершены: " + ", ".join(closed) if closed else "Нет завершённых зависших программ."
    if errors:
        return {
            "status": "error",
            "message": message + " Не удалось завершить PID: " + ", ".join(errors),
        }
    return message


# ==========================================
# 4. СИСТЕМНЫЕ УТИЛИТЫ И ГОРЯЧИЕ КЛАВИШИ
# ==========================================


@skill(
    keywords=[
        "сделай скриншот",
        "снимок экрана",
        "скриншот",
        "сделай снимок",
    ],
    description="Сохранить снимок всех мониторов в папку screenshots",
    category="Windows",
)
def take_screenshot(text: str = "", state=None):
    from screenshots import capture_screen

    path = capture_screen()
    sessions = getattr(getattr(state, "runtime", None), "game_sessions", None)
    if sessions and sessions.active:
        try:
            sessions.add(path=path)
        except (OSError, ValueError):
            return "Скриншот сохранён: " + str(path) + ". В журнал сессии добавить не удалось."
    return "Скриншот сохранён: " + str(path)


@skill(
    keywords=["ножницы", "скриншот области", "выдели область экрана"],
    description="Выделить область средствами Windows",
    category="Windows",
)
def snip_region():
    press_key_combination([VK_LWIN, VK_SHIFT, VK_S])
    return "Выделите область. Windows поместит её в буфер обмена; сохраните снимок в окне Ножниц."


@skill(
    keywords=["открой скриншоты", "папка скриншотов", "где скриншоты"],
    description="Открыть папку сохранённых снимков",
    category="Windows",
)
def show_screenshots():
    from screenshots import open_folder

    return "Скриншоты: " + open_folder()


@skill(
    keywords=["открой диспетчер задач", "диспетчер задач", "диспетчер"],
    description="Запуск Диспетчера задач (Ctrl + Shift + Esc)",
    category="Windows",
)
def open_task_manager(text: str = ""):
    press_key_combination([VK_CONTROL, VK_SHIFT, VK_ESCAPE])
    voice.speak("Открываю Диспетчер задач.")


@skill(
    keywords=["открой проводник", "проводник", "мои файлы", "моё компьютер"],
    description="Запуск Проводника Windows (Win + E)",
    category="Windows",
)
def open_file_explorer(text: str = ""):
    press_key_combination([VK_LWIN, VK_E])
    voice.speak("Открываю Проводник.")


@skill(
    keywords=[
        "открой параметры",
        "параметры windows",
        "настройки windows",
        "системные настройки",
    ],
    description="Запуск Параметров Windows (Win + I)",
    category="Windows",
)
def open_settings(text: str = ""):
    press_key_combination([VK_LWIN, VK_I])
    voice.speak("Открываю настройки Windows.")


@skill(
    keywords=["открой выполнение", "выполнить", "окно выполнить"],
    description="Запуск диалога 'Выполнить' (Win + R)",
    category="Windows",
)
def open_run_dialog(text: str = ""):
    press_key_combination([VK_LWIN, VK_R])
    voice.speak("Открываю диалоговое окно Выполнить.")


@skill(
    keywords=["буфер обмена", "история буфера", "покажи буфер"],
    description="Открыть историю буфера обмена (Win + V)",
    category="Windows",
)
def open_clipboard_history(text: str = ""):
    press_key_combination([VK_LWIN, VK_V])
    voice.speak("Открываю историю буфера обмена.")


@skill(
    keywords=["центр уведомлений", "открой уведомления", "уведомления"],
    description="Открыть Центр уведомлений (Win + A)",
    category="Windows",
)
def open_action_center(text: str = ""):
    key = VK_N if sys.getwindowsversion().build >= 22000 else VK_A
    press_key_combination([VK_LWIN, key])
    voice.speak("Открываю центр уведомлений.")


# ==========================================
# 5. ВИРТУАЛЬНЫЕ РАБОЧИЕ СТОЛЫ
# ==========================================


@skill(
    keywords=["создай рабочий стол", "новый рабочий стол", "добавь рабочий стол"],
    description="Создание нового виртуального рабочего стола (Win + Ctrl + D)",
    category="Windows",
)
def create_virtual_desktop(text: str = ""):
    press_key_combination([VK_LWIN, VK_CONTROL, VK_D])
    voice.speak("Создала новый рабочий стол.")


@skill(
    keywords=["следующий рабочий стол", "рабочий стол вправо"],
    description="Переключение на виртуальный рабочий стол справа (Win + Ctrl + Right)",
    category="Windows",
)
def next_virtual_desktop(text: str = ""):
    press_key_combination([VK_LWIN, VK_CONTROL, VK_RIGHT])
    voice.speak("Переключила на рабочий стол справа.")


@skill(
    keywords=["предыдущий рабочий стол", "рабочий стол влево"],
    description="Переключение на виртуальный рабочий стол слева (Win + Ctrl + Left)",
    category="Windows",
)
def prev_virtual_desktop(text: str = ""):
    press_key_combination([VK_LWIN, VK_CONTROL, VK_LEFT])
    voice.speak("Переключила на рабочий стол слева.")


@skill(
    keywords=["закрой рабочий стол", "удали рабочий стол"],
    description="Закрытие текущего виртуального рабочего стола (Win + Ctrl + F4)",
    category="Windows",
)
def close_virtual_desktop(text: str = ""):
    press_key_combination([VK_LWIN, VK_CONTROL, VK_F4])
    voice.speak("Закрыла рабочий стол.")


# ==========================================
# 6. СИСТЕМНОЕ ОБСЛУЖИВАНИЕ И БЕЗОПАСНОСТЬ
# ==========================================


@skill(
    keywords=["очисти корзину", "очистить корзину", "удали мусор из корзины"],
    description="Полная очистка Корзины Windows без лишних диалогов",
    category="Windows",
)
def empty_recycle_bin(text: str = ""):
    try:
        # Flags: SHERB_NOCONFIRMATION (1) | SHERB_NOPROGRESSUI (2) | SHERB_NOSOUND (4) = 7
        res = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7)
        if res == 0:
            voice.speak("Корзина успешно очищена.")
        else:
            voice.speak("Корзина уже пуста или произошла ошибка.")
            return False
    except Exception as e:
        logger.error(f"Ошибка при очистке корзины: {e}")
        voice.speak("Не удалось очистить корзину.")
        return False


@skill(
    keywords=[
        "заблокируй компьютер",
        "заблокируй пк",
        "заблокируй экран",
        "заблокировать комп",
    ],
    description="Блокировка сеанса пользователя (Win + L / LockWorkStation)",
    category="Windows",
)
def lock_workstation(text: str = ""):
    if not ctypes.windll.user32.LockWorkStation():
        return {"status": "error", "message": "Windows не разрешила блокировку сеанса."}
    return "Сеанс заблокирован."


@skill(
    keywords=["выключи пк", "выключи компьютер", "отключи системный блок"],
    description="Запланировать выключение Windows через 30 секунд",
    category="Windows",
)
def shutdown_windows():
    import subprocess

    result = subprocess.run(
        ["shutdown.exe", "/s", "/t", "30"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
    )
    if result.returncode:
        raise RuntimeError("Windows не запланировала выключение: " + result.stderr.strip())
    return "Выключение запланировано через 30 секунд. Для отмены скажите «отмени выключение»."


@skill(
    keywords=["перезагрузи пк", "перезагрузи компьютер"],
    description="Запланировать перезапуск Windows через 30 секунд",
    category="Windows",
)
def restart_windows():
    import subprocess

    result = subprocess.run(
        ["shutdown.exe", "/r", "/t", "30"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
    )
    if result.returncode:
        raise RuntimeError("Windows не запланировала перезапуск: " + result.stderr.strip())
    return "Перезапуск запланирован через 30 секунд. Для отмены скажите «отмени выключение»."


@skill(
    keywords=["отмени выключение", "отмени перезагрузку"],
    description="Отменить запланированное выключение или перезапуск",
    category="Windows",
)
def cancel_shutdown_windows():
    import subprocess

    result = subprocess.run(
        ["shutdown.exe", "/a"], capture_output=True, text=True, errors="replace", timeout=5
    )
    if result.returncode:
        raise RuntimeError("Нет запланированного выключения или Windows отклонила отмену")
    return "Запланированное выключение или перезапуск отменены."


@skill(
    keywords=["переведи компьютер в сон", "усыпи компьютер"],
    description="Перевести Windows в спящий режим",
    category="Windows",
)
def suspend_windows():
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("powrprof", use_last_error=True)
    api.SetSuspendState.argtypes = [wintypes.BOOLEAN, wintypes.BOOLEAN, wintypes.BOOLEAN]
    api.SetSuspendState.restype = wintypes.BOOLEAN
    if not api.SetSuspendState(False, False, False):
        raise ctypes.WinError(ctypes.get_last_error())
    return "Компьютер возобновил работу после сна."
