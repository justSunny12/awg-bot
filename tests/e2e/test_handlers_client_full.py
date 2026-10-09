"""E2E: полный роутер клиента (handlers/client.py) — меню, устройства, выдача
конфигов, добавление/удаление, карточка устройства без ключа, самоблок,
пауза подписки, отсрочка.

Экраны этих веток в обычном состоянии — в эталоне tests/screens/client.txt;
здесь — БД, память диалога, чистка чата и ветки, которых в эталоне нет
(чужое существующее устройство, старые кнопки у пира без ключа, тихая пауза
администратора, РФ-байты).
"""
import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import client as ch
from awgbot.bot.callbacks import BlockCB, DelDeviceCB, DeviceCB, GraceCB, PauseCB, PresetCB
from awgbot.core.blocks import ClientBlock, DeviceBlock
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e
G = 1024 ** 3


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _fresh(services, client):
    return services.db.get_client(client.id)


async def test_device_open_foreign_existing_device_is_refused(services, fake_bot, make_active_client):
    """Существующее устройство другого клиента не открывается — отказ, а не
    чужая карточка (в эталоне — только несуществующий номер)."""
    client = make_active_client(tg_id=5002)
    cl = _fresh(services, client)
    other = make_active_client(tg_id=5003)
    foreign = services.add_device(other.id, "чужое")
    cb2, nav2 = _cb(fake_bot, 5002)
    await ch.device_open(cb2, DeviceCB(action="open", device_id=foreign.device_id), cl, services, FakeState())
    assert cb2.answers[-1][1] is True, "show_alert «не найдено»"
    assert not any(s[0] == "edit_text" for s in nav2.sent)


async def test_device_connect_menu_of_unmanaged_device(services, fake_bot, make_active_client):
    """Кнопка старого образца «Данные для подключения» у пира без ключа:
    ряда выдачи нет — пояснение и удаление (обычное устройство — в эталоне)."""
    client = make_active_client(tg_id=5003)
    app_id = services.db.create_device(client.id, "app", "PUBY", "PSK", "10.8.0.61", private_key=None)
    cl = _fresh(services, client)
    cb2, nav2 = _cb(fake_bot, 5003)
    await ch.device_connect_menu(cb2, DeviceCB(action="connect_menu", device_id=app_id), cl, services)
    text2, labels2 = last_screen(nav2)
    assert texts.UNMANAGED_DEVICE_LINE in text2 and "🗑 Удалить" in labels2
    assert not any(l in labels2 for l in ("🔗 Ссылка", "🔳 QR", "📄 Файл", "👤 Другу")), \
        "у пира без ключа выдать нечего — только объяснение и удаление"


async def test_gen_from_card_removes_the_menu_under_the_link(
        services, fake_bot, make_active_client):
    """Меню под кнопкой выдачи убрано — живым становится сообщение со ссылкой
    (текст и кнопка — в эталоне)."""
    client = make_active_client(tg_id=5004)
    dc = services.add_device(client.id, "iPhone")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5004)
    await ch.device_gen(cb, DeviceCB(action="gen_link", device_id=dc.device_id), cl, services)
    assert nav.deleted, "меню не должно висеть над ссылкой"


async def test_gen_from_menu_app_shows_dialog(services, fake_bot, make_active_client):
    """QR пиру без ключа: диалог «удали и добавь заново», QR не уходит (в
    эталоне — только ссылка)."""
    client = make_active_client(tg_id=5005)
    app_id = services.db.create_device(client.id, "app", "PUBW", "PSK", "10.8.0.62", private_key=None)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5005)
    await ch.device_gen(cb, DeviceCB(action="gen_qr", device_id=app_id), cl, services)
    text, labels = last_screen(nav)
    assert text == texts.UNMANAGED_DEVICE_DIALOG and "🗑 Удалить" in labels
    assert not any(s[0] in ("photo", "animation") for s in nav.sent), "QR для пира без ключа"


async def test_edit_device_traffic_flow(services, fake_bot, make_active_client):
    """Лимит устройства: «✏️ Другое» помнит устройство в диалоге, мусор не
    применяется, число записывается и диалог закрывается. Экраны — в эталоне."""
    client = make_active_client(tg_id=5006)                 # профиль без лимита
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5006)
    await ch.client_edit_device_traffic(cb, DeviceCB(action="edit_traffic", device_id=dc.device_id),
                                        cl, services, st)
    cb, nav = _cb(fake_bot, 5006)
    await ch.device_limit_preset(cb, PresetCB(kind="devlimit", ref=dc.device_id, val=-1),
                                 cl, services, st)
    assert (await st.get_data())["dev_ref"] == dc.device_id
    m_bad = FakeMessage(text="abc", chat_id=5006, user_id=5006, bot=fake_bot)
    await ch.client_edit_traffic_apply(m_bad, cl, services, st)
    assert services.db.get_device(dc.device_id).traffic_limit == 0
    m_ok = FakeMessage(text="10", chat_id=5006, user_id=5006, bot=fake_bot)
    await ch.client_edit_traffic_apply(m_ok, cl, services, st)
    assert services.db.get_device(dc.device_id).traffic_limit == 10 * G
    assert await st.get_state() is None


async def test_device_limit_preset_applies_at_once_and_refuses_above_profile(services, fake_bot, make_active_client):
    """Пресет — сразу; выше лимита профиля (старая кнопка) — отказ, лимит не
    тронут. Экраны и всплывашка — в эталоне."""
    client = make_active_client(tg_id=5011, traffic_limit=50 * G)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5011)
    await ch.device_limit_preset(cb, PresetCB(kind="devlimit", ref=dc.device_id, val=10),
                                 cl, services, FakeState())
    assert services.db.get_device(dc.device_id).traffic_limit == 10 * G
    cb, nav = _cb(fake_bot, 5011)
    await ch.device_limit_preset(cb, PresetCB(kind="devlimit", ref=dc.device_id, val=100),
                                 cl, services, FakeState())
    assert services.db.get_device(dc.device_id).traffic_limit == 10 * G, "лимит выше профиля принят"


async def test_transfer_puts_the_device_into_pending(services, fake_bot, make_active_client):
    """«👤 Передать» ставит устройство в ожидание друга."""
    client = make_active_client(tg_id=5007)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5007)
    await ch.device_transfer_do(cb, DeviceCB(action="transfer_yes", device_id=dc.device_id), cl, services)
    assert services.db.get_device(dc.device_id).friend_status == "pending"


async def test_add_device_for_friend_flow(services, fake_bot, make_active_client):
    """Своё число при создании другу — лимит устройства, и оно ждёт друга."""
    client = make_active_client(tg_id=5013, device_limit=3)
    cl = _fresh(services, client)
    st = FakeState()
    await st.update_data(for_friend=True, dev_name="ДругНоут")
    m_tr = FakeMessage(text="5", chat_id=5013, user_id=5013, bot=fake_bot)
    await ch.device_add_traffic(m_tr, cl, services, st)
    dev = [d for d in services.db.list_devices(client.id) if d.name == "ДругНоут"][0]
    assert dev.friend_status == "pending" and dev.traffic_limit == 5 * G


async def test_delete_device_flow(services, fake_bot, make_active_client):
    """Вопрос ничего не удаляет; подтверждение стирает устройство из БД."""
    client = make_active_client(tg_id=5014)
    services.add_device(client.id, "a")
    d2 = services.add_device(client.id, "b")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5014)
    await ch.device_delete_ask(cb, DelDeviceCB(device_id=d2.device_id, stage="ask"), cl, services)
    assert services.db.get_device(d2.device_id) is not None, "вопрос удалил устройство"
    cb2, nav2 = _cb(fake_bot, 5014)
    await ch.device_delete_confirm(cb2, DelDeviceCB(device_id=d2.device_id, stage="confirm"), cl, services)
    assert services.db.get_device(d2.device_id) is None


async def test_client_block_unblock_own_device(services, fake_bot, make_active_client):
    """Свой блок ставит и снимает бит USER (экраны — в эталоне)."""
    client = make_active_client(tg_id=5015)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5015)
    await ch.client_block_device(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id), cl, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    cb2, nav2 = _cb(fake_bot, 5015)
    await ch.client_unblock_device(cb2, BlockCB(target="dev", action="menu_unblock", ref=dc.device_id), cl, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER) == 0


async def test_client_unblock_when_not_user_blocked(services, fake_bot, make_active_client):
    """Снять свой блок, которого нет (кнопка устарела), — отказ всплывашкой."""
    client = make_active_client(tg_id=5016)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5016)
    await ch.client_unblock_device(cb, BlockCB(target="dev", action="menu_unblock", ref=dc.device_id), cl, services)
    assert cb.answers[-1][1] is True


async def test_pause_full_cycle(services, fake_bot, make_active_client):
    """Пауза одним экраном: экран дней паузу не ставит, выбор дней ставит
    сразу, снятие — без вопроса. Экраны, итоги и всплывашки — в эталоне."""
    client = make_active_client(tg_id=5017, period_kind="year")
    services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5017)
    await ch.pause_ask(cb, PauseCB(action="ask", ref=client.id), cl, services, FakeState())
    assert not _fresh(services, client).is_paused, "экран паузы уже поставил паузу"
    cb2, nav2 = _cb(fake_bot, 5017)
    await ch.pause_pick(cb2, PauseCB(action="pick", ref=client.id, days=7), cl, services, FakeState())
    fresh = _fresh(services, client)
    assert fresh.is_paused and int(fresh.pause_reserved_days) == 7
    cl2 = _fresh(services, client)
    cb3, nav3 = _cb(fake_bot, 5017)
    await ch.pause_resume(cb3, PauseCB(action="resume", ref=client.id), cl2, services)
    assert not _fresh(services, client).is_paused, "снятие ждёт подтверждения"


async def test_pause_other_accepts_a_number_in_range_only(services, fake_bot, make_active_client):
    """Своё число дней: границы диапазона и не-число отклоняются одной
    строкой (в эталоне — одно значение вне диапазона), пауза не ставится;
    число в диапазоне — ставит."""
    client = make_active_client(tg_id=5023, period_kind="year")
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5023)
    await ch.pause_other(cb, PauseCB(action="other", ref=client.id), cl, services, st)
    for bad in ("0", "29", "abc"):
        m = FakeMessage(text=bad, chat_id=5023, user_id=5023, bot=fake_bot)
        await ch.pause_other_apply(m, cl, services, st)
        assert [s[1] for s in m.sent if s[0] == "answer"] == ["⚠️ Нужно целое число от 1 до 28"], bad
    assert not _fresh(services, client).is_paused
    m = FakeMessage(text="3", chat_id=5023, user_id=5023, bot=fake_bot)
    await ch.pause_other_apply(m, cl, services, st)
    assert int(_fresh(services, client).pause_reserved_days) == 3


async def test_silent_admin_pause_is_invisible_to_the_client(services, fake_bot, make_active_client):
    """Тихая пауза администратора клиенту не видна — ни строки доступа, ни
    паузы в «💳 Подписка». Остальные варианты экрана (истекает, своя пауза,
    пауза администратора, бессрочная, без дней паузы) — в эталоне."""
    q = make_active_client(tg_id=5036, period_kind="year")
    services.enter_admin_pause(q.id, 0)
    services._client_set_block(q.id, ClientBlock.PAUSED | ClientBlock.ADMIN_SILENT)
    text, _ = await ch.sub_parts(services, q.id)
    assert "🟡" not in text and "приостановлен" not in text, \
        f"тихая пауза выдала себя клиенту: {text}"


async def test_resume_guard_blocks_non_user_pause(services, fake_bot, make_active_client):
    """Паузу администратора клиент не снимает: она остаётся (отказ — в эталоне)."""
    client = make_active_client(tg_id=5019, period_kind="year")
    services.enter_admin_pause(client.id, 0)
    services._client_set_block(client.id, ClientBlock.PAUSED)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5019)
    await ch.pause_resume(cb, PauseCB(action="resume", ref=client.id), cl, services)
    assert _fresh(services, client).is_paused


async def test_grace_take_happy_and_stale_ref(services, fake_bot, make_active_client):
    """Кнопка отсрочки с чужим номером профиля — отказ; своя — отсрочка
    записана в профиль."""
    client = make_active_client(tg_id=5020, period_kind="year")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5020)
    await ch.grace_take(cb, GraceCB(action="take", ref=999999), cl, services)
    assert cb.answers[-1][1] is True
    cb2, nav2 = _cb(fake_bot, 5020)
    await ch.grace_take(cb2, GraceCB(action="take", ref=client.id), cl, services)
    assert _fresh(services, client).grace_used == 1


async def test_client_renames_own_device(services, fake_bot, make_active_client, monkeypatch):
    """Клиент переименовывает СВОЁ устройство: имя в БД, приглашение и ввод
    убраны из чата. Итог на карточке — в эталоне."""
    cl = make_active_client(tg_id=7001, name="Клиент")
    dc = services.add_device(cl.id, "Старое")
    cb, nav = _cb(fake_bot, cl.tg_id)
    st = FakeState()
    await ch.client_device_edit_name_start(cb, DeviceCB(action="edit_name", device_id=dc.device_id),
                                            cl, services, st)
    msg = FakeMessage(text="Новое", chat_id=cl.tg_id, user_id=cl.tg_id, bot=fake_bot)
    await ch.client_device_edit_name_apply(msg, cl, services, st)
    assert services.db.get_device(dc.device_id).name == "Новое"
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert {nav.message_id, msg.message_id} <= deleted, "приглашение или ввод остались в чате"


async def test_client_cannot_rename_foreign_device(services, fake_bot, make_active_client, monkeypatch):
    """Чужое устройство (другого клиента) переименовать нельзя — own_device режет."""
    owner = make_active_client(tg_id=7002, name="Владелец")
    other = make_active_client(tg_id=7003, name="Чужой")
    dc = services.add_device(owner.id, "ЧужоеУстройство")
    cb, nav = _cb(fake_bot, other.tg_id)
    st = FakeState()
    # 'other' пытается открыть переименование чужого устройства
    await ch.client_device_edit_name_start(cb, DeviceCB(action="edit_name", device_id=dc.device_id),
                                            other, services, st)
    # own_device вернул None → ранний выход, device_id в state не записан
    assert "device_id" not in (await st.get_data())
    assert services.db.get_device(dc.device_id).name == "ЧужоеУстройство"


# ── РФ-доступ только админу ──────────────

async def test_client_screens_do_not_show_rf_even_when_allowed_and_counted(
        services, fake_bot, make_active_client):
    """РФ-часть трафика — сведения для админа. Клиенту с разрешённым
    РФ-доступом и накопленными байтами карточка устройства и экран подписки
    те же, что без них: иначе вопросы «что за РФ и почему столько»."""
    client = make_active_client(tg_id=5100, name="Ксюша")
    services.db.update_client_fields(client.id, routing_allowed=1)
    dc = services.add_device(client.id, "Телефон")

    async def screens():
        cl = _fresh(services, client)
        cb, nav = _cb(fake_bot, 5100)
        await ch.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), cl, services, FakeState())
        card = last_screen(nav)
        cb, nav = _cb(fake_bot, 5100)
        await ch.menu_info(cb, cl, services)
        return card, last_screen(nav)

    before = await screens()
    services.db.rf_add_bulk([(dc.device_id, 1024 ** 3, 3 * 1024 ** 3)])
    services.db.set_state("rf_month_rx", str(1024 ** 3))
    services.db.set_state("rf_month_tx", str(3 * 1024 ** 3))
    after = await screens()
    assert after == before, "экран клиента изменился от РФ-данных"
    card, sub = after
    assert "🇷🇺" not in card[0], card
    # в подписке «🇷🇺» — только слово в строке лимитов (доступ выдан), без байт
    assert [l for l in sub[0].splitlines() if "🇷🇺" in l] == [
        "Включено в подписку: ∞ ГБ в месяц · 3 устройства · 🇷🇺 РФ-доступ"], sub
