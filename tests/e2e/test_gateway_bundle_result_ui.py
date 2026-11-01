"""Файл первого применения шлюза в чате ВПС, оставшийся от прежних выпусков,
и уведомление о настроенном шлюзе.

Файлов больше не выдаётся (шлюз подключается командой из бота); шифрованного
файла перевыпуска нет тем более — кнопка его выпуска в старом сообщении
устаревшая. Кнопки под файлом прежнего выпуска (`bundle_menu` до 3.1.0,
«🛰 Карточка шлюза» `bundle_cancel` и «⬅️ В меню» `bundle_home`) убирают само
нажатое сообщение — внутри ключ линка — и открывают главную; записи о том, где
лежит файл, с 3.4.0 нет. Агент нового шлюза в первый час после установки
говорит каналом «установлен» (вид `installed`): админу — «✅ Шлюз … успешно
настроен» со ссылкой на бота шлюза, без кнопок, как контент; живое меню выше
остаётся живым.

Тексты, подписи и кнопки этих экранов — в эталоне tests/screens/admin.txt
(adm.gw.bundle*, adm.rt.mark_yes.standby*, adm.rt.new_yes*,
adm.ev.gw_installed*); здесь — что удаляется и что становится живым меню.

Цена ошибки: файл с ключом линка (или токеном агента) остаётся в чате после
нажатия под ним; кнопка удаляет чужое; две живые клавиатуры в чате;
уведомление о настроенном шлюзе с грязным именем Telegram отвергает."""
from __future__ import annotations

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import GwMarkCB, SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_slots_ui import _slot1, _slot2

pytestmark = pytest.mark.e2e
# два слота, заглушки линка и бандла, токены в памяти — сцена экранов слотов
slots = _slots_ui.slots
ADMIN = config.ADMIN_ID
TOKEN2 = "222222222:BB-second-token-value-long-enough"


@pytest.fixture(autouse=True)
def _fresh_card_from_main(monkeypatch):
    """Пометка «карточку открыли с главной» — модульная, по чату админа."""
    from awgbot.bot.handlers import common as _common
    monkeypatch.setattr(_common, "_card_from_main", set())


class _Bot(FakeBot):
    """Бот, запоминающий клавиатуры отправленного."""

    def __init__(self):
        super().__init__()
        self.markups: dict[int, object] = {}

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        sent = await super().send_message(chat_id, text, reply_markup=reply_markup, **kw)
        self.markups[sent.message_id] = reply_markup
        return sent


class _Msg(FakeMessage):
    """Сообщение, у которого документ уходит с клавиатурой и возвращается
    настоящим сообщением с номером — как у Telegram; снятие клавиатуры с
    самого сообщения запоминается."""

    def __init__(self, **kw):
        kw.setdefault("chat_id", ADMIN)
        kw.setdefault("user_id", ADMIN)
        super().__init__(**kw)
        self.docs: list[tuple] = []           # (подпись, клавиатура, отправленное)
        self.markup_cleared = False

    async def answer_document(self, document, caption=None, reply_markup=None, **kw):
        sent = _Msg(chat_id=self.chat.id, bot=self.bot, caption=caption)
        self.docs.append((caption, reply_markup, sent))
        if self.bot:
            self.bot.records.append(("document", self.chat.id, caption))
        return sent

    async def answer(self, text, reply_markup=None, **kw):
        await super().answer(text, reply_markup=reply_markup, **kw)
        sent = _Msg(text=text, chat_id=self.chat.id, bot=self.bot)
        self.sent[-1] = (*self.sent[-1][:2], reply_markup, sent)
        return sent

    async def edit_reply_markup(self, reply_markup=None, **kw):
        if reply_markup is None:
            self.markup_cleared = True
        return await super().edit_reply_markup(reply_markup=reply_markup, **kw)


def _deleted(bot) -> list[int]:
    return [r[2] for r in bot.records if r[0] == "delete_message"]


def _sent(bot) -> list[tuple]:
    return [r for r in bot.records if r[0] == "send_message"]


# ── кнопки выпуска файла перевыпуска в старых сообщениях ─────────────────────

async def test_a_bundle_button_on_an_old_message_is_stale(services, slots):
    """«📤 Выпустить файл» в сообщении прежней версии: файла перевыпуска
    больше нет — кнопка устаревшая: «Кнопка устарела — открыл меню», кнопки
    сообщения сняты, главная новым сообщением, файл не выдан."""
    from awgbot.bot.handlers.admin import _panel_parts
    from awgbot.bot.handlers.stale import STALE_BUTTON
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    old = _Msg(bot=bot)
    cb = FakeCallback(message=old, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle", val="2"), services)
    assert old.docs == [], "по старой кнопке выдан файл"
    assert old.markup_cleared, "старое сообщение осталось с кнопками"
    assert cb.answers and cb.answers[0][0] == STALE_BUTTON, cb.answers
    main_text, _ = await _panel_parts(services)
    assert [s[1] for s in old.sent if s[0] == "answer"] == [main_text]


# ── файл первого применения ──────────────────────────────────────────────────

async def test_connecting_a_gateway_sends_no_file(services, slots):
    """«🛰 Подключить шлюз» из «моих устройств» и новым устройством: в чат —
    сообщение подключения с командой и ни одного файла (файлов первого
    применения больше нет)."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi2.id, slot=0), services, FakeState())
    assert nav.docs == [], "выдан файл"
    assert any(s[0] == "answer" and "--join AWGJ1." in s[1] for s in nav.sent), nav.sent
    assert services.db.gateway(2) is None, "строка слота до кода"


@pytest.mark.parametrize("key,val", [("bundle_menu", "2"), ("bundle_cancel", "2"), ("bundle_home", "2"),
                                     ("bundle_cancel", "")],
                         ids=["до 3.1.0", "карточка шлюза", "в меню", "без номера слота"])
async def test_a_button_under_an_old_file_removes_only_its_message_and_opens_the_main_menu(services, slots, key, val):
    """Кнопка под файлом прежнего выпуска: уходит само нажатое сообщение (в
    файле ключ линка), чужие сообщения не трогаются, дальше — главная новым
    сообщением (эталоны adm.gw.bundle*); колбэк отвечен."""
    from awgbot.bot.handlers.admin import _panel_parts
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    doc = _Msg(bot=bot)
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key=key, val=val), services)
    assert doc.deleted, "сообщение со старым файлом осталось в чате"
    assert _deleted(bot) in ([], [doc.message_id]), f"удалено чужое: {_deleted(bot)}"
    main_text, _ = await _panel_parts(services)
    assert [s[1] for s in doc.sent if s[0] == "answer"] == [main_text]
    assert cb.answers, "колбэк без ответа — у кнопки крутятся часики"


# ── новый шлюз вышел на связь ────────────────────────────────────────────────

async def test_installed_gateway_says_so_once_and_keeps_the_live_menu(services, slots):
    """Агент нового шлюза сказал «установлен»: одно уведомление админу,
    помечено контентом (возврат в меню его уберёт), ничего не удалено, живое
    меню выше не подменено и не погашено (текст без кнопок — эталоны
    adm.ev.gw_installed*)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    services.db.nav_touch(ADMIN, 4242)
    services.db.pop_content_msg_ids(ADMIN)
    await sh.bundle_installed(bot, services, 2)
    assert _deleted(bot) == [], "удалено то, чего бот не выдавал"
    sent = _sent(bot)
    assert [r[1] for r in sent] == [ADMIN], f"не одно уведомление админу: {sent}"
    assert services.db.pop_content_msg_ids(ADMIN) == [max(bot.markups)], "уведомление не помечено контентом"
    assert services.db.get_nav_message_id(ADMIN) == 4242, "живое меню подменено уведомлением"
    assert not [r for r in bot.records if r[0] == "edit_markup"], "живое меню погашено без замены"


def test_installed_text_escapes_names_and_falls_back_to_the_username():
    """Имя слота и имя бота задаёт человек: `<` и `&` без экранирования
    Telegram отвергает, и уведомление не дошло бы вовсе. Бот без имени —
    подписан своим username."""
    got = texts.gateway_installed_text("A<B> (x & y)", {"username": "b_bot", "name": "Шлюз <Pi2> & co"})
    assert got == ('✅ Шлюз <b>A&lt;B&gt; (x &amp; y)</b> успешно настроен 🎉\n'
                   '<b>Бот шлюза:</b> <a href="https://t.me/b_bot">Шлюз &lt;Pi2&gt; &amp; co</a>'), got
    assert texts.gateway_installed_text("Pi", {"username": "pi_bot"}).endswith(
        '<a href="https://t.me/pi_bot">pi_bot</a>')
    for none in ({}, None, {"username": "", "name": "x"}):
        assert texts.gateway_installed_text("Pi", none) == "✅ Шлюз <b>Pi</b> успешно настроен 🎉", none
