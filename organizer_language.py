"""Bounded, deterministic interpretation of private replies. No model or network."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import re
import unicodedata


def normalize(text):
    text = unicodedata.normalize("NFKC", str(text)[:2000]).casefold().replace("ё", "е")
    text = re.sub(r"([а-яa-z])\1{2,}", r"\1\1", text)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s:+?'-]", " ", text)).strip()


# Whole phrases, never substring matches such as «готов» inside «не готов».
BANK = {
    "ready": """готов|готова|готовы|я готов|я готова|все готово|я на месте|я тут|я здесь|на месте|на связи|у компа|я у компа|я за компом|я дома и готов|можем начинать|можно начинать|начинаем|давай начинать|давайте начинать|погнали|погнали уже|погнали играть|погнали катать|погнали катку|погнали в катку|погнали пацаны|погнали ребята|го|го го|гоу|го го го|го катку|го играть|го катать|го некст|го доту|го в доту|го кс|го в кс|го варф|го в варф|гогого|гогогого|гг го|летс го|летсго|lets go|let's go|ready|rdy|im ready|i'm ready|i am ready|ready now|go|go go|lets play|let's play|давай|давайте|давай го|да го|да погнали|гоу играть|поехали|полетели|полетели играть|залетел|залетела|я залетел|я залетела|я в лобби|в лобби|в пати|я в пати|жду инвайт|кидай инвайт|кинь инвайт|давай инвайт|инвайтни|инвайтни меня|приглашай|зови|зови меня|можешь звать|можете звать|можем стартовать|стартуем|запускай|запускайте|я освободился|освободился|освободилась|я свободен|свободен|свободна|я вернулся|я вернулась|вернулся|вернулась|вернулсо|докатал|докатала|катка закончилась|закончил катку|готов играть|готов катать|готов фармить|можно старт|го старт|жду вас|жду остальных|плюс готов|+ готов|плюс я готов|на низком старте|я в дисе|в дисе|в войсе|я в войсе|уже в войсе|уже в лобби|весь во внимании|тут я|на месте я|gotov|gotova|pogнали|pognali|ya gotov|ya tut|go dota|go cs|da go""",
    "joined": """да|ага|угу|агась|угум|ок|окей|окей да|ок да|ок го позже|okay|ok|yes|yep|yeah|sure|да конечно|конечно|согласен|согласна|я за|за|я с вами|с вами|с вами буду|я буду|буду|буду играть|буду катать|приду|подключусь|залечу|залечу к вам|залетаю|зайду|зайду к вам|в деле|я в деле|я в теме|я с вами в деле|вписываюсь|впишусь|вписался|плюс|плюсую|плюсану|плюс один|+|+1|я плюс|меня запиши|запиши меня|считай меня|я участвую|участвую|берете меня|да я с вами|да буду|ну давай|ну давайте|норм|нормас|нормально|звучит хорошо|договорились|договор|договорняк|заметано|заметано тогда|добро|есть|принято|понял|поняла|понял принял|принял|приняла|ладно|ладушки|ладно давай|хорошо|хорош|окидоки|окей окей|ок ок|оукей|oki|oke|da|aga|угу да|не против|я не против|почему бы и нет|да без проблем|без проблем|изи|легко|да изи|да легко|will join|count me in|im in|i'm in|i am in|join|joining|budu|zalety|zaletchu""",
    "declined": """нет|не|неа|не-а|неть|нетушки|не сегодня|сегодня нет|сегодня не буду|сегодня пас|я пас|пас|пасс|pass|no|nope|nah|нет спасибо|не хочу|не хочется|не буду|не играю|не поиграю|не смогу|не могу|сегодня не могу|я не могу|я не смогу|я не буду|без меня|играйте без меня|катайте без меня|го без меня|начинайте без меня|стартуйте без меня|не ждите|не ждите меня|меня не ждите|меня не будет|не приду|не зайду|не залечу|отбой|я отбой|отмена|отменяй меня|вычеркивай|вычеркни меня|я передумал|передумал|передумала|не вывожу|не вывозю|я спать|иду спать|пойду спать|спать|отрубаюсь|вырубаюсь|устал|устала|я устал|слишком устал|работаю|я на работе|на работе|занят сегодня|я занят сегодня|сегодня занят|занят на весь вечер|сегодня никак|не получится|не выйдет|облом|не судьба|мимо|я мимо|скипаю|скип|skip|skipping|скипну|откажусь|отказ|нет желания|не в настроении|я ливаю|ливаю|ливну|я ливну|я ушел|я ушла|я ухожу|ухожу|передумал играть|уже не буду|больше не играю|not today|can't join|cant join|not playing|im out|i'm out|i am out|нет сорян|сорян пас|сори пас|пас сорян|сорян не смогу|сорри не могу|ne budu|ne mogu|ne smogu|bez menya|net|ne segodnya|segodnya pas""",
    "waiting": """позже|попозже|потом|не сейчас|щас не могу|сейчас не могу|еще не готов|не готов|не готова|я еще не готов|пока не готов|подожди|подождите|погоди|погодите|погодь|падажди|погоди немного|погодите чутка|чуть позже|чутка позже|чуток позже|погодь чутка|подождите меня|жди|ждите|жди меня|пока ждите|пока занят|занят|я занят|сек|секунду|секундочку|секунду щас|сек ща|секунду погоди|минутку|минуточку|ща|щас|щя|щяс|щща|щас буду|ща буду|сейчас буду|щас зайду|ща зайду|ща залечу|щас залечу|сейчас зайду|ща приду|щас приду|скоро|скоро буду|скоро зайду|скоро залечу|почти готов|почти|сейчас закончу|щас закончу|докатаю|доиграю|после катки|после этой катки|после матча|после игры|после раунда|катку докатаю|докатаю катку|щас катку докатаю|доиграю катку|заканчиваю катку|заканчиваю|ем|кушаю|поем и зайду|поем и залечу|после еды|пока ем|я ем|я кушаю|сейчас поем|покушаю и зайду|после душа|в душе|душ и зайду|схожу покурю|покурю и зайду|перекур|чай сделаю|делаю чай|отошел|отошла|я отошел|я отошла|афк|я афк|afk|brb|бриб|be right back|back soon|later|wait|wait a bit|one sec|sec|soon|not yet|not ready|not now|shas|schas|sha|schya|pozzhe|potom|doigrayu|dokatyvayu|жду обнову|обновляюсь|обнова|скачиваю|скачиваю обновление|докачаю и зайду|докачивается|запускаю игру|игра грузится|грузится|загружаюсь|заходит|перезапускаю|перезагружусь|ребут|перезагрузка|дис перезапущу|подключаюсь|захожу|захожу в игру|минуту дай|дай минутку|дайте минуту|пару сек|пару секунд|щас погодь""",
    "uncertain": """может|может быть|возможно|наверное|наверно|мб|мб позже|мб зайду|мб залечу|может зайду|может залечу|может позже|посмотрим|посмотрю|подумаю|не знаю|незнаю|не уверен|не уверена|не факт|хз|хз пока|хз еще|пока хз|хрен знает|хуй знает|фиг знает|хз мб|под вопросом|вопрос|не обещаю|ничего не обещаю|не гарантирую|постараюсь|попробую|если получится|если успею|как получится|как пойдет|по возможности|50 на 50|50 50|пятьдесят на пятьдесят|пока не скажу|не решил|не решила|думаю|мейби|мейб|maybe|perhaps|idk|not sure|dunno|mb|hz|ne znayu|posmotrim""",
    "question": """когда|во сколько|сколько ждать|че играем|что играем|во что играем|во что|куда|кто идет|кто играет|кто будет|кто с нами|кто еще|сколько нас|сколько человек|с кем|какая игра|какой режим|какой сервак|какой сервер|когда старт|когда начинаем|во скок|воскок|во скока|во сколько играем|че по времени|что по времени|а когда|а во сколько|когда сбор|а кто|кто собрался|есть место|место есть|есть слот|сколько мест|where|when|what game|who|when start|во сколько старт|кто там|кто в пати|че за игра|го куда|во сколько собираемся""",
}
PHRASES = {
    kind: frozenset(normalize(v) for v in values.split("|")) for kind, values in BANK.items()
}
ALL_PHRASES = frozenset().union(*PHRASES.values())
LABELS = {
    "ready": "Готов",
    "joined": "Участвует; готовность ещё не подтверждена",
    "delayed": "Будет позже",
    "declined": "Отказался",
    "waiting": "Просит подождать",
    "uncertain": "Пока не решил",
    "question": "Задаёт вопрос",
    "unknown": "Нужно уточнение",
}
NUMBERS = {
    "одну": 1,
    "один": 1,
    "одна": 1,
    "две": 2,
    "два": 2,
    "пару": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "пяток": 5,
    "пятерочку": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "десятку": 10,
    "десяточку": 10,
    "пятнадцать": 15,
    "пятнашку": 15,
    "двадцать": 20,
    "двадцатку": 20,
    "двадцать пять": 25,
    "тридцать": 30,
    "сорок": 40,
    "сорок пять": 45,
    "пятьдесят": 50,
    "шестьдесят": 60,
}
NUM_PATTERN = r"(?:\d{1,3}|" + "|".join(sorted(NUMBERS, key=len, reverse=True)) + r")"
UNCERTAIN = re.compile(
    r"\b(?:может|возможно|наверно|наверное|мб|хз|если|постараюсь|попробую|вряд ли|не обещаю|не уверен|не факт|maybe|idk|mb|hz)\b"
)
NEGATIVE = re.compile(
    r"\b(?:не (?:буду|смогу|зайду|приду|залечу|хочу)|без меня|не ждите|сегодня пас|сегодня не|передумал|передумала|скипаю|ливаю)\b"
)
FUTURE = re.compile(
    r"\b(?:через|минут|минута|минуты|мин|час|часа|часов|вечером|завтра|после|позже|потом|буду|зайду|залечу|скоро|ща|щас)\b"
)


@dataclass(frozen=True)
class Interpretation:
    kind: str
    reason: str
    due: float | None = None
    approximate: bool = False

    def dict(self):
        return {**asdict(self), "label": LABELS[self.kind]}


def delay_time(text, now):
    """Only explicit duration/time. An ETA is not a readiness confirmation."""
    s = normalize(text)
    if re.search(r"\bчерез (?:полчаса|пол часика)\b", s):
        return now + 1800, False
    if re.search(r"\bчерез полтора (?:часа|часика)\b", s):
        return now + 5400, False
    if re.search(r"\bчерез (?:час|часик)\b", s):
        return now + 3600, False
    match = re.search(
        rf"\b(?:через\s+)?(?P<n>{NUM_PATTERN})(?:\s*-\s*(?P<end>\d{{1,3}}))?\s*(?P<unit>минут\w*|мин|минуточ\w*|час\w*|minutes?|mins?|hours?|hrs?)\b",
        s,
    ) or re.search(rf"\b(?P<unit>минут\w*|мин|час\w*)\s+(?:через\s+)?(?P<n>{NUM_PATTERN})\b", s)
    if match:
        word = match["n"]
        n = int(word) if word.isdigit() else NUMBERS[word]
        end = match.groupdict().get("end")
        if end:
            if int(end) < n:
                return None, False
            n = int(end)
        seconds = n * (3600 if re.match(r"час|hour|hr", match["unit"]) else 60)
        if 0 < seconds <= 86400:
            return now + seconds, bool(end or word == "пару")
    match = re.search(r"\b(?:в|к|at)\s+(\d{1,2})(?::(\d{2}))?\b", s)
    if match:
        hour, minute = int(match[1]), int(match[2] or 0)
        if hour < 24 and minute < 60:
            value = datetime.fromtimestamp(now).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if "завтра" in s.split():
                value += timedelta(days=1)
            if value.timestamp() > now:
                return value.timestamp(), False
    return None, False


def interpret(text, now, custom=None):
    raw = str(text)[:2000]
    if raw.strip() in {"👍", "👌", "✅", "➕"}:
        return Interpretation("joined", "Короткое подтверждение; готовность нужно уточнить")
    if raw.strip() in {"❌", "🚫", "🙅"}:
        return Interpretation("declined", "Явный знак отказа")
    s = normalize(raw)
    if s and not s.strip("+"):
        s = "+"
    if s not in ALL_PHRASES and not re.search(r"[а-я]", s):
        fixed = s.translate(
            str.maketrans("qwertyuiop[]asdfghjkl;'zxcvbnm,.", "йцукенгшщзхъфывапролджэячсмитьбю")
        )
        if any(fixed in bank for bank in PHRASES.values()):
            s = fixed
    shortened = re.sub(r"([а-яa-z])\1+", r"\1", s)
    if any(shortened in bank for bank in PHRASES.values()):
        s = shortened
    s = re.sub(r"\b(?:чз|чез)\b", "через", s)
    s = re.sub(r"\bнеготов(а)?\b", r"не готов\1", s)
    if not s:
        return Interpretation("unknown", "Нет текстового ответа")
    if "?" in raw or s in PHRASES["question"]:
        return Interpretation("question", "Вопрос не подтверждает участие")
    if re.search(
        r"\b(?:он|она|они|друг) (?:сказал\w*|пишет|говорит|готов\w*|будет)\b|[«»\"“”]",
        raw.casefold(),
    ):
        return Interpretation("unknown", "Цитата или сообщение о другом человеке")
    if s in PHRASES["uncertain"] or UNCERTAIN.search(s):
        return Interpretation("uncertain", "Ответ содержит сомнение или условие")
    if s in PHRASES["declined"] or NEGATIVE.search(s):
        return Interpretation("declined", "Явный отказ или отмена участия")
    # Other negations must not be swallowed by a positive word or a custom alias.
    negative = re.search(r"\b(?:не|нет|not|cant|can't)\b", s) and s not in PHRASES["joined"]
    due, approximate = delay_time(s, now)
    if due is not None:
        if negative and not re.search(r"не (?:сейчас|готов|готова|могу сейчас)", s):
            return Interpretation("unknown", "Отрицание рядом со временем; нужно уточнение")
        return Interpretation(
            "delayed", "Указано время ожидания; готовность ещё не подтверждена", due, approximate
        )
    if re.search(r"\bчерез\b|\b(?:в|к|at)\s+\d", s):
        return Interpretation("unknown", "Время указано неполно или уже прошло")
    if s in PHRASES["waiting"] or re.search(
        r"\b(?:после (?:катки|игры|матча|еды|душа)|докатаю|доиграю|щас|ща|скоро)\b", s
    ):
        return Interpretation("waiting", "Просит ждать, точного времени нет")
    if negative:
        return Interpretation("unknown", "Отрицание требует уточнения")
    # Ignore only a small, explicit set of conversational fillers at the edges.
    simple = re.sub(r"^(?:ну |короче |крч |лан |ладно )", "", s)
    simple = re.sub(r" (?:брат|бро|пацаны|ребята|дружище|блять|бля|нахуй)$", "", simple)
    for kind in ("ready", "joined", "waiting", "declined"):
        if s in PHRASES[kind] or simple in PHRASES[kind]:
            return Interpretation(kind, "Совпадение с фразой словаря")
    if FUTURE.search(s):
        return Interpretation("uncertain", "Будущее действие без точной договорённости")
    for row in custom or ():
        if normalize(row.get("phrase", "")) == s and row.get("kind") in {
            "ready",
            "joined",
            "declined",
            "waiting",
            "uncertain",
        }:
            return Interpretation(row["kind"], "Личная фраза из словаря")
    return Interpretation("unknown", "Фраза не распознана; согласие не предполагается")


def request_text(text):
    """A request to tell the owner; never an executable PC command."""
    raw = str(text)[:2000].strip()
    if re.search(
        r"\bне\s+(?:(?:надо|нужно)\s+)?(?:передай|передавать|скажи|говорить|напомни|напоминать)\b",
        raw,
        re.I,
    ):
        return ""
    match = re.search(
        r"\b(?:передай|скажи|напомни)\s+(?:(?:ему|ей|хозяину|владельцу)\s+)?(?:что\s+)?(.+)",
        raw,
        re.I,
    )
    if match:
        return match[1].strip()[:700]
    if re.search(
        r"(?:когда|как)\s+(?:освободится|выйдет|закончит).*(?:жду|позови|напиши|пусть)", raw, re.I
    ):
        return raw[:700]
    return ""
