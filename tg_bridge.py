import asyncio
import random
import datetime
import time
import os
import sys
import logging
import sqlite3
import threading
import uuid

from storage import atomic_write_json, read_json

try:
    from pyrogram import Client, filters, enums
    from pyrogram.handlers import MessageHandler
    from pyrogram.errors import (
        AuthKeyUnregistered,
        SessionRevoked,
        UserDeactivated,
        Unauthorized,
        FloodWait,
        SessionPasswordNeeded,
        PhoneCodeExpired,
        PhoneCodeInvalid,
        PasswordHashInvalid,
    )

    PYROGRAM_AVAILABLE = True
except ImportError:
    PYROGRAM_AVAILABLE = False

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from configuration import CONFIG_PATH

CONFIG_FILE = str(CONFIG_PATH)
from app_paths import data_dir

BASE_DIR = str(data_dir())

STATUS_FILE = os.path.join(BASE_DIR, "current_status.json")
SESSION_NAME = "krest_user_session"
SESSION_PATH = os.path.join(BASE_DIR, f"{SESSION_NAME}.session")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TG-BRIDGE]: %(message)s")
logger = logging.getLogger("TGBridge")

spam_control = {}
last_outgoing = {}

QUOTES = [
    "Я курю эври дей...",
    "i can't fix that... Maybe again?",
    "03:33",
    "Мальборо Голд - я Ван Гог",
    "Шипучка - Твоя Мать Горячая Штучка",
    "На моей руке кресты, на кровати две пизды",
    "SNOW WHITE MARBLE",
    "Глаза красные, но не от слез",
    "yeyo!",
    "я типо сигарета",
    "Моя ассистентка Крест следит за тобой",
    "Слишком много мыслей, слишком мало смысла",
    "Я умру в ахуенных вещах",
    "Ты ищешь меня там, где меня больше нет",
    "stalk ur socials",
    "I can’t fix my life, but I can fix this code",
    "#TWOPHONES xd",
    "Музыка на максимум, чувства на минимум.",
    "Фура переехала мои мозги как тесто",
    "Подонок",
    "zzz",
    "Нахуй SLEEP??",
    "error 404: feelings not found",
    "Ctrl+Z не работает в реальной жизни.",
    "System.NullReferenceException: Soul not found",
    "03:33??? FFM<<<",
    "dead inside, coding outside.",
    "digital ghost.",
    "offline in real life.",
]


def load_app_config() -> dict:
    """Use the same vault hydration and validation as the desktop application."""
    from configuration import ConfigManager

    try:
        return ConfigManager(CONFIG_FILE).snapshot()
    except (OSError, ValueError) as exc:
        logger.error("Ошибка чтения настроек Telegram: %s", type(exc).__name__)
        return {}


def get_owner_name() -> str:
    cfg = load_app_config()
    return cfg.get("owner_name") or "Владелец"


async def get_local_status() -> dict:
    from storage import read_json

    try:
        value = read_json(STATUS_FILE)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def session_transport_state(client):
    """Read SDK events only; Client.is_connected is not a reconnect signal."""
    session = getattr(client, "session", None)
    # Pyrogram 2.0.106 uses is_started; some compatible SDKs expose is_connected.
    for name in ("is_connected", "is_started"):
        event = getattr(session, name, None)
        probe = getattr(event, "is_set", None)
        if callable(probe):
            try:
                value = probe()
            except Exception:
                return None
            if isinstance(value, bool):
                return value
    return None


class TelegramHealth:
    def __init__(self, client):
        self.client = client
        self.state = "ready"  # The startup get_me call has just succeeded.

    def _status(self, state):
        if self.state == state:
            return
        from bridge_sdk import emit

        self.state = state
        text = (
            "Telegram переподключается; SDK ещё не восстановил сессию"
            if state == "reconnecting"
            else "Связь Telegram пока не подтверждена; ожидается очередной ответ API"
        )
        emit("status", text, health=state)

    def sample(self):
        connected = session_transport_state(self.client)
        if connected is False:
            self._status("reconnecting")
        elif connected is None or self.state == "reconnecting":
            self._status("unknown")

    def unconfirmed(self):
        self._status("reconnecting" if session_transport_state(self.client) is False else "unknown")

    def confirmed(self):
        # An SDK Event.set alone is insufficient; called only after an existing API call.
        if session_transport_state(self.client) is not True:
            self.unconfirmed()
            return
        if self.state != "ready":
            from bridge_sdk import emit

            self.state = "ready"
            emit("ready", "Telegram снова подтвердил связь ответом API")

    async def watch(self):
        while True:
            self.sample()
            await asyncio.sleep(1)


async def status_updater(client, health=None):
    while True:
        try:
            status_data = await get_local_status()
            is_playing = status_data.get("is_playing", False)
            game = status_data.get("game", "")

            if is_playing and game:
                new_bio = f"Занят в {game} 🎮 | КРЕСТ Online"
            else:
                new_bio = random.choice(QUOTES)

            if len(new_bio) > 70:
                new_bio = new_bio[:67] + "..."

            await client.update_profile(bio=new_bio)
            if health is not None:
                health.confirmed()
            logger.info(f"Обновлен Telegram Bio: {new_bio}")
        except FloodWait as e:
            if health is not None:
                health.unconfirmed()
            logger.warning(f"FloodWait от Telegram: ждем {e.value} секунд")
            await asyncio.sleep(e.value)
        except Exception as e:
            if health is not None:
                health.unconfirmed()
            logger.debug(f"Ошибка обновления статуса Bio: {type(e).__name__}")

        await asyncio.sleep(300)


async def auto_delete_message(message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


async def track_outgoing(client, message):
    chat_id = message.chat.id
    last_outgoing[chat_id] = time.monotonic()
    if chat_id in spam_control:
        del spam_control[chat_id]


async def auto_reply(client, message):
    if not message.from_user:
        return

    chat_id = message.chat.id
    current_time = time.monotonic()
    owner_name = get_owner_name()

    if chat_id in last_outgoing and (current_time - last_outgoing[chat_id] < 300):
        return

    keys_to_del = [k for k, v in spam_control.items() if current_time - v > 3600]
    for k in keys_to_del:
        del spam_control[k]

    out_to_del = [k for k, v in last_outgoing.items() if current_time - v > 3600]
    for k in out_to_del:
        del last_outgoing[k]

    text = message.text.lower() if message.text else ""
    is_urgent = any(word in text for word in ["срочно", "важно", "горит", "ответь"])

    if chat_id in spam_control and current_time - spam_control[chat_id] < (
        60 if is_urgent else 300
    ):
        return

    spam_control[chat_id] = current_time
    sender_name = message.from_user.first_name or "Пользователь"
    logger.info(f"Входящее сообщение от {sender_name}")

    try:
        await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)

        status_data = await get_local_status()
        is_playing = status_data.get("is_playing", False)
        activity_type = status_data.get("activity_type", "IDLE")
        full_msg = status_data.get("full_message", "")

        current_hour = datetime.datetime.now().hour
        is_night = 1 <= current_hour < 8

        await asyncio.sleep(random.uniform(1.2, 2.5))

        if message.voice or message.video_note:
            reply_text = (
                f"💠 **KREST: Голосовое сообщение**\n"
                f"───\n"
                f"{owner_name} сейчас на движе / занят.\n"
                f"Модули распознавания голоса отключены. Напишите текстом."
            )
        elif is_night:
            reply_text = (
                f"💠 **KREST: Ночной режим**\n"
                f"───\n"
                f"● `Статус:` Восстановление ресурсов 🌙\n"
                f"───\n"
                f"{owner_name} может отдыхать. Сейчас действует ночной режим автоответчика."
            )
        elif is_playing or activity_type in ["GAME", "WORK", "REST", "SOCIAL"]:
            context_msg = full_msg if full_msg else f"{owner_name} сейчас занят делами."
            if is_urgent:
                context_msg = (
                    f"⚠️ Приоритетный сигнал!\n{context_msg}\nСообщение содержит признак срочности."
                )
            reply_text = f"💠 **KREST: Автоответчик**\n───\n{context_msg}"
        else:
            reply_text = (
                f"💠 **KREST: Автономный режим**\n"
                f"───\n"
                f"● `Статус:` В движении / Отошел\n"
                f"───\n"
                f"{owner_name} отошел от ПК. Ваше сообщение останется в этом чате."
            )

        footer = "\n\nАвтоответ КРЕСТ. Попробую удалить его через 5 минут."
        # Telegram counts UTF-16 units; disable parsing for untrusted status text.
        body = reply_text.replace("**", "").replace("`", "")
        body = body.encode("utf-16-le")[: (4000 - len(footer)) * 2].decode(
            "utf-16-le", errors="ignore"
        )
        final_text = body + footer
        if last_outgoing.get(chat_id, float("-inf")) >= current_time:
            return
        sent_msg = await message.reply(final_text, parse_mode=enums.ParseMode.DISABLED)

        asyncio.create_task(auto_delete_message(sent_msg, 300))

        if getattr(client, "notifications_queue", None) is not None:
            if not is_night:
                client.notifications_queue.put_nowait(sender_name)

    except Exception as e:
        logger.error(f"Ошибка отправки автоответа: {type(e).__name__}")


AUTH_STATE_TTL = 600


def auth_state_active(path):
    """The bridge only needs a short-lived lease, never authentication secrets."""
    import psutil

    try:
        if os.path.getsize(path) > 4096:
            return False
        data = read_json(path)

        return (
            isinstance(data, dict)
            and data.get("status") in ("sending_code", "code_sent", "signing_in")
            and isinstance(data.get("attempt"), str)
            and 0 <= time.time() - float(data.get("created_at", 0)) < AUTH_STATE_TTL
            and abs(psutil.Process(int(data["pid"])).create_time() - float(data["process_started"]))
            < 1
        )
    except psutil.AccessDenied:
        return True
    except (OSError, ValueError, TypeError, KeyError, psutil.NoSuchProcess):
        return False


class AuthManager:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.client = None
        self.phone_code_hash = None
        self.phone = None
        self._lock = threading.RLock()
        self._auth_operation_lock = threading.Lock()
        self._async_operation_lock = asyncio.Lock()
        self._pending_auth = None
        self._state_id = None
        self._expiry_task = None
        self._operation_done = None
        self._session_lock = None

    @property
    def state_file(self):
        return os.path.join(BASE_DIR, "tg_auth_temp.json")

    def _ensure_loop(self):
        with self._lock:
            if self.thread is None or not self.thread.is_alive():
                self.loop = asyncio.new_event_loop()
                self.thread = threading.Thread(
                    target=self._run_loop,
                    name="KrestTelegramAuthLoop",
                    daemon=True,
                )
                self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        phone_clean = str(phone or "").strip().replace(" ", "").replace("-", "")
        if not phone_clean:
            raise ValueError("Номер телефона не указан!")

        if phone_clean.startswith("8") and len(phone_clean) == 11:
            phone_clean = "+7" + phone_clean[1:]
        elif not phone_clean.startswith("+"):
            phone_clean = "+" + phone_clean

        if len(phone_clean) < 8:
            raise ValueError("Некорректный номер телефона!")
        return phone_clean

    @staticmethod
    def _validate_api(api_id, api_hash):
        try:
            api_id_int = int(str(api_id).strip())
        except (TypeError, ValueError):
            raise ValueError("Telegram API ID должен быть числом!")

        api_hash_clean = str(api_hash or "").strip()
        if not api_hash_clean:
            raise ValueError("Telegram API Hash не указан!")
        return api_id_int, api_hash_clean

    def _write_state(self, data: dict):
        import psutil

        atomic_write_json(
            self.state_file,
            {
                "status": data["status"],
                "attempt": self._state_id,
                "pid": os.getpid(),
                "process_started": psutil.Process().create_time(),
                "created_at": time.time(),
            },
        )

    def _remove_state(self):
        try:
            data = read_json(self.state_file)
            if isinstance(data, dict) and data.get("attempt") == self._state_id:
                os.remove(self.state_file)
        except FileNotFoundError:
            pass
        except (ValueError, TypeError):
            pass
        except OSError as e:
            logger.warning(
                f"Не удалось удалить временное состояние авторизации: {type(e).__name__}"
            )

    async def _disconnect_client(self):
        if self.client is not None:
            client = self.client
            try:
                if getattr(client, "is_connected", False):
                    try:
                        await client.disconnect()
                        return
                    except Exception as exc:
                        logger.debug("Ошибка отключения auth-клиента: %s", type(exc).__name__)
                # Pyrogram sets is_connected only after session.start(). A cancelled
                # connect() can already own a socket and open SQLite storage.
                try:
                    session = getattr(client, "session", None)
                    if session is not None:
                        await session.stop()
                except Exception as exc:
                    logger.debug("Ошибка остановки auth-сессии: %s", type(exc).__name__)
                finally:
                    storage = getattr(client, "storage", None)
                    if storage is not None:
                        try:
                            await storage.close()
                        except Exception as exc:
                            logger.debug("Ошибка закрытия auth-хранилища: %s", type(exc).__name__)
            finally:
                self.client = None

    async def _clear_auth(self):
        if self._expiry_task is not None and self._expiry_task is not asyncio.current_task():
            self._expiry_task.cancel()
        self._expiry_task = None
        try:
            await self._disconnect_client()
        finally:
            self._remove_state()
            self._pending_auth = None
            self.phone_code_hash = None
            self.phone = None
            self._state_id = None
            if self._session_lock is not None:
                self._session_lock.close()
                self._session_lock = None

    async def _expire_auth(self):
        await asyncio.sleep(AUTH_STATE_TTL)
        async with self._async_operation_lock:
            await self._clear_auth()

    def _run_operation(self, factory, timeout, timeout_message):
        with self._auth_operation_lock:
            if self._operation_done is not None and not self._operation_done.is_set():
                raise RuntimeError(
                    "Предыдущая авторизация ещё освобождает соединение. Повторите позже."
                )
            self._ensure_loop()
            done = threading.Event()
            self._operation_done = done

            async def run():
                try:
                    return await factory()
                finally:
                    done.set()

            future = asyncio.run_coroutine_threadsafe(run(), self.loop)
            try:
                return future.result(timeout=timeout)
            except TimeoutError:
                future.cancel()
                # A cancelled concurrent Future completes before coroutine cleanup does.
                done.wait(5)
                raise TimeoutError(timeout_message)

    def send_code(self, api_id: str, api_hash: str, phone: str) -> str:
        if not PYROGRAM_AVAILABLE:
            raise RuntimeError("Pyrogram не установлен. Установите pyrogram и tgcrypto.")

        return self._run_operation(
            lambda: self._async_send_code(api_id, api_hash, phone),
            90,
            "Telegram слишком долго отвечает на запрос кода (90 сек).",
        )

    async def _async_send_code(self, api_id: str, api_hash: str, phone: str) -> str:
        async with self._async_operation_lock:
            return await self._send_code(api_id, api_hash, phone)

    async def _send_code(self, api_id, api_hash, phone):
        api_id_int, api_hash_clean = self._validate_api(api_id, api_hash)
        phone_clean = self._normalize_phone(phone)

        if self._state_id is None and auth_state_active(self.state_file):
            raise RuntimeError("Авторизация Telegram уже открыта в другом окне КРЕСТ.")
        await self._clear_auth()
        from process_lock import ProcessLock

        session_lock = ProcessLock(os.path.join(BASE_DIR, ".tg_auth.lock"))
        if not session_lock.acquire():
            raise RuntimeError("Авторизация Telegram уже открыта в другом окне КРЕСТ.")
        self._session_lock = session_lock
        self._state_id = uuid.uuid4().hex
        logger.info("Запрашиваем код Telegram")

        try:
            self._write_state({"status": "sending_code"})

            probe = Client(
                SESSION_NAME,
                api_id=api_id_int,
                api_hash=api_hash_clean,
                workdir=BASE_DIR,
                device_model="PC",
                system_version="Windows 11",
                app_version="4.14.9 x64",
                lang_code="ru",
            )
            self.client = probe

            try:
                await self.client.connect()
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower():
                    raise RuntimeError(
                        "Файл Telegram-сессии занят работающим юзерботом. "
                        "Подождите несколько секунд и нажмите «Отправить код» ещё раз."
                    ) from e
                raise

            try:
                me = await self.client.get_me()
            except Exception:
                me = None

            if me is not None:
                raise ValueError(
                    "Этот Telegram-аккаунт уже авторизован в КРЕСТ. "
                    "Новый код не требуется. Перезапустите программу, если bridge ещё не активен."
                )

            sent_code_info = await self.client.send_code(phone_clean)
            self.phone_code_hash = sent_code_info.phone_code_hash
            self.phone = phone_clean

            self._pending_auth = {"api_id": api_id_int, "api_hash": api_hash_clean}
            self._write_state({"status": "code_sent"})
            self._expiry_task = asyncio.create_task(self._expire_auth())

            logger.info(
                "Запрос кода Telegram принят. Код обычно приходит в Telegram-приложение, "
                "а способ доставки выбирает Telegram."
            )
            return "Запрос кода авторизации успешно отправлен Telegram."

        except (Exception, asyncio.CancelledError) as exc:
            logger.error("Ошибка запроса кода Telegram: %s", type(exc).__name__)
            await self._clear_auth()
            raise

    def login(self, code: str, password_callback=None) -> str:
        if not PYROGRAM_AVAILABLE:
            raise RuntimeError("Pyrogram не установлен. Установите pyrogram и tgcrypto.")

        return self._run_operation(
            lambda: self._async_login(code, password_callback),
            180,
            "Авторизация Telegram слишком долго выполняется (180 сек).",
        )

    async def _async_login(self, code: str, password_callback=None) -> str:
        async with self._async_operation_lock:
            return await self._login(code, password_callback)

    async def _login(self, code, password_callback):
        code_clean = str(code or "").strip().replace(" ", "")
        if not code_clean:
            raise ValueError("Код Telegram не введён!")

        try:
            if not self._pending_auth or not self.phone_code_hash:
                raise ValueError(
                    "Нажмите «Получить код» и дождитесь подтверждения запроса. "
                    "После перезапуска КРЕСТ незавершённую авторизацию нужно начать заново."
                )
            if self._expiry_task is not None:
                self._expiry_task.cancel()
                self._expiry_task = None
            self._write_state({"status": "signing_in"})
            api_id = self._pending_auth["api_id"]
            api_hash = self._pending_auth["api_hash"]

            # Переиспользуем то же самое подключение, чтобы Telegram не сбрасывал хэш кода
            if self.client is None or not getattr(self.client, "is_connected", False):
                self.client = Client(
                    SESSION_NAME,
                    api_id=api_id,
                    api_hash=api_hash,
                    workdir=BASE_DIR,
                    device_model="PC",
                    system_version="Windows 11",
                    app_version="4.14.9 x64",
                    lang_code="ru",
                )
                try:
                    await self.client.connect()
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower():
                        raise RuntimeError(
                            "Файл Telegram-сессии занят другим процессом. "
                            "Закройте второй экземпляр КРЕСТ и повторите авторизацию."
                        ) from e
                    raise

            try:
                await self.client.sign_in(
                    self.phone,
                    self.phone_code_hash,
                    code_clean,
                )
            except SessionPasswordNeeded:
                if password_callback is None:
                    raise ValueError(
                        "На аккаунте включён облачный 2FA. Введите пароль 2FA в окне КРЕСТ."
                    )

                pwd = password_callback()
                if not pwd:
                    raise ValueError("Облачный 2FA пароль не введён!")

                try:
                    await self.client.check_password(str(pwd))
                except PasswordHashInvalid as e:
                    raise ValueError("Неверный пароль облачного 2FA!") from e
            except PhoneCodeExpired as e:
                raise ValueError("Срок действия кода истёк. Запросите новый код.") from e
            except PhoneCodeInvalid as e:
                raise ValueError("Введён неверный код подтверждения Telegram.") from e

            me = await self.client.get_me()
            if me is None:
                raise RuntimeError("Telegram не вернул данные авторизованного пользователя.")

            name = " ".join(x for x in [me.first_name, me.last_name] if x) or "Пользователь"
            user_info = f"{name} (@{me.username})" if me.username else name
            logger.info("Telegram успешно авторизован: %s", user_info)
            return user_info

        except Exception as exc:
            logger.error("Ошибка авторизации Telegram: %s", type(exc).__name__)
            raise
        finally:
            await self._clear_auth()


_auth_mgr = AuthManager()


def send_code(api_id: str, api_hash: str, phone: str) -> str:
    return _auth_mgr.send_code(api_id, api_hash, phone)


def login(code: str, password_callback=None) -> str:
    return _auth_mgr.login(code, password_callback=password_callback)


def start_tg_bridge(notifications_queue=None):
    if not PYROGRAM_AVAILABLE:
        logger.error("Pyrogram не найден в системе. Установите pyrogram и tgcrypto.")
        return

    state_file = os.path.join(BASE_DIR, "tg_auth_temp.json")

    while True:
        cfg = load_app_config()
        api_keys = cfg.get("api_keys") or {}
        api_id = api_keys.get("telegram_api_id")
        api_hash = api_keys.get("telegram_api_hash")

        if not api_id or not api_hash:
            logger.info("Ожидание настройки Telegram API ключей в GUI...")
            time.sleep(3)
            continue

        if auth_state_active(state_file):
            time.sleep(1)
            continue

        if not os.path.exists(SESSION_PATH):
            logger.info("Сессия Telegram не найдена. Ожидание авторизации через GUI...")
            time.sleep(3)
            continue

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        app = Client(
            SESSION_NAME,
            api_id=int(api_id),
            api_hash=str(api_hash).strip(),
            workdir=BASE_DIR,
            device_model="PC",
            system_version="Windows 11",
            app_version="4.14.9 x64",
            lang_code="ru",
        )
        app.notifications_queue = notifications_queue
        app.add_handler(
            MessageHandler(
                auto_reply,
                filters.private & ~filters.me & ~filters.bot,
            )
        )
        app.add_handler(
            MessageHandler(
                track_outgoing,
                filters.private & filters.me,
            )
        )

        async def run_bot():
            updater_task = None
            auth_watch_task = None
            health_task = None
            stop_event = asyncio.Event()
            stop_reason = {"value": "STOPPED"}

            async def watch_auth_state():
                while True:
                    await asyncio.sleep(0.5)
                    if auth_state_active(state_file):
                        logger.info("GUI начала авторизацию Telegram — освобождаем session.")
                        stop_reason["value"] = "AUTH_REQUIRED"
                        stop_event.set()
                        return

            try:
                logger.info("Запуск Telegram Юзербота...")
                await app.start()
                me = await app.get_me()
                if me is None:
                    stop_reason["value"] = "UNAUTHORIZED"
                    return stop_reason["value"]

                from bridge_sdk import emit

                emit("ready", "Telegram подтвердил авторизацию аккаунта")

                logger.info(
                    "Юзербот активен на аккаунте: %s (@%s)",
                    me.first_name,
                    me.username or "нет username",
                )

                health = TelegramHealth(app)
                health_task = asyncio.create_task(health.watch())
                updater_task = asyncio.create_task(status_updater(app, health))
                auth_watch_task = asyncio.create_task(watch_auth_state())
                await stop_event.wait()
                return stop_reason["value"]

            except (AuthKeyUnregistered, SessionRevoked, UserDeactivated, Unauthorized):
                logger.warning("Сессия Telegram недействительна или сброшена.")
                return "UNAUTHORIZED"
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower():
                    logger.warning(
                        "База данных Telegram-сессии заблокирована. Повтор через 3 сек..."
                    )
                    return "LOCKED"
                logger.error("Ошибка SQLite Telegram: %s", type(e).__name__)
                return "ERROR"
            except Exception as exc:
                logger.error("Ошибка Telegram юзербота: %s", type(exc).__name__)
                return "ERROR"
            finally:
                tasks = [task for task in (updater_task, auth_watch_task, health_task) if task]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await app.stop()
                except Exception:
                    pass

        try:
            status = loop.run_until_complete(run_bot())

            if status == "UNAUTHORIZED":
                try:
                    if os.path.exists(SESSION_PATH):
                        os.remove(SESSION_PATH)
                except OSError as e:
                    logger.warning(
                        f"Не удалось удалить недействительную session: {type(e).__name__}"
                    )
                time.sleep(3)
                continue

            if status == "LOCKED":
                time.sleep(3)
                continue

            if status == "AUTH_REQUIRED":
                time.sleep(1)
                continue

            if status == "STOPPED":
                break

            time.sleep(3)

        except KeyboardInterrupt:
            logger.info("Остановка юзербота по запросу.")
            break
        except Exception as exc:
            logger.error("Ошибка цикла Telegram: %s", type(exc).__name__)
            time.sleep(3)
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop=loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            finally:
                try:
                    loop.close()
                except Exception:
                    pass


class TgBridge:
    def send_code(self, api_id: str, api_hash: str, phone: str) -> str:
        return send_code(api_id, api_hash, phone)

    def login(self, code: str, password_callback=None) -> str:
        return login(code, password_callback=password_callback)

    def start_tg_bridge(self, notifications_queue=None):
        return start_tg_bridge(notifications_queue=notifications_queue)


if __name__ == "__main__":
    from licensing import require_premium
    from configuration import ConfigManager

    require_premium(ConfigManager().snapshot())
    from notifications import NotificationQueue

    start_tg_bridge(NotificationQueue())
