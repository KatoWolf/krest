import sys
import os
import time
import threading
import logging
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass, field

try:
    import psutil
except ImportError:
    psutil = None

try:
    import winreg
except ImportError:
    winreg = None


from skill_manager import skill  # type: ignore # pyright: ignore

import voice

logger = logging.getLogger("DotaGuide")

DOTA_PROCESS_NAME = "dota2.exe"
WARNING_LEAD_TIME = 30  # Оповещать за 30 секунд до события
GSI_PORT = 3000


def auto_setup_gsi_config(install_path="") -> bool:
    """Discover Steam libraries and atomically provision only Krest's GSI file."""
    from pathlib import Path
    from steam_service import local_games
    from storage import atomic_write_text

    try:
        games = local_games(install_path)
        game = next((g for g in games if g["appid"] == "570" and g["installed"]), None)
        if not game:
            dota_state.setup_message = "Dota 2 не установлена в найденных библиотеках Steam"
            return False
        folder = (
            Path(game["library"]) / "steamapps/common" / (game.get("install_dir") or "dota 2 beta")
        )
        if folder.resolve().parent != (Path(game["library"]) / "steamapps/common").resolve():
            raise ValueError("Некорректная папка игры в манифесте Steam")
        if not (folder / "game/dota").is_dir():
            raise ValueError("Папка Dota 2 из манифеста Steam не найдена")
        target = folder / "game/dota/cfg/gamestate_integration/gamestate_integration_krest.cfg"
        content = "\n".join(
            [
                '"Krest Dota Guide Configuration"',
                "{",
                f'    "uri" "http://127.0.0.1:{GSI_PORT}/"',
                '    "timeout" "5.0"',
                '    "buffer" "0.1"',
                '    "throttle" "0.5"',
                '    "heartbeat" "5.0"',
                '    "data"',
                "    {",
                *[
                    f'        "{key}" "1"'
                    for key in ("provider", "map", "player", "hero", "abilities", "items")
                ],
                "    }",
                "}",
                "",
            ]
        )
        changed = not target.exists() or target.read_text(encoding="utf-8") != content
        if changed:
            if target.exists():
                import shutil

                backup = target.with_suffix(".cfg.bak")
                if not backup.exists():
                    shutil.copy2(target, backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, content)
        dota_state.gsi_configured = True
        dota_state.setup_message = (
            "Связь подготовлена. Если Dota уже открыта, перезапустите игру."
            if changed
            else "Файл связи готов. Жду данные матча."
        )
        dota_state.config_file = str(target)
        return True
    except (OSError, ValueError) as exc:
        dota_state.setup_message = "Настройка Dota: " + str(exc)
        logger.warning(dota_state.setup_message)
        return False


@dataclass
class DotaState:
    is_active: bool = False
    match_started: bool = False
    match_state: str = "DOTA_GAMERULES_STATE_INIT"
    match_time_seconds: int = 0
    roshan_death_time: int | None = None
    last_announced_events: set = field(default_factory=set)

    dota_running: bool = False
    waiting_for_user_answer: bool = False
    timer_thread: threading.Thread | None = None

    setup_message: str = "Связь ещё не подготовлена"
    config_file: str = ""
    gsi_configured: bool = False
    last_gsi_update: float = 0.0
    activation_time: float = 0.0
    diagnostic_warned: bool = False
    auto_mode: bool = False
    hero: dict = field(default_factory=dict)
    player: dict = field(default_factory=dict)


dota_state = DotaState()


def announce(message: str, event_id: str):
    """Озвучивает сообщение с защитой от дублирования."""
    if event_id not in dota_state.last_announced_events:
        logger.info(f"[DotaGuide Alert] {event_id}: {message}")
        voice.speak(message.replace("30 секунд", f"{WARNING_LEAD_TIME} секунд"))
        dota_state.last_announced_events.add(event_id)
        if len(dota_state.last_announced_events) > 150:
            dota_state.last_announced_events.clear()


_service_stop = threading.Event()
_service_lock = threading.RLock()
_lifecycle_lock = threading.RLock()
_server = None
_threads = []
_setup_settings = {}


def apply_gsi(data):
    if not isinstance(data, dict) or not isinstance(data.get("map"), dict):
        raise ValueError("Ожидается объект map")
    hero = data.get("hero") if isinstance(data.get("hero"), dict) else {}
    player = data.get("player") if isinstance(data.get("player"), dict) else {}
    data = data["map"]
    clock = data.get("clock_time", 0)
    if (
        isinstance(clock, bool)
        or not isinstance(clock, (int, float))
        or not -3600 <= clock <= 86400
    ):
        raise ValueError("Некорректное время матча")
    new_state = str(data.get("game_state", ""))
    with _service_lock:
        dota_state.hero = {
            key: hero.get(key) for key in ("name", "health_percent", "mana_percent", "alive")
        }
        dota_state.player = {key: player.get(key) for key in ("kills", "deaths", "assists", "gold")}
        dota_state.last_gsi_update = time.time()
        dota_state.match_state = new_state
        if new_state in (
            "DOTA_GAMERULES_STATE_PRE_GAME",
            "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
        ):
            if clock < dota_state.match_time_seconds - 10 or (
                new_state.endswith("PRE_GAME") and dota_state.match_started
            ):
                dota_state.last_announced_events.clear()
                dota_state.roshan_death_time = None
            dota_state.match_time_seconds = int(clock)
            if dota_state.auto_mode:
                dota_state.is_active = True
            dota_state.match_started = new_state.endswith("GAME_IN_PROGRESS")
        elif new_state in (
            "DOTA_GAMERULES_STATE_POST_GAME",
            "DOTA_GAMERULES_STATE_DISCONNECT",
        ):
            dota_state.match_started = False
            dota_state.is_active = False


class GSIDataHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.connection.settimeout(2)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 65536:
                self.send_error(413)
                return
            data = json.loads(self.rfile.read(length))
            apply_gsi(data)
        except (ValueError, TypeError, OSError):
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")


def _start_service(settings=None):
    global _server, GSI_PORT, WARNING_LEAD_TIME, DOTA_PROCESS_NAME, _threads, _setup_settings
    with _service_lock:
        if _server is not None:
            return _server.server_port
        if settings is None:
            from app_state import get_state

            settings = get_state().config_getter().get("dota_guide", {})
        GSI_PORT = int(settings.get("gsi_port", GSI_PORT))
        WARNING_LEAD_TIME = int(settings.get("warning_lead_time", WARNING_LEAD_TIME))
        DOTA_PROCESS_NAME = settings.get("process_name", DOTA_PROCESS_NAME).lower()
        dota_state.auto_mode = bool(settings.get("auto_mode", True))
        _setup_settings = dict(settings)
        try:
            server = HTTPServer(("127.0.0.1", GSI_PORT), GSIDataHandler)
        except OSError as exc:
            import errno

            if exc.errno not in (errno.EADDRINUSE, 10048):
                raise
            server = HTTPServer(("127.0.0.1", 0), GSIDataHandler)
        GSI_PORT = server.server_port
        _server = server
        _hero_alert_latches.clear()
        _service_stop.clear()
        _threads = [
            threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
            ),
            threading.Thread(target=dota_timer_loop, daemon=True),
            threading.Thread(target=monitor_dota_process, daemon=True),
        ]
        for thread in _threads:
            thread.start()
        return server.server_port


def _stop_service():
    global _server, _threads
    with _service_lock:
        server, _server = _server, None
        _service_stop.set()
        dota_state.is_active = False
        dota_state.match_started = False
        threads, _threads = _threads, []
    if server:
        server.shutdown()
        server.server_close()
    for thread in threads:
        if thread is not threading.current_thread():
            thread.join(timeout=3)


def start_service(settings=None):
    with _lifecycle_lock:
        return _start_service(settings)


def stop_service():
    with _lifecycle_lock:
        _stop_service()


def service_status():
    return {
        "running": _server is not None,
        "port": _server.server_port if _server else None,
        "active": dota_state.is_active,
        "match_seconds": dota_state.match_time_seconds,
        "last_gsi_update": dota_state.last_gsi_update,
        "connected": 0 < time.time() - dota_state.last_gsi_update < 15,
        "setup_message": dota_state.setup_message,
        "config_file": dota_state.config_file,
        "hero": dict(dota_state.hero),
        "player": dict(dota_state.player),
        "diagnosis": connection_diagnosis(),
    }


def connection_diagnosis():
    if _server is None:
        return "Гид выключен. Нажмите «Играть с гидом», чтобы подготовить связь и запустить Dota."
    if not dota_state.gsi_configured:
        return dota_state.setup_message
    if not dota_state.dota_running:
        return "Файл GSI готов. Ожидаю запуск Dota 2."
    age = time.time() - dota_state.last_gsi_update
    if not dota_state.last_gsi_update or age > 15:
        return "Dota запущена, но свежих данных нет. Нужен параметр запуска -gamestateintegration; после первого подключения перезапустите игру через «Играть с гидом»."
    return f"Данные Dota получены {max(0, int(age))} сек. назад."


@skill(
    keywords=["проверь связь с дотой", "диагностика доты"],
    description="Проверить связь Dota GSI и причину отсутствия данных",
    category="Dota 2",
)
def diagnose_dota():
    return connection_diagnosis()


@skill(
    keywords=["что делать в доте", "подсказка дота", "совет по герою"],
    description="Короткая подсказка по свежему состоянию героя Dota",
    category="Dota 2",
)
def hero_hint():
    if time.time() - dota_state.last_gsi_update > 15:
        return connection_diagnosis()
    hero = dota_state.hero
    if hero.get("alive") is False:
        return "Герой погиб. Пока ждёте возрождения, проверьте предметы и ближайшие тайминги рун."
    hints = []
    for key, threshold, text in (
        ("health_percent", 25, "Мало здоровья — проверьте возможность отойти или восстановиться."),
        ("mana_percent", 20, "Мало маны — сохраните её для необходимой способности."),
    ):
        value = hero.get(key)
        if type(value) in (int, float) and 0 <= value <= threshold:
            hints.append(text)
    return (
        " ".join(hints)
        or "«Когда руны» — ближайший тайминг; «сводка матча» — доступные данные о герое."
    )


_hero_alert_latches = set()
_hero_alert_at = 0


def hero_alert(now=None):
    """Fresh GSI only; hysteresis prevents repeated warnings while health stays low."""
    global _hero_alert_at
    now = time.time() if now is None else now
    if not _setup_settings.get("hero_alerts", True) or not dota_state.is_active:
        return ""
    if not 0 <= now - dota_state.last_gsi_update <= 15:
        return ""
    hero = dota_state.hero
    if hero.get("alive") is not True:
        _hero_alert_latches.clear()
        return ""
    for key, threshold, text in (
        ("health_percent", 25, "Мало здоровья. Проверь возможность отойти."),
        ("mana_percent", 20, "Мало маны. Побереги её для важной способности."),
    ):
        value = hero.get(key)
        if type(value) not in (int, float) or not 0 <= value <= 100:
            continue
        if value >= threshold + 15:
            _hero_alert_latches.discard(key)
        if value <= threshold and key not in _hero_alert_latches and now - _hero_alert_at >= 45:
            _hero_alert_latches.add(key)
            _hero_alert_at = now
            return text
    return ""


def dota_timer_loop():
    """Основной защищенный цикл проверки игровых таймингов."""
    while not _service_stop.is_set():
        if not dota_state.is_active:
            _service_stop.wait(0.2)
            continue
        hint = hero_alert()
        if hint:
            voice.speak(hint)
        # Диагностическая проверка связи через 2 минуты
        if not dota_state.match_started and dota_state.is_active:
            if time.time() - dota_state.activation_time > 120 and not dota_state.diagnostic_warned:
                if time.time() - dota_state.last_gsi_update > 60:
                    voice.speak(
                        "Внимание. Данные из Доты не поступают. Перезапустите игру кнопкой «Играть с гидом» в меню Крест."
                    )
                dota_state.diagnostic_warned = True

        if dota_state.match_started and (
            not dota_state.last_gsi_update or time.time() - dota_state.last_gsi_update < 15
        ):
            t = dota_state.match_time_seconds
            target = t + WARNING_LEAD_TIME

            # Используем безопасное окне триггеров (окно в 3 секунды) чтобы избежать пропуска из-за тиков
            def check_trigger(event_id: str, condition: bool, message: str):
                if condition:
                    announce(message, event_id)

            # 1. Руны Богатства (Bounty Runes) — каждые 3 минуты (180 сек)
            if target > 0 and (target % 180 in [0, 1, 2]):
                check_trigger(
                    f"bounty_{target // 180}",
                    True,
                    "Через 30 секунд появятся руны богатства.",
                )

            # 2. Водные руны (Water Runes) — 2:00 и 4:00 (120 и 240 сек)
            if target in [120, 121, 240, 241]:
                check_trigger(f"water_{target // 120}", True, "Через 30 секунд руны воды на миду.")

            # 3. Активные руны (Power Runes) — каждые 2 минуты с 6:00 (360+ сек)
            if target >= 360 and (target % 120 in [0, 1, 2]):
                check_trigger(
                    f"power_{target // 120}",
                    True,
                    "Через 30 секунд появится активная руна на реке.",
                )

            # 4. Руны Мудрости (Wisdom Runes) — каждые 7 минут (420 сек)
            if target > 0 and (target % 420 in [0, 1, 2]):
                check_trigger(
                    f"wisdom_{target // 420}",
                    True,
                    "Внимание! Через 30 секунд руны мудрости! Не забудьте забрать.",
                )

            # 5. Терзатель (Tormentor) — первая спавн на 20:00 (1200 сек)
            if target in [1200, 1201, 1202]:
                check_trigger("tormentor_spawn", True, "Через 30 секунд появятся Терзатели!")

            # 6. Цветки Лотоса (Lotus Pools) — каждые 3 минуты (180 сек)
            if target >= 180 and (target % 180 in [0, 1, 2]):
                check_trigger(f"lotus_{target // 180}", True, "Скоро появятся цветки лотоса.")

            # 7. Нейтральные предметы (Neutral Items Tiers)
            neutrals_map = {
                420: "первого",  # 7:00
                1020: "второго",  # 17:00
                1620: "третьего",  # 27:00
                2220: "четвертого",  # 37:00
                3600: "пятого",  # 60:00
            }
            for n_time, n_tier in neutrals_map.items():
                if target in [n_time, n_time + 1, n_time + 2]:
                    check_trigger(
                        f"neutrals_{n_time}",
                        True,
                        f"Через 30 секунд нейтральные предметы {n_tier} тира.",
                    )

            # 8. Смена дня и ночи — каждые 5 минут (300 сек)
            if target > 0 and (target % 300 in [0, 1, 2]):
                cycle = target // 300
                if cycle % 2 == 1:
                    check_trigger(
                        f"night_{cycle}",
                        True,
                        "Через 30 секунд наступит ночь. Обзор будет снижен.",
                    )
                else:
                    check_trigger(f"day_{cycle}", True, "Через 30 секунд наступит день.")

            # 9. Таймер Рошана и Аегиса
            if dota_state.roshan_death_time is not None:
                roshan_elapsed = t - dota_state.roshan_death_time
                # Аегис исчезает через 5 минут (300 сек) -> предупредить за 30 сек (270 сек)
                if 300 - WARNING_LEAD_TIME <= roshan_elapsed <= 302 - WARNING_LEAD_TIME:
                    check_trigger("aegis_warn", True, "Внимание! Аегис исчезнет через 30 секунд.")
                # Минимальный спавн Рошана — 8 минут (480 сек)
                elif 480 <= roshan_elapsed <= 482:
                    check_trigger("rosh_min", True, "Рошан может возродиться в любой момент!")
                # Максимальный спавн Рошана — 11 минут (660 сек)
                elif 660 <= roshan_elapsed <= 662:
                    check_trigger("rosh_max", True, "Рошан точно возродился!")
                    dota_state.roshan_death_time = None

            # Если GSI временно не присылает обновлений, инкрементируем время сами
            if dota_state.last_gsi_update == 0:
                dota_state.match_time_seconds += 1

        _service_stop.wait(1)


def monitor_dota_process():
    """Фоновый монитор запуска процесса игры."""
    if not psutil:
        return

    last_setup = float("-inf")
    while not _service_stop.is_set():
        if _setup_settings.get("auto_setup", True) and time.monotonic() - last_setup > 60:
            auto_setup_gsi_config(_setup_settings.get("steam_path", ""))
            last_setup = time.monotonic()
        dota_found = False
        try:
            for proc in psutil.process_iter(["name"]):
                if proc.info["name"] and proc.info["name"].lower() == DOTA_PROCESS_NAME:
                    dota_found = True
                    break
        except Exception:
            pass

        if dota_found and not dota_state.dota_running:
            dota_state.dota_running = True
            logger.info("Обнаружен запуск Dota 2!")

            # Обновляем конфиг при запуске на всякий случай

            if dota_state.auto_mode:
                dota_state.is_active = True
                dota_state.activation_time = time.time()

                voice.speak("Замечена Дота 2! Голосовой гид активирован и готов к игре.")
            else:
                dota_state.waiting_for_user_answer = True
                voice.speak("Зафиксировала запуск Доты 2. Включить киберспорт-гида?")

        elif not dota_found and dota_state.dota_running:
            dota_state.dota_running = False
            if dota_state.is_active:
                dota_state.is_active = False
                dota_state.match_started = False
                voice.speak("Дота закрыта. Отключаю гида.")

        _service_stop.wait(5)


@skill(
    keywords=["включай гид", "включи гид", "активируй гид"],
    description="Согласие на включение Dota-гида",
    category="Dota 2",
)
def accept_dota_guide(text: str = ""):
    start_service()
    dota_state.waiting_for_user_answer = False
    dota_state.is_active = True
    dota_state.match_started = False
    dota_state.match_time_seconds = 0
    dota_state.activation_time = time.time()
    dota_state.diagnostic_warned = False
    dota_state.last_announced_events.clear()

    voice.speak("Гид включен! Жду начала матча.")


@skill(
    keywords=["без гида", "отмена гида"],
    description="Отказ от включения Dota-гида",
    category="Dota 2",
)
def decline_dota_guide(text: str = ""):
    dota_state.waiting_for_user_answer = False
    dota_state.is_active = False
    voice.speak("Поняла. Гид отключен. Хорошей игры!")


@skill(
    keywords=["матч начался", "старт матча", "игра началась", "запусти таймер доты"],
    description="Ручной запуск отсчета таймингов матча",
    category="Dota 2",
)
def start_match_timer(text: str = ""):
    start_service()
    dota_state.is_active = True
    dota_state.match_started = True
    dota_state.match_time_seconds = 0
    dota_state.last_gsi_update = 0
    dota_state.roshan_death_time = None
    dota_state.last_announced_events.clear()

    voice.speak("Таймер запущен вручную! Отслеживаю руны и важные тайминги.")


@skill(
    keywords=["рошан", "убили рошана", "рошан упал", "засеки рошана", "минус рошан"],
    description="Засекает время смерти Рошана для отсчета Аегиса и спавна",
    category="Dota 2",
)
def roshan_down(text: str = ""):
    if not dota_state.match_started:
        voice.speak("Таймер матча ещё не запущен.")
        return

    dota_state.last_announced_events.difference_update({"aegis_warn", "rosh_min", "rosh_max"})
    dota_state.roshan_death_time = dota_state.match_time_seconds
    voice.speak("Зафиксировала смерть Рошана! Предупрежу за 30 секунд до пропадания Аегиса.")


@skill(
    keywords=["когда руны", "когда руна", "когда баунти", "когда вода"],
    description="Узнать время до ближайшей руны",
    category="Dota 2",
)
def query_runes_info(text: str = ""):
    if not dota_state.match_started:
        voice.speak("Матч еще не запущен.")
        return

    t = dota_state.match_time_seconds
    next_bounty = 180 - (t % 180)
    next_power = 120 - (t % 120) if t >= 360 else (360 - t)

    voice.speak(
        f"До рун богатства осталось {next_bounty} секунд. До активной руны {next_power} секунд."
    )


@skill(
    keywords=["когда мудрость", "когда руна мудрости"],
    description="Узнать время до появления руны мудрости",
    category="Dota 2",
)
def query_wisdom_info(text: str = ""):
    if not dota_state.match_started:
        voice.speak("Матч еще не активен.")
        return

    t = dota_state.match_time_seconds
    rem = 420 - (t % 420)
    mins = rem // 60
    secs = rem % 60

    if mins > 0:
        voice.speak(f"Руна мудрости появится через {mins} минут {secs} секунд.")
    else:
        voice.speak(f"Руна мудрости появится через {secs} секунд!")


@skill(
    keywords=["когда терзатель", "скоро терзатель"],
    description="Узнать время до появления Терзателя",
    category="Dota 2",
)
def query_tormentor_info(text: str = ""):
    if not dota_state.match_started:
        voice.speak("Матч еще не активен.")
        return

    t = dota_state.match_time_seconds
    if t < 1200:
        rem = 1200 - t
        voice.speak(f"Первый Терзатель появится через {rem // 60} минут {rem % 60} секунд.")
    else:
        voice.speak("Терзатель уже доступен на карте!")


@skill(
    keywords=["статус гида", "проверка доты", "гид работает", "че по доте"],
    description="Диагностика работоспособности интеграции с Dota 2",
    category="Dota 2",
)
def check_dota_status(text: str = ""):
    if not dota_state.is_active:
        voice.speak("Гид по Доте отключен.")
        return

    gsi_ok = (time.time() - dota_state.last_gsi_update) < 10
    if gsi_ok:
        mins = dota_state.match_time_seconds // 60
        secs = dota_state.match_time_seconds % 60
        voice.speak(f"Гид работает отлично! Игровое время: {mins} минут {secs} секунд.")
    else:
        voice.speak(
            "Гид запущен, но прямой поток данных от Dota 2 не обнаружен. Нажмите 'Матч начался' при необходимости."
        )


@skill(
    keywords=["останови гид", "выключи доту", "стоп дота"],
    description="Принудительная остановка помощника",
    category="Dota 2",
)
def stop_dota_guide(text: str = ""):
    stop_service()
    dota_state.is_active = False
    dota_state.match_started = False
    voice.speak("Гид по Доте успешно остановлен.")


@skill(
    keywords=["запусти доту с гидом", "играть в доту с гидом"],
    description="Запустить Dota через Steam с передачей состояния матча",
    category="Dota 2",
)
def launch_dota_guide():
    from app_state import get_state

    cfg = get_state().config_getter()
    start_service({**cfg["dota_guide"], "steam_path": cfg["steam"]["install_path"]})
    dota_state.auto_mode = True
    dota_state.is_active = True
    dota_state.activation_time = time.time()
    if not auto_setup_gsi_config(cfg["steam"]["install_path"]):
        return dota_state.setup_message
    if os.name != "nt":
        return "Запуск Dota с гидом доступен в Windows."
    os.startfile("steam://run/570//-gamestateintegration/")
    return "Запуск Dota с передачей состояния запрошен. Если игра уже открыта, сначала завершите её и повторите команду."


@skill(
    keywords=["сводка матча", "мой герой в доте"],
    description="Состояние героя, счёт и золото из Dota GSI",
    category="Dota 2",
)
def match_briefing():
    if time.time() - dota_state.last_gsi_update > 15:
        return "Свежих данных матча нет. Запустите игру кнопкой «Играть с гидом» и войдите в матч."
    hero, player = dota_state.hero, dota_state.player

    def number(value):
        return (
            str(int(value))
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= value <= 100000
            else "нет данных"
        )

    return (
        "Здоровье: "
        + number(hero.get("health_percent"))
        + " процентов. Мана: "
        + number(hero.get("mana_percent"))
        + " процентов. Счёт: "
        + "/".join(number(player.get(k)) for k in ("kills", "deaths", "assists"))
        + ". Золото: "
        + number(player.get("gold"))
        + "."
    )
