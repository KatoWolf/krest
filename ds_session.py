import discord
import json
import random
import asyncio
import os
import sys
import gc
import datetime
import time

# --- ПУТИ ПРОЕКТА (АДАПТАЦИЯ ПОД .EXE) ---
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATUS_FILE = os.path.join(BASE_DIR, "current_status.json")

try:
    import config
except ImportError:
    print("--- [DS Error]: Файл config.py не найден! ---")
    sys.exit()

# --- МЕГА-БАЗА ЦИТАТ (ЭСТЕТИКА, КОДИНГ, ДЕПРЕССИЯ) ---
QUOTES = [
    "Я курю эври дей...",
    "i can't fix that... Maybe again?",
    "03:33",
    "Снова в хлам, привет, Кишлак",
    "Я в мусоре, но мне комфортно",
    "Эту жизнь я не выбирал, но я в ней главный герой",
    "Самый одинокий человек на земле",
    "Глаза красные, но не от слез",
    "Мам, я в порядке, просто музыка громко",
    "Внутри пустота, снаружи только бас",
    "Моя ассистентка Крест следит за тобой",
    "Слишком много мыслей, слишком мало смысла",
    "Я не сплю, я просто медленно гасну",
    "Ты ищешь меня там, где меня больше нет",
    "В плеере Кишлак — в голове бардак",
    "I can’t fix my life, but I can fix this code",
    "Свет за окном чужой, а мой внутри перегорел",
    "Музыка на максимум, чувства на минимум.",
    "Отключен от реальности.",
    "Холодный неон греет лучше людей.",
    "Молчание громче крика.",
    "Люди уходят, код остаётся.",
    "Этот трек на репите, как мои ошибки.",
    "Застрял между вчера и никогда.",
    "Слишком устал, чтобы засыпать.",
    "Растворяюсь в пикселях монитора.",
    "Сгорел, но продолжаю светить.",
    "Оставь сообщение после звукового сигнала...",
    "Временно вне зоны доступа к счастью.",
    "Тону в собственных мыслях.",
    "Один на один с темнотой.",
    "Слишком поздно для «привет».",
    "В наушниках тишина, в голове шум.",
    "Не пиши, я всё равно не отвечу.",
    "Просто жду, когда всё это закончится.",
    "Мой лучший друг — это бессонница.",
    "Смеюсь снаружи, мертв внутри.",
    "Смотрю в монитор, вижу только пустоту.",
    "error 404: feelings not found",
    "Просто цифровой призрак.",
    "Ctrl+Z не работает в реальной жизни.",
    "System.NullReferenceException: Soul not found",
    "Код компилируется, жизнь — нет.",
    "Я не онлайн, это просто скрипт.",
    "Заряжен на 1%, но продолжаю работать.",
    "nothing to say.",
    "dead inside, coding outside.",
    "lost in the web.",
    "digital ghost.",
    "offline in real life.",
    "my code is clean, my mind is a mess.",
    "disconnecting from the matrix...",
    "just watching the world burn.",
    "press any key to continue living.",
    "reality is just a bad game.",
    "no signal.",
    "system failure.",
    "rebooting...",
]


class KrestDS(discord.Client):
    def __init__(self, notifications_queue=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.notifications_queue = notifications_queue

        self.spam_control = {}
        self.last_outgoing = {}  # Для отслеживания личной активности KatoWolf

    async def on_ready(self):
        print(f"--- [DS]: Сессия активна (Аккаунт: {self.user}). RAM Optimized. ---")
        if not getattr(self, "_background_started", False):
            self._background_started = True
            self.loop.create_task(self.status_rotator())
            self.loop.create_task(self.memory_cleaner())

    async def memory_cleaner(self):
        """Очистка ОЗУ от мусора Discord-клиента раз в 15 минут"""
        while not self.is_closed():
            await asyncio.sleep(900)
            gc.collect()

    async def get_local_status(self):
        """Чтение статуса из файла (без запросов к интернету)"""
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass
        return {}

    async def status_rotator(self):
        """Оптимизированная смена статуса (ТОЛЬКО цитаты)"""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                status_data = await self.get_local_status()
                activity_type = status_data.get("activity_type", "IDLE")

                # Выбираем случайную цитату
                status_text = random.choice(QUOTES)
                activity = discord.CustomActivity(name=status_text)

                # Если ты играешь/кодишь — ставим значок "Не беспокоить" (красный), иначе "Не активен" (луна)
                ds_status = (
                    discord.Status.dnd if activity_type in ["GAME", "WORK"] else discord.Status.idle
                )

                await self.change_presence(activity=activity, status=ds_status)
            except Exception as e:
                pass  # Игнорируем ошибки сети

            # Ждем 5 минут перед сменой цитаты
            await asyncio.sleep(300)

    async def auto_delete_message(self, message, delay):
        """Фоновое удаление сообщений-автоответов"""
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception:
            pass  # Игнорируем, если собеседник или сам Wolf уже удалил сообщение

    async def on_message(self, message):
        # 1. ТРЕКИНГ ИСХОДЯЩИХ СООБЩЕНИЙ (Если ты сам пишешь)
        if message.author == self.user:
            if isinstance(message.channel, discord.DMChannel):
                channel_id = message.channel.id
                self.last_outgoing[channel_id] = time.time()
                # Сбрасываем анти-спам для этого собеседника
                if channel_id in self.spam_control:
                    del self.spam_control[channel_id]
            return

        # Только личка (DM) и игнорируем ботов
        if isinstance(message.channel, discord.DMChannel) and not message.author.bot:
            user_id = message.author.id
            channel_id = message.channel.id
            current_time = time.time()

            # 2. УМНАЯ ПРОВЕРКА АКТИВНОСТИ
            # Если ты сам общался с человеком менее 5 минут назад, автоответчик молчит
            if channel_id in self.last_outgoing and (
                current_time - self.last_outgoing[channel_id] < 300
            ):
                return

            # Очистка старых записей анти-спама (предотвращает утечку ОЗУ)
            keys_to_del = [k for k, v in self.spam_control.items() if current_time - v > 3600]
            for k in keys_to_del:
                del self.spam_control[k]

            out_to_del = [k for k, v in self.last_outgoing.items() if current_time - v > 3600]
            for k in out_to_del:
                del self.last_outgoing[k]

            text = message.content.lower() if message.content else ""
            is_urgent = any(word in text for word in ["срочно", "важно", "горит", "ответь"])

            # 3. Анти-спам (5 минут молчания, если не срочно)
            if (
                not is_urgent
                and user_id in self.spam_control
                and current_time - self.spam_control[user_id] < 300
            ):
                return

            self.spam_control[user_id] = current_time
            print(f"--- [DS]: Пришло сообщение от {message.author.name} ---")

            try:
                # Читаем контекст ПК
                status_data = await self.get_local_status()
                is_playing = status_data.get("is_playing", False)
                activity_type = status_data.get("activity_type", "IDLE")
                full_msg = status_data.get("full_message", "")

                current_hour = datetime.datetime.now().hour
                is_night = 1 <= current_hour < 8  # Ночной режим (с 01:00 до 08:00)

                # Имитация печати
                async with message.channel.typing():
                    # Случайная задержка
                    await asyncio.sleep(random.uniform(1.5, 3.0))

                    # Логика ответов (Идентична Telegram V3.0)
                    if message.attachments:
                        reply_text = (
                            f"💠 **KREST_V3.0: Ошибка обработки**\n"
                            f"───\n"
                            f"KatoWolf сейчас недоступен.\n"
                            f"Я — автоматический помощник. Вложения и голосовые сообщения в данный момент не распознаются.\n"
                            f"Пожалуйста, изложите суть текстом."
                        )
                    elif is_night:
                        reply_text = (
                            f"💠 **KREST_V3.0: Ночной протокол**\n"
                            f"───\n"
                            f"● `Статус:` Сон 🌙\n"
                            f"───\n"
                            f"KatoWolf сейчас спит. Уведомления отключены, он прочтет ваше сообщение утром."
                        )
                    elif is_playing or activity_type in [
                        "GAME",
                        "WORK",
                        "REST",
                        "SOCIAL",
                    ]:
                        context_msg = full_msg if full_msg else "KatoWolf сейчас занят."
                        if is_urgent:
                            context_msg = f"⚠️ Зафиксирован приоритетный сигнал.\n{context_msg}\nЯ поставила пометку «СРОЧНО» на твое сообщение."

                        reply_text = (
                            f"💠 **KREST_V3.0: Интеллектуальный ответ**\n───\n{context_msg}"
                        )
                    else:
                        reply_text = (
                            f"💠 **KREST_V3.0: Автономный режим**\n"
                            f"───\n"
                            f"● `Статус:` Ожидание\n"
                            f"───\n"
                            f"KatoWolf отошел от терминала. Я передам ему сообщение, как только он вернется."
                        )

                    # Формируем финальный текст с припиской об автоудалении
                    final_text = f"{reply_text}\n\n_(Это автоматический ответ системы. Он исчезнет через 5 минут.)_"

                    # Отправляем ответ
                    sent_msg = await message.channel.send(final_text)
                    print(f"--- [DS AutoReply]: Ответил пользователю {message.author.name} ---")

                    # Фоново запускаем таймер на удаление
                    self.loop.create_task(self.auto_delete_message(sent_msg, 300))

                # 4. Передаем имя в очередь для озвучки Крест (только днем)
                if self.notifications_queue is not None and not is_night:
                    try:
                        sender_name = message.author.display_name or message.author.name
                        self.notifications_queue.put_nowait(f"{sender_name} в Дискорде")
                    except:
                        pass

            except Exception as e:
                print(f"--- [DS Error]: Сбой обработки сообщения: {e} ---")


def start_ds_bridge(notifications_queue=None):
    if not config.DS_USER_TOKEN:
        raise RuntimeError("Укажите Discord Bot Token в меню API и сохраните настройки.")
    print("--- [DS]: Попытка входа... ---")

    while True:  # Бесконечный цикл на случай отключения интернета
        try:
            # КЛЮЧ К ОПТИМИЗАЦИИ: Отключаем все события, кроме сообщений
            intents = discord.Intents.none()
            intents.messages = True
            intents.dm_messages = True
            intents.message_content = True

            client = KrestDS(notifications_queue=notifications_queue, intents=intents)
            client.run(config.DS_USER_TOKEN)

        except discord.PrivilegedIntentsRequired as e:
            raise RuntimeError("Включите Message Content Intent в Discord Developer Portal") from e
        except discord.LoginFailure as e:
            raise RuntimeError("Discord отклонил токен бота") from e
        except Exception as e:
            print(f"--- [DS Critical Error]: {e} ---")

        print("--- [DS]: Потеряно соединение. Перезапуск через 10 секунд... ---")
        time.sleep(10)


if __name__ == "__main__":
    # Запуск напрямую для теста (без очереди)
    from notifications import NotificationQueue

    start_ds_bridge(NotificationQueue())
