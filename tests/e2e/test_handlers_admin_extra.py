"""E2E: добор веток роутера админа — лимит устройств пресетами без
подтверждения, продление, выдача файла, блок устройства, личные qr/file,
устройство профилю, «Мои устройства», карточка пира без приватного ключа.
Главная, ссылки и трафик — test_admin_home.py и test_admin_traffic_tree.py.

Объявления — test_handlers_broadcast.py; почта и бэкапы из чата —
test_handlers_settings_mail_backup.py; раскладка настроек — test_settings_layout.py.

Экраны этих веток — в эталоне tests/screens/admin.txt, в том числе
понижение лимита ниже занятого, гонка за слот, кнопки выдачи старого образца
(шлюза в выборе нет), приглашение другу и пир без ключа на карточке админа,
пометки адресатов объявления с продлением; здесь — БД, память диалога и
число уведомлений владельцу.
"""

import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers.admin import devices as admin_devices
from awgbot.bot.callbacks import ClientCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


# ── понижение лимита ниже числа устройств — без подтверждения ──────────────
async def test_lowering_the_device_limit_by_preset_applies_at_once_and_notifies_once(
        services, fake_bot, make_active_client):
    """Лимит ниже числа устройств — без диалога подтверждения, сразу в БД,
    владельцу — одно уведомление; итог первой строкой «✏️ Изменить» с «⚠️
    сейчас 2 из 1» — снимок adm.cl.limit.preset.low. Верни подтверждение —
    лишний шаг на каждое изменение."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=6300, device_limit=5)
    services.add_device(client.id, "a")
    services.add_device(client.id, "b")
    cb2, nav2 = _acb(fake_bot)
    await ah.edit_limit_preset(cb2, PresetCB(kind="cli_devs", ref=client.id, val=1), services, FakeState())
    assert services.db.get_client(client.id).device_limit == 1, "лимит не применён сразу"
    notes = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 6300]
    assert len(notes) == 1, "клиент должен узнать о новом лимите ровно одним уведомлением"


async def test_typed_device_limit_rejects_garbage_and_applies_the_number(services, fake_bot, make_active_client):
    """«✏️ Другое» → мусор не применяется и переспрашивается, число —
    применяется, диалог закрыт. Переспрос и итог на «✏️ Изменить» — снимки
    adm.cl.limit.other.bad, adm.cl.limit.other.done."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=6301, device_limit=2)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_preset(cb, PresetCB(kind="cli_devs", ref=client.id, val=-1), services, st)
    bad = _amsg(fake_bot, "много")
    await ah.edit_limit_apply(bad, services, st)
    assert services.db.get_client(client.id).device_limit == 2, "мусор применился как лимит"
    ok = _amsg(fake_bot, "7")
    await ah.edit_limit_apply(ok, services, st)
    assert services.db.get_client(client.id).device_limit == 7
    assert await st.get_data() == {}, "диалог не закрыт"


# ── устройство профилю ───────────────────────────────────────────────────────
async def test_add_device_to_a_profile_creates_it_and_notifies_the_owner_once(
        services, fake_bot, make_active_client):
    """«➕ Устройство» в карточке профиля: одно имя → устройство в БД,
    владельцу — ровно одно уведомление. Экраны и текст уведомления — снимок
    adm.cl.add_device.done."""
    client = make_active_client(tg_id=6307, name="Клиент-6307", device_limit=3)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_start(cb, ClientCB(action="add_device", client_id=client.id), services, st)
    msg = _amsg(fake_bot, "Планшет")
    await ah.admin_add_device_name(msg, services, st)
    assert [d.name for d in services.db.list_devices(client.id)] == ["Планшет"]
    owner_notes = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 6307]
    assert len(owner_notes) == 1, owner_notes


async def test_add_device_to_a_full_profile_opens_no_name_input_and_keeps_the_limit(
        services, fake_bot, make_active_client):
    """Лимит исчерпан — экран «добавить слот?» (в эталоне), ввод имени не
    открыт: при полном лимите он кончался бы ошибкой сервиса; лимит без
    согласия не растёт."""
    client = make_active_client("Коля", tg_id=6308, device_limit=1)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_start(cb, ClientCB(action="add_device", client_id=client.id), services, st)
    assert await st.get_state() is None, "ввод имени открыт при исчерпанном лимите"
    assert services.db.get_client(client.id).device_limit == 1, "лимит поднят без согласия"


async def test_add_slot_raises_the_limit_notifies_the_owner_once_and_asks_the_name(
        services, fake_bot, make_active_client):
    """«➕ Слот и добавить»: лимит = занято + 1, владельцу ровно одно
    сообщение (текст — снимок adm.cl.add_device.slot), дальше ввод имени и
    устройство создаётся. Без уведомления клиент увидит чужой слот молча; без
    ввода имени кнопка ничего не добавляет."""
    client = make_active_client("Коля", tg_id=6309, device_limit=1)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await admin_devices.admin_add_device_slot(cb, ClientCB(action="add_device_slot", client_id=client.id), services, st)
    assert services.db.get_client(client.id).device_limit == 2
    notes = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 6309]
    assert len(notes) == 1, notes
    assert await st.get_state() == "AdminAddDevice:name"
    msg = _amsg(fake_bot, "Планшет")
    await ah.admin_add_device_name(msg, services, st)
    assert sorted(d.name for d in services.db.list_devices(client.id)) == ["a", "Планшет"]


async def test_add_slot_pressed_twice_raises_the_limit_only_once(
        services, fake_bot, make_active_client):
    """Повторное нажатие (или два окна) — лимит уже не исчерпан: второй раз
    лимит не растёт и владелец второго уведомления не получает."""
    client = make_active_client(tg_id=6310, device_limit=1)
    services.add_device(client.id, "a")
    for _ in range(2):
        cb, _ = _acb(fake_bot)
        await admin_devices.admin_add_device_slot(cb, ClientCB(action="add_device_slot", client_id=client.id),
                                       services, FakeState())
    assert services.db.get_client(client.id).device_limit == 2, "двойное нажатие дало два слота"
    notes = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 6310]
    assert len(notes) == 1, notes


async def test_limit_race_while_typing_the_name_creates_nothing(
        services, fake_bot, make_active_client):
    """Пока админ вводил имя, слот заняли — устройство не создано (заметка
    «⚠️ Достигнут лимит устройств: … удали N» — снимок adm.cl.add_device.race)."""
    client = make_active_client(tg_id=6311, device_limit=2)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.admin_add_device_start(cb, ClientCB(action="add_device", client_id=client.id), services, st)
    assert await st.get_state() == "AdminAddDevice:name"
    services.add_device(client.id, "b")                   # гонка: слот занят
    msg = _amsg(fake_bot, "Планшет")
    await ah.admin_add_device_name(msg, services, st)
    assert "Планшет" not in [d.name for d in services.db.list_devices(client.id)], "устройство сверх лимита"


async def test_self_add_over_a_limit_opens_no_name_input(services, fake_bot):
    """Своему профилю админ лимит обычно не ставит, но если стоит и занят —
    ввод имени не открывается (всплывашка «… удали N» — снимок
    adm.self.add.full)."""
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "phone")
    services.db.update_client_fields(ac.id, device_limit=1)
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.self_add_start(cb, services, st)
    assert await st.get_state() is None, "ввод имени открыт при занятом лимите"


def test_limit_reached_line_counts_what_to_delete():
    """N — сколько удалить, чтобы добавить одно: при занятом сверх лимита
    (лимит понижен) — больше единицы."""
    assert texts.limit_reached_line(3, 3) == "Достигнут лимит устройств: чтобы добавить новое, удали 1"
    assert texts.limit_reached_line(5, 3).endswith("удали 3")


async def test_device_limit_other_refuses_above_profile_and_applies_within(
        services, fake_bot, make_active_client):
    """«✏️ Другое» у лимита устройства: число выше лимита профиля не
    принимается, в пределах — записывается. Экраны ввода и итога — в эталоне."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=6309, traffic_limit=50 * 1024 ** 3)
    dc = services.add_device(client.id, "Тел")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.device_limit_preset(cb, PresetCB(kind="devlimit", ref=dc.device_id, val=-1), services, st)
    over = _amsg(fake_bot, "70")
    await ah.edit_traffic_apply(over, services, st)
    assert int(services.db.get_device(dc.device_id).traffic_limit) == 0, "лимит выше профиля принят"
    ok = _amsg(fake_bot, "20")
    await ah.edit_traffic_apply(ok, services, st)
    assert int(services.db.get_device(dc.device_id).traffic_limit) == 20 * 1024 ** 3
