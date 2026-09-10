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
    assert "☑️ К1 🔴" in labels_none and "☑️ К2 🔴" in labels_none
    assert not any(l.startswith("✅ К1") for l in labels_none)

    some = kb.broadcast_targets(clients, {1})
    labels = _btn_texts(some)
    assert "✅ К1 🔴" in labels and "☑️ К2 🔴" in labels
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
    """Четыре формы отчёта — только ФАКТ доставки, со ссылкой «выше»: само
    объявление остаётся в чате предыдущим сообщением, и пересказывать его
    (текст, вложения) значит удваивать каждую рассылку в истории. Число
    адресатов называем, только когда в рассылку вошли друзья: без них оно
    равно числу профилей и уже видно из перечисления."""
    from awgbot.bot import texts as T

    one = T.broadcast_report(["Наташа"], False, 1, 0)
    assert one == "✅ Объявление выше доставлено владельцу профиля Наташа"
    assert "адресат" not in one

    one_fr = T.broadcast_report(["Наташа"], True, 2, 0)
    assert ("владельцу профиля Наташа и тем, с кем он поделился устройствами: "
            "всего 2 адресата.") in one_fr

    many = T.broadcast_report(["Наташа", "Ксюша"], False, 2, 0)
    assert many == "✅ Объявление выше доставлено владельцам профилей Наташа, Ксюша"

    many_fr = T.broadcast_report(["Наташа", "Ксюша"], True, 4, 0)
    assert ("владельцам профилей Наташа, Ксюша и тем, с кем они поделились "
            "устройствами: всего 4 адресата.") in many_fr

    for r in (one, one_fr, many, many_fr):
        assert "Текст объявления" not in r and "картинк" not in r, \
            "отчёт снова пересказывает объявление"


def test_broadcast_report_declines_recipient_word():
    from awgbot.bot import texts as T
    assert "всего 1 адресат." in T.broadcast_report(["А"], True, 1, 0)
    assert "всего 5 адресатов." in T.broadcast_report(["А"], True, 5, 0)


def test_broadcast_report_does_not_hide_failures():
    """«Доставлено» при недоставленных было бы неправдой, а узнать об этом
    больше неоткуда."""
    from awgbot.bot import texts as T
    r = T.broadcast_report(["А"], True, 3, 2)
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

    # текстовая рассылка: превью редактируется в ЧИСТЫЙ текст объявления
    # (след в чате), отчёт-факт приходит следом, панель — последней
    edits = [s for s in nav.sent if s[0] == "edit_text"]
    trace = edits[-1]
    assert trace[1] == "Профилактика <b>в ночь</b>", "след объявления потерян"
    assert trace[2] is None, "на следе не должно остаться кнопок"

    answers = [s for s in nav.sent if s[0] == "answer"]
    report = answers[0]
    assert report[1].startswith("✅ Объявление выше доставлено владельцу профиля Наташа")
    assert "Текст объявления" not in report[1], "отчёт дублирует текст"
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


async def test_only_the_last_update_finisher_keeps_its_menu_button(
        services, fake_bot, monkeypatch):
    """Цепочка ступеней self-update — живая кнопка «В меню» только у последнего
    финишера. У прежнего она снимается при отправке следующего; текст его при
    этом не трогается — история «какая ступень чем закончилась» остаётся.
    """
    from awgbot.runtime.main import report_update_result
    from awgbot.domain.services import Notification
    from awgbot.bot import keyboards as kb
    import awgbot.core.config as cfg

    step = {"n": 0}

    def fake_confirm():
        step["n"] += 1
        return Notification(cfg.ADMIN_ID, f"обновлён, ступень {step['n']}",
                            reply_markup=kb.update_done_menu())

    monkeypatch.setattr(services, "confirm_applied_update", fake_confirm)
    monkeypatch.setattr(services, "pop_update_wait", lambda: None)

    await report_update_result(fake_bot, services)       # ступень 1
    await report_update_result(fake_bot, services)       # ступень 2 (рестарт)
    await report_update_result(fake_bot, services)       # ступень 3

    stripped = [mid for kind, chat, mid in fake_bot.records if kind == "edit_markup"]
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 3, "финишеры не отправлены"
    # у двух прошлых кнопки сняты, у последнего — нет; история знает только его
    assert len(stripped) == 2, "снято не у всех прошлых (или у лишнего)"
    remaining = services.pop_update_reports()
    assert len(remaining) == 1 and remaining[0][1] not in stripped


async def test_admin_start_purges_menu_history(services, fake_bot):
    """/start админа удаляет все прошлые меню чата (история ведётся send_menu/edit_nav)."""
    from awgbot.bot.handlers import admin as admin_h
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    chat = cfg.ADMIN_ID
    for mid in (101, 102, 103):
        services.db.push_nav_history(chat, mid)
    msg = FakeMessage(chat_id=chat, user_id=chat, bot=fake_bot)
    await admin_h.admin_start(msg, services, FakeState())
    deleted = sorted(r[2] for r in fake_bot.records if r[0] == "delete_message")
    assert deleted == [101, 102, 103]
    assert services.db.pop_nav_history(chat) != [101, 102, 103], "история не очищена"


async def test_bundle_document_carries_menu_button_and_dims_settings(
        services, make_active_client, fake_bot, monkeypatch):
    """Бандл уходит с кнопкой «В меню», а экран настроек гаснет: живым остаётся
    одно меню — на самом бандле."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    monkeypatch.setattr(services, "gw_bundle_encrypted", lambda: (b"AWGGWB1\nxx", "b.enc"))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    sent_docs = []

    async def answer_document(doc, caption=None, reply_markup=None, **kw):
        sent_docs.append((caption, reply_markup)); return msg
    msg.answer_document = answer_document
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle"), services)
    assert sent_docs and sent_docs[0][1] is not None, "у бандла нет кнопки «В меню»"
    assert any(r[0] == "edit_reply_markup" for r in fake_bot.records), "экран настроек не погашен"
    assert msg.message_id in services.db.pop_content_msg_ids(cfg.ADMIN_ID), \
        "инструкция не помечена как контент — «В меню» её не удалит"


async def test_bundle_menu_button_deletes_the_file_message(services, fake_bot, monkeypatch):
    """«В меню» на бандле удаляет само сообщение с файлом (внутри ключ линка),
    а не снимает клавиатуру, как общая кнопка обновлений."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle_menu"), services)
    assert any(r[0] == "delete" for r in fake_bot.records), "сообщение с бандлом не удалено"
    assert any(r[0] == "answer" for r in fake_bot.records), "меню не показано"


def test_routing_lists_info_reports_count_age_and_period(services, monkeypatch):
    """Сводка по спискам: записи, возраст обновления, период — то, чего в чате
    не было вовсе, пока списки обновлялись молча."""
    import time
    from awgbot.core import settings
    monkeypatch.setattr(services, "_routing_read_cache", lambda name: ["a.ru", "b.ru"])
    monkeypatch.setattr(settings, "get", lambda k, d=None: 12 if k.endswith("lists_refresh_hours") else d)
    services.db.set_state(services._RT_LISTS_KEY, str(int(time.time()) - 7200))
    info = services.routing_lists_info()
    assert info["count"] == 2 and info["every_hours"] == 12
    assert 7000 <= info["age_seconds"] <= 7300
    from awgbot.bot import texts
    line = texts.routing_lists_block(info)
    assert "2 записей" in line and "2 ч назад" in line and "период 12 ч" in line


async def test_routing_lists_info_and_controls(services, fake_bot, monkeypatch):
    """Пикер периода пишет горячий ключ; «обновить сейчас» зовёт обновление с force."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from awgbot.core import settings
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    written = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [])
    forced = []
    monkeypatch.setattr(services, "routing_update_lists", lambda force=False: forced.append(force) or 7)
    async def noop_render(cb, sec, services_): pass
    monkeypatch.setattr(sh, "_render", noop_render)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)

    await sh.pick(cb, SetCB(sec="rt", act="pick", key="lists", val="12"), services)
    assert written["app.routing.lists_refresh_hours"] == 12

    cb.answers.clear()
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="lists_refresh"), services)
    assert forced == [True], "«обновить сейчас» не форсирует обновление"
    # ровно один ответ на колбэк: второй Telegram не показывает
    assert len(cb.answers) == 1 and "7 записей" in (cb.answers[0][0] or "")


async def test_menu_button_dismisses_every_other_update_window(services, fake_bot):
    """«В меню» на любом окне обновления снимает кнопки и у всех остальных —
    в цепочке ступеней живой должна остаться одна."""
    from awgbot.bot.handlers import admin as admin_h
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    chat = cfg.ADMIN_ID
    for mid in (501, 502, 503):
        services.remember_update_report(chat, mid)
    msg = FakeMessage(chat_id=chat, user_id=chat, bot=fake_bot, message_id=503)
    cb = FakeCallback(message=msg, user_id=chat, bot=fake_bot)
    await admin_h.update_menu(cb, services, FakeState())
    # фильтр ДО распаковки: у записей фейка разная длина
    stripped = sorted(r[2] for r in fake_bot.records if r[0] == "edit_markup")
    assert stripped == [501, 502, 503]
    assert services.pop_update_reports() == [], "история не очищена"


def test_routing_section_is_four_buttons():
    """Раздел маршрутизации: выключатель и три подраздела при включённой
    функции; при выключенной — только выключатель. Переключатели профилей и
    пикер периода в корне не живут — они в своих подразделах."""
    from awgbot.bot import keyboards as kb
    on = [b.text for row in kb.settings_routing(True).inline_keyboard for b in row]
    assert on[:4] == ["🟢 Условная маршрутизация", "⚙️ Конфигурация шлюза",
                      "📋 Списки маршрутизации", "👥 Доступность пользователям"], on
    assert not any("шифр" in t for t in on), "приписки про шифрование — не для UI"
    off = [b.text for row in kb.settings_routing(False).inline_keyboard for b in row]
    assert len(off) == 2 and "Условная маршрутизация" in off[0]      # выключатель + назад

    lists = [b.text for row in kb.settings_routing_lists(6).inline_keyboard for b in row]
    assert "🔘 6 ч" in lists and any("Обновить" in t for t in lists)


async def test_routing_subsections_render_and_are_empty_when_off(services, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.core import settings, config
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: True)
    monkeypatch.setattr(services, "routing_lists_info",
                        lambda: {"count": 3, "updated_at": None, "age_seconds": None,
                                 "every_hours": 12, "sources": 2})
    monkeypatch.setattr(services, "routing_grantable_clients", lambda: [])
    text, markup = await sh._screen("rt_lists", services)
    assert "3 записей" in text and "12 ч" in text
    text, markup = await sh._screen("rt_users", services)
    assert "Доступность" in text
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: False)
    text, markup = await sh._screen("rt_lists", services)
    assert "выключена" in text


async def test_bundle_button_opens_intro_screen_before_issuing(services, fake_bot, monkeypatch):
    """«Конфигурация шлюза» не выпускает файл сразу: сначала экран «что
    произойдёт» с «Выпустить файл» и «Отмена» (назад в раздел)."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from awgbot.bot import keyboards as kb
    from awgbot.core import settings as st
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "ROUTING_ENABLED", True)
    monkeypatch.setattr(st, "get_bool", lambda key, default=False: True)
    markup = kb.settings_routing(True)
    btn = [b for row in markup.inline_keyboard for b in row if "Конфигурация" in b.text][0]
    assert btn.callback_data == SetCB(sec="rt_bundle", act="open").pack()
    text, markup = await sh._screen("rt_bundle", services)
    assert "Что произойдёт" in text
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert SetCB(sec="rt", act="do", key="bundle").pack() in datas, "нет «Выпустить»"
    assert SetCB(sec="rt").pack() in datas, "нет «Отмена» назад в раздел"


# ── потребление за месяц: ссылки из панели ───────────────────────────────────

def _cmd(args):
    from aiogram.filters import CommandObject
    return CommandObject(prefix="/", command="start", args=args)


async def test_panel_traffic_line_is_a_deep_link(services, fake_bot):
    from awgbot.bot import texts
    out = texts.admin_panel({"ok": True, "traffic_rx": 1, "traffic_tx": 2},
                            bot_username="awg_test_bot")
    assert 'href="https://t.me/awg_test_bot?start=traffic">📊 Потребление за месяц (все)</a>' in out
    assert "Потребление за месяц (все)</a>: 3 Б" in out


async def test_start_traffic_opens_profiles_and_removes_the_command(
        services, make_active_client, fake_bot):
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    services.bot_username = "awg_test_bot"
    c = make_active_client("Профиль А")
    msg = FakeMessage(text="/start traffic", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd("traffic"))
    assert any(r[0] == "delete" for r in fake_bot.records), "команда /start traffic не удалена"
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "Потребление трафика за текущий месяц" in sent[-1]
    assert f'👤 <a href="https://t.me/awg_test_bot?start=traffic-{c.id}">Профиль А</a>' in sent[-1]


async def test_start_traffic_replaces_the_active_menu_in_place(services, make_active_client, fake_bot):
    """Экран потребления встаёт НА МЕСТО панели (редактированием), а не под ней."""
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    services.db.set_nav_message_id(cfg.ADMIN_ID, 777)
    msg = FakeMessage(text="/start traffic", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd("traffic"))
    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits and "Потребление трафика за текущий месяц" in edits[-1][2]
    assert not any(kind == "answer" for kind, _, _ in msg.sent), "экран ушёл новым сообщением"


async def test_start_traffic_client_opens_devices_and_back_leads_to_profiles(
        services, make_active_client, fake_bot):
    from awgbot.bot.handlers import admin as ah
    from awgbot.bot.callbacks import Menu
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    c = make_active_client("Профиль Б")
    msg = FakeMessage(text=f"/start traffic-{c.id}", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd(f"traffic-{c.id}"))
    sent = [(t, m) for kind, t, m in msg.sent if kind == "answer"]
    assert sent and "Потребление профиля Профиль Б за текущий месяц:" in sent[-1][0]
    back = [b for row in sent[-1][1].inline_keyboard for b in row]
    assert back and back[0].callback_data == Menu(action="traffic").pack()
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_traffic_profiles(cb, services)
    assert any("Потребление трафика за текущий месяц" in t for kind, t, _ in msg.sent if kind == "edit_text")


async def test_plain_start_still_purges_and_shows_panel(services, fake_bot):
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    msg = FakeMessage(text="/start", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd(None))
    assert any("Панель администратора" in t for kind, t, _ in msg.sent if kind == "answer")


async def test_devices_breakdown_lists_real_devices_with_traffic(services, make_active_client, fake_bot):
    """Регресс: с реальными устройствами экран падал на поле трафика (в бою —
    AttributeError, в тесте профиль был без устройств)."""
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    c = make_active_client("Профиль В")
    services.add_device(c.id, "Телефон")
    dev = services.db.list_devices(c.id)[0]
    services.db.add_traffic(dev.id, 3 * 1024 ** 2, 5 * 1024 ** 2)
    msg = FakeMessage(text=f"/start traffic-{c.id}", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd(f"traffic-{c.id}"))
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "🔴 Телефон: 8.0 МБ (↑ 3.0 МБ | ↓ 5.0 МБ)" in sent[-1]


def test_transfer_buttons_are_split_by_role(services, make_active_client):
    """Владелец: «Передать другу», без «в другой профиль». Админ: наоборот."""
    from awgbot.bot import keyboards as kbs
    c = make_active_client("Профиль Г")
    services.add_device(c.id, "Ноут")
    dev = services.db.list_devices(c.id)[0]
    owner = [b.text for row in kbs.device_actions(dev, is_admin=False, back_target="x").inline_keyboard for b in row]
    admin = [b.text for row in kbs.device_actions(dev, is_admin=True, back_target="x",
                                                  reassign_label="🔀 Передать в другой профиль").inline_keyboard for b in row]
    assert "👤 Передать другу" in owner and "🔀 Передать в другой профиль" not in owner
    assert "🔀 Передать в другой профиль" in admin and "👤 Передать другу" not in admin


# ── онлайн: статус в списке получателей и экран устройств онлайн ─────────────

def test_broadcast_targets_show_status_and_put_online_first(services, make_active_client):
    from awgbot.bot import keyboards as kbs
    a = make_active_client("Анна", tg_id=1001); b = make_active_client("Борис", tg_id=1002)
    labels = [btn.text for row in kbs.broadcast_targets([a, b], set(), {b.id}).inline_keyboard
              for btn in row if btn.text.startswith(("✅", "☑️")) and "все" not in btn.text]
    assert labels == ["☑️ Борис 🟢", "☑️ Анна 🔴"]


async def test_online_link_opens_the_list_of_online_devices(services, make_active_client, fake_bot, monkeypatch):
    from awgbot.bot import texts
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    from awgbot.util import timeutil
    import awgbot.core.config as cfg
    out = texts.admin_panel({"ok": True, "online_count": 2}, bot_username="awg_test_bot")
    assert 'href="https://t.me/awg_test_bot?start=online">📶 Устройств онлайн</a>: 2' in out
    c = make_active_client("Профиль Д")
    services.add_device(c.id, "iPhone 16 Pro"); services.add_device(c.id, "Старый ноут")
    on, off = services.db.list_devices(c.id)
    monkeypatch.setattr(timeutil, "handshake_is_online", lambda hs: hs == "on")
    monkeypatch.setattr(services, "online_devices",
                        lambda: [(d, "Профиль Д") for d in services.db.list_devices(c.id) if d.id == on.id])
    msg = FakeMessage(text="/start online", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd("online"))
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "📶 <b>Устройства онлайн (1):</b>" in sent[-1]
    assert f"📱 iPhone 16 Pro (Профиль Д) — {texts.plain_ip(on.address)}" in sent[-1] and "Старый ноут" not in sent[-1]
    assert f"<code>{on.address}</code>" in sent[-1], "адрес ушёл голым — Telegram сделает из него ссылку"


def test_device_line_format_and_plain_ip(services, make_active_client):
    from awgbot.bot import texts
    c = make_active_client("Профиль Е")
    services.add_device(c.id, "iPhone 16 Pro")
    dev = services.db.list_devices(c.id)[0]
    line = texts.device_line(dev)
    assert line.startswith(f"🔴 iPhone 16 Pro ({texts.plain_ip(dev.address)}), последний коннект: ")
    assert texts.plain_ip("10.9.1.2") == "<code>10.9.1.2</code>"


def test_list_screens_separate_entries_with_a_blank_line(services, make_active_client):
    from awgbot.bot import texts
    a = make_active_client("А", tg_id=1101); b = make_active_client("Б", tg_id=1102)
    out = texts.traffic_profiles_text([(a, 1, 1), (b, 2, 2)], "bot")
    assert "\n\n👤 " in out and out.count("\n\n") == 2


# ── истекающие подписки: панель, экран, продление с возвратом ────────────────

async def test_expiring_line_is_conditional_and_linked(services, make_active_client, fake_bot):
    from awgbot.bot import texts
    assert "Истекающие" not in texts.admin_panel({"ok": True}, bot_username="b", expiring=0)
    out = texts.admin_panel({"ok": True}, bot_username="b", expiring=2)
    assert out.endswith('<b><a href="https://t.me/b?start=expiring">⏳ Истекающие подписки</a>: 2</b>')


async def test_expiring_screen_and_extend_returns_to_it_or_menu(services, make_active_client, fake_bot):
    import datetime as dt
    from awgbot.bot import texts
    from awgbot.bot.handlers import admin as ah
    from awgbot.bot.callbacks import PeriodCB, Menu
    from awgbot.util import timeutil
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    services.bot_username = "b"
    now = timeutil.now()
    c = make_active_client("Скоро", tg_id=3001)
    services.db.update_client_fields(c.id, period_start=timeutil.to_iso(now - dt.timedelta(days=20)),
                                     period_end=timeutil.to_iso(now + dt.timedelta(days=3)), period_kind="month")
    msg = FakeMessage(text="/start expiring", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    state = FakeState()
    await ah.admin_start(msg, services, state, command=_cmd("expiring"))
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "⏳ <b>Истекающие подписки:</b>" in sent[-1]
    assert "👤 Скоро — осталось 2 дня 23 часа" in sent[-1] and "Период подписки: " in sent[-1]
    assert f'?start=extend-{c.id}">Продлить?</a>' in sent[-1]
    # «Продлить?» → выбор срока с отменой обратно в список (активное меню
    # сбрасываем, чтобы экран ушёл ответом, а не правкой — так видна клавиатура)
    services.db.set_nav_message_id(cfg.ADMIN_ID, None)
    await ah.admin_start(msg, services, state, command=_cmd(f"extend-{c.id}"))
    sent = [(t, m) for kind, t, m in msg.sent if kind == "answer"]
    assert "На какой срок продлить?" in sent[-1][0]
    cancel = [b for row in sent[-1][1].inline_keyboard for b in row if "Отмена" in b.text][0]
    assert cancel.callback_data == Menu(action="expiring").pack()
    assert (await state.get_data())["return_to"] == "expiring"
    # продление на год: остатка нет вопроса? остаток есть → вопрос; отвечаем «нет»
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.extend_period_chosen(cb, PeriodCB(kind="year", ctx="extend", ref=c.id), services, state)
    from awgbot.bot.callbacks import ConfirmCB
    await ah.extend_keep_answer(cb, ConfirmCB(action="keep", ref=c.id, yes=False), services, state)
    edits = [t for kind, t, _ in msg.sent if kind == "edit_text"]
    assert any(t.startswith("✅ Подписка профиля Скоро продлена на 1 год, до ")
               for t in edits), "итог не остался инфосообщением"
    answers = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert "Панель администратора" in answers[-1], "после продления не вернулись в меню (список опустел)"
    assert "продлена на" not in answers[-1], "меню дублирует инфосообщение"


# ── ✉️ E-mail: мастер подключения из чата ────────────────────────────────────

def _email_store(monkeypatch):
    from awgbot.core import settings
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: bool(store.get(k, d)))
    return store


async def test_email_wizard_known_provider_saves_after_live_check(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    store = _email_store(monkeypatch)
    checked = []
    monkeypatch.setattr(services, "email_check", lambda acc=None: (checked.append(acc), (True, "ок"))[1])
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    state = FakeState()
    await sh.email_action(cb, SetCB(sec="email", act="do", key="setup"), services, state)
    assert any("Подключение ящика" in t for kind, t, _ in msg.sent if kind == "edit_text")
    addr = FakeMessage(text="box@icloud.com", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.email_address(addr, state, services)
    assert any("Провайдер распознан" in t and "app-specific" in t for kind, t, _ in addr.sent)
    pw = FakeMessage(text="s3cret", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.email_password(pw, state, services)
    assert any(r[0] == "delete" for r in fake_bot.records), "сообщение с паролем не удалено"
    assert checked and checked[0].password == "s3cret" and checked[0].imap_host == "imap.mail.me.com"
    assert services.db.get_state("email_login") == "box@icloud.com"
    assert store["email.smtp_host"] == "smtp.mail.me.com"
    assert any("подключён" in t for kind, t, _ in pw.sent)
    assert await state.get_data() == {}


async def test_email_wizard_unknown_domain_asks_servers_and_failed_check_saves_nothing(
        services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    _email_store(monkeypatch)
    monkeypatch.setattr(services, "email_check", lambda acc=None: (False, "IMAP отверг логин/пароль"))
    state = FakeState(); await state.set_state("x")
    m = lambda t: FakeMessage(text=t, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    a = m("box@corp.example"); await sh.email_address(a, state, services)
    assert any("IMAP-сервер" in t for kind, t, _ in a.sent)
    await sh.email_imap_host(m("imap.corp.example"), state)
    bad = m("99999"); await sh.email_imap_port(bad, state)
    assert any("порта" in t for kind, t, _ in bad.sent)
    await sh.email_imap_port(m("993"), state)
    await sh.email_smtp_host(m("smtp.corp.example"), state)
    await sh.email_smtp_port(m("587"), state)
    pw = m("pw"); await sh.email_password(pw, state, services)
    assert any("Не подключено" in t and "IMAP отверг" in t for kind, t, _ in pw.sent)
    assert not services.db.get_state("email_login")


async def test_email_forget_needs_confirmation_and_toggle_resume(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    store = _email_store(monkeypatch)
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    from tests.conftest import FakeState
    await sh.email_action(cb, SetCB(sec="email", act="do", key="forget"), services, FakeState())
    assert services.email_account() is not None
    assert any("Отключить почту?" in t for kind, t, _ in msg.sent if kind == "edit_text")
    await sh.toggle(cb, SetCB(sec="email", act="toggle", key="email.resume_enabled"), services)
    assert store["email.resume_enabled"] is False
    await sh.email_action(cb, SetCB(sec="email", act="do", key="forget!"), services, FakeState())
    assert services.email_account() is None
    assert any("Почта отключена" in t for kind, t, _ in msg.sent if kind == "answer")


# ── бэкап на почту и запасной канал для критичных алертов ────────────────────

async def test_backup_channel_email_requires_mailbox_and_encryption(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    store = _email_store(monkeypatch)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.pick(cb, SetCB(sec="backup", act="pick", key="channel", val="email"), services)
    assert any("Почта не настроена" in t for kind, t, _ in msg.sent if kind == "edit_text")
    assert "app.scheduler.backup_channel" not in store
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    await sh.pick(cb, SetCB(sec="backup", act="pick", key="channel", val="email"), services)
    assert "app.scheduler.backup_channel" not in store, "без шифрования почтовый канал не включается"
    services.backup_set_passphrase("correct horse battery")
    await sh.pick(cb, SetCB(sec="backup", act="pick", key="channel", val="email"), services)
    assert store["app.scheduler.backup_channel"] == "email"
    # «создать сейчас» уходит письмом
    mailed = []
    monkeypatch.setattr(services, "make_backup", lambda: ["/tmp/a.enc", "/tmp/b.enc"])
    monkeypatch.setattr(services, "email_send_backup", lambda paths: mailed.append(paths))
    await sh.do_action(cb, SetCB(sec="backup", act="do", key="now"), services)
    assert mailed == [["/tmp/a.enc", "/tmp/b.enc"]]
    assert any("отправлена на ящик" in t and "box@icloud.com" in t for kind, t, _ in msg.sent if kind == "answer")


async def test_email_fallback_toggle_offers_setup_without_mailbox(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    store = _email_store(monkeypatch)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.toggle(cb, SetCB(sec="notify", act="toggle", key="notifications.email_fallback"), services)
    assert any("Почта не настроена" in t for kind, t, _ in msg.sent if kind == "edit_text")
    assert "notifications.email_fallback" not in store
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    await sh.toggle(cb, SetCB(sec="notify", act="toggle", key="notifications.email_fallback"), services)
    assert store["notifications.email_fallback"] is True


async def test_critical_alert_goes_to_email_when_telegram_is_down(monkeypatch):
    from aiogram.exceptions import TelegramNetworkError
    from awgbot.bot import notifier
    from awgbot.domain.services import Notification
    import awgbot.core.config as cfg

    class DeadBot:
        async def send_message(self, *a, **k):
            raise TelegramNetworkError(method=None, message="network down")

    mailed = []

    async def fb(text):
        mailed.append(text)
    notifier.set_email_fallback(fb)
    try:
        await notifier.send_notifications(DeadBot(), [
            Notification(cfg.ADMIN_ID, "обычное", critical=False),
            Notification(cfg.ADMIN_ID, "🚨 сервис лежит", critical=True),
            Notification(12345, "🚨 чужой критичный", critical=True),
        ])
    finally:
        notifier.set_email_fallback(None)
    assert mailed == ["🚨 сервис лежит"], "на почту — только критичное и только админу"


# ── 🔐 шифрование бэкапов из чата ────────────────────────────────────────────

async def test_backup_passphrase_flow_deletes_messages_and_requires_match(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from awgbot.bot import keyboards as kbs
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    _email_store(monkeypatch)
    rows = [[b.text for b in r] for r in kbs.settings_backup(False).inline_keyboard]
    assert rows[1] == ["🔐 Шифрование: 🔴 выключено"], rows
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.do_action(cb, SetCB(sec="backup", act="do", key="enc"), services)
    assert any("Шифрование резервных копий" in t for kind, t, _ in msg.sent if kind == "edit_text")
    state = FakeState()
    await sh.backup_passphrase_start(cb, state)
    m = lambda t: FakeMessage(text=t, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    short = m("abc"); await sh.backup_passphrase_first(short, state)
    assert any("короче" in t for kind, t, _ in short.sent)
    await sh.backup_passphrase_first(m("correct horse battery"), state)
    wrong = m("correct horse batery"); await sh.backup_passphrase_second(wrong, state, services)
    assert any("не совпали" in t for kind, t, _ in wrong.sent) and not services.backup_encryption_enabled()
    await sh.backup_passphrase_first(m("correct horse battery"), state)
    ok = m("correct horse battery"); await sh.backup_passphrase_second(ok, state, services)
    assert services.backup_enc_kwargs() == {"passphrase": "correct horse battery"}
    deletes = [r for r in fake_bot.records if r[0] == "delete"]
    assert len(deletes) >= 4, "сообщения с фразой должны удаляться"
    assert not any("correct horse" in t for kind, t, _ in ok.sent), "фраза не должна печататься обратно"
    assert [[b.text for b in r] for r in kbs.settings_backup(True).inline_keyboard][1] == ["🔐 Шифрование: ✅ включено"]


# ── ♻️ восстановление из файла в чате ────────────────────────────────────────

async def test_backup_file_in_chat_offers_restore_and_confirm_launches(services, fake_bot, monkeypatch, tmp_path):
    import io, json, tarfile
    from awgbot.bot.handlers import admin as ah, settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = json.dumps({"role": "main", "created_at": "2026-09-09T10:30:00+03:00"}).encode()
        ti = tarfile.TarInfo("state/backup-meta.json"); ti.size = len(raw); tar.addfile(ti, io.BytesIO(raw))
    blob = buf.getvalue()

    class DlBot(FakeBot):
        async def download(self, doc, destination=None):
            destination.write(blob)
    bot = DlBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    msg.document = type("D", (), {"file_name": "awg-bot-backup-main-x.tgz", "file_size": len(blob), "file_id": "F"})()
    state = FakeState()
    await ah.admin_document(msg, services, state)
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "бэкап настроек бота и сервиса от 09.09.2026 10:30" in sent[-1] and "Важно!" in sent[-1]
    launched = []
    monkeypatch.setattr(services, "launch_restore", lambda path: launched.append(path))
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await sh.backup_restore_action(cb, SetCB(sec="backup", act="do", key="restore!"), services, state)
    assert launched and launched[0].endswith("restore-pending.tgz")
    assert (tmp_path / "restore-pending.tgz").read_bytes() == blob
    assert any("Восстанавливаю" in t for kind, t, _ in msg.sent if kind == "answer")
    assert await state.get_data() == {}


async def test_foreign_role_backup_is_rejected_in_chat(services, fake_bot, monkeypatch):
    import io, json, tarfile
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeBot, FakeMessage, FakeState
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "ROLE", "client")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = json.dumps({"role": "gw", "created_at": "2026-09-09T10:30:00+03:00"}).encode()
        ti = tarfile.TarInfo("state/backup-meta.json"); ti.size = len(raw); tar.addfile(ti, io.BytesIO(raw))
    blob = buf.getvalue()

    class DlBot(FakeBot):
        async def download(self, doc, destination=None):
            destination.write(blob)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=DlBot())
    msg.document = type("D", (), {"file_name": "b.tgz", "file_size": len(blob), "file_id": "F"})()
    await ah.admin_document(msg, services, FakeState())
    assert any("копия агента шлюза" in t for kind, t, _ in msg.sent if kind == "answer")


def test_notify_section_layout_and_profiles_submenu(monkeypatch):
    from awgbot.bot import keyboards as kbs
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: d)
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: d)
    rows = [[b.text for b in r] for r in kbs.settings_notify().inline_keyboard]
    assert rows[0] == ["🔴 E-mail при недоступности Telegram"]
    assert rows[1] == ["🟢 Тихие часы"]
    assert rows[-2] == ["👥 События профилей"] and rows[-1][0].endswith("Назад")
    assert not any("Активация" in b for r in rows for b in r), "события профилей ушли в подменю"
    sub = [[b.text for b in r] for r in kbs.settings_notify_clients().inline_keyboard]
    assert sub[0] == ["🟢 Активация профиля"] and len(sub) == 5
