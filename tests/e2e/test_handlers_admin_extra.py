"""E2E: добор веток роутера админа — лимит устройств пресетами без
подтверждения, продление, выдача файла, блок устройства, личные qr/file,
устройство профилю, «Мои устройства», карточка пира без приватного ключа.
Главная, ссылки и трафик — test_admin_home.py и test_admin_traffic_tree.py.

Объявления — test_handlers_broadcast.py; почта и бэкапы из чата —
test_handlers_settings_mail_backup.py; раскладка настроек — test_settings_layout.py.

Экраны этих веток в обычном состоянии — в эталоне tests/screens/admin.txt;
здесь — БД, память диалога, уведомления владельцу, гонки и состояния, которых
в эталоне нет (понижение лимита ниже занятого, шлюз в «Моих устройствах»,
приглашение другу на карточке админа).
"""

import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers.admin import devices as admin_devices
from awgbot.bot.callbacks import AdminSelfCB, ClientCB, DeviceCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


# ── понижение лимита ниже числа устройств — без подтверждения ──────────────
async def test_lowering_the_device_limit_by_preset_applies_at_once_with_a_warning(
        services, fake_bot, make_active_client):
    """Лимит ниже числа устройств — без диалога подтверждения: «✏️ Изменить»
    с итогом первой строкой «✅ Устройств: 5 → 1 · ⚠️ сейчас 2 из 1 — …».
    Потеряй строку ⚠️ — админ не узнает, что профиль теперь не сможет добавить
    ни одного устройства; верни подтверждение — лишний шаг на каждое изменение."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=6300, device_limit=5)
    services.add_device(client.id, "a")
    services.add_device(client.id, "b")
    cb2, nav2 = _acb(fake_bot)
    await ah.edit_limit_preset(cb2, PresetCB(kind="cli_devs", ref=client.id, val=1), services, FakeState())
    assert services.db.get_client(client.id).device_limit == 1, "лимит не применён сразу"
    text2, _ = last_screen(nav2)
    first = text2.splitlines()[0]
    assert first == "✅ Устройств: 5 → 1 · ⚠️ сейчас 2 из 1 — новые не добавить, пока не станет меньше", text2
    notes = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 6300]
    assert len(notes) == 1, "клиент должен узнать о новом лимите ровно одним уведомлением"


async def test_typed_device_limit_rejects_garbage_and_applies_the_number(services, fake_bot, make_active_client):
    """«✏️ Другое» → мусор не применяется и переспрашивается, число —
    применяется, диалог закрыт. Итог на «✏️ Изменить» — в эталоне."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=6301, device_limit=2)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_preset(cb, PresetCB(kind="cli_devs", ref=client.id, val=-1), services, st)
    bad = _amsg(fake_bot, "много")
    await ah.edit_limit_apply(bad, services, st)
    assert [s[1] for s in bad.sent if s[0] == "answer"] == [texts.NUMBER_BAD_LIMIT]
    assert services.db.get_client(client.id).device_limit == 2, "мусор применился как лимит"
    ok = _amsg(fake_bot, "7")
    await ah.edit_limit_apply(ok, services, st)
    assert services.db.get_client(client.id).device_limit == 7
    assert await st.get_data() == {}, "диалог не закрыт"


# ── личные qr/file, устройство профилю ───────────────────────────────────────
async def test_self_gen_qr_file_pickers(services, fake_bot):
    """QR и файл с главной админа предлагают его устройство (в эталоне —
    только выбор под ссылку)."""
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "d")
    for action in ("gen_qr", "gen_file"):
        cb, nav = _acb(fake_bot)
        await ah.self_gen_pick(cb, AdminSelfCB(action=action), services)
        _, labels = last_screen(nav)
        assert any("d" == l.strip("🖥📱💻 ") or l.endswith(" d") or l == "d" for l in labels) \
            or any("d" in l for l in labels), "устройство не предложено к выбору"


async def test_add_device_to_a_profile_creates_it_and_notifies_the_owner_once(
        services, fake_bot, make_active_client):
    """«➕ Устройство» в карточке профиля: одно имя → устройство в БД,
    владельцу — ровно одно уведомление. Экраны — в эталоне."""
    client = make_active_client(tg_id=6307, name="Клиент-6307", device_limit=3)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_start(cb, ClientCB(action="add_device", client_id=client.id), services, st)
    msg = _amsg(fake_bot, "Планшет")
    await ah.admin_add_device_name(msg, services, st)
    assert [d.name for d in services.db.list_devices(client.id)] == ["Планшет"]
    owner_notes = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 6307]
    assert len(owner_notes) == 1 and "Планшет" in owner_notes[0][2]


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
    """«➕ Слот и добавить»: лимит = занято + 1, владельцу — «Лимит устройств
    изменён: 1 → 2» ровно одно сообщение, дальше ввод имени и устройство
    создаётся. Без уведомления клиент увидит чужой слот молча; без ввода
    имени кнопка ничего не добавляет."""
    client = make_active_client("Коля", tg_id=6309, device_limit=1)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await admin_devices.admin_add_device_slot(cb, ClientCB(action="add_device_slot", client_id=client.id), services, st)
    assert services.db.get_client(client.id).device_limit == 2
    notes = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 6309]
    assert notes == [texts.limit_changed_notice(1, 2)], notes
    assert notes[0] == "Лимит устройств изменён: 1 → 2"
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


async def test_limit_race_while_typing_the_name_says_how_many_to_delete(
        services, fake_bot, make_active_client):
    """Пока админ вводил имя, слот заняли — заметка «⚠️ Достигнут лимит
    устройств: чтобы добавить новое, удали N», устройство не создано.
    Без заметки админ решит, что устройство добавилось."""
    client = make_active_client(tg_id=6311, device_limit=2)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.admin_add_device_start(cb, ClientCB(action="add_device", client_id=client.id), services, st)
    assert await st.get_state() == "AdminAddDevice:name"
    services.add_device(client.id, "b")                   # гонка: слот занят
    msg = _amsg(fake_bot, "Планшет")
    await ah.admin_add_device_name(msg, services, st)
    assert "Планшет" not in [d.name for d in services.db.list_devices(client.id)]
    shown = " ".join(t or "" for kind, t, _ in msg.sent if kind in ("answer", "edit_text"))
    assert "⚠️ Достигнут лимит устройств: чтобы добавить новое, удали 1" in shown, shown


async def test_self_add_over_a_limit_says_how_many_to_delete(services, fake_bot):
    """Своему профилю админ лимит обычно не ставит, но если стоит и занят —
    всплывашка той же строкой «Достигнут лимит устройств: … удали N», ввод
    имени не открывается."""
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "phone")
    services.db.update_client_fields(ac.id, device_limit=1)
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.self_add_start(cb, services, st)
    assert cb.answers == [("Достигнут лимит устройств: чтобы добавить новое, удали 1", True)], cb.answers
    assert await st.get_state() is None


def test_limit_reached_line_counts_what_to_delete():
    """N — сколько удалить, чтобы добавить одно: при занятом сверх лимита
    (лимит понижен) — больше единицы."""
    assert texts.limit_reached_line(3, 3) == "Достигнут лимит устройств: чтобы добавить новое, удали 1"
    assert texts.limit_reached_line(5, 3).endswith("удали 3")


async def test_admin_menu_devices_puts_the_gateway_on_top(services, fake_bot):
    """Шлюз в «Моих устройствах» — вверху с «[шлюз]» (в эталоне шлюза в
    списке нет)."""
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "Ноут")
    pi = services.add_device(ac.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30")
    cb, nav = _acb(fake_bot)
    await ah.admin_menu_devices(cb, services, FakeState())
    _, labels = last_screen(nav)
    assert labels[0] == "🛰 NASPi [шлюз]", labels
    assert any(l.endswith(" Ноут") for l in labels[1:]), labels


async def test_admin_card_hides_the_reinvite_button(services, make_active_client):
    """Устройство с незавершённым инвайтом другу: у владельца кнопка «Перевыдать
    инвайт» есть (эталон cl.dev.pending), у админа на карточке того же
    устройства — нет.

    Хендлер перевыдачи живёт в роутере клиента (ссылка уходит в чат владельца);
    для админа нажатие уходило в никуда — спиннер до таймаута.
    """
    from awgbot.bot import keyboards as kb

    client = make_active_client(tg_id=5009)
    did = services.add_device(client.id, "d").device_id
    services.make_device_friendly(did)
    dev = services.db.get_device(did)
    assert dev.friend_status == "pending"

    admin = _btn_texts(kb.device_actions(dev, is_admin=True, back_target="x"))
    assert not any("Приглашение" in t for t in admin), admin


async def test_unmanaged_device_card_via_the_old_button_says_there_is_no_link(
        services, fake_bot):
    """Пир без приватного ключа, открытый старой кнопкой «Данные для
    подключения»: карточка говорит «ссылки нет», ряда выдачи в ней нет.
    Диалог выдачи такому пиру — в эталоне (adm.gen.unmanaged)."""
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "чужой", "PUBQ2", "PSK", "10.8.1.10",
                                    private_key=None)
    cb, nav = _acb(fake_bot)
    await ah.admin_device_connect_menu(cb, DeviceCB(action="connect_menu", device_id=did),
                                       services, FakeState())
    text, labels = last_screen(nav)
    assert "✳️ Добавлено не ботом — ссылки нет" in text, text
    assert not {"🔗 Ссылка", "🔳 QR", "📄 Файл"} & set(labels), labels

def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# ── онлайн: статус в списке получателей и экран устройств онлайн ─────────────

def test_broadcast_targets_mark_subscription_only_in_extend_mode(services, make_active_client):
    """Онлайн-кружков в выборе адресатов нет (не онлайн — прочитает потом). В
    режиме с продлением справа от имени — состояние подписки: ∞ бессрочная,
    🟡 и дата — истекла; активной — ничего."""
    from awgbot.bot import keyboards as kbs
    a = make_active_client("Анна", tg_id=1001)
    b = make_active_client("Борис", tg_id=1002, period_kind="never")
    v = make_active_client("Вера", tg_id=1003)
    services.db.update_client_fields(v.id, period_end="2026-09-01T00:00:00+03:00", status="expired")
    rows = [services.db.get_client(c.id) for c in (a, b, v)]
    def labels(extend):
        return [btn.text for row in kbs.broadcast_targets(rows, set(), extend=extend).inline_keyboard
                for btn in row if btn.callback_data.startswith("bc:tgl:")]
    assert labels(False) == ["☑️ Анна", "☑️ Борис", "☑️ Вера"]
    # год текущий — в дате его нет
    assert labels(True) == ["☑️ Анна", "☑️ Борис ∞", "☑️ Вера 🟡 01.09"]


# ── шлюз не предлагается под ссылку/QR/файл ──────────────────────────────────
async def test_gateway_is_not_offered_for_link_qr_file(services, fake_bot):
    """Сервис выдачу шлюзу отвергает; кнопки старого образца «Ссылка/QR/Файл»
    ставили его в список — клик вёл в алерт. В пикере его нет, а профиль с
    одним шлюзом считается без устройств. Старая «Выдать конфиг» профиля
    открывает карточку профиля, а у профиля админа карточки нет — главная."""
    services.ensure_admin_client()
    ac = services.admin_client()
    pi = services.add_device(ac.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30")
    for action in ("gen_link", "gen_qr", "gen_file"):
        cb, nav = _acb(fake_bot)
        await ah.self_gen_pick(cb, AdminSelfCB(action=action), services)
        assert any("добавь устройство" in a.lower() for a, _ in cb.answers if a), "шлюз сошёл за устройство"
    services.add_device(ac.id, "phone")
    cb, nav = _acb(fake_bot)
    await ah.self_gen_pick(cb, AdminSelfCB(action="gen_link"), services)
    _, labels = last_screen(nav)
    assert any("phone" in l for l in labels) and not any("NASPi" in l for l in labels)
    # у профиля админа карточки нет — старая кнопка ведёт на главную
    cb, nav = _acb(fake_bot)
    await ah.admin_gen_for(cb, ClientCB(action="gen_for", client_id=ac.id), services, FakeState())
    text, _ = last_screen(nav)
    assert text.startswith("🛠 "), f"старая «Выдать конфиг» у профиля админа не привела на главную: {text}"


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
