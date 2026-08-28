"""E2E: добор веток роутера админа — понижение лимита с подтверждением,
продление, выдача файла, блок с приостановкой (FSM дней), личные qr/file,
выбор устройства для добавления, карточка пира без приватного ключа.
"""
import asyncio

import pytest

from awgbot.bot.handlers import admin as ah
from awgbot.bot.callbacks import BlockCB, ClientCB, ConfirmCB, DeviceCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


# ── понижение лимита ниже числа устройств ────────────────────────────────────
async def test_edit_limit_lower_confirm_yes(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6300, device_limit=5)
    services.add_device(client.id, "a")
    services.add_device(client.id, "b")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_start(cb, ClientCB(action="edit_limit", client_id=client.id), services, st)
    await ah.edit_limit_apply(_amsg(fake_bot, "1"), services, st)   # 1 < 2 → диалог подтверждения
    assert (await st.get_data())["pending_limit"] == 1
    cb2, nav2 = _acb(fake_bot)
    await ah.edit_limit_confirm(cb2, ConfirmCB(action="lower_limit", ref=client.id, yes=True), services, st)
    assert services.db.get_client(client.id).device_limit == 1


async def test_edit_limit_lower_confirm_no(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6301, device_limit=5)
    services.add_device(client.id, "a")
    services.add_device(client.id, "b")
    st = FakeState()
    await st.update_data(client_id=client.id, pending_limit=1)
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_confirm(cb, ConfirmCB(action="lower_limit", ref=client.id, yes=False), services, st)
    assert services.db.get_client(client.id).device_limit == 5   # не изменён


# ── продление / файл ─────────────────────────────────────────────────────────
async def test_extend_start_renders(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6303, period_kind="year")
    cb, nav = _acb(fake_bot)
    await ah.extend_start(cb, ClientCB(action="extend", client_id=client.id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)


async def test_admin_dev_file(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6304)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_dev_file(cb, DeviceCB(action="gen_file", device_id=dc.device_id), services)
    assert any(s[0] == "document" for s in nav.sent)


# ── блок клиента с приостановкой (FSM дней) ──────────────────────────────────
async def test_block_client_pause_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6305, period_kind="year")
    cb, nav = _acb(fake_bot)
    await ah.admin_block_menu(cb, BlockCB(target="cli", action="menu_block", ref=client.id))
    assert any(s[0] == "edit_text" for s in nav.sent)      # спросили про приостановку
    st = FakeState()
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_block_pause_yes(cb2, BlockCB(target="cli", action="pause_yes", ref=client.id), st)
    assert (await st.get_data())["block_client"] == client.id
    m_days = _amsg(fake_bot, "7")
    await ah.admin_block_pause_days(m_days, services, st)
    assert await st.get_data() == {}                        # FSM закрыт
    assert any(s[0] == "answer" for s in m_days.sent)       # показан выбор уведомления
    # ветка «без приостановки»
    cb3, nav3 = _acb(fake_bot)
    await ah.admin_block_pause_no(cb3, BlockCB(target="cli", action="pause_no", ref=client.id))
    assert any(s[0] == "edit_text" for s in nav3.sent)


async def test_block_menu_device_branch(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6306)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_block_menu(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id))
    assert any(s[0] == "edit_text" for s in nav.sent)


# ── личные qr/file, выбор устройства ─────────────────────────────────────────
async def test_self_gen_qr_file_pickers(services, fake_bot):
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "d")
    for handler in (ah.self_gen_qr, ah.self_gen_file):
        cb, nav = _acb(fake_bot)
        await handler(cb, services)
        assert any(s[0] == "edit_text" for s in nav.sent)


async def test_add_device_choice_and_pick(services, fake_bot, make_active_client):
    make_active_client(tg_id=6307)
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_choice(cb, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_add_device_pick(cb2, services)
    assert any(s[0] == "edit_text" for s in nav2.sent)


async def test_admin_menu_devices(services, fake_bot):
    services.ensure_admin_client()
    cb, nav = _acb(fake_bot)
    await ah.admin_menu_devices(cb, services)
    assert any(s[0] == "edit_text" for s in nav.sent)


# ── регресс: у устройства без ключа не должно остаться тупиковых кнопок ───────
async def test_unmanaged_device_offers_no_dead_restore_button(services, fake_bot):
    """Пир, подхваченный с сервера: реставрации больше нет — и кнопок в неё тоже.

    Обработчика action="restore" в боте не осталось. Кнопка, которая на него
    ссылается, молча ничего не делает: нажатие уходит в пустоту, а человек
    остаётся с ощущением сломанного бота. Проверяем оба экрана, где такое
    устройство вообще показывается.
    """
    from awgbot.bot import keyboards as kb

    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "Неизвестный пир 10.8.1.9", "PUBQ", "PSK",
                                    "10.8.1.9", private_key=None)
    dev = services.db.get_device(did)
    assert not dev.is_managed

    markups = [kb.device_actions(dev, is_admin=True, back_target="x",
                                 reassign_label="🔀 Передать"),
               kb.unmanaged_device_dialog(did)]
    for m in markups:
        for row in m.inline_keyboard:
            for b in row:
                assert "restore" not in (b.callback_data or ""), b.text


async def test_unmanaged_device_connect_menu_says_there_is_no_link(
        services, fake_bot):
    """Карточка «как подключить» для такого пира честно говорит: ссылки нет."""
    from awgbot.bot import texts

    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "чужой", "PUBQ2", "PSK", "10.8.1.10",
                                    private_key=None)
    cb, nav = _acb(fake_bot)
    await ah.admin_device_connect_menu(cb, DeviceCB(action="connect_menu", device_id=did),
                                       services)
    shown = [r[1] for r in nav.sent if r[0] == "edit_text"]
    assert texts.UNMANAGED_DEVICE_DIALOG in shown


# ── объявление: вход только с главной админа ─────────────────────────────────

def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _btn_data(markup, needle):
    for row in markup.inline_keyboard:
        for b in row:
            if needle in b.text:
                return b.callback_data
    return None


def test_client_card_has_no_announcement_button(services, make_active_client):
    """В карточке профиля кнопки объявления быть не должно.

    Вход у рассылки ровно один — с главной админа, где следующим шагом
    выбираются адресаты. Кнопка в карточке отвечала на тот же вопрос «кому», но
    лезла в глаза там, где админ занят совсем другим.
    """
    from awgbot.bot import keyboards as kb
    c = make_active_client(name="c1", tg_id=4001)
    client = services.db.get_client(c.id)

    for owner in (True, False):
        labels = _btn_texts(kb.admin_client_actions(client, is_admin_owner=owner))
        assert not any("Объявление" in x for x in labels), owner


def test_main_menu_entry_opens_target_picker():
    """Кнопка с главной ведёт на выбор адресатов, а не сразу на ввод текста."""
    from awgbot.bot import keyboards as kb
    data = _btn_data(kb.admin_main(0), "Объявление")
    assert data == "bc:pick:0"


def test_target_picker_marks_selection_and_offers_bulk():
    """Отметки видны на самих кнопках, а массовое действие меняет смысл:
    отмечено всё — осмысленно только снять."""
    from awgbot.core import models
    from awgbot.bot import keyboards as kb

    def _c(i):
        return models.Client(id=i, tg_id=100 + i, name=f"К{i}", device_limit=1,
                             block_reason=0, is_service=0, activation_status="active",
                             invite_code=None, created_at="2026-01-01")

    clients = [_c(1), _c(2)]
    none = kb.broadcast_targets(clients, set())
    labels_none = _btn_texts(none)
    assert labels_none[0].endswith("Отметить все")
    # проверяем строки профилей, а не кнопку массового действия — она сама
    # начинается с галочки и под фильтр «отмечено» попала бы ложно
    assert "☑️ К1" in labels_none and "☑️ К2" in labels_none
    assert "✅ К1" not in labels_none

    some = kb.broadcast_targets(clients, {1})
    labels = _btn_texts(some)
    assert "✅ К1" in labels and "☑️ К2" in labels
    assert labels[0].endswith("Отметить все")        # отмечено не всё

    every = kb.broadcast_targets(clients, {1, 2})
    assert _btn_texts(every)[0].endswith("Снять все")


def test_broadcast_cancel_clears_the_input_state():
    """Отмена обязана сбросить FSM, иначе следующее сообщение админа станет
    черновиком объявления.

    Раньше отмена вела прямо в главное меню, чей хендлер чистит состояние
    попутно, — работало, но держалось на побочном эффекте соседа.
    """
    from awgbot.bot import keyboards as kb
    for markup in (kb.broadcast_cancel(), kb.broadcast_confirm()):
        data = _btn_data(markup, "Отмена")
        assert data == "bc:cancel:0", data


def test_broadcast_confirm_leads_to_send():
    from awgbot.bot import keyboards as kb
    assert _btn_data(kb.broadcast_confirm(), "Отправить") == "bc:send:0"


async def test_broadcast_keeps_telegram_formatting(services, make_active_client, fake_bot):
    """Форматирование, сделанное средствами Telegram, обязано дожить до превью.

    Жирный/курсив/ссылки живут не в тексте сообщения, а в entities. Пока
    читали `message.text`, объявление уходило голым — и превью тоже, поэтому
    заметить это до отправки было невозможно.
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7001)
    state = FakeState()
    await state.update_data(targets=[c.id])

    msg = FakeMessage(text="Профилактика в ночь на 12-е",
                      html_text="Профилактика <b>в ночь на 12-е</b>",
                      chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)

    preview = "".join(s[1] for s in msg.sent if s[0] == "answer")
    assert "<b>в ночь на 12-е</b>" in preview, preview
    assert (await state.get_data())["text"] == "Профилактика <b>в ночь на 12-е</b>"


async def test_broadcast_rejects_blank_before_reading_markup(services, make_active_client,
                                                             fake_bot):
    """Пустое сообщение отбиваем по тексту, а не по разметке: у сообщения без
    текста html_text брать неоткуда."""  # формулировку см. texts.BROADCAST_EMPTY
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7002)
    state = FakeState()
    await state.update_data(targets=[c.id])

    msg = FakeMessage(text="   ", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)
    assert any("жду текст объявления или картинку" in s[1]
               for s in msg.sent if s[0] == "answer")


def _photo(file_id):
    """PhotoSize-лесенка, как её отдаёт Telegram: последний размер — оригинал."""
    import types
    return [types.SimpleNamespace(file_id=f"{file_id}_small"),
            types.SimpleNamespace(file_id=file_id)]


async def _settled(admin_h, chat_id):
    """Дождаться отложенного рендера превью (пауза на хвост альбома)."""
    task = admin_h._bc_render_tasks.get(chat_id)
    if task:
        await task


async def test_photos_without_text_get_a_real_preview_not_a_demand(
        services, make_active_client, fake_bot, monkeypatch):
    """Картинки без текста — это уже превью, а не требование «пришли текст».

    Подпись едет на одном из апдейтов альбома, и медленный аплоад растягивает
    их на десятки секунд: требовать текст в этом зазоре — значит требовать то,
    что админ уже отправил. Превью строится сразу, блок подтверждения прямо
    говорит про пустой текст, и отправить можно как есть.
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7010)
    state = FakeState()
    await state.update_data(targets=[c.id])

    msgs = [FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo(f"FILE{n}")) for n in (1, 2)]
    for m in msgs:
        await admin_h.broadcast_receive(m, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)

    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    assert len(albums) == 1, "превью-альбом не построен без текста"
    assert [m.media for m in albums[0][2]] == ["FILE1", "FILE2"]
    said = [t for m in msgs for kind, t, _ in m.sent if kind == "answer"]
    confirm = [t for t in said if "Отправляем?" in t]
    assert len(confirm) == 1, said
    assert "Текста в нём нет" in confirm[0], "блок молчит про пустой текст"
    assert not any("Пришли текст" in t for t in said), \
        "второй шаг вернулся: бот требует текст, который мог ещё не доехать"


async def test_one_batch_renders_the_preview_exactly_once(
        services, make_active_client, fake_bot, monkeypatch):
    """Пачка апдейтов одного альбома — РОВНО ОДИН рендер превью.

    Ранняя отмена ожидающего рендера (в начале обработки апдейта) с
    конкурентной пачкой не справляется: второй апдейт выполняет её раньше, чем
    первый создал таску, — отменять нечего, выживают обе, и превью видимо
    пересоздаётся по разу на апдейт. Единственность живой таски обязана давать
    атомарная пара cancel+create в конце обработки: в каком бы порядке ни
    финишировали апдейты, остаётся таска последнего.
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0.2)
    renders = []
    real_preview = admin_h._bc_preview

    async def counting_preview(message, state, services_):
        renders.append(1)
        await real_preview(message, state, services_)

    monkeypatch.setattr(admin_h, "_bc_preview", counting_preview)
    c = make_active_client(name="Ксюша", tg_id=7021)
    state = FakeState()
    await state.update_data(targets=[c.id])

    msgs = [FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo(f"B{n}"), caption="текст" if n == 0 else None)
            for n in range(3)]
    await asyncio.gather(*(admin_h.broadcast_receive(m, state, services)
                           for m in msgs))
    await asyncio.sleep(0.5)          # дать пережившим таскам отработать

    assert len(renders) == 1, f"превью рендерилось {len(renders)} раз(а) на одну пачку"
    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    assert len(albums) == 1, "альбом-превью пересоздавался"


async def test_late_caption_joins_the_preview(services, make_active_client,
                                              fake_bot, monkeypatch):
    """Подпись со снимка, пришедшего после превью, вливается пересборкой.

    Страховочный путь: в норме альбом приезжает пачкой (клиент шлёт его одним
    запросом после загрузки всех файлов), но если апдейт добрался позже
    рендера — превью-огрызок заменяется полным без единого действия админа.
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7017)
    state = FakeState()
    await state.update_data(targets=[c.id])

    first = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo("SLOW1"))
    await admin_h.broadcast_receive(first, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)
    old_ids = (await state.get_data())["preview_ids"]
    assert old_ids, "превью без текста не показано"

    late = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                       photo=_photo("SLOW2"), caption="Важно! Обновление")
    await admin_h.broadcast_receive(late, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)

    deleted = [r[2] for r in fake_bot.records if r[0] == "delete_message"]
    assert set(old_ids) <= set(deleted), "старое превью-огрызок остался в чате"
    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    media = albums[-1][2]
    assert [m.media for m in media] == ["SLOW1", "SLOW2"]
    assert media[0].caption == "Важно! Обновление"
    data = await state.get_data()
    assert data["text"] == "Важно! Обновление"


async def test_broadcast_sends_photos_without_text(services, make_active_client,
                                                   fake_bot):
    """Объявление из одних картинок отправляется: превью прямо спрашивало про
    пустой текст, и «Отправить» — легитимный ответ на этот вопрос."""
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7018)
    state = FakeState()
    await state.update_data(targets=[c.id], photos=["A", "B"])

    cb = FakeCallback(message=FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                                          bot=fake_bot),
                      user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_send(cb, state, services)

    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    assert albums and albums[-1][1] == 7018, "объявление без текста не ушло"
    assert albums[-1][2][0].caption is None, "пустая строка уехала подписью"


async def test_every_draft_prompt_offers_a_way_out(services, make_active_client,
                                                   fake_bot):
    """Из любой отбивки черновика есть выход кнопкой — тупиков не бывает.

    «Отмена» текстом бот понять не обязан (и не пытается: слово ушло бы в
    рассылку), значит кнопка обязана быть на каждом сообщении, где диалог
    чего-то ждёт: и на отказе по длине, и на «жду текст или картинку».
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7019)
    state = FakeState()
    await state.update_data(targets=[c.id], photos=["A"])

    long_text = "я" * (cfg.TG_CAPTION_MAX + 1)
    over = FakeMessage(text=long_text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                       bot=fake_bot)
    await admin_h.broadcast_receive(over, state, services)
    markups = [mk for kind, t, mk in over.sent if kind == "answer"]
    assert markups and markups[-1] is not None, "отказ по длине — тупик без кнопки"

    empty = FakeMessage(text="   ", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                        bot=fake_bot)
    await admin_h.broadcast_receive(empty, state, services)
    markups = [mk for kind, t, mk in empty.sent if kind == "answer"]
    assert markups and markups[-1] is not None, "пустое сообщение — тупик без кнопки"


async def test_broadcast_album_with_caption_is_one_action(
        services, make_active_client, fake_bot, monkeypatch):
    """Снимки + текст, набранный в окне вложений, — готовое превью без
    дополнительных шагов. Подпись приезжает на ОДНОМ из сообщений альбома, и
    черновик обязан подхватить её с любого."""
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7013)
    state = FakeState()
    await state.update_data(targets=[c.id])

    first = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo("A"), caption="Переезд начался")
    second = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                         photo=_photo("B"))
    await admin_h.broadcast_receive(first, state, services)
    await admin_h.broadcast_receive(second, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)

    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    assert len(albums) == 1, "превью-альбом не отправлен или отправлен дважды"
    media = albums[0][2]
    assert [m.media for m in media] == ["A", "B"]
    assert media[0].caption == "Переезд начался"
    confirms = [t for m in (first, second) for kind, t, mk in m.sent
                if kind == "answer" and mk is not None]
    assert confirms and "Отправляем?" in confirms[-1], "нет блока подтверждения"
    ids = (await state.get_data())["preview_ids"]
    assert len(ids) == 3, "в preview_ids не альбом плюс блок подтверждения"


async def test_broadcast_photos_after_text_rebuild_the_preview(
        services, make_active_client, fake_bot, monkeypatch):
    """Обратный порядок — текст, потом картинки — тоже одно объявление.

    Прежнее превью при этом убирается: иначе в чате остались бы два живых блока
    подтверждения и запись, неотличимая от разосланной.
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7014)
    state = FakeState()
    await state.update_data(targets=[c.id])

    txt = FakeMessage(text="Переезд начался", chat_id=cfg.ADMIN_ID,
                      user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_receive(txt, state, services)
    old_ids = (await state.get_data())["preview_ids"]
    assert old_ids, "текстовое превью не показано"

    pic = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                      photo=_photo("A"))
    await admin_h.broadcast_receive(pic, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)

    deleted = [r[2] for r in fake_bot.records if r[0] == "delete_message"]
    assert set(old_ids) <= set(deleted), "старое превью осталось в чате"
    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    photos_sent = [r for r in fake_bot.records if r[0] == "send_photo"]
    assert albums or photos_sent, "превью не пересобрано с картинкой"


async def test_broadcast_refuses_caption_over_limit_and_keeps_the_draft(
        services, make_active_client, fake_bot, monkeypatch):
    """С картинками лимит 1024: текст едет подписью, а длинная подпись — это
    привилегия Premium-аккаунта, которым бот быть не может.

    Отказ обязан сохранять черновик: заставить пересылать десять картинок
    заново из-за одного лишнего абзаца — худший из возможных ответов.
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7011)
    state = FakeState()
    await state.update_data(targets=[c.id], photos=["FILE1"])

    long_text = "я" * (cfg.TG_CAPTION_MAX + 1)
    msg = FakeMessage(text=long_text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                      bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)

    said = " ".join(t for _, t, _ in msg.sent)
    assert str(cfg.TG_CAPTION_MAX) in said and str(cfg.TG_CAPTION_MAX + 1) in said, \
        "не названы ни лимит, ни фактическая длина"
    assert "Отправляем?" not in said, "показано превью сверх лимита"
    assert (await state.get_data())["photos"] == ["FILE1"], "черновик потерян"

    # тот же текст БЕЗ картинок в лимит укладывается — лимита два, и они разные
    state2 = FakeState()
    await state2.update_data(targets=[c.id])
    msg2 = FakeMessage(text=long_text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                       bot=fake_bot)
    await admin_h.broadcast_receive(msg2, state2, services)
    assert any("Отправляем?" in t for _, t, _ in msg2.sent)


async def test_broadcast_send_revalidates_the_limit(services, make_active_client,
                                                    fake_bot):
    """Кнопка «Отправить» перепроверяет лимит по итоговому черновику.

    Картинка, добавленная после законного длинного текста, меняет лимит задним
    числом. Уйди такое в Telegram — каждый получатель вернул бы Bad Request, а
    отчёт записал бы всех в «заблокировали бота».
    """
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7015)
    state = FakeState()
    long_text = "я" * (cfg.TG_CAPTION_MAX + 1)
    await state.update_data(targets=[c.id], photos=["FILE1"],
                            text=long_text, text_len=len(long_text))

    cb = FakeCallback(message=FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                                          bot=fake_bot),
                      user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_send(cb, state, services)
    assert cb.answers and cb.answers[-1][1] is True, "отправка не остановлена"
    assert not [r for r in fake_bot.records if r[0] == "send_media_group"], \
        "объявление ушло сверх лимита"


async def test_broadcast_concurrent_album_updates_lose_nothing(
        services, make_active_client, fake_bot, monkeypatch):
    """Апдейты альбома aiogram обрабатывает ПАРАЛЛЕЛЬНО (handle_as_tasks).

    Без замка два конкурентных read-modify-write по FSM читают одинаковый
    список, и один снимок молча затирает другой — альбом уходит неполным.

    Хранилище здесь НАРОЧНО уступает петлю на каждом вызове, как это делает
    любое сетевое (Redis). На MemoryStorage чтение и запись стоят вплотную без
    точки переключения, и гонка не складывается СЛУЧАЙНО — замок делает
    целостность свойством кода, а не удачным свойством хранилища.
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    class NetworkishState(FakeState):
        async def get_data(self):
            await asyncio.sleep(0)
            return await super().get_data()

        async def update_data(self, **kw):
            await asyncio.sleep(0)
            await super().update_data(**kw)

    async def direct_call(fn, *a, **k):
        return fn(*a, **k)          # без to_thread: детерминированный интерливинг

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    monkeypatch.setattr(admin_h, "call", direct_call)
    c = make_active_client(name="Ксюша", tg_id=7016)
    state = NetworkishState()
    await state.update_data(targets=[c.id])

    msgs = [FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo(f"F{n}")) for n in range(4)]
    await asyncio.gather(*(admin_h.broadcast_receive(m, state, services)
                           for m in msgs))
    await _settled(admin_h, cfg.ADMIN_ID)
    assert sorted((await state.get_data())["photos"]) == ["F0", "F1", "F2", "F3"], \
        "конкурентные апдейты потеряли снимок"


async def test_broadcast_stops_at_the_album_limit(services, make_active_client,
                                                  fake_bot, monkeypatch):
    """Одиннадцатая картинка не принимается: Telegram не берёт в альбом больше
    десяти. Отбиваем на приёме, а не на отправке — иначе объявление упало бы
    целиком, после набранного текста."""
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7012)
    state = FakeState()
    await state.update_data(targets=[c.id],
                            photos=[f"F{i}" for i in range(cfg.TG_ALBUM_MAX)])

    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                      photo=_photo("EXTRA"))
    await admin_h.broadcast_receive(msg, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)
    assert any("лишние не приняты" in t for _, t, _ in msg.sent), msg.sent
    assert "EXTRA" not in (await state.get_data())["photos"]


async def test_announcement_is_one_message_with_caption_on_the_first_photo(fake_bot):
    """Объявление с картинками — ОДНО сообщение: альбом, подпись на первом
    вложении. Подпись на втором Telegram показал бы отдельным блоком, а текст
    отдельным сообщением дал бы в чате две записи вместо одной."""
    from awgbot.bot.notifier import send_announcement

    await send_announcement(fake_bot, 555, "текст объявления", ["A", "B", "C"])
    kind, chat, media = [r for r in fake_bot.records if r[0] == "send_media_group"][0]
    assert chat == 555 and len(media) == 3
    assert media[0].caption == "текст объявления"
    assert [m.caption for m in media[1:]] == [None, None], "подпись не только на первом"


async def test_announcement_with_one_photo_is_not_an_album(fake_bot):
    """Альбом из одного вложения Telegram не принимает — шлём обычное фото."""
    from awgbot.bot.notifier import send_announcement

    await send_announcement(fake_bot, 555, "текст", ["ONLY"])
    assert not [r for r in fake_bot.records if r[0] == "send_media_group"]
    kind, chat, caption, photo = [r for r in fake_bot.records if r[0] == "send_photo"][0]
    assert caption == "текст" and photo == "ONLY"


async def test_broadcast_draft_chain_is_cleaned_on_cancel(services, make_active_client,
                                                          fake_bot):
    """Отмена обязана стереть всю переписку с набором черновика.

    Навигация при переходе на превью лишь СНИМАЕТ кнопки с прежнего экрана
    (_dismiss_previous_nav), не удаляя его. Поэтому без явного трекинга после
    отмены висели и приглашение «пришли объявление», и само сообщение админа с
    текстом — то есть черновик оставался в чате.
    """
    from tests.conftest import FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7010)
    chat = cfg.ADMIN_ID
    services.db.set_nav_message_id(chat, 555)          # «приглашение» — нав-экран

    state = FakeState()
    await state.update_data(targets=[c.id])
    msg = FakeMessage(text="Профилактика", chat_id=chat, user_id=chat, bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)

    tracked = services.db.pop_content_msg_ids(chat)
    assert msg.message_id in tracked, "сообщение админа осталось бы висеть"
    assert 555 in tracked, "экран-приглашение осталось бы висеть"


def test_broadcast_report_wording_by_shape():
    """Четыре формы отчёта. Число адресатов называем, только когда в рассылку
    вошли друзья: без них оно равно числу профилей и уже видно из перечисления."""
    from awgbot.bot import texts as T
    txt = "Профилактика <b>в ночь</b>"

    one = T.broadcast_report(["Наташа"], False, 1, 0, txt)
    assert one.startswith("✅ Объявление доставлено владельцу профиля Наташа\n\n")
    assert "адресат" not in one

    one_fr = T.broadcast_report(["Наташа"], True, 2, 0, txt)
    assert ("владельцу профиля Наташа и тем, с кем он поделился устройствами: "
            "всего 2 адресата.") in one_fr

    many = T.broadcast_report(["Наташа", "Ксюша"], False, 2, 0, txt)
    assert many.startswith("✅ Объявление доставлено владельцам профилей Наташа, Ксюша\n\n")

    many_fr = T.broadcast_report(["Наташа", "Ксюша"], True, 4, 0, txt)
    assert ("владельцам профилей Наташа, Ксюша и тем, с кем они поделились "
            "устройствами: всего 4 адресата.") in many_fr

    for r in (one, one_fr, many, many_fr):
        assert r.endswith(f"\n\nТекст объявления: {txt}")


def test_broadcast_report_declines_recipient_word():
    from awgbot.bot import texts as T
    assert "всего 1 адресат." in T.broadcast_report(["А"], True, 1, 0, "x")
    assert "всего 5 адресатов." in T.broadcast_report(["А"], True, 5, 0, "x")


def test_broadcast_report_does_not_hide_failures():
    """«Доставлено» при недоставленных было бы неправдой, а узнать об этом
    больше неоткуда."""
    from awgbot.bot import texts as T
    r = T.broadcast_report(["А"], True, 3, 2, "x")
    assert "⚠️ Не доставлено 2 адресатам" in r


async def test_send_leaves_report_and_opens_panel_separately(
        services, make_active_client, fake_bot):
    """Отчёт остаётся в чате без кнопок, панель приходит СЛЕДУЮЩИМ сообщением.

    Раньше отчёт нёс на себе клавиатуру главного меню: тогда он либо
    переписывался при следующей навигации, либо оставлял в чате второе живое
    меню — держать инвариант «одно активное» было нечем.
    """
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    from awgbot.bot.handlers import admin as admin_h
    import awgbot.core.config as cfg

    admin_h._last_broadcast_at.clear()
    c = make_active_client(name="Наташа", tg_id=7100)
    chat = cfg.ADMIN_ID

    state = FakeState()
    await state.update_data(targets=[c.id], text="Профилактика <b>в ночь</b>")
    nav = FakeMessage(chat_id=chat, user_id=chat, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=chat, bot=fake_bot)

    await admin_h.broadcast_send(cb, state, services)

    edits = [s for s in nav.sent if s[0] == "edit_text"]
    report = edits[-1]
    assert report[1].startswith("✅ Объявление доставлено владельцу профиля Наташа")
    assert "Текст объявления: Профилактика <b>в ночь</b>" in report[1]
    assert report[2] is None, "на отчёте не должно остаться кнопок"

    answers = [s for s in nav.sent if s[0] == "answer"]
    assert answers, "панель должна прийти отдельным сообщением"
    assert answers[-1][2] is not None, "у панели должна быть клавиатура"


# ── обещание вернуться после перезапуска ─────────────────────────────────────

async def test_restart_promise_is_kept_by_the_new_process(services, fake_bot):
    """«Вернётся через несколько секунд» обещает уходящий процесс, а исполняет
    новый: обещание подменяется отчётом, следом приходит панель.

    Прежде не исполнял никто — после старта в чат никто не пишет, и админ
    оставался с мёртвым сообщением без кнопок, пока сам не слал /start.
    """
    from awgbot.bot import texts
    services.set_restart_wait(ADMIN, 4242)
    await ah.restore_panel_after_restart(fake_bot, services)

    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert len(edits) == 1 and edits[0][1] == ADMIN
    assert edits[0][2] == texts.BOT_RESTARTED, "обещание не сменилось отчётом"

    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 1, "панель не пришла отдельным сообщением"
    assert services.db.get_nav_message_id(ADMIN) != 4242, \
        "активным меню осталось отчётное сообщение — два живых меню в чате"


async def test_restart_promise_is_one_shot(services, fake_bot):
    """Флаг одноразовый: иначе каждый следующий старт переписывал бы давно
    отработавшее сообщение — в том числе спустя недели."""
    services.set_restart_wait(ADMIN, 4242)
    await ah.restore_panel_after_restart(fake_bot, services)
    fake_bot.records.clear()
    await ah.restore_panel_after_restart(fake_bot, services)
    assert fake_bot.records == []


async def test_ordinary_start_says_nothing(services, fake_bot):
    """Перезапуск не из чата (ребут хоста, падение, systemctl руками) — молчим.
    Панель без спроса была бы шумом, которого админ не заказывал."""
    await ah.restore_panel_after_restart(fake_bot, services)
    assert fake_bot.records == []


async def test_restart_panel_survives_an_unavailable_message(services, fake_bot, monkeypatch):
    """Сообщение удалили или оно старше суток — отчёт потерян, но панель обязана
    прийти всё равно: остаться без навигации админ не должен."""
    async def boom(*a, **k):
        raise RuntimeError("message to edit not found")
    monkeypatch.setattr(fake_bot, "edit_message_text", boom)

    services.set_restart_wait(ADMIN, 4242)
    await ah.restore_panel_after_restart(fake_bot, services)

    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 1 and sent[0][1] == ADMIN
    assert services.db.get_nav_message_id(ADMIN) != 4242, "нав указывает на мёртвое сообщение"


# ── порядок фильтров в роутере настроек ──────────────────────────────────────

def test_specific_settings_handlers_are_registered_before_the_generic_one():
    """Специфичные обработчики раздела обязаны стоять ВЫШЕ общего do_action.

    Фильтры проверяются в порядке регистрации, а у do_action он широкий
    (`F.act == "do"`). Окажись он первым — он перехватил бы и sec="mig", и
    sec="rt": ключ не подошёл бы ни к одной его ветке, функция закончилась бы
    молча, колбэк остался бы без ответа, а на кнопке — вечный спиннер.

    Ровно это и случилось на боевом сервере: рычаг переезда нажимался, хендлер
    отрабатывал за две миллисекунды и не делал ничего. Прямой вызов функции в
    тестах этого поймать не мог — маршрутизация там не участвует.
    """
    from awgbot.bot.handlers import settings as sh

    order = [h.callback.__name__ for h in sh.router.callback_query.handlers]
    generic = order.index("do_action")
    for specific in ("routing_action", "migration_action"):
        assert order.index(specific) < generic, (
            f"{specific} зарегистрирован после do_action — тот перехватит его "
            f"колбэки, и кнопка будет крутиться без ответа")
