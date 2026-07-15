"""Общий каркас тестов.

ВАЖНО: конфтест грузится pytest'ом ДО тестовых модулей — здесь ставится шим
enum.StrEnum для Python < 3.11 (проект боево работает на 3.12, но CI/песочница
может быть на 3.10). Без шима `from enum import StrEnum` в awgbot.core.enums упадёт
на импорте. Это тест-окруженческий костыль, не правка кода.
"""
import enum
import os
import sys

# ── шим StrEnum (py3.10) — до любого импорта awgbot ──────────────────────────
if not hasattr(enum, "StrEnum"):
    class StrEnum(str, enum.Enum):          # noqa: D401 — минимальный эквивалент 3.11+
        def __str__(self) -> str:           # члены печатаются как их строковое значение
            return str(self.value)
    enum.StrEnum = StrEnum

# ── корень репо на sys.path (дублирует pytest pythonpath, для надёжности) ─────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Секреты, которые config.validate() требует всегда (чтобы импорт config и часть
# тестов не спотыкались о них). Реальные значения тестам не нужны.
os.environ.setdefault("BOT_TOKEN", "12345:test-token")
os.environ.setdefault("ADMIN_ID", "1")

import pytest  # noqa: E402

# settings читает conf/*.yaml в горячий кэш; мигрированные точки (лимиты, пауза,
# grace, тихие часы, пороги алертов) идут через settings.get, поэтому кэш нужен
# инициализировать теми же conf-файлами, что читает config.py — иначе get вернёт
# дефолты вместо репозиторных значений и часть тестов разъедется.
from awgbot.core import config as _config          # noqa: E402
from awgbot.core import settings as _settings       # noqa: E402
_settings.init(_config.CONF_DIR)


@pytest.fixture()
def tz():
    """Часовой пояс проекта (UTC+3) — для конструирования aware-datetime в тестах."""
    from awgbot.util import timeutil
    return timeutil.TZ


# ─────────────────────────────────────────────────────────────────────────────
# Интеграционные фикстуры: временная БД, фейковый awg-слой, Services
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def db(tmp_path):
    """Настоящая SQLite-БД во временном файле (со схемой и служебным клиентом)."""
    from awgbot.infra.db import Database
    database = Database(str(tmp_path / "bot.db"))
    database.init_schema()
    yield database
    database.close()


@pytest.fixture()
def fake_awg(monkeypatch):
    """Замена awg-слоя (docker exec) на in-memory фейк. Патчим модуль
    awgbot.infra.awg — тот же объект, что видит services. AwgError оставляем
    настоящим, чтобы `except awg.AwgError` в коде работал.

    Возвращает объект состояния: .blocked (set IP), .peers, .clientstable,
    .occupied (живые IP из «конфига»), .responding, .started_at.
    """
    import threading
    import types

    from awgbot.infra import awg
    from awgbot.domain import configgen

    state = types.SimpleNamespace(
        blocked=set(), peers={}, clientstable={}, occupied=set(),
        responding=True, started_at="2026-01-01T00:00:00+03:00",
        _n=0, privpub={},
        ssh_targets=["172.29.172.1", "172.17.0.1", "88.218.78.157"],
        ssh_rules=None,
    )
    server_params = {
        "obfuscation": {k: str(i) for i, k in enumerate(configgen._OBF_ORDER)},
        "listen_port": 43125, "server_pubkey": "SRVPUB==", "psk": "PSK==",
    }

    def gen_keypair():
        state._n += 1
        priv, pub = f"priv{state._n}", f"pub{state._n}"
        state.privpub[priv] = pub
        return priv, pub

    def _set(name, fn):
        monkeypatch.setattr(awg, name, fn, raising=False)

    monkeypatch.setattr(awg, "mutation_lock", threading.Lock(), raising=False)
    _set("read_occupied_ips", lambda: set(state.occupied))
    _set("gen_keypair", gen_keypair)
    _set("pubkey_of", lambda priv: state.privpub.get(priv, f"derived-{priv}"))
    _set("read_server_params", lambda: {**server_params,
                                        "obfuscation": dict(server_params["obfuscation"])})
    _set("add_peer", lambda pub, psk, ip: state.peers.__setitem__(pub, (psk, ip)))
    _set("remove_peer", lambda pub: state.peers.pop(pub, None))
    _set("clientstable_upsert", lambda pub, name: state.clientstable.__setitem__(pub, name))
    _set("clientstable_remove", lambda pub: state.clientstable.pop(pub, None))
    _set("block_ip", lambda addr: state.blocked.add(addr))
    _set("unblock_ip", lambda addr: state.blocked.discard(addr))
    _set("is_blocked", lambda addr: addr in state.blocked)
    _set("container_started_at", lambda: state.started_at)
    _set("awg_responding", lambda: state.responding)
    _set("host_ssh_targets", lambda: list(state.ssh_targets))
    _set("ssh_reconcile",
         lambda admin_ips, targets: setattr(state, "ssh_rules",
                                            (sorted(admin_ips), list(targets))))
    _set("ensure_ssh_failsafe", lambda: False)
    return state


@pytest.fixture()
def services(db, fake_awg):
    """Services поверх временной БД и фейкового awg."""
    from awgbot.domain.services import Services
    return Services(db)


# ─────────────────────────────────────────────────────────────────────────────
# Харнесс хендлеров: фейковые aiogram Bot / Message / CallbackQuery / FSMContext.
# Тестируем тела хендлеров прямым вызовом (роутинг/фильтры покрыты smoke, гвард —
# отдельными тестами middleware). Все исходящие вызовы записываются в bot.records.
# ─────────────────────────────────────────────────────────────────────────────
import itertools as _itertools

_msg_ids = _itertools.count(1000)


class FakeUser:
    def __init__(self, uid, username=None, first_name="U"):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.full_name = first_name
        self.is_bot = False


class FakeChat:
    def __init__(self, cid, ctype="private"):
        self.id = cid
        self.type = ctype


class FakeBot:
    """Записывает исходящие вызовы. records — список кортежей ('вид', ...)."""
    def __init__(self):
        self.records = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.records.append(("send_message", chat_id, text))
        return FakeMessage(text=text, chat_id=chat_id, bot=self)

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **kw):
        self.records.append(("edit_markup", chat_id, message_id))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.records.append(("edit_message_text", chat_id, text))

    async def me(self):
        import types as _t
        return _t.SimpleNamespace(username="test_bot", id=999, is_bot=True, first_name="Bot")


class FakeMessage:
    def __init__(self, text=None, chat_id=1, user_id=1, bot=None, caption=None,
                 username=None, message_id=None, photo=None):
        self.text = text
        self.caption = caption
        self.photo = photo       # None или список PhotoSize (в фейке — маркер truthy)
        self.message_id = message_id if message_id is not None else next(_msg_ids)
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser(user_id, username=username)
        self.bot = bot
        self.sent = []           # что этот объект «ответил»

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append(("answer", text, reply_markup))
        if self.bot:
            self.bot.records.append(("answer", self.chat.id, text))
        return FakeMessage(text=text, chat_id=self.chat.id, user_id=self.from_user.id, bot=self.bot)

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kw):
        self.sent.append(("photo", caption, reply_markup))
        if self.bot:
            self.bot.records.append(("photo", self.chat.id, caption))
        return FakeMessage(caption=caption, chat_id=self.chat.id, user_id=self.from_user.id,
                           bot=self.bot, photo=["fake"])

    async def edit_media(self, media, reply_markup=None, **kw):
        self.sent.append(("edit_media", getattr(media, "caption", None), reply_markup))
        if self.bot:
            self.bot.records.append(("edit_media", self.chat.id))
        self.photo = ["fake"]
        return self

    async def answer_document(self, document, caption=None, **kw):
        self.sent.append(("document", caption, None))
        if self.bot:
            self.bot.records.append(("document", self.chat.id, caption))
        return FakeMessage(chat_id=self.chat.id, bot=self.bot)

    async def answer_animation(self, animation, caption=None, **kw):
        self.sent.append(("animation", caption, None))
        if self.bot:
            self.bot.records.append(("animation", self.chat.id, caption))
        return FakeMessage(chat_id=self.chat.id, bot=self.bot)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.sent.append(("edit_text", text, reply_markup))
        if self.bot:
            self.bot.records.append(("edit_text", self.chat.id, text))
        return self

    async def edit_reply_markup(self, reply_markup=None, **kw):
        if self.bot:
            self.bot.records.append(("edit_reply_markup", self.chat.id))
        return self

    async def delete(self):
        if self.bot:
            self.bot.records.append(("delete", self.chat.id))


class FakeCallback:
    def __init__(self, data="", message=None, user_id=1, bot=None):
        self.data = data
        self.message = message
        self.from_user = FakeUser(user_id)
        self.bot = bot
        self.answers = []        # (text, show_alert)

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    """Минимальный FSMContext: тестируемым хендлерам достаточно этих методов."""
    def __init__(self):
        self._data = {}

    async def clear(self):
        self._data = {}

    async def set_state(self, *a, **k):
        pass

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)


@pytest.fixture()
def fake_bot():
    return FakeBot()


@pytest.fixture()
def make_active_client(services):
    """Фабрика: создать клиента и сразу активировать инвайт. Возвращает Client."""
    def _make(name="Клиент", tg_id=1000, period_kind="year", device_limit=3,
              traffic_limit=0):
        created = services.create_client(name, device_limit, period_kind, traffic_limit)
        res = services.activate_client(created.invite_code, tg_id)
        assert res.ok, res.reason
        return res.client
    return _make
