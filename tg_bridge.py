import asyncio
import random
import datetime
import time
import os
import sys
import json
import logging
import sqlite3
import threading

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

from auto_replies import ReplyGate

reply_gate = ReplyGate()

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
    """Безопасное чтение конфигурации с защитой от мгновенной обнуляющей перезаписи."""
    if os.path.exists(CONFIG_FILE) and os.path.getsize(CONFIG_FILE) > 0:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                return json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            pass
        except Exception as e:
            logger.error(f"Ошибка чтения config.json: {e}")
    return {}


def get_owner_name() -> str:
    cfg = load_app_config()
    return cfg.get("owner_name") or "Владелец"


async def get_local_status() -> dict:
    if os.path.exists(STATUS_FILE) and os.path.getsize(STATUS_FILE) > 0:
        try:
            with open(STATUS_FILE, "r", encoding="utf-8-sig") as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {}


async def status_updater(client):
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
            logger.info(f"Обновлен Telegram Bio: {new_bio}")
        except FloodWait as e:
            logger.warning(f"FloodWait от Telegram: ждем {e.value} секунд")
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.debug(f"Ошибка обновления статуса Bio: {e}")

        await asyncio.sleep(300)


async def auto_delete_message(message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


async def track_outgoing(client, message):
    chat_id = message.chat.id
    last_outgoing[chat_id] = time.time()
    reply_gate.outgoing(chat_id)
    if chat_id in spam_control:
        del spam_control[chat_id]


async def auto_reply(client, message):
    if not message.from_user:
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    current_time = time.time()
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

    from configuration import ConfigManager

    if not reply_gate.allow(chat_id, ConfigManager().snapshot()["auto_replies"], is_urgent):
        return

    spam_control[user_id] = current_time
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

        from auto_replies import render_reply
        from configuration import ConfigManager
        from pyrogram.enums import ParseMode

        reply_cfg = ConfigManager().snapshot()["auto_replies"]
        if not reply_cfg["enabled"]:
            return
        final_text = render_reply(
            owner_name,
            status_data,
            reply_cfg,
            bool(message.voice or message.video_note),
            is_urgent,
            is_night,
        )
        sent_msg = await message.reply(final_text, parse_mode=ParseMode.DISABLED)
        if reply_cfg["delete_seconds"]:
            asyncio.create_task(auto_delete_message(sent_msg, reply_cfg["delete_seconds"]))

        if hasattr(client, "notifications_queue") and client.notifications_queue:
            if not is_night:
                client.notifications_queue.put(sender_name)

    except Exception as e:
        logger.error(f"Ошибка отправки автоответа: {e}")


class AuthManager:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.client = None
        self.phone_code_hash = None
        self.phone = None
        self._lock = threading.RLock()
        self._auth_operation_lock = threading.Lock()

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
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_file)

    def _remove_state(self):
        try:
            os.remove(self.state_file)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"Не удалось удалить временное состояние авторизации: {e}")

    async def _disconnect_client(self):
        if self.client is not None:
            try:
                if getattr(self.client, "is_connected", False):
                    await self.client.disconnect()
            except Exception as e:
                logger.debug(f"Ошибка отключения auth-клиента: {e}")
            finally:
                self.client = None

    def send_code(self, api_id: str, api_hash: str, phone: str) -> str:
        if not PYROGRAM_AVAILABLE:
            raise RuntimeError("Pyrogram не установлен. Установите pyrogram и tgcrypto.")

        with self._auth_operation_lock:
            self._ensure_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._async_send_code(api_id, api_hash, phone), self.loop
            )
            try:
                return future.result(timeout=90)
            except TimeoutError:
                future.cancel()
                raise TimeoutError("Telegram слишком долго отвечает на запрос кода (90 сек).")

    async def _async_send_code(self, api_id: str, api_hash: str, phone: str) -> str:
        api_id_int, api_hash_clean = self._validate_api(api_id, api_hash)
        phone_clean = self._normalize_phone(phone)

        logger.info("Запрашиваем код Telegram для %s", phone_clean)
        self._write_state({"status": "sending_code", "phone": phone_clean})

        try:
            await self._disconnect_client()

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

            self._write_state(
                {
                    "status": "code_sent",
                    "api_id": api_id_int,
                    "api_hash": api_hash_clean,
                    "phone": phone_clean,
                    "phone_code_hash": self.phone_code_hash,
                    "created_at": time.time(),
                }
            )

            logger.info(
                "Запрос кода Telegram принят. Код обычно приходит в Telegram-приложение, "
                "а способ доставки выбирает Telegram."
            )
            return "Запрос кода авторизации успешно отправлен Telegram."

        except Exception:
            logger.exception("ОШИБКА ПРИ ЗАПРОСЕ КОДА TELEGRAM")
            await self._disconnect_client()
            self._remove_state()
            self.phone_code_hash = None
            raise

    def login(self, code: str, password_callback=None) -> str:
        if not PYROGRAM_AVAILABLE:
            raise RuntimeError("Pyrogram не установлен. Установите pyrogram и tgcrypto.")

        with self._auth_operation_lock:
            self._ensure_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._async_login(code, password_callback), self.loop
            )
            try:
                return future.result(timeout=180)
            except TimeoutError:
                future.cancel()
                raise TimeoutError("Авторизация Telegram слишком долго выполняется (180 сек).")

    async def _async_login(self, code: str, password_callback=None) -> str:
        code_clean = str(code or "").strip().replace(" ", "")
        if not code_clean:
            raise ValueError("Код Telegram не введён!")

        data = None
        try:
            if not os.path.exists(self.state_file):
                raise ValueError(
                    "Сначала нажмите «Отправить код» и дождитесь подтверждения запроса."
                )

            with open(self.state_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)

            if data.get("status") != "code_sent":
                raise ValueError("Код ещё не был запрошен. Сначала нажмите «Отправить код».")

            api_id = int(data["api_id"])
            api_hash = str(data["api_hash"]).strip()
            self.phone = data["phone"]
            self.phone_code_hash = data["phone_code_hash"]

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

        except Exception:
            logger.exception("ОШИБКА ПРИ АВТОРИЗАЦИИ TELEGRAM")
            raise
        finally:
            await self._disconnect_client()
            self._remove_state()
            self.phone_code_hash = None


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

        if os.path.exists(state_file):
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
            stop_event = asyncio.Event()
            stop_reason = {"value": "STOPPED"}

            async def watch_auth_state():
                while True:
                    await asyncio.sleep(0.5)
                    if os.path.exists(state_file):
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

                updater_task = asyncio.create_task(status_updater(app))
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
                logger.exception("Ошибка SQLite Telegram")
                return "ERROR"
            except Exception:
                logger.exception("Ошибка во время работы Telegram юзербота")
                return "ERROR"
            finally:
                for task in (updater_task, auth_watch_task):
                    if task and not task.done():
                        task.cancel()
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
                    logger.warning(f"Не удалось удалить недействительную session: {e}")
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
        except Exception:
            logger.exception("Критическая ошибка цикла Telegram")
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
