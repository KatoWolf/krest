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
import re

# Pyrogram's synchronous wrappers inspect the current loop during import, even
# when every client operation is async. GUI workers have no loop. Supply an idle
# bootstrap loop only for that import; clients still belong to their running loop.
_import_loop = None
try:
    asyncio.get_event_loop()
except RuntimeError:
    _import_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_import_loop)

try:
    from pyrogram import Client, filters, enums, raw, types
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
        ApiIdInvalid,
        PhoneNumberInvalid,
    )

    PYROGRAM_AVAILABLE = True
except ImportError:
    PYROGRAM_AVAILABLE = False
finally:
    if _import_loop is not None:
        asyncio.set_event_loop(None)
        _import_loop.close()

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

from auto_replies import ReplyGate, urgent

reply_gate = ReplyGate()


def load_app_config() -> dict:
    """Use the shared loader, including keys stored in Windows Credential Manager."""
    from configuration import ConfigManager

    return ConfigManager(CONFIG_FILE).snapshot()


def get_owner_name() -> str:
    cfg = load_app_config()
    return cfg.get("owner_name") or "Владелец"


async def get_local_status() -> dict:
    from bot_activity import local_activity

    return local_activity(BASE_DIR)


async def status_updater(client):
    last_bio = None
    while True:
        try:
            settings = load_app_config()["telegram_user"]
            if not settings["update_bio"]:
                await asyncio.sleep(30)
                continue
            from bot_activity import status_text

            new_bio = status_text(load_app_config(), "telegram_user", await get_local_status())

            if len(new_bio) > 70:
                new_bio = new_bio[:67] + "..."

            if new_bio != last_bio:
                await client.update_profile(bio=new_bio)
                last_bio = new_bio
                logger.info("Описание Telegram обновлено")
        except FloodWait as e:
            logger.warning(f"FloodWait от Telegram: ждем {e.value} секунд")
            reply_gate.blocked_until = max(reply_gate.blocked_until, time.monotonic() + e.value)
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
    if not reply_gate.own_echo(chat_id, message.text or "", getattr(message, "id", None)):
        reply_gate.outgoing(chat_id)


async def auto_reply(client, message, deferred=False):
    if not message.from_user:
        return
    chat_id = message.chat.id
    if not deferred and not reply_gate.incoming(chat_id, getattr(message, "id", None)):
        return
    sender_name = message.from_user.first_name or "контакта"
    text = message.text.lower() if message.text else ""
    is_urgent = urgent(text)
    from bridge_sdk import emit

    if not deferred:
        emit(
            "notification",
            "Telegram: новое сообщение от " + str(sender_name)[:80],
            priority="high" if is_urgent else "normal",
            notice_kind="message",
            target={
                "chat": str(chat_id),
                "sender": str(sender_name)[:80],
                "message": str(getattr(message, "id", "")),
            },
        )
    cfg = load_app_config()
    from bot_activity import chat_muted

    if chat_muted(BASE_DIR, "tg_bridge.py", chat_id):
        reply_gate.pending.pop(str(chat_id), None)
        return
    from bot_activity import reply_options

    reply_cfg = reply_options(cfg, "telegram_user")
    if not cfg["telegram_user"]["auto_reply"]:
        return
    if not reply_gate.allow(chat_id, reply_cfg, is_urgent):
        reply_gate.postpone(chat_id, message, reply_cfg, is_urgent)
        return
    reply_gate.pending.pop(str(chat_id), None)

    try:
        try:
            await asyncio.wait_for(
                client.send_chat_action(message.chat.id, enums.ChatAction.TYPING), 1
            )
        except Exception:
            pass  # Typing is cosmetic; it must never prevent the reply itself.

        status_data = await get_local_status()

        current_hour = datetime.datetime.now().hour
        is_night = 1 <= current_hour < 8

        # No cosmetic sleep: the send is already rate limited per contact.

        from auto_replies import render_reply
        from pyrogram.enums import ParseMode

        current_cfg = load_app_config()
        reply_cfg = reply_options(current_cfg, "telegram_user")
        if (
            not reply_cfg["enabled"]
            or not current_cfg["telegram_user"]["auto_reply"]
            or time.time() < reply_cfg["paused_until"]
            or (reply_cfg["urgent_only"] and not is_urgent)
            or reply_gate.clock() - reply_gate.manual.get(str(chat_id), -1e9) < 300
        ):
            reply_gate.failed(chat_id)
            return
        final_text = render_reply(
            current_cfg["owner_name"],
            status_data,
            reply_gate.reply_settings(
                chat_id,
                reply_cfg,
                current_cfg["profile"]["personality"],
                status_data.get("is_playing", False),
            ),
            bool(message.voice or message.video_note),
            is_urgent,
            is_night,
            personality=current_cfg["profile"]["personality"],
        )
        reply_gate.sending(chat_id, final_text)
        sent_msg = await message.reply(final_text, parse_mode=ParseMode.DISABLED)
        reply_gate.delivered(chat_id, sent_msg.id)
        reply_gate.advance(
            chat_id,
            reply_cfg,
            current_cfg["profile"]["personality"],
            status_data.get("is_playing", False),
        )
        reply_gate.last_reason = "Автоответ доставлен"
        if reply_cfg["delete_seconds"]:
            asyncio.create_task(auto_delete_message(sent_msg, reply_cfg["delete_seconds"]))

    except FloodWait as exc:
        # Release the per-contact reservation, respect the platform-wide limit.
        reply_gate.failed(chat_id)
        reply_gate.blocked_until = time.monotonic() + max(1, exc.value)
        logger.warning("Telegram просит подождать %s секунд перед автоответом", exc.value)
    except Exception as e:
        reply_gate.failed(chat_id)
        logger.error(f"Ошибка отправки автоответа: {e}")


async def reply_worker(client):
    """One latest message per contact, with no repeating unsolicited sends."""
    while True:
        for _, message, _ in reply_gate.due():
            await auto_reply(client, message, deferred=True)
        await asyncio.sleep(1)


class AuthRetryError(ValueError):
    """The challenge remains valid; the user can retry without requesting a code."""


class AuthManager:
    SEND_CODE_TIMEOUT = 90
    LOGIN_TIMEOUT = 180

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
        phone_clean = re.sub(r"[\s()\-]", "", str(phone or ""))
        if not phone_clean:
            raise ValueError("Номер телефона не указан!")

        if phone_clean.startswith("8") and len(phone_clean) == 11:
            phone_clean = "+7" + phone_clean[1:]
        elif not phone_clean.startswith("+"):
            phone_clean = "+" + phone_clean

        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone_clean):
            raise ValueError(
                "Укажите полный номер телефона с кодом страны, например +7 900 123-45-67."
            )
        if phone_clean.startswith("+7") and len(phone_clean) != 12:
            raise ValueError("Для номера с кодом +7 нужны ещё 10 цифр. Проверьте полный номер.")
        return phone_clean

    @staticmethod
    def _validate_api(api_id, api_hash):
        try:
            api_id_int = int(str(api_id).strip())
        except (TypeError, ValueError):
            raise ValueError("Telegram API ID должен быть числом!")

        api_hash_clean = str(api_hash or "").strip()
        if api_id_int <= 0:
            raise ValueError("Telegram API ID должен быть положительным числом!")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash_clean):
            raise ValueError(
                "Telegram API Hash должен содержать 32 символа. Скопируйте его целиком."
            )
        return api_id_int, api_hash_clean

    def _write_state(self, data: dict):
        from storage import atomic_write_json
        from secret_store import protect_bootstrap

        os.makedirs(BASE_DIR, exist_ok=True)
        stored = dict(data)
        if os.name == "nt" and stored.get("api_hash"):
            stored["api_hash_protected"] = protect_bootstrap(stored.pop("api_hash"))
        atomic_write_json(self.state_file, stored)

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
                    await asyncio.wait_for(self.client.disconnect(), timeout=5)
                else:
                    # connect() may fail after opening SQLite but before setting
                    # is_connected. Release that handle so the next attempt works.
                    session = getattr(self.client, "session", None)
                    if session is not None and getattr(session, "connection", None) is not None:
                        await asyncio.wait_for(session.stop(), timeout=5)
            except Exception as e:
                logger.debug(f"Ошибка отключения auth-клиента: {e}")
            finally:
                storage = getattr(self.client, "storage", None)
                if storage is not None and getattr(storage, "conn", None) is not None:
                    try:
                        await storage.close()
                    except Exception:
                        pass
                self.client = None

    def _run_auth(self, operation, timeout, timeout_message):
        self._ensure_loop()

        async def bounded():
            # wait_for cancels and awaits cleanup before releasing the GUI worker.
            return await asyncio.wait_for(operation, timeout=timeout)

        future = asyncio.run_coroutine_threadsafe(bounded(), self.loop)
        try:
            return future.result(timeout=timeout + 15)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(timeout_message) from None
        except ApiIdInvalid:
            raise ValueError(
                "Telegram отклонил API ID / API Hash. Проверьте оба поля на my.telegram.org."
            ) from None
        except PhoneNumberInvalid:
            raise ValueError(
                "Telegram отклонил номер телефона. Укажите полный номер с кодом страны."
            ) from None
        except FloodWait as exc:
            raise ValueError(
                f"Telegram ограничил попытки входа. Повторите через {exc.value} сек."
            ) from None

    def send_code(self, api_id: str, api_hash: str, phone: str) -> str:
        if not PYROGRAM_AVAILABLE:
            raise RuntimeError("Pyrogram не установлен. Установите pyrogram и tgcrypto.")

        with self._auth_operation_lock:
            return self._run_auth(
                self._async_send_code(api_id, api_hash, phone),
                self.SEND_CODE_TIMEOUT,
                "Telegram слишком долго отвечает. Проверьте подключение и нажмите «Получить код» снова.",
            )

    async def _async_send_code(self, api_id: str, api_hash: str, phone: str) -> str:
        api_id_int, api_hash_clean = self._validate_api(api_id, api_hash)
        phone_clean = self._normalize_phone(phone)

        logger.info("Запрашиваем код Telegram")
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
                no_updates=True,
                sleep_threshold=0,
            )
            self.client = probe

            try:
                authorized = await self.client.connect()
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower():
                    raise RuntimeError(
                        "Файл Telegram-сессии занят работающим юзерботом. "
                        "Подождите несколько секунд и нажмите «Получить код» ещё раз."
                    ) from e
                raise

            if authorized:
                me = await self.client.get_me()
                if me is None:
                    raise RuntimeError(
                        "Telegram не подтвердил данные аккаунта. Повторите подключение."
                    )
                existing_phone = str(getattr(me, "phone_number", "") or "").lstrip("+")
                if existing_phone and existing_phone != phone_clean.lstrip("+"):
                    raise ValueError(
                        "В Крест уже подключён другой Telegram-аккаунт. "
                        "Для замены завершите сессию Крест в Telegram → Устройства."
                    )
                await self._disconnect_client()
                self._remove_state()
                self.phone_code_hash = None
                return (
                    "Аккаунт уже авторизован. Код не нужен — нажмите «Запустить» у Telegram User."
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
            return "Код отправлен. Проверьте сообщения Telegram, введите код и нажмите «Войти»."

        except (Exception, asyncio.CancelledError):
            await self._disconnect_client()
            self._remove_state()
            self.phone_code_hash = None
            raise

    def login(self, code: str, password_callback=None) -> str:
        if not PYROGRAM_AVAILABLE:
            raise RuntimeError("Pyrogram не установлен. Установите pyrogram и tgcrypto.")

        with self._auth_operation_lock:
            return self._run_auth(
                self._async_login(code, password_callback),
                self.LOGIN_TIMEOUT,
                "Вход в Telegram занял слишком много времени. Нажмите «Получить код» и повторите вход.",
            )

    async def _async_login(self, code: str, password_callback=None) -> str:
        code_clean = str(code or "").strip().replace(" ", "")
        if not code_clean:
            raise ValueError("Код Telegram не введён!")

        preserve_challenge = False
        try:
            if not os.path.exists(self.state_file):
                raise ValueError(
                    "Сначала нажмите «Получить код» и дождитесь подтверждения запроса."
                )

            from storage import read_json
            from secret_store import protect_bootstrap

            data = read_json(self.state_file)
            if data.get("api_hash_protected"):
                data["api_hash"] = protect_bootstrap(data["api_hash_protected"], decrypt=True)

            if data.get("status") not in ("code_sent", "password_required"):
                raise ValueError("Код ещё не был запрошен. Сначала нажмите «Получить код».")

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
                    no_updates=True,
                    sleep_threshold=0,
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

            if data["status"] == "code_sent":
                try:
                    user = await self.client.sign_in(self.phone, self.phone_code_hash, code_clean)
                    if user is False or isinstance(user, types.TermsOfService):
                        raise ValueError(
                            "Сначала зарегистрируйте этот номер в официальном Telegram."
                        )
                except SessionPasswordNeeded:
                    # Telegram has accepted the code: do not submit it again if
                    # the password is mistyped or the dialog is cancelled.
                    data["status"] = "password_required"
                    self._write_state(data)
                except PhoneCodeExpired as exc:
                    raise ValueError("Срок действия кода истёк. Нажмите «Получить код».") from exc
                except PhoneCodeInvalid as exc:
                    raise AuthRetryError(
                        "Неверный код Telegram. Проверьте его и нажмите «Войти» ещё раз."
                    ) from exc

            if data["status"] == "password_required":
                if password_callback is None:
                    raise AuthRetryError(
                        "На аккаунте включён облачный 2FA. Введите пароль 2FA в окне КРЕСТ."
                    )

                # The Tk dialog waits on the GUI thread. Keep the Telegram loop
                # alive so its network heartbeat continues while the user types.
                pwd = await asyncio.to_thread(password_callback)
                if not pwd:
                    raise AuthRetryError("Пароль 2FA не введён. Нажмите «Войти», чтобы продолжить.")

                try:
                    await self.client.check_password(str(pwd))
                except PasswordHashInvalid as e:
                    raise AuthRetryError(
                        "Неверный пароль 2FA. Нажмите «Войти» и введите пароль ещё раз."
                    ) from e

            me = await self.client.get_me()
            if me is None:
                raise RuntimeError("Telegram не вернул данные авторизованного пользователя.")

            name = " ".join(x for x in [me.first_name, me.last_name] if x) or "Пользователь"
            user_info = f"{name} (@{me.username})" if me.username else name
            logger.info("Telegram успешно авторизован")
            return user_info

        except (AuthRetryError, FloodWait):
            preserve_challenge = True
            raise
        finally:
            if not preserve_challenge:
                await self._disconnect_client()
                self._remove_state()
                self.phone_code_hash = None


_auth_mgr = AuthManager()


def send_code(api_id: str, api_hash: str, phone: str) -> str:
    return _auth_mgr.send_code(api_id, api_hash, phone)


def login(code: str, password_callback=None) -> str:
    return _auth_mgr.login(code, password_callback=password_callback)


async def _start_authorized_client(app):
    """Start only a GUI-authorized session; never prompt in a hidden EXE console."""
    if not await app.connect():
        return None
    me = await app.get_me()
    if me is None:
        return None
    app.me = me
    await app.invoke(raw.functions.updates.GetState())
    await app.initialize()
    return me


def start_tg_bridge(notifications_queue=None):
    from account_control import AccountControl

    account_control = AccountControl()
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
            replies_task = health_task = None
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
                me = await _start_authorized_client(app)
                if me is None:
                    stop_reason["value"] = "UNAUTHORIZED"
                    return stop_reason["value"]

                from bridge_sdk import emit

                async def manual_reply(chat, text):
                    if str(chat) not in {key[0] for key in reply_gate.received}:
                        raise ValueError("Нет свежего входящего от этого контакта")
                    reply_gate.outgoing(chat)
                    await app.send_message(int(chat), text, parse_mode=enums.ParseMode.DISABLED)

                account_control.attach(manual_reply)
                emit("ready", "Telegram подтвердил авторизацию аккаунта", capabilities=["reply"])
                emit("health", "Готов к входящим сообщениям", connected=True)

                logger.info(
                    "Юзербот активен на аккаунте: %s (@%s)",
                    me.first_name,
                    me.username or "нет username",
                )

                async def health():
                    while True:
                        await asyncio.sleep(30)
                        if time.monotonic() < reply_gate.blocked_until:
                            emit(
                                "health",
                                "Telegram запросил паузу; соединение не перезапускаю",
                                connected=True,
                                pending=len(reply_gate.pending),
                            )
                            continue
                        try:
                            await asyncio.wait_for(app.get_me(), 15)
                            emit(
                                "health",
                                reply_gate.last_reason,
                                connected=True,
                                pending=len(reply_gate.pending),
                                replies=len(reply_gate.sent),
                            )
                        except FloodWait as exc:
                            reply_gate.blocked_until = max(
                                reply_gate.blocked_until, time.monotonic() + exc.value
                            )
                            emit(
                                "health",
                                "Telegram ограничил проверки; ожидаю разрешённое время",
                                connected=True,
                            )
                        except (AuthKeyUnregistered, SessionRevoked, UserDeactivated, Unauthorized):
                            emit("auth_required", "Telegram: войдите в аккаунт заново")
                            stop_reason["value"] = "UNAUTHORIZED"
                            stop_event.set()
                            return
                        except Exception:
                            emit("disconnected", "Telegram не ответил на проверку; переподключаюсь")
                            stop_reason["value"] = "ERROR"
                            stop_event.set()
                            return

                updater_task = asyncio.create_task(status_updater(app))
                replies_task = asyncio.create_task(reply_worker(app))
                health_task = asyncio.create_task(health())
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
                account_control.detach()
                for task in (updater_task, auth_watch_task, replies_task, health_task):
                    if task and not task.done():
                        task.cancel()
                try:
                    if app.is_initialized:
                        await app.stop()
                    elif app.is_connected:
                        await app.disconnect()
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
