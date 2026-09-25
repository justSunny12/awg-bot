"""Файл конфигурации шлюза в чате ВПС и его итог (канал линка, вид
`applied`): под файлом — «⬅️ Отмена» с номером слота, где лежат файл и
инструкция — запомнено; «Отмена» убирает оба и открывает карточку слота;
итог с шлюза убирает оба и ставит строку «применено / не получилось» с
«⬅️ Назад» в карточку. Файл первого применения — так же.

Цена ошибки: файл с ключом линка остаётся в чате после того, как отслужил;
«Отмена» уводит на главную вместо карточки, из которой пришли; итог с
грязным текстом ошибки от шлюза Telegram отвергает, и человек не узнаёт,
применилось ли."""
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
    настоящим сообщением с номером — как у Telegram."""

    def __init__(self, **kw):
        kw.setdefault("chat_id", ADMIN)
        kw.setdefault("user_id", ADMIN)
        super().__init__(**kw)
        self.docs: list[tuple] = []           # (подпись, клавиатура, отправленное)

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


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _deleted(bot) -> list[int]:
    return [r[2] for r in bot.records if r[0] == "delete_message"]


async def _card(services, bot, slot):
    """Карточка слота так, как её рисует обычный вход в неё."""
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
    return _screen(nav)


async def _issue(services, bot, slot=2):
    """Админ на экране «Конфигурация шлюза» слота жмёт «выпустить»: вернуть
    экран-инструкцию и сообщение с файлом."""
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle", val=str(slot)), services)
    assert len(nav.docs) == 1, f"файл конфигурации не выдан: {nav.sent}"
    return nav, nav.docs[0]


# ── выдача файла ─────────────────────────────────────────────────────────────

async def test_the_config_file_offers_cancel_for_its_slot_and_is_remembered(services, slots):
    """Под файлом одна кнопка — «⬅️ Отмена» с номером слота (по нему она
    вернёт в карточку этого слота), а где лежат файл и инструкция над ним —
    записано: итог с шлюза должен найти оба."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    nav, (_caption, markup, doc) = await _issue(services, bot, 2)
    assert _buttons(markup) == [("⬅️ Отмена", CANCEL2)], _buttons(markup)
    where = services.gw_bundle_msg_get(2)
    assert len(where.pop("fp", "")) == 16, "запись без отпечатка файла"
    assert where == {"chat": ADMIN, "file": doc.message_id, "instr": nav.message_id}, \
        "итог не найдёт файл и инструкцию"
    assert services.gw_bundle_msg_get(1) == {}, "файл слота 2 записан за слотом 1"


async def test_a_reissued_file_removes_the_previous_one_and_takes_its_record(services, slots):
    """Выпустили файл слота второй раз — прежний файл и инструкция над ним
    уходят из чата (в файле тот же ключ линка, а итог придёт только на
    новый), запись указывает на новый файл и его инструкцию."""
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
    """Файл не собрался — записи нет: итогу нечего убирать, «Отмене» тоже."""
    from awgbot.domain.services import ServiceError
    _, pi, _ = slots
    _slot1(services, pi)

    def _fail(slot=None):
        raise ServiceError("нет ключей")
    monkeypatch.setattr(services, "gw_bundle_encrypted", _fail)
    msg = _Msg(bot=_Bot())
    assert await sh.send_gw_bundle(msg, services, 1, instr_id=77) is False
    assert services.gw_bundle_msg_get(1) == {}


async def test_the_first_install_file_also_offers_cancel_and_is_remembered(services, slots):
    """Файл первого применения (назначение устройства из «моих») — та же
    «⬅️ Отмена» с номером нового слота, и в записи — сообщение с инструкцией
    установки над ним: итог уберёт и её."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = "222222222:BB-second-token-value-long-enough"
    bot = _Bot()
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi2.id, slot=0), services, FakeState())
    assert services.db.gateway_by_device(pi2.id).id == 2
    assert len(nav.docs) == 1 and "первого применения" in nav.docs[0][0], nav.docs
    _caption, markup, doc = nav.docs[0]
    assert _buttons(markup) == [("⬅️ Отмена", CANCEL2)], _buttons(markup)
    instr = next(s[3] for s in nav.sent if s[0] == "answer" and "--install" in s[1])
    where = services.gw_bundle_msg_get(2)
    assert len(where.pop("fp", "")) == 16, "запись без отпечатка файла"
    assert where == {"chat": ADMIN, "file": doc.message_id, "instr": instr.message_id}


async def test_the_first_install_file_of_a_new_machine_is_remembered_too(services, slots):
    """«Новое устройство» с уже известным токеном — тот же путь выдачи."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.token[2] = "222222222:BB-second-token-value-long-enough"
    bot = _Bot()
    nav = _Msg(bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes", slot=0), services, FakeState())
    assert len(nav.docs) == 1, nav.sent
    _caption, markup, doc = nav.docs[0]
    assert _buttons(markup) == [("⬅️ Отмена", CANCEL2)], _buttons(markup)
    instr = next(s[3] for s in nav.sent if s[0] == "answer" and "--install" in s[1])
    where = services.gw_bundle_msg_get(2)
    assert len(where.pop("fp", "")) == 16, "запись без отпечатка файла"
    assert where == {"chat": ADMIN, "file": doc.message_id, "instr": instr.message_id}


# ── «Отмена» под файлом ──────────────────────────────────────────────────────

async def test_cancel_removes_the_file_and_its_instruction_and_opens_the_slot_card(services, slots):
    """«Отмена»: из чата уходят файл (внутри ключ линка) и инструкция над ним,
    запись о них стирается, человек оказывается в карточке слота, из которого
    выпускал файл, — новым сообщением."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    want_text, want_labels = await _card(services, bot, 2)
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    cb = FakeCallback(message=doc, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert {nav.message_id, doc.message_id} <= set(_deleted(bot)), \
        f"в чате остались файл или инструкция: удалены {_deleted(bot)}"
    assert services.gw_bundle_msg_get(2) == {}, "запись о файле пережила «Отмену»"
    assert cb.answers, "колбэк без ответа — у кнопки крутятся часики"
    shown = [s for s in doc.sent if s[0] == "answer"]
    assert len(shown) == 1, f"после «Отмены» показано не одно сообщение: {shown}"
    _, text, markup, _sent = shown[0]
    assert text == want_text, "после «Отмены» показана не карточка слота 2"
    assert [b.text for row in markup.inline_keyboard for b in row] == want_labels


async def test_cancel_still_opens_the_card_when_telegram_refuses_to_delete(services, slots):
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


async def test_cancel_for_a_slot_removed_meanwhile_goes_to_the_main_menu(services, slots):
    """Слот сняли, пока файл лежал в чате: «Отмена» убирает файл и ведёт на
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


async def test_cancel_without_a_slot_number_removes_only_its_own_message(services, slots):
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


async def test_cancel_on_a_previous_file_removes_only_that_file(services, slots):
    """Прежний файл пережил перевыпуск (Telegram не дал удалить) и на нём
    нажали «Отмену»: уходит только он. Новый файл, его инструкция и запись о
    них целы — итог с шлюза должен найти именно их."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    _nav1, (_c, _m, doc1) = await _issue(services, bot, 2)
    bot.delete_fails = True
    nav2, (_c, _m, doc2) = await _issue(services, bot, 2)
    bot.delete_fails = False
    record = services.gw_bundle_msg_get(2)
    cb = FakeCallback(message=doc1, user_id=ADMIN, bot=bot)
    await sh.routing_action(cb, SetCB.unpack(CANCEL2), services)
    assert _deleted(bot) == [doc1.message_id], f"«Отмена» на прежнем файле удалила лишнее: {_deleted(bot)}"
    assert services.gw_bundle_msg_get(2) == record, "«Отмена» на прежнем файле стёрла запись о новом"
    assert cb.answers


# ── итог с шлюза ─────────────────────────────────────────────────────────────

async def test_success_replaces_the_file_with_a_result_leading_back_to_the_card(services, slots):
    """Шлюз сказал «применено»: файл и инструкция уходят, на их месте —
    строка итога с «⬅️ Назад» в карточку слота; итог — живое меню чата
    (следующий экран его погасит); запись стёрта."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дом 2")
    bot = _Bot()
    nav, (_c, _m, doc) = await _issue(services, bot, 2)
    services.db.pop_content_msg_ids(ADMIN)                # что пометила сама выдача — не предмет теста
    await sh.bundle_applied(bot, services, 2, True, "")
    assert {nav.message_id, doc.message_id} <= set(_deleted(bot)), _deleted(bot)
    sent = [r for r in bot.records if r[0] == "send_message"]
    assert len(sent) == 1, sent
    _, chat, text = sent[0]
    assert chat == ADMIN
    assert text == "✅ Конфигурация успешно применена на стороне шлюза «Pi2» (дом 2)", text
    mid = max(bot.markups)
    assert _buttons(bot.markups[mid]) == [("⬅️ Назад", GwSlotCB(action="card", slot=2).pack())]
    assert services.gw_bundle_msg_get(2) == {}
    assert services.db.get_nav_message_id(ADMIN) == mid, "итог не стал живым меню — повиснет второй клавиатурой"


async def test_failure_shows_the_escaped_reason(services, slots):
    """Отказ: причина от шлюза — чужой текст; `<` и `&` без экранирования
    Telegram отвергает, и итог не дошёл бы вовсе."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="<дом & 2>")
    bot = _Bot()
    await _issue(services, bot, 2)
    await sh.bundle_applied(bot, services, 2, False, "нет <места> & прав")
    text = next(r[2] for r in bot.records if r[0] == "send_message")
    assert text == ("⚠️ Применить конфигурацию на стороне шлюза «Pi2» (&lt;дом &amp; 2&gt;) "
                    "не получилось: нет &lt;места&gt; &amp; прав"), text


async def test_a_result_without_a_file_record_still_reaches_the_admin(services, slots):
    """Файл выдан до обновления бота (записи нет) или «Отмену» уже нажали —
    итог всё равно приходит админу, ничего не удаляется."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    await sh.bundle_applied(bot, services, 2, True, "")
    assert _deleted(bot) == [], "удалено то, чего бот не выдавал"
    sent = [r for r in bot.records if r[0] == "send_message"]
    assert [(r[1], r[2]) for r in sent] == [(ADMIN, "✅ Конфигурация успешно применена на стороне шлюза «Pi2»")]


async def test_a_refused_delete_does_not_hold_back_the_result(services, slots):
    """Старое сообщение удалить не дали — итог всё равно приходит, запись
    стирается (иначе следующий итог снова споткнулся бы о те же id)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    bot = _Bot()
    await _issue(services, bot, 2)
    bot.delete_fails = True
    await sh.bundle_applied(bot, services, 2, False, "")
    texts_sent = [r[2] for r in bot.records if r[0] == "send_message"]
    assert texts_sent == ["⚠️ Применить конфигурацию на стороне шлюза «Pi2» не получилось"], texts_sent
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


def test_result_text_without_a_reason_has_no_dangling_colon():
    assert texts.gateway_bundle_applied_text("«Pi»", False, "") == \
        "⚠️ Применить конфигурацию на стороне шлюза «Pi» не получилось"
    assert texts.gateway_bundle_applied_text("«Pi»", True, "лишнее") == \
        "✅ Конфигурация успешно применена на стороне шлюза «Pi»", "у успеха причины не бывает"


async def test_a_result_about_another_file_leaves_the_current_one(services, slots):
    """Итог пришёл с чужим отпечатком (применили прежний файл, пока в чате
    уже лежит новый): новый файл и его инструкция остаются — его ещё
    пересылать, запись цела; сам итог админу всё равно приходит."""
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
    assert [r[2] for r in bot.records if r[0] == "send_message"] == \
        ["✅ Конфигурация успешно применена на стороне шлюза «Pi2»"], "итог не дошёл до админа"


@pytest.mark.parametrize("same", [True, False], ids=["same-fp", "old-agent-no-fp"])
async def test_a_result_about_this_file_or_without_fingerprint_removes_it(services, slots, same):
    """Отпечаток совпал с выданным — файл и инструкция уходят. Итог без
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
