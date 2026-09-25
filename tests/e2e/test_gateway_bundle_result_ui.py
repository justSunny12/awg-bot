"""Файл конфигурации шлюза в чате ВПС и что с ним происходит дальше.

Шифрованный файл выпускается прямо из карточки слота («⚙️ Конфигурация
шлюза»): карточка гаснет и становится контентом, под файлом — «⬅️ В меню»
(`bundle_cancel` с номером слота); где лежат файл и карточка над ним —
запомнено. «В меню» убирает оба и открывает карточку слота. Итог применения
с шлюза (канал линка, вид `applied`) сообщения в чат не шлёт — его человек
видел в чате бота шлюза: успех убирает файл с карточкой и ставит карточку
слота новым живым меню, отказ и итог о другом файле не трогают ничего.

Файл первого применения — под инструкцией, с «⬅️ В меню» (`bundle_menu`):
убирает файл и инструкцию, дальше главная. Выдача ставит ожидание нового
шлюза: его первый выход на связь каналом убирает файл с инструкцией и
приносит «✅ Шлюз … успешно настроен» со ссылкой на бота шлюза и «Назад» в
карточку.

Цена ошибки: файл с ключом линка (или токеном агента) остаётся в чате после
того, как отслужил; «В меню» уводит не туда или удаляет чужое; итог о
прежнем файле убирает новый, который ещё пересылать; две живые клавиатуры в
чате; уведомление о настроенном шлюзе с грязным именем Telegram отвергает."""
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
MENU2 = SetCB(sec="rt", act="do", key="bundle_menu", val="2").pack()
TOKEN2 = "222222222:BB-second-token-value-long-enough"


@pytest.fixture(autouse=True)
def _fresh_card_home(monkeypatch):
    """Пометка «карточку открыли с главной» — модульная, по чату админа."""
    from awgbot.bot.handlers import common as _common
    monkeypatch.setattr(_common, "_card_home", set())


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


async def _issue(services, bot, slot=2):
    """Админ в карточке слота жмёт «⚙️ Конфигурация шлюза»: вернуть карточку
    (над файлом) и сообщение с файлом."""
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gw_slot_bundle(cb, GwSlotCB(action="bundle", slot=slot), services)
    assert len(nav.docs) == 1, f"файл конфигурации не выдан: {nav.sent}"
    nav.cb = cb
    return nav, nav.docs[0]


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


# ── выпуск из карточки ───────────────────────────────────────────────────────

async def test_the_card_button_issues_the_file_at_once_and_the_card_goes_dark(services, slots):
    """«⚙️ Конфигурация шлюза» в карточке — файл сразу, без промежуточного
    экрана. Карточка теряет клавиатуру (живое меню одно — «В меню» на файле)
    и помечается контентом: возврат в меню её уберёт. Под файлом одна кнопка —
    «⬅️ В меню» с номером слота; запись указывает на файл и на карточку над
    ним — итог с шлюза должен найти оба."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav, (_caption, markup, doc) = await _issue(services, bot, 2)
    assert nav.cb.answers == [("Собираю и шифрую…", False)], nav.cb.answers
    assert nav.markup_cleared, "карточка над файлом осталась с кнопками — два живых меню"
    assert not [s for s in nav.sent if s[0] == "edit_text"], "вместо файла нарисован промежуточный экран"
    assert nav.message_id in services.db.pop_content_msg_ids(ADMIN), \
        "карточка не помечена как контент — возврат в меню её не уберёт"
    assert _buttons(markup) == [("⬅️ В меню", CANCEL2)], _buttons(markup)
    where = services.gw_bundle_msg_get(2)
    assert len(where.pop("fp", "")) == 16, "запись без отпечатка файла"
    assert where == {"chat": ADMIN, "file": doc.message_id, "instr": nav.message_id, "plain": False}, \
        "итог не найдёт файл и карточку над ним (или файл записан как файл первого применения)"
    assert services.gw_bundle_msg_get(1) == {}, "файл слота 2 записан за слотом 1"


async def test_the_file_caption_says_where_to_forward_and_that_menu_removes_it(services, slots):
    """Подпись под шифрованным файлом: для какого шлюза, что переслать и
    кому, где будет итог и что «В меню» уберёт сообщение."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дом 2")
    _nav, (caption, _m, _doc) = await _issue(services, _Bot(), 2)
    assert caption == ("⚙️ Конфигурация шлюза <b>«Pi2» (дом 2)</b>.\n"
                       "Перешли это сообщение боту шлюза — он проверит и применит сам.\n"
                       "Результат применения конфигурации сообщит бот шлюза.\n\n"
                       "ℹ️ Возврат в меню удалит это сообщение"), caption


async def test_a_bundle_button_on_an_old_message_issues_the_same_way(services, slots):
    """«📤 Выпустить файл» на сообщении из прежней версии (экран до выпуска
    упразднён, а сообщение могло остаться в чате) — тот же выпуск: файл с
    «В меню», сообщение над ним гаснет и запомнено."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    old = _Msg(bot=bot)
    cb = FakeCallback(message=old, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle", val="2"), services)
    assert len(old.docs) == 1, old.sent
    _c, markup, doc = old.docs[0]
    assert _buttons(markup) == [("⬅️ В меню", CANCEL2)], _buttons(markup)
    assert old.markup_cleared, "старое сообщение над файлом осталось с кнопками"
    where = services.gw_bundle_msg_get(2)
    assert (where["file"], where["instr"]) == (doc.message_id, old.message_id), where


async def test_a_reissued_file_removes_the_previous_one_and_takes_its_record(services, slots):
    """Выпустили файл слота второй раз — прежний файл и карточка над ним
    уходят из чата (в файле тот же ключ линка, а итог придёт только на
    новый), запись указывает на новый файл и его карточку."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav1, (_c, _m, doc1) = await _issue(services, bot, 2)
    assert _deleted(bot) == [], "первая выдача что-то удалила"
    nav2, (_c, _m, doc2) = await _issue(services, bot, 2)
    assert {nav1.message_id, doc1.message_id} <= set(_deleted(bot)), \
        f"прежний файл с ключом линка остался в чате: удалены {_deleted(bot)}"
    assert not {nav2.message_id, doc2.message_id} & set(_deleted(bot)), "удалён только что выданный файл"
    where = services.gw_bundle_msg_get(2)
    assert (where["file"], where["instr"]) == (doc2.message_id, nav2.message_id), where


async def test_reissue_of_one_slot_keeps_the_file_of_another(services, slots):
    """Перевыпуск для слота 2 не трогает лежащий в чате файл слота 1."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav1, (_c, _m, doc1) = await _issue(services, bot, 1)
    await _issue(services, bot, 2)
    await _issue(services, bot, 2)
    assert not {nav1.message_id, doc1.message_id} & set(_deleted(bot)), "перевыпуск слота 2 убрал файл слота 1"
    assert services.gw_bundle_msg_get(1)["file"] == doc1.message_id


async def test_a_failed_build_remembers_nothing(services, slots, monkeypatch):
    """Файл не собрался — записи нет: итогу нечего убирать, «В меню» тоже."""
    from awgbot.domain.services import ServiceError
    _, pi, _ = slots
    _slot1(services, pi)

    def _fail(slot=None):
        raise ServiceError("нет ключей")
    monkeypatch.setattr(services, "gw_bundle_encrypted", _fail)
    msg = _Msg(bot=_Bot())
    assert await sh.send_gw_bundle(msg, services, 1, instr_id=77) is False
    assert services.gw_bundle_msg_get(1) == {}


# ── файл первого применения ──────────────────────────────────────────────────

async def test_the_first_install_file_offers_menu_and_is_remembered(services, slots):
    """Файл первого применения (назначение устройства из «моих») — под ним
    «⬅️ В меню» с номером нового слота (`bundle_menu`: шлюз уже назначен,
    отменять нечего), подпись отсылает к инструкции выше; в записи —
    сообщение с инструкцией установки над ним: уберётся и она."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    nav, instr, (caption, markup, doc) = await _issue_plain(services, _Bot(), pi2)
    assert _buttons(markup) == [("⬅️ В меню", MENU2)], _buttons(markup)
    assert caption == ("🛰 Файл конфигурации шлюза\n"
                       "Воспользуйся инструкцией выше для настройки нового шлюза: <b>«Pi2»</b>\n\n"
                       "После возврата в меню сообщение с файлом и инструкция удалятся из чата."), caption
    where = services.gw_bundle_msg_get(2)
    assert len(where.pop("fp", "")) == 16, "запись без отпечатка файла"
    assert where == {"chat": ADMIN, "file": doc.message_id, "instr": instr.message_id, "plain": True}, \
        "запись не найдёт файл и инструкцию или не знает, что это файл первого применения"


async def test_the_first_install_file_starts_waiting_for_the_new_gateway(services, slots):
    """Выдача файла первого применения ставит ожидание: первый полный снимок
    канала слота скажет админу «шлюз настроен». Без ожидания уведомления не
    будет вовсе; ожидание снимается один раз."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    assert services.gw_install_wait_take(2) is False, "ожидание стоит до выдачи файла"
    await _issue_plain(services, _Bot(), pi2)
    assert services.gw_install_wait_take(2) is True, "файл выдан, а нового шлюза не ждут"
    assert services.gw_install_wait_take(2) is False, "ожидание снимается не один раз — уведомление повторится"
    assert services.gw_install_wait_take(1) is False, "ожидание встало не на тот слот"


async def test_the_encrypted_file_does_not_wait_for_an_install(services, slots):
    """Перевыпуск шифрованного файла — не установка: шлюз и так на связи, и
    «успешно настроен» после каждого переподключения было бы враньём."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    await _issue(services, _Bot(), 2)
    assert services.gw_install_wait_take(2) is False


async def test_the_first_install_file_of_a_new_machine_is_remembered_too(services, slots):
    """«Новое устройство» с уже известным токеном — тот же путь выдачи."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes", slot=0), services, FakeState())
    assert len(nav.docs) == 1, nav.sent
    caption, markup, doc = nav.docs[0]
    assert _buttons(markup) == [("⬅️ В меню", MENU2)], _buttons(markup)
    assert "<b>«Шлюз 2»</b>" in caption, caption
    instr = next(s[3] for s in nav.sent if s[0] == "answer" and "--install" in s[1])
    where = services.gw_bundle_msg_get(2)
    assert len(where.pop("fp", "")) == 16, "запись без отпечатка файла"
    assert where == {"chat": ADMIN, "file": doc.message_id, "instr": instr.message_id, "plain": True}, \
        "запись не найдёт файл и инструкцию или не знает, что это файл первого применения"
    assert services.gw_install_wait_take(2) is True


async def test_menu_under_the_first_install_file_removes_it_with_the_instruction(services, slots):
    """«⬅️ В меню» под файлом первого применения: уходят файл (ключи и токен
    агента) и инструкция над ним, запись стёрта, главная — новым сообщением."""
    from awgbot.bot.handlers.admin import _panel_parts
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
    main_text, _ = await _panel_parts(services)
    assert [s[1] for s in doc.sent if s[0] == "answer"] == [main_text], "после «В меню» не главная"
    assert cb.answers, "колбэк без ответа — у кнопки крутятся часики"


async def test_menu_on_a_message_without_a_record_removes_only_that_message(services, slots):
    """«В меню» на файле, о котором записи нет (выдан до обновления бота или
    уже вытеснен новым), — уходит только нажатое сообщение: инструкция и
    файл, на которые указывает запись, остаются, запись цела."""
    from awgbot.bot.handlers.admin import _panel_parts
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    _nav, instr, (_c, _m, doc) = await _issue_plain(services, bot, pi2)
    record = services.gw_bundle_msg_get(2)
    stale = _Msg(bot=bot)
    cb = FakeCallback(message=stale, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(MENU2), services)
    # «Назначаю шлюз…» над инструкцией — контент, его уберёт любой возврат в
    # меню; предмет теста — файл и инструкция, на которые указывает запись
    assert stale.message_id in _deleted(bot), "нажатое сообщение с файлом осталось в чате"
    assert not {instr.message_id, doc.message_id} & set(_deleted(bot)), \
        f"«В меню» на чужом сообщении удалила файл или инструкцию из записи: {_deleted(bot)}"
    assert services.gw_bundle_msg_get(2) == record, "«В меню» на чужом сообщении стёрла запись"
    main_text, _ = await _panel_parts(services)
    assert [s[1] for s in stale.sent if s[0] == "answer"] == [main_text]

    # кнопка без номера слота (файл до 3.1.0) — тоже только само сообщение
    bot2 = _Bot()
    older = _Msg(bot=bot2)
    await sh.routing_action(FakeCallback(message=older, user_id=ADMIN, bot=bot2),
                            SetCB(sec="rt", act="do", key="bundle_menu"), services)
    assert _deleted(bot2) == [older.message_id], _deleted(bot2)
    assert services.gw_bundle_msg_get(2) == record


# ── «В меню» под шифрованным файлом ──────────────────────────────────────────

async def test_menu_removes_the_file_and_the_dark_card_and_opens_the_slot_card(services, slots):
    """«В меню» под шифрованным файлом: из чата уходят файл (внутри ключ
    линка) и погасшая карточка над ним, запись стирается, человек снова в
    карточке слота, из которого выпускал файл, — новым сообщением."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    want_text, want_labels = await _card(services, bot, 2)
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert {nav.message_id, doc.message_id} <= set(_deleted(bot)), \
        f"в чате остались файл или карточка: удалены {_deleted(bot)}"
    assert services.gw_bundle_msg_get(2) == {}, "запись о файле пережила «В меню»"
    assert cb.answers, "колбэк без ответа — у кнопки крутятся часики"
    shown = [s for s in doc.sent if s[0] == "answer"]
    assert len(shown) == 1, f"после «В меню» показано не одно сообщение: {shown}"
    _, text, markup, _sent = shown[0]
    assert text == want_text, "после «В меню» показана не карточка слота 2"
    assert [b.text for row in markup.inline_keyboard for b in row] == want_labels


async def test_menu_still_opens_the_card_when_telegram_refuses_to_delete(services, slots):
    """Удалить не дали (файл удалили руками раньше) — карточка всё равно
    показана, запись стёрта."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    want_text, _ = await _card(services, bot, 2)
    _nav, (_c, _m, doc) = await _issue(services, bot, 2)
    bot.delete_fails = True
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert [s[1] for s in doc.sent if s[0] == "answer"] == [want_text]
    assert services.gw_bundle_msg_get(2) == {}


async def test_menu_for_a_slot_removed_meanwhile_goes_to_the_main_menu(services, slots):
    """Слот сняли, пока файл лежал в чате: «В меню» убирает файл и ведёт на
    главную, а не падает на карточке, которой нет."""
    from awgbot.bot.handlers.admin import _panel_parts
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    services.db.gateway_delete(2)
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert {nav.message_id, doc.message_id} <= set(_deleted(bot)), _deleted(bot)
    main_text, _ = await _panel_parts(services)
    assert [s[1] for s in doc.sent if s[0] == "answer"] == [main_text], "не главная после снятого слота"
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


async def test_menu_on_a_previous_file_removes_only_that_file(services, slots):
    """Прежний файл пережил перевыпуск (Telegram не дал удалить) и на нём
    нажали «В меню»: уходит только он. Новый файл, его карточка и запись о
    них целы — итог с шлюза должен найти именно их."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    _nav1, (_c, _m, doc1) = await _issue(services, bot, 2)
    bot.delete_fails = True
    _nav2, (_c, _m, _doc2) = await _issue(services, bot, 2)
    bot.delete_fails = False
    record = services.gw_bundle_msg_get(2)
    cb = FakeCallback(message=doc1, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert _deleted(bot) == [doc1.message_id], f"«В меню» на прежнем файле удалила лишнее: {_deleted(bot)}"
    assert services.gw_bundle_msg_get(2) == record, "«В меню» на прежнем файле стёрла запись о новом"
    assert cb.answers


# ── итог применения с шлюза ──────────────────────────────────────────────────

async def test_success_removes_the_file_and_brings_the_slot_card_as_the_live_menu(services, slots):
    """Шлюз сказал «применено»: файл и карточка над ним уходят, сообщения
    «применено» нет (его человек видел у бота шлюза) — вместо них карточка
    слота новым сообщением, и она — живое меню чата; запись стёрта."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дом 2")
    bot = _Bot()
    want_text, want_labels = await _card(services, bot, 2)
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    await sh.bundle_applied(bot, services, 2, True, "")
    assert {nav.message_id, doc.message_id} <= set(_deleted(bot)), _deleted(bot)
    sent = _sent(bot)
    assert len(sent) == 1, f"после итога в чат ушло не одно сообщение: {sent}"
    _, chat, text = sent[0]
    assert chat == ADMIN
    assert text == want_text, f"вместо карточки слота 2 показано:\n{text}"
    mid = max(bot.markups)
    assert [b.text for row in bot.markups[mid].inline_keyboard for b in row] == want_labels
    assert services.gw_bundle_msg_get(2) == {}
    assert services.db.get_nav_message_id(ADMIN) == mid, "карточка не стала живым меню — повиснет вторая клавиатура"


async def test_success_for_a_slot_removed_meanwhile_brings_the_main_menu(services, slots):
    """Слот сняли, пока файл ехал: итог убирает файл и ставит главную, а не
    карточку, которой нет."""
    from awgbot.bot.handlers.admin import _panel_parts
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    services.db.gateway_delete(2)
    await sh.bundle_applied(bot, services, 2, True, "")
    assert {nav.message_id, doc.message_id} <= set(_deleted(bot)), _deleted(bot)
    main_text, _ = await _panel_parts(services)
    assert [r[2] for r in _sent(bot)] == [main_text], "после итога по снятому слоту не главная"


async def test_a_failure_leaves_the_file_in_place_and_says_nothing(services, slots):
    """Отказ: файл остаётся — его можно переслать снова; в чат ВПС ничего не
    уходит (причину человек видел у бота шлюза), запись цела."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    await _issue(services, bot, 2)
    record = services.gw_bundle_msg_get(2)
    await sh.bundle_applied(bot, services, 2, False, "нет <места> & прав", fp=record["fp"])
    assert _deleted(bot) == [], f"отказ убрал файл, который ещё пересылать: {_deleted(bot)}"
    assert _sent(bot) == [], f"отказ принёс сообщение в чат ВПС: {_sent(bot)}"
    assert services.gw_bundle_msg_get(2) == record


async def test_a_result_without_a_file_record_sends_nothing(services, slots):
    """Файл выдан до обновления бота (записи нет) или «В меню» уже нажали —
    убирать нечего, и карточка поверх того, что человек делает, не всплывает."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    await sh.bundle_applied(bot, services, 2, True, "")
    assert _deleted(bot) == [], "удалено то, чего бот не выдавал"
    assert _sent(bot) == [], f"итог без файла в чате принёс сообщение: {_sent(bot)}"


async def test_a_refused_delete_does_not_hold_back_the_card(services, slots):
    """Старое сообщение удалить не дали — карточка всё равно приходит, запись
    стирается (иначе следующий итог снова споткнулся бы о те же id)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    want_text, _ = await _card(services, bot, 2)
    await _issue(services, bot, 2)
    bot.delete_fails = True
    await sh.bundle_applied(bot, services, 2, True, "")
    assert [r[2] for r in _sent(bot)] == [want_text]
    assert services.gw_bundle_msg_get(2) == {}


async def test_the_result_of_one_slot_does_not_touch_the_file_of_another(services, slots):
    """Лежат два файла — для слота 1 и слота 2; итог слота 1 убирает только
    свой: файл второго шлюза ещё не переслан."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav1, (_c, _m, doc1) = await _issue(services, bot, 1)
    nav2, (_c, _m, doc2) = await _issue(services, bot, 2)
    await sh.bundle_applied(bot, services, 1, True, "")
    gone = set(_deleted(bot))
    assert {nav1.message_id, doc1.message_id} <= gone
    assert not {nav2.message_id, doc2.message_id} & gone, "итог слота 1 убрал файл слота 2"
    assert services.gw_bundle_msg_get(2)["file"] == doc2.message_id


async def test_a_result_about_another_file_leaves_the_current_one(services, slots):
    """Итог пришёл с чужим отпечатком (применили прежний файл, пока в чате
    уже лежит новый): новый файл и его карточка остаются — его ещё
    пересылать, запись цела, в чат ничего не уходит."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    record = services.gw_bundle_msg_get(2)
    assert record.get("fp") and record["fp"] != "0" * 16
    await sh.bundle_applied(bot, services, 2, True, "", fp="0" * 16)
    assert not {nav.message_id, doc.message_id} & set(_deleted(bot)), \
        f"итог о другом файле убрал новый: удалены {_deleted(bot)}"
    assert services.gw_bundle_msg_get(2) == record, "итог о другом файле стёр запись о новом"
    assert _sent(bot) == [], f"итог о другом файле принёс сообщение: {_sent(bot)}"


@pytest.mark.parametrize("same", [True, False], ids=["same-fp", "old-agent-no-fp"])
async def test_a_result_about_this_file_or_without_fingerprint_removes_it(services, slots, same):
    """Отпечаток совпал с выданным — файл и карточка уходят. Итог без
    отпечатка (агент до этого обновления) — тоже: иначе после обновления ВПС
    файлы у старых агентов не убирались бы никогда."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    fp = services.gw_bundle_msg_get(2)["fp"] if same else ""
    await sh.bundle_applied(bot, services, 2, True, "", fp=fp)
    assert {nav.message_id, doc.message_id} <= set(_deleted(bot)), _deleted(bot)
    assert services.gw_bundle_msg_get(2) == {}
    assert len(_sent(bot)) == 1, _sent(bot)


async def test_the_fingerprint_on_record_is_the_one_the_gateway_computes(services, slots, monkeypatch):
    """Отпечаток в записи ВПС — от тех же байтов, что уйдут файлом; агент
    считает его от присланного файла той же функцией. Разойдись они — ни
    один итог не убрал бы файл."""
    import hashlib
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    monkeypatch.setattr(services, "gw_bundle_encrypted", lambda slot=None: (b"ENC-BYTES", "b.enc"))
    await _issue(services, _Bot(), 2)
    assert services.gw_bundle_msg_get(2)["fp"] == hashlib.sha256(b"ENC-BYTES").hexdigest()[:16]


# ── новый шлюз вышел на связь ────────────────────────────────────────────────

async def test_installed_gateway_removes_the_file_and_says_so_with_its_bot(services, slots):
    """Шлюз, поставленный файлом первого применения, впервые на связи:
    файл (ключи и токен агента) и инструкция уходят, админу — «✅ Шлюз …
    успешно настроен 🎉» со ссылкой на бота шлюза и «⬅️ Назад» в карточку
    слота; сообщение — живое меню чата, запись стёрта."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    _nav, instr, (_c, _m, doc) = await _issue_plain(services, bot, pi2)
    services.db.gateway_update(2, label="дом 2")
    services.set_gw_bot_identity(2, "pi2_gw_bot", "Шлюз <Pi2>")
    await sh.bundle_installed(bot, services, 2)
    assert {instr.message_id, doc.message_id} <= set(_deleted(bot)), \
        f"файл с токеном агента или инструкция остались в чате: удалены {_deleted(bot)}"
    sent = _sent(bot)
    assert len(sent) == 1, sent
    _, chat, text = sent[0]
    assert chat == ADMIN
    assert text == ('✅ Шлюз <b>«Pi2» (дом 2)</b> успешно настроен 🎉\n'
                    '<b>Бот шлюза:</b> <a href="https://t.me/pi2_gw_bot">Шлюз &lt;Pi2&gt;</a>'), text
    mid = max(bot.markups)
    assert _buttons(bot.markups[mid]) == [("⬅️ Назад", GwSlotCB(action="card", slot=2).pack())]
    assert services.db.get_nav_message_id(ADMIN) == mid, "уведомление не стало живым меню"
    assert services.gw_bundle_msg_get(2) == {}, "запись о файле пережила настройку шлюза"


async def test_installed_gateway_without_a_known_bot_has_no_bot_line(services, slots):
    """Бот шлюза серверу ещё не известен (getMe не отвечал, снимка с ботом
    нет) — строки бота нет вовсе, без пустой ссылки."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    await _issue_plain(services, bot, pi2)
    await sh.bundle_installed(bot, services, 2)
    assert [r[2] for r in _sent(bot)] == ["✅ Шлюз <b>«Pi2»</b> успешно настроен 🎉"], _sent(bot)


async def test_installed_gateway_without_a_file_in_chat_still_says_so(services, slots):
    """Файл уже убрали «В меню» — удалять нечего, но о настроенном шлюзе
    админ узнаёт: ради этого и ждали."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    await sh.bundle_installed(bot, services, 2)
    assert _deleted(bot) == [], "удалено то, чего бот не выдавал"
    assert [r[2] for r in _sent(bot)] == ["✅ Шлюз <b>«Pi2»</b> успешно настроен 🎉"], _sent(bot)


async def test_installed_gateway_leaves_an_encrypted_file_issued_after_it(services, slots):
    """После файла первого применения успели выпустить шифрованный файл из
    карточки (он вытеснил первый). Выход шлюза на связь — не итог этого
    файла: файл и погасшая карточка над ним остаются (их уберёт итог
    применения или «В меню»), запись о них цела; «настроен» всё равно приходит."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = TOKEN2
    bot = _Bot()
    await _issue_plain(services, bot, pi2)
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    record = services.gw_bundle_msg_get(2)
    assert record["plain"] is False, record
    before = set(_deleted(bot))
    await sh.bundle_installed(bot, services, 2)
    gone = set(_deleted(bot)) - before
    assert not {nav.message_id, doc.message_id} & gone, \
        f"уведомление о настройке убрало шифрованный файл, который ещё пересылать: {gone}"
    assert services.gw_bundle_msg_get(2) == record, "уведомление о настройке стёрло запись о шифрованном файле"
    assert [r[2] for r in _sent(bot)] == ["✅ Шлюз <b>«Pi2»</b> успешно настроен 🎉"], _sent(bot)


def test_installed_text_escapes_names_and_falls_back_to_the_username():
    """Имя слота и имя бота задаёт человек: `<` и `&` без экранирования
    Telegram отвергает, и уведомление не дошло бы вовсе. Бот без имени —
    подписан своим username."""
    got = texts.gateway_installed_text("«A<B>» (x & y)", {"username": "b_bot", "name": "Шлюз <Pi2> & co"})
    assert got == ('✅ Шлюз <b>«A&lt;B&gt;» (x &amp; y)</b> успешно настроен 🎉\n'
                   '<b>Бот шлюза:</b> <a href="https://t.me/b_bot">Шлюз &lt;Pi2&gt; &amp; co</a>'), got
    assert texts.gateway_installed_text("«Pi»", {"username": "pi_bot"}).endswith(
        '<a href="https://t.me/pi_bot">pi_bot</a>')
    for none in ({}, None, {"username": "", "name": "x"}):
        assert texts.gateway_installed_text("«Pi»", none) == "✅ Шлюз <b>«Pi»</b> успешно настроен 🎉", none
