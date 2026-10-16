"""Файл первого применения шлюза в чате ВПС и что с ним происходит дальше.

Шифрованного файла перевыпуска больше нет: кнопка его выпуска в старом
сообщении — устаревшая.

Файл первого применения — под инструкцией, с той же «⬅️ В меню»
(`bundle_cancel`): убирает файл и инструкцию, дальше карточка слота. Агент нового шлюза в первый час
после установки говорит каналом «установлен» (вид `installed`): админу —
«✅ Шлюз … успешно настроен» со ссылкой на бота шлюза, без кнопок, как
контент; если файл с инструкцией ещё в чате — они уходят и следом карточка
слота новым живым сообщением.

Тексты, подписи и кнопки этих экранов — в эталоне tests/screens/admin.txt
(adm.gw.bundle*, adm.rt.mark_yes.standby*, adm.rt.new_yes.standby,
adm.ev.gw_installed*); здесь — что удаляется, что
запоминается и что становится живым меню.

Цена ошибки: файл с ключом линка (или токеном агента) остаётся в чате после
того, как отслужил; «В меню» уводит не туда или удаляет чужое; две живые
клавиатуры в чате; уведомление о настроенном шлюзе с грязным именем Telegram отвергает."""
from __future__ import annotations

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import GwMarkCB, GwSlotCB, SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_slots_ui import _screen, _slot1, _slot2

pytestmark = pytest.mark.e2e
# два слота, заглушки линка и бандла, токены в памяти — сцена экранов слотов
slots = _slots_ui.slots
ADMIN = config.ADMIN_ID
CANCEL2 = SetCB(sec="rt", act="do", key="bundle_cancel", val="2").pack()
TOKEN2 = "222222222:BB-second-token-value-long-enough"


@pytest.fixture(autouse=True)
def _fresh_card_from_main(monkeypatch):
    """Пометка «карточку открыли с главной» — модульная, по чату админа."""
    from awgbot.bot.handlers import common as _common
    monkeypatch.setattr(_common, "_card_from_main", set())


class _Bot(FakeBot):
    """Бот, запоминающий клавиатуры отправленного; удаление может отказать
    (сообщение старше 48 ч или уже удалено руками)."""

    def __init__(self):
        super().__init__()
        self.markups: dict[int, object] = {}
        self.delete_fails = False

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        sent = await super().send_message(chat_id, text, reply_markup=reply_markup, **kw)
        self.markups[sent.message_id] = reply_markup
        return sent

    async def delete_message(self, chat_id, message_id, **kw):
        if self.delete_fails:
            raise RuntimeError("message to delete not found")
        await super().delete_message(chat_id, message_id, **kw)


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


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _deleted(bot) -> list[int]:
    return [r[2] for r in bot.records if r[0] == "delete_message"]


def _sent(bot) -> list[tuple]:
    return [r for r in bot.records if r[0] == "send_message"]


async def _card(services, bot, slot):
    """Карточка слота так, как её рисует обычный вход в неё."""
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
    return _screen(nav)


async def _issue_plain(services, bot, pi2, slot=2):
    """Назначение устройства Pi2 шлюзом слота 2 из «моих устройств» при
    известном токене: инструкция и файл первого применения."""
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi2.id, slot=0), services, FakeState())
    assert services.db.gateway_by_device(pi2.id).id == slot, "сцена собрана не так"
    assert len(nav.docs) == 1, f"файл первого применения не выдан: {nav.sent}"
    instr = next(s[3] for s in nav.sent if s[0] == "answer" and "--install" in s[1])
    return nav, instr, nav.docs[0]


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
    assert services.gw_bundle_msg_get(2) == {}


# ── файл первого применения ──────────────────────────────────────────────────

async def test_the_first_install_file_offers_menu_and_is_remembered(services, slots):
    """Файл первого применения (назначение устройства из «моих»): в записи —
    сообщение с инструкцией установки над ним, уберётся и она. Подпись и
    кнопки под файлом с номером нового слота (`bundle_cancel`: вернёт в
    карточку этого слота) — эталон adm.rt.mark_yes.standby."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    nav, instr, (_caption, _markup, doc) = await _issue_plain(services, _Bot(), pi2)
    where = services.gw_bundle_msg_get(2)
    assert where == {"chat": ADMIN, "file": doc.message_id, "instr": instr.message_id}, \
        "запись не найдёт файл и инструкцию или не знает, что это файл первого применения"


async def test_the_first_install_file_of_a_new_machine_is_remembered_too(services, slots):
    """«Новое устройство» с уже известным токеном — тот же путь выдачи (файл
    «Шлюз 2» и кнопки под ним — эталон adm.rt.new_yes.standby)."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes", slot=0), services, FakeState())
    assert len(nav.docs) == 1, nav.sent
    _caption, _markup, doc = nav.docs[0]
    instr = next(s[3] for s in nav.sent if s[0] == "answer" and "--install" in s[1])
    where = services.gw_bundle_msg_get(2)
    assert where == {"chat": ADMIN, "file": doc.message_id, "instr": instr.message_id}, \
        "запись не найдёт файл и инструкцию или не знает, что это файл первого применения"


async def test_menu_under_the_first_install_file_removes_it_and_opens_the_slot_card(services, slots):
    """«⬅️ В меню» под файлом первого применения: уходят файл (ключи и токен
    агента) и инструкция над ним, запись стёрта, дальше одно новое сообщение
    (карточка нового слота — эталон adm.rt.mark_yes.standby.menu)."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    _nav, instr, (_c, markup, doc) = await _issue_plain(services, bot, pi2)
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(_buttons(markup)[0][1]), services)
    assert {instr.message_id, doc.message_id} <= set(_deleted(bot)), \
        f"в чате остались файл или инструкция: удалены {_deleted(bot)}"
    assert services.gw_bundle_msg_get(2) == {}, "запись о файле пережила «В меню»"
    assert len([s for s in doc.sent if s[0] == "answer"]) == 1, "после «В меню» не одно новое сообщение"
    assert cb.answers, "колбэк без ответа — у кнопки крутятся часики"


async def test_menu_on_a_message_without_a_record_removes_only_that_message(services, slots):
    """«В меню» на файле, о котором записи нет (уже вытеснен новым), — уходит
    только нажатое сообщение: инструкция и файл, на которые указывает запись,
    остаются, запись цела; дальше — карточка слота."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    _nav, instr, (_c, _m, doc) = await _issue_plain(services, bot, pi2)
    want_text, _ = await _card(services, bot, 2)
    record = services.gw_bundle_msg_get(2)
    stale = _Msg(bot=bot)
    cb = FakeCallback(message=stale, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert _deleted(bot) == [stale.message_id], f"«В меню» на чужом сообщении удалила лишнее: {_deleted(bot)}"
    assert services.gw_bundle_msg_get(2) == record, "«В меню» на чужом сообщении стёрла запись"
    assert [s[1] for s in stale.sent if s[0] == "answer"] == [want_text]


async def test_an_old_menu_button_removes_only_its_message_and_opens_the_main_menu(services, slots):
    """Кнопка `bundle_menu` под файлом до 3.1.0: уходит само сообщение (как
    раньше — без записи и инструкции); запись о нынешнем файле цела (главная
    следом — эталон adm.gw.bundle.menu.old)."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    await _issue_plain(services, bot, pi2)
    record = services.gw_bundle_msg_get(2)
    older = _Msg(bot=bot)
    await sh.routing_action(FakeCallback(message=older, user_id=ADMIN, bot=bot),
                            SetCB(sec="rt", act="do", key="bundle_menu", val="2"), services)
    assert older.deleted, "сообщение со старым файлом осталось в чате"
    assert services.gw_bundle_msg_get(2) == record, "кнопка прежней версии стёрла запись о нынешнем файле"


# ── «В меню» под файлом: особые случаи ───────────────────────────────────────

async def test_menu_still_opens_the_card_when_telegram_refuses_to_delete(services, slots):
    """Удалить не дали (файл удалили руками раньше) — карточка всё равно
    показана, запись стёрта."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    _nav, _instr, (_c, _m, doc) = await _issue_plain(services, bot, pi2)
    want_text, _ = await _card(services, bot, 2)
    bot.delete_fails = True
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert [s[1] for s in doc.sent if s[0] == "answer"] == [want_text]
    assert services.gw_bundle_msg_get(2) == {}


async def test_menu_for_a_slot_removed_meanwhile_goes_to_the_main_menu(services, slots):
    """Слот сняли, пока файл лежал в чате: «В меню» убирает файл и не падает
    на карточке, которой нет (главная — эталон adm.gw.bundle.card.gone)."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    _nav, instr, (_c, _m, doc) = await _issue_plain(services, bot, pi2)
    services.db.gateway_delete(2)
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert {instr.message_id, doc.message_id} <= set(_deleted(bot)), _deleted(bot)
    assert len([s for s in doc.sent if s[0] == "answer"]) == 1, "после снятого слота ничего не показано"
    assert cb.answers


async def test_menu_without_a_slot_number_removes_only_its_own_message(services, slots):
    """Кнопка без номера слота (файл на заглушке): уходит само сообщение с
    файлом, чужие сообщения не трогаются, дальше — главная."""
    from awgbot.bot.handlers.admin import _panel_parts
    _, pi, _ = slots
    _slot1(services, pi)
    bot = _Bot()
    doc = _Msg(bot=bot)
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle_cancel", val=""), services)
    assert _deleted(bot) == [doc.message_id], _deleted(bot)
    main_text, _ = await _panel_parts(services)
    assert [s[1] for s in doc.sent if s[0] == "answer"] == [main_text]


# ── новый шлюз вышел на связь ────────────────────────────────────────────────

async def test_installed_gateway_removes_the_file_and_says_so_with_its_bot(services, slots):
    """Агент нового шлюза сказал «установлен»: файл (ключи и токен агента) и
    инструкция уходят; уведомление помечено контентом (возврат в меню его
    уберёт); следом — новое сообщение, оно и есть живое меню; запись стёрта.
    Тексты («✅ Шлюз … успешно настроен 🎉» с подписью слота и
    экранированным именем бота, карточка слота) — эталон
    adm.ev.gw_installed.label."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    _nav, instr, (_c, _m, doc) = await _issue_plain(services, bot, pi2)
    services.db.pop_content_msg_ids(ADMIN)                # что пометила сама выдача — не предмет теста
    await sh.bundle_installed(bot, services, 2)
    assert {instr.message_id, doc.message_id} <= set(_deleted(bot)), \
        f"файл с токеном агента или инструкция остались в чате: удалены {_deleted(bot)}"
    sent = _sent(bot)
    assert [r[1] for r in sent] == [ADMIN, ADMIN], f"не уведомление и карточка админу: {sent}"
    note_id, card_id = sorted(bot.markups)[-2:]
    assert services.db.pop_content_msg_ids(ADMIN) == [note_id], "уведомление не помечено контентом"
    assert services.db.get_nav_message_id(ADMIN) == card_id, "живым меню стала не карточка слота"
    assert services.gw_bundle_msg_get(2) == {}, "запись о файле пережила настройку шлюза"


async def test_installed_gateway_dims_the_previous_live_menu(services, slots):
    """Карточка после уведомления гасит прежнее живое меню (кнопки
    снимаются) — две живые клавиатуры в чате значат два пути сразу."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    await _issue_plain(services, bot, pi2)
    services.db.nav_touch(ADMIN, 4242)
    await sh.bundle_installed(bot, services, 2)
    assert ("edit_markup", ADMIN, 4242) in bot.records, "прежнее живое меню не погашено"


async def test_installed_gateway_without_a_file_in_chat_still_says_so(services, slots):
    """Файла в чате нет (записи нет) — удалять нечего, но о настроенном шлюзе
    админ узнаёт: одно уведомление и никакого нового меню — живое меню выше и
    так на месте (текст без кнопок — эталон adm.ev.gw_installed.nofile)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    services.db.nav_touch(ADMIN, 4242)
    await sh.bundle_installed(bot, services, 2)
    assert _deleted(bot) == [], "удалено то, чего бот не выдавал"
    assert len(_sent(bot)) == 1, f"не одно уведомление: {_sent(bot)}"
    assert services.db.get_nav_message_id(ADMIN) == 4242, "живое меню подменено уведомлением"
    assert not [r for r in bot.records if r[0] == "edit_markup"], "живое меню погашено без замены"


async def test_installed_gateway_after_menu_was_pressed_sends_no_second_menu(services, slots):
    """Файл уже убрали «В меню» (карточка пришла тогда) — уведомление одно,
    второй карточки нет, живое меню прежнее (текст — эталон
    adm.ev.gw_installed.nofile)."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    _nav, _instr, (_c, markup, doc) = await _issue_plain(services, bot, pi2)
    await sh.routing_action(FakeCallback(message=doc, user_id=ADMIN, bot=bot),
                            SetCB.unpack(CANCEL2), services)
    nav_before = services.db.get_nav_message_id(ADMIN)
    before = len(_sent(bot))
    await sh.bundle_installed(bot, services, 2)
    after = [r[2] for r in _sent(bot)][before:]
    assert len(after) == 1, f"после «В меню» не одно уведомление: {after}"
    assert services.db.get_nav_message_id(ADMIN) == nav_before, "живое меню подменено"


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
