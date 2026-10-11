"""An explicit allowlist for remote commands; local GUI remains unrestricted."""

import os

DEFAULT_ALLOWED = [
    "статус",
    "📊 статус",
    "время",
    "список таймеров",
    "план на сегодня",
    "пауза",
    "плей",
    "громче",
    "тише",
    "следующий",
    "🎵 плей / пауза",
    "📦 инвентарь cs2",
    "💾 диски",
    "🤝 трейды",
    "диски",
    "процессы",
    "окно",
    "скриншот",
    "инвентарь",
    "трейды",
    "предыдущий трек",
    "что играет",
    "поставь лайк",
    "мут",
    "анмут",
    "список будильников",
    "сводка варфрейм",
    "циклы варфрейм",
    "сводка матча",
    "заблокируй компьютер",
    "отмени выключение",
    "файлы",
    "файл *",
    "таймер на *",
    "напомни через *",
    "проверь связь с дотой",
]
PARAMETER_COMMANDS = {
    "таймер на",
    "напомни через",
    "файл",
    "скажи",
    "поиск",
    "запусти игру",
    "запусти программу",
}


def session_locked():
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.SwitchDesktop.argtypes = [wintypes.HANDLE]
    user.SwitchDesktop.restype = wintypes.BOOL
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    desktop = user.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        return True
    try:
        return not bool(user.SwitchDesktop(desktop))
    finally:
        user.CloseDesktop(desktop)


def authorize(text, settings, locked=None):
    cfg = settings.get("remote_control", {})
    if not cfg.get("enabled", False):
        return False, "Удалённые команды выключены в меню Крест."
    command = text.casefold().strip()
    allowed = {v.casefold().strip() for v in cfg.get("allowed_commands", DEFAULT_ALLOWED)}
    parameter = any(
        prefix + " *" in allowed and command.startswith(prefix + " ") and len(command) <= 2000
        for prefix in PARAMETER_COMMANDS
    )
    if command not in allowed and not parameter:
        return False, "Команда отсутствует в разрешённом списке меню Крест."
    if (session_locked() if locked is None else locked) and cfg.get("block_when_locked", True):
        return False, "Компьютер заблокирован; удалённое выполнение приостановлено."
    return True, ""
