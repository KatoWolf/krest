import os
import sys
import ctypes
import time
import logging
import subprocess
import psutil
import voice


try:
    from skill_manager import skill  # type: ignore # pyright: ignore
except ImportError:
    from ..skill_manager import skill  # type: ignore # pyright: ignore

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
VK_R = 0x52
VK_S = 0x53
VK_V = 0x56
VK_F4 = 0x73

KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
SW_MINIMIZE = 6


def press_key_combination(keys: list):
    """Отправляет комбинацию клавиш (одновременное нажатие и отпускание в обратном порядке)."""
    for key in keys:
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
    time.sleep(0.05)
    for key in reversed(keys):
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
    description="Свернуть все окна и показать рабочий стол (Win + D)",
    category="Windows",
)
def minimize_all_windows(text: str = ""):
    press_key_combination([VK_LWIN, VK_D])
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
    description="Завершение процесса по имени или фрагменту названия",
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
        voice.speak(f"Не удалось найти запущенный процесс {target}.")


@skill(
    keywords=[
        "закрой зависшие",
        "убей зависшие",
        "проверь зависшие",
        "зависшие программы",
    ],
    description="Проверка окон в состоянии 'Не отвечает' и их принудительное завершение",
    category="Windows",
)
def kill_frozen_processes(text: str = ""):
    frozen_count = 0
    killed_names = []

    def enum_frozen_callback(hwnd, extra):
        nonlocal frozen_count
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            if ctypes.windll.user32.IsHungAppWindow(hwnd):
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    proc = psutil.Process(pid.value)
                    proc_name = proc.name()
                    proc.kill()
                    killed_names.append(proc_name)
                    frozen_count += 1
                    logger.info(f"Завершён зависший процесс: {proc_name} (PID: {pid.value})")
                except Exception as e:
                    logger.error(f"Не удалось завершить зависший PID {pid.value}: {e}")
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_frozen_callback), 0)

    if frozen_count > 0:
        names_str = ", ".join(set(killed_names))
        voice.speak(f"Обнаружены и завершены зависшие программы: {names_str}.")
    else:
        voice.speak("Зависших программ в системе не обнаружено.")


# ==========================================
# 4. СИСТЕМНЫЕ УТИЛИТЫ И ГОРЯЧИЕ КЛАВИШИ
# ==========================================


@skill(
    keywords=[
        "сделай скриншот",
        "снимок экрана",
        "скриншот",
        "ножницы",
        "сделай снимок",
    ],
    description="Открыть область выделения скриншота Windows (Win + Shift + S)",
    category="Windows",
)
def take_screenshot(text: str = ""):
    press_key_combination([VK_LWIN, VK_SHIFT, VK_S])
    voice.speak("Выделите область для скриншота.")


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
    press_key_combination([VK_LWIN, VK_A])
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
    except Exception as e:
        logger.error(f"Ошибка при очистке корзины: {e}")
        voice.speak("Не удалось очистить корзину.")


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
    voice.speak("Блокирую систему.")
    ctypes.windll.user32.LockWorkStation()
