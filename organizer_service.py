"""Small event-driven party coordinator. Incoming chat text is never a PC command."""

import copy
from datetime import datetime
import queue
import re
import threading
import time
import uuid
from pathlib import Path

from organizer_bridge import SERVICES, digest
from organizer_language import LABELS, PHRASES, delay_time, interpret, normalize, request_text
from organizer_messages import (
    invitation,
    question_answer,
    ready_text,
    message as styled_message,
    summary as styled_summary,
    style as personality_style,
    control,
    FEATURES,
)
from storage import atomic_write_json

ACTIVE = {"collecting", "ready", "paused"}
MEMBERS = {
    **LABELS,
    "reserve": "Резерв",
    "invited": "Приглашён",
    "queued": "Приглашение в очереди",
    "no_response": "Не ответил",
    "send_failed": "Доставка не подтверждена",
}
GAMES = {
    "дота": ("Dota 2", "570"),
    "доту": ("Dota 2", "570"),
    "dota": ("Dota 2", "570"),
    "dota 2": ("Dota 2", "570"),
    "дота 2": ("Dota 2", "570"),
    "кс": ("Counter-Strike 2", "730"),
    "кс 2": ("Counter-Strike 2", "730"),
    "контру": ("Counter-Strike 2", "730"),
    "контра": ("Counter-Strike 2", "730"),
    "cs2": ("Counter-Strike 2", "730"),
    "counter-strike 2": ("Counter-Strike 2", "730"),
    "варф": ("Warframe", "230410"),
    "варфрейм": ("Warframe", "230410"),
    "warframe": ("Warframe", "230410"),
}


class Organizer:
    def __init__(self, runtime, store, folder):
        self.runtime, self.store, self.folder = runtime, store, Path(folder)
        self.lock = threading.RLock()
        self.wake, self.closed = threading.Event(), threading.Event()
        self.inbox = queue.Queue(maxsize=128)
        self.thread = None
        self.last_send = 0
        self.message_variants = {}
        self.last_steam = 0
        self.steam = {}
        self.steam_error = "Steam ещё не проверен"
        self.steam_busy = False
        self.address_books = {}
        self.contact_loads = {}
        self.contact_status = {}
        self.state = store.get("organizer", "state") or dict(
            id="state", contacts=[], party=None, requests=[], outbox=[], seen=[], aliases=[]
        )
        self.state.setdefault(
            "preferences",
            dict(messenger="tg_bridge.py", game="дота", size=5, when="сейчас", channel=""),
        )
        if self.state.get("party") and self.state["party"]["status"] in ACTIVE:
            self.state["party"]["status"] = "paused"
        for item in self.state["outbox"]:
            if item["status"] in {"sending", "queued"}:
                item["status"] = "uncertain" if item["status"] == "sending" else "cancelled"
                if item["status"] == "uncertain" or item.get("request"):
                    self._failed_invite(item)
        self._persist()

    def _require(self):
        from licensing import require_premium

        if self.closed.is_set():
            raise ValueError("Организатор завершает работу")
        if self.runtime.settings().get("safe_mode"):
            raise ValueError("Организатор отключён в безопасном режиме")
        require_premium(self.runtime.settings())

    def _persist(self):
        pending = [r for r in self.state["outbox"] if r["status"] in {"queued", "sending"}]
        finished = [r for r in self.state["outbox"] if r["status"] not in {"queued", "sending"}]
        self.state["outbox"] = sorted(
            pending + finished[-max(1, 200 - len(pending)) :], key=lambda r: r["created"]
        )
        self.state["requests"] = self.state["requests"][-100:]
        self.state["seen"] = self.state["seen"][-2000:]
        self.store.put("organizer", self.state, limit=1)
        self._routes()
        self.runtime.events.put(("organizer_changed", None))
        self.wake.set()

    def _routes(self):
        if self.closed.is_set():
            atomic_write_json(
                self.folder / "organizer_routes.json", {"contacts": [], "messages": []}
            )
            return
        party = self.state.get("party")
        members = (
            {m["id"] for m in party["members"] if m["state"] != "reserve" and not m.get("excluded")}
            if party and party["status"] in ACTIVE
            else set()
        )
        messenger = self.state["preferences"]["messenger"]
        contacts = [
            dict(
                service=c["service"],
                chat=c["chat"],
                party=c["id"] in members,
                requests=c["requests"] and c["service"] == messenger,
            )
            for c in self.state["contacts"]
            if c["requests"] and c["service"] == messenger or c["id"] in members
        ]
        messages = [
            dict(
                id=r["id"],
                service=r["service"],
                chat=r["chat"],
                expires=r["expires"],
                digest=digest(r["text"]),
            )
            for r in self.state["outbox"]
            if r["status"] in {"queued", "sending"}
        ]
        atomic_write_json(
            self.folder / "organizer_routes.json", {"contacts": contacts, "messages": messages}
        )

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._worker, name="krest-organizer", daemon=True)
            self.thread.start()

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=1)
        with self.lock:
            atomic_write_json(
                self.folder / "organizer_routes.json", {"contacts": [], "messages": []}
            )

    def enqueue(self, kind, source, data):
        if source not in SERVICES or self.closed.is_set():
            return
        try:
            self.inbox.put_nowait((kind, source, data))
            self.wake.set()
        except queue.Full:
            self.runtime.events.put(
                ("error", "Очередь организатора заполнена; сообщение не обработано")
            )

    def _contact(self, ident):
        return next(c for c in self.state["contacts"] if c["id"] == ident)

    def _queue(self, contact, text, purpose, party="", request=""):
        if sum(r["status"] in {"queued", "sending"} for r in self.state["outbox"]) >= 150:
            raise ValueError("Очередь организатора заполнена. Дождитесь доставки или отмените сбор")
        if purpose in {"ready", "reserve", "question", "clarify"}:
            previous = next(
                (
                    r
                    for r in self.state["outbox"]
                    if r["contact"] == contact["id"]
                    and r["purpose"] == purpose
                    and r["party"] == party
                    and r["status"] not in {"cancelled", "expired"}
                ),
                None,
            )
            if previous:
                return previous["id"]
        row = dict(
            id=uuid.uuid4().hex,
            service=contact["service"],
            chat=contact["chat"],
            contact=contact["id"],
            text=text.strip()[:1800],
            purpose=purpose,
            party=party,
            request=request,
            created=time.time(),
            expires=time.time() + 300,
            status="queued",
        )
        self.state["outbox"].append(row)
        return row["id"]

    def _failed_invite(self, row):
        party = self.state.get("party")
        if party and row["purpose"] == "invite" and row["party"] == party["id"]:
            for m in party["members"]:
                if m["id"] == row["contact"] and m["state"] == "queued":
                    m.update(
                        state="send_failed", detail=row.get("detail", "Доставка не подтверждена")
                    )
        if row.get("request"):
            for item in self.state["requests"]:
                if item["id"] == row["request"] and item["status"] == "reply_queued":
                    item["status"] = (
                        "reply_uncertain" if row["status"] == "uncertain" else "reply_failed"
                    )

    def _notice(self, text, important=False):
        # Feed the existing scheduler: network callbacks never run TTS.
        self.runtime.notifications.put(
            text, source="Организатор", priority="high" if important else "normal"
        )

    def _party(self):
        party = self.state.get("party")
        if not party or party["status"] not in ACTIVE:
            raise ValueError("Сначала создайте сбор компании")
        return party

    def _occupied(self, party):
        return [
            m
            for m in party["members"]
            if m["state"] not in {"reserve", "declined", "no_response", "send_failed"}
        ]

    def _fill(self, party, now):
        if party["status"] not in {"collecting", "ready"}:
            return
        free = party["size"] - 1 - len(self._occupied(party))
        for member in party["members"]:
            if free <= 0:
                break
            if member["state"] == "reserve" and member.get("invited_at"):
                # A late answer can put an already invited friend into reserve.
                # A newly available place still needs fresh confirmation.
                member.update(
                    state="joined",
                    asked=True,
                    updated=now,
                    due=None,
                    detail="Освободилось место; уточняю готовность",
                )
                for row in self.state["outbox"]:
                    if (
                        row["party"] == party["id"]
                        and row["contact"] == member["id"]
                        and row["purpose"] == "reserve"
                        and row["status"] == "queued"
                    ):
                        row["status"] = "cancelled"
                self._queue(
                    self._contact(member["id"]),
                    self._message("check", reason="В составе освободилось место."),
                    "promotion",
                    party["id"],
                )
                free -= 1
            elif member["state"] == "reserve":
                member.update(
                    state="queued", invited_at=now, detail="Ожидаю подтверждение доставки"
                )
                self._queue(
                    self._contact(member["id"]),
                    invitation(self.runtime.settings(), party, self._variant("intro")),
                    "invite",
                    party["id"],
                )
                free -= 1

    def _invite_attempted(self, party, member):
        return any(
            row["party"] == party["id"]
            and row["contact"] == member["id"]
            and row["purpose"] == "invite"
            and row["status"] in {"sending", "delivered", "uncertain"}
            for row in self.state["outbox"]
        )

    def add_contact(self, **values):
        self._require()
        name, service, chat = (
            str(values.get("name", "")).strip(),
            values.get("service"),
            str(values.get("chat", "")).strip(),
        )
        steam = str(values.get("steam", "")).strip()
        if (
            not 1 <= len(name) <= 60
            or service not in SERVICES
            or not re.fullmatch(r"[1-9]\d{3,24}", chat)
        ):
            raise ValueError("Нужны имя, Telegram/Discord и цифровой ID личного чата")
        if steam and not re.fullmatch(r"\d{17}", steam):
            raise ValueError("SteamID64 должен состоять из 17 цифр")
        with self.lock:
            if self.state.get("party") and self.state["party"]["status"] in ACTIVE:
                raise ValueError("Завершите сбор перед изменением контактов")
            ident = values.get("id") or uuid.uuid4().hex
            rows = self.state["contacts"]
            if len(rows) >= 30 and not any(c["id"] == ident for c in rows):
                raise ValueError("В списке допускается до 30 друзей")
            if any(
                c["id"] != ident
                and c["service"] == service
                and (
                    normalize(c["name"]) == normalize(name)
                    or c["chat"] == chat
                    or steam
                    and c["steam"] == steam
                )
                for c in rows
            ):
                raise ValueError("Имя, чат или SteamID уже используется в этом мессенджере")
            row = dict(
                id=ident,
                name=name,
                service=service,
                chat=chat,
                steam=steam,
                enabled=bool(values.get("enabled", True)),
                requests=bool(values.get("requests", False)),
            )
            self.state["contacts"] = (
                [row if c["id"] == ident else c for c in rows]
                if any(c["id"] == ident for c in rows)
                else [*rows, row]
            )
            self._persist()
        return "Контакт сохранён"

    def create(self, game=None, size=None, when=None, channel=None):
        self._require()
        prefs = self.state["preferences"]
        game, size, when, channel = (
            game or prefs["game"],
            size if size is not None else prefs["size"],
            when or prefs["when"],
            channel if channel is not None else prefs["channel"],
        )
        if type(size) is not int or not 2 <= size <= 10:
            raise ValueError("Размер компании — от 2 до 10 вместе с вами")
        if channel and not re.fullmatch(r"https://discord\.com/channels/\d+/\d+", channel):
            raise ValueError(
                "Нужна ссылка на канал Discord: https://discord.com/channels/сервер/канал"
            )
        entry = GAMES.get(normalize(game))
        if not entry:
            from steam_service import cached_catalog

            matches = [
                g
                for g in cached_catalog(self.runtime.state)
                if normalize(g["name"]) == normalize(game)
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Игра не найдена. Выберите Dota 2, CS2, Warframe или точное название из Steam"
                )
            entry = (matches[0]["name"], str(matches[0]["appid"]))
        now = time.time()
        if normalize(when) in {"сейчас", "теперь", ""}:
            starts = now
        elif normalize(when) in {"вечером", "сегодня вечером"}:
            starts = max(
                now,
                datetime.fromtimestamp(now)
                .replace(hour=19, minute=0, second=0, microsecond=0)
                .timestamp(),
            )
        else:
            starts, _ = delay_time(when, now)
            if starts is None or starts > now + 86400:
                raise ValueError(
                    "Укажите «сейчас», «через 30 минут», «вечером» или «в 21:30» в пределах суток"
                )
        with self.lock:
            if self.state.get("party") and self.state["party"]["status"] in ACTIVE:
                raise ValueError("Уже идёт сбор. Завершите или отмените его")
            contacts = [
                c
                for c in self.state["contacts"]
                if c["enabled"] and c["service"] == prefs["messenger"]
            ]
            if len(contacts) < size - 1:
                raise ValueError(
                    f"Выберите хотя бы {size - 1} друзей; сейчас выбрано {len(contacts)}"
                )
            party = dict(
                id=uuid.uuid4().hex,
                messenger=prefs["messenger"],
                game=entry[0],
                appid=entry[1],
                size=size,
                starts=starts,
                expires=starts + 4 * 3600,
                channel=channel,
                status="collecting",
                members=[
                    dict(
                        id=c["id"],
                        name=c["name"],
                        state="reserve",
                        detail="Резерв; приглашения ещё не было",
                        invited_at=0,
                        updated=0,
                        due=None,
                        asked=False,
                        clarified=False,
                        answered_question=False,
                    )
                    for c in contacts
                ],
                announced=False,
                log=[],
            )
            self.state["party"] = party
            self._fill(party, now)
            self.last_steam = 0
            self._persist()
        return (
            control(self.runtime.settings(), "create")
            + " "
            + entry[0]
            + f", {size} человек с вами. Доставка — в меню «Компания»."
        )

    def receive(self, source, data):
        self._require()
        text, chat, message = (
            str(data.get("text", ""))[:2000],
            str(data.get("chat", "")),
            str(data.get("message", ""))[:80],
        )
        if not message or not text.strip():
            return
        with self.lock:
            contact = next(
                (c for c in self.state["contacts"] if c["service"] == source and c["chat"] == chat),
                None,
            )
            if not contact:
                return
            key = source + ":" + chat + ":" + message
            if key in self.state["seen"]:
                return
            self.state["seen"].append(key)
            now = time.time()
            request = (
                request_text(text)
                if contact["requests"] and source == self.state["preferences"]["messenger"]
                else ""
            )
            if request:
                previous = next(
                    (
                        r
                        for r in self.state["requests"]
                        if r["contact"] == contact["id"]
                        and r["text"] == request
                        and r["status"] in {"waiting", "delivered"}
                        and now - r["created"] < 600
                    ),
                    None,
                )
                if previous:
                    self._persist()
                    return
                self.state["requests"].append(
                    dict(
                        id=uuid.uuid4().hex,
                        contact=contact["id"],
                        name=contact["name"],
                        text=request,
                        created=now,
                        status="waiting",
                        after_game=bool(self.runtime.performance.game),
                    )
                )
                self._queue(
                    contact,
                    self._message("ack", after_game=bool(self.runtime.performance.game)),
                    "request_ack",
                )
            party = self.state.get("party")
            member = (
                next(
                    (
                        m
                        for m in party["members"]
                        if m["id"] == contact["id"] and m["invited_at"] and not m.get("excluded")
                    ),
                    None,
                )
                if party and party["status"] in ACTIVE
                else None
            )
            if member and not request:
                result = interpret(text, now, self.state["aliases"])
                party["log"] = [
                    *party["log"],
                    dict(name=contact["name"], text=text[:250], result=result.dict(), time=now),
                ][-80:]
                member["detail"] = result.reason
                member["last_text"] = text[:250]
                if result.kind == "question":
                    if not member["answered_question"] and party["status"] != "paused":
                        self._queue(
                            contact,
                            question_answer(
                                party,
                                party["members"],
                                self.runtime.settings(),
                                self._variant("question"),
                            ),
                            "question",
                            party["id"],
                        )
                        member["answered_question"] = True
                elif result.kind in {"unknown", "uncertain"}:
                    # Do not silently overwrite a previous clear answer with banter.
                    if (
                        result.kind == "uncertain"
                        or "Отрицание" in result.reason
                        or member["state"] not in {"ready", "joined", "delayed", "waiting"}
                    ):
                        member["state"] = result.kind
                    if not member["clarified"] and party["status"] != "paused":
                        self._queue(
                            contact,
                            self._message("clarify"),
                            "clarify",
                            party["id"],
                        )
                        member["clarified"] = True
                else:
                    active_others = [m for m in self._occupied(party) if m["id"] != member["id"]]
                    state = result.kind
                    if state != "declined" and len(active_others) >= party["size"] - 1:
                        state = "reserve"
                        self._queue(
                            contact,
                            self._message("reserve"),
                            "reserve",
                            party["id"],
                        )
                    elif state == "ready" and party["starts"] - now > 15 * 60:
                        state = "joined"
                        member["detail"] = (
                            "Участие подтверждено; готовность уточню перед временем сбора"
                        )
                    member.update(state=state, updated=now, due=result.due, asked=False)
                    self._notice(
                        f"{contact['name']}: {MEMBERS[state]}"
                        + (
                            f", примерно в {datetime.fromtimestamp(result.due):%H:%M}"
                            if result.due
                            else ""
                        )
                    )
                self._fill(party, now)
            self._persist()

    def receipt(self, source, data):
        with self.lock:
            row = next(
                (
                    r
                    for r in self.state["outbox"]
                    if r["id"] == data.get("id") and r["service"] == source
                ),
                None,
            )
            if not row or row["status"] not in {"sending", "uncertain"}:
                return
            row["status"] = "delivered" if data.get("ok") is True else "failed"
            row["detail"] = str(data.get("text", ""))[:250]
            row["message_id"] = str(data.get("message_id", ""))[:80]
            party = self.state.get("party")
            if row["purpose"] == "invite" and party and row["party"] == party["id"]:
                member = next(m for m in party["members"] if m["id"] == row["contact"])
                if member["state"] in {"queued", "send_failed"} and not member.get("excluded"):
                    state = "send_failed"
                    if row["status"] == "delivered":
                        occupied = [m for m in self._occupied(party) if m["id"] != member["id"]]
                        state = "invited" if len(occupied) < party["size"] - 1 else "reserve"
                    member.update(
                        state=state,
                        detail=row["detail"],
                        invited_at=time.time(),
                    )
            if row.get("request"):
                for request in self.state["requests"]:
                    if request["id"] == row["request"]:
                        if request["status"] != "closed":
                            request["status"] = (
                                "answered" if row["status"] == "delivered" else "reply_uncertain"
                            )
            self._persist()

    def _tick(self, now):
        changed = False
        for source, pending in list(self.contact_loads.items()):
            if now - pending["started"] > 60:
                self.contact_loads.pop(source, None)
                self.contact_status[source] = (
                    "Список не получен. Проверьте юзербота и повторите загрузку."
                )
        for item in self.state["requests"]:
            if item["status"] == "waiting" and (
                not item["after_game"] or not self.runtime.performance.game
            ):
                self._notice(f"Просьба от {item['name']}: {item['text']}", True)
                item.update(status="delivered", delivered=now)
                changed = True
        party = self.state.get("party")
        if party and party["status"] in {"collecting", "ready"}:
            if now >= party["expires"]:
                self._finish("expired")
                self._notice("Время сбора истекло. Новые приглашения остановлены")
                return
            before = len(self.state["outbox"])
            for m in party["members"]:
                if m["state"] == "invited" and now - m["invited_at"] >= 600:
                    m.update(
                        state="no_response", detail="Нет ответа за 10 минут; место передано резерву"
                    )
                    changed = True
                if m["state"] == "ready" and now - m["updated"] > 1800:
                    m.update(state="joined", detail="Готовность устарела; нужна новая проверка")
                    changed = True
                due = m.get("due")
                ask_due = (m["state"] == "delayed" and due and now >= due) or (
                    m["state"] == "joined" and now >= party["starts"] - 600
                )
                if ask_due and not m["asked"]:
                    self._queue(
                        self._contact(m["id"]),
                        self._message("check", reason="Подошло время сбора."),
                        "eta",
                        party["id"],
                    )
                    m["asked"] = True
                    changed = True
            self._fill(party, now)
            changed |= len(self.state["outbox"]) != before
            ready = sum(m["state"] == "ready" for m in party["members"])
            status = (
                "ready" if ready >= party["size"] - 1 and now >= party["starts"] else "collecting"
            )
            if status != party["status"]:
                party["status"] = status
                changed = True
                if status != "ready":
                    party["announced"] = False
                    for queued in self.state["outbox"]:
                        if (
                            queued["party"] == party["id"]
                            and queued["purpose"] == "ready"
                            and queued["status"] == "queued"
                        ):
                            queued["status"] = "cancelled"
            if status == "ready" and not party["announced"]:
                party["announced"] = True
                self._notice(
                    ready_text(self.runtime.settings(), self._variant("ready_owner"))
                    + " Скажи «Крест, запускай нашу игру».",
                    True,
                )
                for m in party["members"]:
                    if m["state"] == "ready":
                        self._queue(
                            self._contact(m["id"]),
                            "Крест: "
                            + ready_text(self.runtime.settings(), self._variant("ready_friends"))
                            + " Сообщила владельцу; ждём начала.",
                            "ready",
                            party["id"],
                        )
                changed = True
        for row in self.state["outbox"]:
            if (row["status"] == "queued" and row["expires"] <= now) or (
                row["status"] == "sending" and now - row.get("sent_at", now) >= 35
            ):
                row["status"] = "expired" if row["status"] == "queued" else "uncertain"
                row["detail"] = "Доставка не подтверждена; автоматического повтора нет"
                self._failed_invite(row)
                if party and row["party"] == party["id"] and row["purpose"] == "invite":
                    for m in party["members"]:
                        if m["id"] == row["contact"] and m["state"] == "queued":
                            m.update(state="send_failed", detail=row["detail"])
                changed = True
        if changed:
            self._persist()

    def _send_one(self):
        now = time.time()
        if now - self.last_send < 2:
            return
        self._require()
        with self.lock:
            for row in self.state["outbox"]:
                if row["status"] != "queued" or row["expires"] <= now:
                    continue
                details = self.runtime.integrations.details(row["service"])
                if self.runtime.center.muted(row["service"], row["chat"]):
                    row.update(status="cancelled", detail="Контакт приглушён в уведомлениях")
                    self._failed_invite(row)
                    self._persist()
                    continue
                bridge = details.get("bridge", {})
                if (
                    not details.get("running", True)
                    or not bridge.get("ready")
                    or "organizer_send" not in bridge.get("capabilities", [])
                ):
                    continue
                row.update(status="sending", sent_at=now)
                self.last_send = now
                self._persist()  # Persist before crossing a process/network boundary.
                try:
                    self.runtime.integrations.send(
                        row["service"],
                        "organizer_send",
                        chat=row["chat"],
                        text=row["text"],
                        request_id=row["id"],
                    )
                except Exception:
                    row.update(
                        status="uncertain",
                        detail="Отправка не подтверждена. Проверьте чат перед новым приглашением",
                    )
                    self._failed_invite(row)
                    self._persist()
                return

    def _refresh_steam(self):
        now = time.time()
        with self.lock:
            party = self.state.get("party")
            if (
                self.steam_busy
                or not party
                or party["status"] not in {"collecting", "ready"}
                or now - self.last_steam < 120
            ):
                return
            contacts = [
                c
                for c in self.state["contacts"]
                if c["steam"] and c["id"] in {m["id"] for m in party["members"]}
            ]
            self.last_steam = now
            cfg = self.runtime.settings()
            key, owner = cfg["api_keys"]["steam_api_key"], cfg["api_keys"]["steam_id"]
            if not contacts or not key or not owner:
                self.steam_error = "Для статусов нужны SteamID друзей и ключ Steam в подключениях"
                return
            self.steam_busy = True
        threading.Thread(
            target=self._steam_fetch,
            args=(contacts, key, owner, now),
            name="krest-party-steam",
            daemon=True,
        ).start()

    def _steam_fetch(self, contacts, key, owner, now):
        from steam_service import SteamClient

        try:
            client = SteamClient(key, owner)
            players = (
                client.api(
                    "ISteamUser/GetPlayerSummaries/v2/",
                    steamids=",".join(c["steam"] for c in contacts),
                )
                .get("response", {})
                .get("players", [])
            )
            by_id = {str(p.get("steamid")): p for p in players}
            with self.lock:
                self.steam = {
                    c["id"]: dict(
                        updated=now,
                        available=c["steam"] in by_id,
                        game=str(by_id.get(c["steam"], {}).get("gameextrainfo", ""))[:100],
                        online=bool(by_id.get(c["steam"], {}).get("personastate", 0)),
                    )
                    for c in contacts
                }
                self.steam_error = (
                    "Статус Steam — только подсказка; участие подтверждается сообщением"
                )
        except Exception:
            self.last_steam = now + 480
            self.steam_error = "Steam недоступен. Повтор через 10 минут; готовность не изменена"
        finally:
            self.steam_busy = False

    def _worker(self):
        while not self.closed.is_set():
            self.wake.clear()
            try:
                for _ in range(64):
                    try:
                        kind, source, data = self.inbox.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "incoming":
                        self.receive(source, data)
                    elif kind == "receipt":
                        self.receipt(source, data)
                    elif kind == "contacts":
                        self.contacts_result(source, data)
                self._require()
                with self.lock:
                    self._tick(time.time())
                self._send_one()
                self._refresh_steam()
            except (PermissionError, ValueError):
                pass
            except Exception:
                self.runtime.events.put(
                    ("error", "Организатор приостановил обработку; проверьте подключения")
                )
            with self.lock:
                pending = any(r["status"] in {"queued", "sending"} for r in self.state["outbox"])
                active = self.state.get("party") and self.state["party"]["status"] in {
                    "collecting",
                    "ready",
                }
            self.wake.wait(2 if pending else 30 if active else 60)

    def _finish(self, status):
        party = self._party()
        party["status"] = status
        for row in self.state["outbox"]:
            if row["party"] == party["id"] and row["status"] == "queued":
                row["status"] = "cancelled"
        self._persist()

    def _variant(self, event):
        key = (personality_style(self.runtime.settings()), event)
        value = self.message_variants.get(key, 0)
        self.message_variants[key] = (value + 1) % 4
        return value

    def _message(self, event, **values):
        return styled_message(self.runtime.settings(), event, self._variant(event), **values)

    def summary(self):
        with self.lock:
            return styled_summary(self.runtime.settings(), self.state.get("party"))

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(
                {
                    **self.state,
                    "steam": self.steam,
                    "steam_error": self.steam_error,
                    "summary": self.summary(),
                    "dictionary_size": sum(map(len, PHRASES.values())),
                    "personality": personality_style(self.runtime.settings()),
                    "personality_feature": FEATURES[personality_style(self.runtime.settings())],
                    "labels": MEMBERS,
                    "address_books": self.address_books,
                    "contact_status": self.contact_status,
                    "connections": self._connections(),
                }
            )

    def contacts_result(self, source, data):
        with self.lock:
            pending = self.contact_loads.get(source)
            if (
                not pending
                or pending["id"] != data.get("id")
                or time.time() - pending["started"] > 60
            ):
                return
            rows = data.get("rows", [])
            if not isinstance(rows, list):
                return
            for row in rows[:10]:
                if isinstance(row, dict) and re.fullmatch(
                    r"[1-9]\d{3,24}", str(row.get("chat", ""))
                ):
                    pending["rows"][str(row["chat"])] = dict(
                        chat=str(row["chat"]),
                        name=str(row.get("name", "Контакт"))[:60],
                        username=str(row.get("username", ""))[:60],
                    )
            if len(pending["rows"]) > 200:
                return
            if data.get("done"):
                if not data.get("error"):
                    self.address_books[source] = list(pending["rows"].values())
                self.contact_status[source] = str(
                    data.get("error")
                    or f"Загружено: {len(pending['rows'])}. Отметьте нужных друзей; приглашений пока нет."
                )[:200]
                self.contact_loads.pop(source, None)
                self.runtime.events.put(("organizer_changed", None))

    def _steam_friends(self):
        from steam_service import SteamClient

        try:
            cfg = self.runtime.settings()["api_keys"]
            client = SteamClient(cfg["steam_api_key"], cfg["steam_id"])
            response = client.api(
                "ISteamUser/GetFriendList/v1/", steamid=cfg["steam_id"], relationship="friend"
            )
            if "friendslist" not in response:
                raise ValueError("Список друзей Steam недоступен")
            friends = response["friendslist"].get("friends", [])
            ids = [
                str(r["steamid"])
                for r in friends
                if re.fullmatch(r"\d{17}", str(r.get("steamid", "")))
            ][:200]
            rows = []
            for start in range(0, len(ids), 100):
                if self.closed.is_set():
                    return
                players = (
                    client.api(
                        "ISteamUser/GetPlayerSummaries/v2/",
                        steamids=",".join(ids[start : start + 100]),
                    )
                    .get("response", {})
                    .get("players", [])
                )
                rows.extend(
                    dict(
                        chat=str(p["steamid"]),
                        name=str(p.get("personaname", "Друг Steam"))[:60],
                        username="Steam",
                    )
                    for p in players
                    if str(p.get("steamid", "")) in ids
                )
            with self.lock:
                self.address_books["steam"] = rows
                self.contact_status["steam"] = (
                    f"Steam: загружено друзей — {len(rows)}. Выберите профиль для связи с другом."
                )
        except Exception:
            self.contact_status["steam"] = (
                "Не удалось получить друзей Steam. Проверьте Web API Key, SteamID и доступность списка друзей."
            )
        finally:
            self.steam_busy = False

    def _connections(self):
        integration = getattr(self.runtime, "integrations", None)
        if integration is None:
            return {}
        result = {}
        for source in SERVICES:
            detail = integration.details(source)
            bridge = detail.get("bridge", {})
            result[source] = dict(
                ready=bool(bridge.get("ready") and detail.get("running", True)),
                capable="organizer_contacts" in bridge.get("capabilities", []),
            )
        return result

    def call(self, action, **values):
        if action == "snapshot":
            return self.snapshot()
        if action == "interpret":
            return interpret(values.get("text", ""), time.time(), self.state["aliases"]).dict()
        if action == "dictionary":
            query = normalize(values.get("query", ""))
            return [
                {"phrase": p, "kind": kind, "label": LABELS[kind]}
                for kind, phrases in PHRASES.items()
                for p in sorted(phrases)
                if not query or query in p
            ][:1000]
        if action == "recent_contacts":
            rows, seen = [], set()
            for n in self.runtime.center.list():
                source, target = n["source"], n.get("target", {})
                key = (source, target.get("chat"))
                if (
                    source == "tg_bridge.py"
                    and str(target.get("chat", "")).isdigit()
                    and key not in seen
                ):
                    seen.add(key)
                    rows.append(
                        dict(
                            service=source,
                            chat=target["chat"],
                            name=target.get("sender", "Контакт"),
                        )
                    )
            return rows[:50]
        self._require()
        if action == "steam_friends":
            with self.lock:
                if self.steam_busy:
                    return "Запрос Steam уже выполняется"
                self.steam_busy = True
                self.contact_status["steam"] = "Загружаю друзей Steam…"
            threading.Thread(
                target=self._steam_friends, name="krest-steam-friends", daemon=True
            ).start()
            return "Загружаю друзей Steam. История чатов не читается"
        if action == "contacts_load":
            source = values.get("service", self.state["preferences"]["messenger"])
            if source not in SERVICES:
                raise ValueError("Выберите Telegram или Discord")
            with self.lock:
                old = self.contact_loads.get(source)
                if old and time.time() - old["started"] < 60:
                    return "Список друзей уже загружается"
                ident = uuid.uuid4().hex
                self.contact_loads[source] = dict(id=ident, started=time.time(), rows={})
                self.contact_status[source] = "Загружаю друзей через юзербота…"
            try:
                self.runtime.integrations.send(source, "organizer_contacts", request_id=ident)
            except Exception:
                self.contact_loads.pop(source, None)
                self.contact_status[source] = "Запустите обновлённый юзербот в подключениях"
                raise
            return "Запрос списка друзей отправлен юзерботу"
        if action == "contact_save":
            return self.add_contact(**values)
        if action == "create":
            return self.create(**values)
        if action == "summary":
            return self.summary()
        if action == "launch":
            import webbrowser

            with self.lock:
                party = self._party()
                self._tick(time.time())
                if party["status"] != "ready":
                    raise ValueError(
                        "Ещё не все подтвердили готовность или время сбора не наступило"
                    )
                appid, channel = party["appid"], party["channel"]
                self._finish("started")
            webbrowser.open("steam://rungameid/" + appid)
            if channel:
                webbrowser.open(channel)
            return (
                "Команда запуска передана Steam"
                + ("; канал Discord открыт" if channel else "")
                + ". Подключение к голосу подтвердите в Discord."
            )
        with self.lock:
            if action in {"member_remove", "member_remind"}:
                party = self._party()
                selected = [
                    m
                    for m in party["members"]
                    if m["id"] == values.get("id")
                    or normalize(m["name"]) == normalize(values.get("name", ""))
                ]
                if len(selected) != 1:
                    raise ValueError("Выберите одного участника или назовите его точное имя")
                member = selected[0]
                if action == "member_remove":
                    if member.get("excluded"):
                        return "Участник уже исключён из этого сбора"
                    member.update(
                        state="declined", excluded=True, detail="Исключён владельцем из этого сбора"
                    )
                    for row in self.state["outbox"]:
                        if (
                            row["party"] == party["id"]
                            and row["contact"] == member["id"]
                            and row["status"] == "queued"
                        ):
                            row["status"] = "cancelled"
                    if self._invite_attempted(party, member):
                        self._queue(
                            self._contact(member["id"]),
                            self._message(
                                "close",
                                reason="Владелец исключил тебя из текущего сбора в "
                                + party["game"]
                                + ".",
                            ),
                            "removed",
                        )
                    self._fill(party, time.time())
                else:
                    if not member["invited_at"] or member["state"] in {
                        "declined",
                        "reserve",
                        "send_failed",
                    }:
                        raise ValueError("Участник не находится в активном составе")
                    if time.time() - member.get("reminded", 0) < 120:
                        raise ValueError(
                            "Уже напомнила; следующая просьба — не раньше чем через две минуты"
                        )
                    member["reminded"] = time.time()
                    self._queue(
                        self._contact(member["id"]),
                        self._message(
                            "check", reason="Владелец уточняет готовность к " + party["game"] + "."
                        ),
                        "remind",
                        party["id"],
                    )
                self._persist()
                return (
                    "Состав обновлён"
                    if action == "member_remove"
                    else "Уточнение готовности поставлено в очередь"
                )
            if action == "preferences":
                prefs = dict(
                    self.state["preferences"],
                    **{
                        k: v
                        for k, v in values.items()
                        if k in {"messenger", "game", "size", "when", "channel"}
                    },
                )
                if (
                    prefs["messenger"] not in SERVICES
                    or type(prefs["size"]) is not int
                    or not 2 <= prefs["size"] <= 10
                    or any(
                        not isinstance(prefs[k], str) or len(prefs[k]) > 150
                        for k in ("game", "when", "channel")
                    )
                ):
                    raise ValueError("Проверьте мессенджер, игру и размер состава (2–10)")
                if prefs["channel"] and not re.fullmatch(
                    r"https://discord\.com/channels/\d+/\d+", prefs["channel"]
                ):
                    raise ValueError("Нужна ссылка на канал Discord")
                if (
                    self.state.get("party")
                    and self.state["party"]["status"] in ACTIVE
                    and prefs["messenger"] != self.state["preferences"]["messenger"]
                ):
                    raise ValueError("Завершите сбор перед сменой мессенджера")
                self.state["preferences"] = prefs
                self._persist()
                return "Настройки компании сохранены"
            if action in {"pause", "cancel"}:
                party = self._party()
                if action == "cancel":
                    contacts = [
                        self._contact(m["id"])
                        for m in party["members"]
                        if self._invite_attempted(party, m)
                        and not m.get("excluded")
                        and m["state"] != "declined"
                    ]
                    self._finish("cancelled")
                    for contact in contacts:
                        self._queue(
                            contact,
                            self._message(
                                "close", reason="Владелец отменил сбор в " + party["game"] + "."
                            ),
                            "cancel",
                        )
                else:
                    self._finish("paused")
                self._persist()
                return (
                    control(self.runtime.settings(), "cancel")
                    if action == "cancel"
                    else control(self.runtime.settings(), "pause")
                    + " Начатая отправка может завершиться."
                )
            if action == "resume":
                party = self._party()
                if party["expires"] <= time.time():
                    raise ValueError("Время сбора истекло; создайте новый")
                party["status"] = "collecting"
                for m in party["members"]:
                    if m["state"] == "queued" and not any(
                        r["party"] == party["id"]
                        and r["contact"] == m["id"]
                        and r["purpose"] == "invite"
                        and r["status"] in {"sending", "uncertain", "delivered"}
                        for r in self.state["outbox"]
                    ):
                        m.update(state="reserve", invited_at=0)
                    elif m["state"] == "queued" and any(
                        r["party"] == party["id"]
                        and r["contact"] == m["id"]
                        and r["status"] == "uncertain"
                        for r in self.state["outbox"]
                    ):
                        m.update(
                            state="send_failed", detail="Отправка до перезапуска не подтверждена"
                        )
                self._fill(party, time.time())
                self._persist()
                return (
                    control(self.runtime.settings(), "resume")
                    + " Неподтверждённые отправки не повторяю."
                )
            if action == "contact_delete":
                if self.state.get("party") and self.state["party"]["status"] in ACTIVE:
                    raise ValueError("Завершите сбор перед удалением контакта")
                self.state["contacts"] = [
                    c for c in self.state["contacts"] if c["id"] != values["id"]
                ]
                for row in self.state["outbox"]:
                    if row["contact"] == values["id"] and row["status"] == "queued":
                        row["status"] = "cancelled"
                self._persist()
                return "Контакт удалён"
            if action == "contact_move":
                if self.state.get("party") and self.state["party"]["status"] in ACTIVE:
                    raise ValueError("Завершите сбор перед изменением порядка")
                rows = self.state["contacts"]
                at = next(i for i, c in enumerate(rows) if c["id"] == values["id"])
                positions = [i for i, c in enumerate(rows) if c["service"] == rows[at]["service"]]
                index = positions.index(at)
                other = positions[
                    max(0, min(len(positions) - 1, index + (1 if values.get("down") else -1)))
                ]
                rows[at], rows[other] = rows[other], rows[at]
                self._persist()
                return "Порядок приглашений обновлён"
            if action == "alias_save":
                phrase, kind = normalize(values.get("phrase", "")), values.get("kind")
                if not 1 <= len(phrase) <= 100 or kind not in {
                    "ready",
                    "joined",
                    "waiting",
                    "declined",
                    "uncertain",
                }:
                    raise ValueError("Выберите короткую фразу и значение")
                if any(phrase in words for words in PHRASES.values()):
                    raise ValueError(
                        "Эта фраза уже есть во встроенном словаре. Личный словарь дополняет его"
                    )
                if len(self.state["aliases"]) >= 100 and not any(
                    r["phrase"] == phrase for r in self.state["aliases"]
                ):
                    raise ValueError("В личном словаре допускается 100 фраз")
                self.state["aliases"] = [
                    r for r in self.state["aliases"] if r["phrase"] != phrase
                ] + [dict(phrase=phrase, kind=kind)]
                self._persist()
                return "Личная фраза сохранена. Вопросы и отрицания по-прежнему требуют проверки"
            if action == "alias_delete":
                self.state["aliases"] = [
                    r for r in self.state["aliases"] if r["phrase"] != values["phrase"]
                ]
                self._persist()
                return "Фраза удалена"
            if action == "request_reply":
                rows = [
                    r
                    for r in self.state["requests"]
                    if r["status"] in {"delivered", "reply_failed"}
                    or values.get("id")
                    and r["status"] == "reply_uncertain"
                ]
                if values.get("id"):
                    rows = [r for r in rows if r["id"] == values["id"]]
                else:
                    rows = [r for r in rows if time.time() - r.get("delivered", 0) < 120]
                if len(rows) != 1:
                    raise ValueError(
                        "Выберите конкретную просьбу в меню: адресат не определён однозначно"
                    )
                text = str(values.get("text", "")).strip()
                if not 1 <= len(text) <= 1000:
                    raise ValueError("Ответ должен содержать от 1 до 1000 символов")
                row = rows[0]
                self._queue(
                    self._contact(row["contact"]),
                    "Крест передаёт ответ владельца: " + text,
                    "request_reply",
                    request=row["id"],
                )
                row["status"] = "reply_queued"
                self._persist()
                return (
                    "Ответ для "
                    + row["name"]
                    + " поставлен в очередь; ожидаю подтверждение доставки"
                )
            if action == "request_done":
                for row in self.state["requests"]:
                    if row["id"] == values["id"]:
                        row["status"] = "closed"
                        for out in self.state["outbox"]:
                            if out.get("request") == row["id"] and out["status"] == "queued":
                                out["status"] = "cancelled"
                self._persist()
                return "Просьба закрыта"
        raise ValueError("Неизвестное действие организатора")
