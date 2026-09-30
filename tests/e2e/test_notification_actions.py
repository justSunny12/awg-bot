"""E2E: кнопки действий и ссылки в уведомлениях.

Notification.action — подсказка представления: notifier ставит кнопку
действия над «Скрыть»; нажатие (NoteCB) снимает кнопки с уведомления и
открывает экран новым живым меню. Имена в текстах уведомлений — ссылки:
профиль у админа — cl-<id>, шлюз — gw-<слот>, устройство у владельца —
dev-<id>; где действие — просто открыть объект, кнопки нет.

Цена ошибки: без кнопки «⏱ Продлить» админ из «подписка истекает» идёт
искать профиль в списке; кнопка на «шлюз лёг» или «лимит устройства» — лишний
шум там, где хватает ссылки; уведомление с живыми кнопками после нажатия —
второе меню в чате.
"""
import datetime
from types import SimpleNamespace

import pytest

from awgbot.bot import keyboards as kb
from awgbot.bot import notifier
from awgbot.bot.callbacks import GraceCB, HideCB, NoteCB
from awgbot.bot.handlers import hide as hide_h
from awgbot.bot.handlers import reply_commands as rc
from awgbot.core import config, settings
from awgbot.domain.services import BYTES_PER_GB, Notification
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
BOT = "awg_test_bot"


def _link(payload, label):
    return f'<a href="https://t.me/{BOT}?start={payload}">{label}</a>'


def _rows(markup):
    return [[(b.text, b.callback_data) for b in r] for r in markup.inline_keyboard]


class RecordingBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.calls.append((chat_id, text, reply_markup))
        return FakeMessage(text=text, chat_id=chat_id)


# ── notifier: подсказка → кнопка ─────────────────────────────────────────────

@pytest.mark.parametrize("action, label", [
    (("extend", 7), "⏱ Продлить"), (("gwcfg", 2), "📤 Конфигурация"),
    (("unassigned", 0), "📦 Без профиля"), (("sub", 0), "💳 Подписка")])
def test_action_becomes_a_button_above_hide(action, label):
    rows = _rows(notifier.action_markup(action))
    assert rows == [[(label, NoteCB(kind=action[0], ref=action[1]).pack())],
                    [("Скрыть", HideCB().pack())]], rows


@pytest.mark.parametrize("action", [(), ("unknown", 1), None])
def test_no_or_unknown_action_leaves_only_hide(action):
    assert _rows(notifier.action_markup(action)) == [[("Скрыть", HideCB().pack())]]


async def test_send_notifications_uses_the_action_and_keeps_own_markup_first():
    """Своя клавиатура уведомления идёт первой, кнопка подсказки — под ней,
    «Скрыть» одна и последней; без обеих — «Скрыть»."""
    bot = RecordingBot()
    own = kb.grace_offer(5, 14)
    await notifier.send_notifications(bot, [
        Notification(111, "a", action=("extend", 3)),
        Notification(222, "b", reply_markup=own, action=("sub", 0)),
        Notification(333, "c")])
    by = {c[0]: c[2] for c in bot.calls}
    assert _rows(by[111])[0] == [("⏱ Продлить", NoteCB(kind="extend", ref=3).pack())]
    assert _rows(by[222]) == [[("Продли чуток? 🙏 (+14 дн.)", GraceCB(action="take", ref=5).pack())],
                              [("💳 Подписка", NoteCB(kind="sub", ref=0).pack())],
                              [("Скрыть", HideCB().pack())]], _rows(by[222])
    assert _rows(by[333]) == [[("Скрыть", HideCB().pack())]]


# ── NoteCB: кнопки сняты, экран — новым меню ─────────────────────────────────

async def _press(services, fake_bot, kind, ref, *, role="admin", client=None, chat=ADMIN):
    note = FakeMessage(text="уведомление", chat_id=chat, user_id=chat, bot=fake_bot)
    cb = FakeCallback(message=note, user_id=chat, bot=fake_bot)
    await rc.on_note_action(cb, NoteCB(kind=kind, ref=ref), FakeState(), services, role=role, client=client)
    return cb, note


async def test_extend_button_strips_the_note_and_opens_extension_as_new_menu(
        services, fake_bot, make_active_client):
    c = make_active_client("Ксюша", tg_id=5101)
    services.db.nav_touch(ADMIN, 4242)
    cb, note = await _press(services, fake_bot, "extend", c.id)
    assert ("edit_reply_markup", ADMIN) in fake_bot.records, "кнопки с уведомления не сняты"
    assert not [s for s in note.sent if s[0] == "edit_text"], "текст уведомления переписан — история потеряна"
    shown = [(t, m) for kind, t, m in note.sent if kind == "answer"]
    assert len(shown) == 1 and shown[0][0].startswith("⏱ <b>Продление:</b> Ксюша\n"), shown
    assert ("edit_markup", ADMIN, 4242) in fake_bot.records, "прежнее живое меню не погашено"
    assert services.db.get_nav_message_id(ADMIN) not in (None, 4242, note.message_id)
    assert cb.answers


async def test_unassigned_button_opens_the_quarantine_list(services, fake_bot):
    svc = services.db.get_service_client_id()
    services.db.create_device(svc, "чужой", "PUBQ", "PSK", "10.8.0.77")
    _, note = await _press(services, fake_bot, "unassigned", 0)
    shown = [t for kind, t, _ in note.sent if kind == "answer"]
    assert shown == ["📦 <b>Без профиля:</b> 1 — пир создан мимо бота"], shown


async def test_extend_for_a_deleted_profile_falls_back_to_home(services, fake_bot):
    services.ensure_admin_client()
    _, note = await _press(services, fake_bot, "extend", 999999)
    shown = [t for kind, t, _ in note.sent if kind == "answer"]
    assert shown and shown[-1].startswith("🛠 "), shown


async def test_client_sub_button_opens_the_subscription(services, fake_bot, make_active_client):
    cl = make_active_client("Вася", tg_id=5102, period_kind="year")
    _, note = await _press(services, fake_bot, "sub", 0, role="client", client=cl, chat=5102)
    shown = [t for kind, t, _ in note.sent if kind == "answer"]
    assert shown and shown[-1].startswith("💳 <b>Подписка:</b> годовая"), shown


async def test_admin_only_hints_never_open_admin_screens_for_a_client(services, fake_bot, make_active_client):
    """Подсказка админа, дошедшая до клиента (старое сообщение, ошибка
    адресата), открывает главную клиента — не экран продления чужого профиля."""
    other = make_active_client("Чужой", tg_id=5103)
    cl = make_active_client("Вася", tg_id=5104)
    for kind, ref in (("extend", other.id), ("unassigned", 0)):
        _, note = await _press(services, fake_bot, kind, ref, role="client", client=cl, chat=5104)
        shown = [t for k, t, _ in note.sent if k == "answer"]
        assert shown and shown[-1].startswith("👋 "), (kind, shown)


async def test_gwcfg_button_issues_the_slot_configuration(services, fake_bot, monkeypatch):
    """«📤 Конфигурация» под «конфигурация шлюза неактуальна» — файл слота
    сразу, как из карточки."""
    from awgbot.bot.handlers import settings as sh
    issued = []

    async def fake_bundle(message, services_, slot=0, instr_id=None):
        issued.append((message.chat.id, slot))
        return True
    monkeypatch.setattr(sh, "send_gw_bundle", fake_bundle)
    cb, _ = await _press(services, fake_bot, "gwcfg", 2)
    assert issued == [(ADMIN, 2)]
    assert ("edit_reply_markup", ADMIN) in fake_bot.records


async def test_hide_still_deletes_the_note(services, fake_bot):
    note = FakeMessage(text="x", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    cb = FakeCallback(message=note, user_id=ADMIN, bot=fake_bot)
    await hide_h.on_hide(cb)
    assert note.deleted and cb.answers


# ── ссылки и подсказки в уведомлениях домена ─────────────────────────────────

def _period(services, client, start, end, kind):
    services.db.update_client_fields(client.id, period_start=timeutil.to_iso(start),
                                     period_end=timeutil.to_iso(end), period_kind=kind)


def test_expiring_and_expired_notes_link_the_profile_and_offer_extend(services, make_active_client):
    """Админу «⏳ [Имя]: подписка истекает…» и «🔴 [Имя]: подписка истекла» —
    имя ссылкой cl-<id>, кнопка «⏱ Продлить»; клиенту — «💳 Подписка». По
    одному разу на порог: второй проход молчит."""
    services.bot_username = BOT
    now = timeutil.now()
    soon = make_active_client("Скоро", tg_id=5201)
    _period(services, soon, now - datetime.timedelta(days=25), now + datetime.timedelta(hours=20), "month")
    gone = make_active_client("Истёк", tg_id=5202)
    _period(services, gone, now - datetime.timedelta(days=31), now - datetime.timedelta(minutes=5), "month")
    notes = services.check_expiry()
    admin = {n.action: n for n in notes if n.tg_id == ADMIN}
    exp = admin[("extend", soon.id)]
    assert exp.text.startswith(f"⏳ {_link(f'cl-{soon.id}', 'Скоро')}: подписка истекает через "), exp.text
    dead = admin[("extend", gone.id)]
    assert dead.text == f"🔴 {_link(f'cl-{gone.id}', 'Истёк')}: подписка истекла, доступ приостановлен", dead.text
    client = [n for n in notes if n.tg_id == 5201]
    assert len(client) == 1 and client[0].text.startswith("⏳ Подписка истекает через ")
    assert client[0].action == ("sub", 0)
    assert [n.action for n in notes if n.tg_id == 5202] == [("sub", 0)]
    assert [n for n in services.check_expiry() if n.tg_id in (ADMIN, 5201, 5202)] == [], "повтор на тот же порог"


def test_without_bot_name_profile_names_stay_plain_text(services, make_active_client):
    services.bot_username = ""
    now = timeutil.now()
    c = make_active_client("<Скоро>", tg_id=5203)
    _period(services, c, now - datetime.timedelta(days=25), now + datetime.timedelta(hours=20), "month")
    admin = [n for n in services.check_expiry() if n.tg_id == ADMIN]
    assert admin and admin[0].text.startswith("⏳ &lt;Скоро&gt;: подписка истекает"), admin[0].text


async def test_year_client_expiring_note_keeps_subscription_button_next_to_grace(
        services, make_active_client):
    """Годовому клиенту «подписка истекает» — «💳 Подписка» и «Продли чуток?»
    вместе: отсрочка не отменяет входа в подписку."""
    from awgbot.runtime.scheduler import setup_scheduler
    now = timeutil.now()
    c = make_active_client("Год", tg_id=5204, period_kind="year")
    _period(services, c, now - datetime.timedelta(days=364), now + datetime.timedelta(hours=20), "year")
    bot = RecordingBot()
    sched = setup_scheduler(services, bot, services.db)
    await sched.get_job("expiry").func()
    mine = [m for chat, _, m in bot.calls if chat == 5204]
    assert len(mine) == 1
    labels = [b.text for r in mine[0].inline_keyboard for b in r]
    assert any(l.startswith("Продли чуток?") for l in labels), labels
    assert "💳 Подписка" in labels, f"годовому клиенту пропала кнопка подписки: {labels}"


def test_device_limit_notes_link_the_device_without_a_button(services, make_active_client):
    """Клиенту «⚠️ Устройство [iPhone]: израсходовано ~80%» и «🔴 … исчерпан»
    — имя ссылкой dev-<id> на его карточку, кнопки действия нет; каждое — один
    раз на месяц."""
    services.bot_username = BOT
    c = make_active_client("Вася", tg_id=5301)
    d = services.add_device(c.id, "iPhone")
    services.set_device_traffic_limit(d.device_id, 10 * BYTES_PER_GB)
    warn = settings.get_int("limits.traffic_warn_percent", 80)
    services.db.add_traffic_bulk([(d.device_id, (warn + 1) * BYTES_PER_GB // 10, 0)])
    notes = [n for n in services.check_traffic_limits() if n.tg_id == 5301]
    assert [n.text for n in notes] == [
        f"⚠️ Устройство {_link(f'dev-{d.device_id}', '«iPhone»')}: израсходовано ~{warn}% месячного лимита"]
    assert notes[0].action == ()
    assert [n for n in services.check_traffic_limits() if n.tg_id == 5301] == [], "повтор 80%"
    services.db.add_traffic_bulk([(d.device_id, 5 * BYTES_PER_GB, 0)])
    over = [n for n in services.check_traffic_limits() if n.tg_id == 5301]
    assert len(over) == 1 and over[0].text.startswith(
        f"🔴 Устройство {_link(f'dev-{d.device_id}', '«iPhone»')}: месячный лимит исчерпан."), over
    assert over[0].action == ()


def test_unknown_peer_alarm_offers_the_quarantine(services, fake_awg, monkeypatch):
    from awgbot.infra import awg
    services.ensure_admin_client()
    conf = ("[Interface]\nPrivateKey = x\n\n[Peer]\nPublicKey = strangerPUB\n"
            "AllowedIPs = 10.8.0.77/32\n")
    monkeypatch.setattr(awg, "read_file", lambda path: conf, raising=False)
    monkeypatch.setattr(awg, "read_clients_table", lambda: [], raising=False)
    notes = [n for n in services.reconcile_peers() if n.tg_id == ADMIN]
    assert len(notes) == 1 and notes[0].text.startswith("🚨 Чужой пир в конфиге сервера: <code>10.8.0.77</code>"), notes
    assert notes[0].action == ("unassigned", 0) and notes[0].force_sound


def _gw(services, slot, name):
    services.ensure_admin_client()
    dev = services.add_device(services.admin_client().id, name)
    services.db.gateway_add(dev.device_id, f"awglink{slot}", 440 + slot, f"10.99.9{slot}.0/30", slot_id=slot)
    return services.db.gateway(slot)


def test_gateway_notes_link_the_slot_card_and_have_no_button(services):
    """«Шлюз лёг», «переключение», «резерв лёг» — имя шлюза ссылкой gw-<слот>,
    без кнопки: действие — открыть карточку. Без имени бота — просто имя."""
    services.bot_username = BOT
    a, b = _gw(services, 1, "NASPi"), _gw(services, 2, "Pi4")
    down = services._txt_rt_gw_down(a)
    assert down.startswith(f"🔴 Шлюз {_link('gw-1', '«NASPi»')} не отвечает."), down
    switched = services._txt_rt_switched(a, b, "down")
    assert switched.startswith(f"🔁 Шлюз переключён: {_link('gw-1', '«NASPi»')} не отвечает, "
                               f"трафик идёт через {_link('gw-2', '«Pi4»')}."), switched
    assert services._txt_rt_standby_down(b, a, 4).startswith(
        f"⚠️ Резервный шлюз {_link('gw-2', '«Pi4»')} не отвечает уже "), "резерв лёг — без ссылки"
    services.bot_username = ""
    assert services._txt_rt_gw_down(a).startswith("🔴 Шлюз «NASPi» не отвечает.")


def test_link_port_note_links_the_slot_and_offers_the_configuration(services, monkeypatch, tmp_path):
    services.bot_username = BOT
    g = _gw(services, 1, "NASPi")
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink1")
    monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
    monkeypatch.setattr(services, "_gw_firewall_refresh", lambda: None)
    (tmp_path / "awglink1.conf").write_text("[Interface]\nAddress = 10.99.91.1/30\nListenPort = 5555\n")
    notes = services.gateway_sync_link_ports()
    assert len(notes) == 1, notes
    assert notes[0].text.startswith(f"🛰 {_link('gw-1', '«NASPi»')}: порт линка изменён на 5555 (был {g.link_port})")
    # «Перевыпусти» — ссылка, по которой бот сразу отдаёт файл слота
    assert f"{_link('gwcfg-1', 'Перевыпусти')} конфигурацию шлюза" in notes[0].text, \
        notes[0].text
    assert notes[0].action == ("gwcfg", 1)


def test_switch_note_links_the_card_where_traffic_can_be_moved_back(services):
    """«вернуть трафик обратно можно в карточке шлюза» — «в карточке шлюза»
    ссылкой gw-<слот> нового активного: иначе админ ищет, где это делается."""
    services.bot_username = BOT
    a, b = _gw(services, 1, "NASPi"), _gw(services, 2, "Pi4")
    text = services._txt_rt_switched(a, b, "down")
    assert f"вернуть трафик обратно можно {_link('gw-2', 'в карточке шлюза')}." in text, text
    services.bot_username = ""
    assert "обратно можно в карточке шлюза." in services._txt_rt_switched(a, b, "down")


async def test_gwcfg_link_removes_the_command_and_issues_the_slot_configuration(
        services, fake_bot, monkeypatch):
    """Переход по «Перевыпусти» (/start gwcfg-<слот>): команда из чата убрана,
    файл конфигурации слота отдан сразу — без захода в карточку."""
    from aiogram.filters import CommandObject
    from awgbot.bot.handlers import admin as ah
    from awgbot.bot.handlers import settings as sh
    issued = []

    async def fake_bundle(message, services_, slot=0, instr_id=None):
        issued.append((message.chat.id, slot))
        return True
    monkeypatch.setattr(sh, "send_gw_bundle", fake_bundle)
    services.ensure_admin_client()
    msg = FakeMessage(text="/start gwcfg-2", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(),
                         command=CommandObject(prefix="/", command="start", args="gwcfg-2"))
    assert issued == [(ADMIN, 2)], issued
    assert msg.deleted, "команда /start gwcfg-2 осталась в чате"
    assert not [t for k, t, _ in msg.sent if k == "answer"], "вместо файла — ещё и главная"


def test_admin_device_over_limit_is_a_notice_only_once(services, make_active_client):
    """Устройство админа сверх своего лимита не блокируется — админ отрезал
    бы себе VPN, а с ним и бота; уведомление справочное и одно на месяц."""
    from awgbot.core.blocks import DeviceBlock
    services.bot_username = BOT
    admin = make_active_client("Админ", tg_id=ADMIN, device_limit=0)
    d = services.add_device(admin.id, "iPhone")
    services.set_device_traffic_limit(d.device_id, 1 * BYTES_PER_GB)
    services.db.add_traffic_bulk([(d.device_id, 2 * BYTES_PER_GB, 0)])
    notes = [n for n in services.check_traffic_limits() if n.tg_id == ADMIN]
    assert [n.text for n in notes] == [
        f"🔴 Устройство {_link(f'dev-{d.device_id}', '«iPhone»')}: месячный лимит исчерпан "
        "(уведомление, доступ не тронут)"], notes
    dev = services.db.get_device(d.device_id)
    assert not int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER), "устройство админа заблокировано"
    assert [n for n in services.check_traffic_limits() if n.tg_id == ADMIN] == [], "повтор уведомления"


# ── периодическая проверка обновлений ────────────────────────────────────────

async def test_update_check_job_records_the_tag_with_notifications_muted_and_asks_once(
        services, monkeypatch):
    """Задача проверки обновлений записывает найденную версию для шапки и при
    выключенных уведомлениях; за один проход — один запрос к списку
    релизов (строго периодические запросы и так выглядят маячком)."""
    from awgbot.runtime.scheduler import setup_scheduler
    asked = []

    def update_next():
        asked.append(1)
        return SimpleNamespace(tag="v3.2.0", body="")
    monkeypatch.setattr(services, "update_next", update_next)
    services.mute_updates()
    bot = RecordingBot()
    job = setup_scheduler(services, bot, services.db).get_job("update_check_startup").func
    await job()
    assert services.update_available_tag() == "v3.2.0"
    assert bot.calls == [], "уведомления выключены, а уведомление ушло"
    services.unmute_updates()
    asked.clear()
    monkeypatch.setattr("awgbot.runtime.scheduler.notify_update_available",
                        lambda *a, **k: _noop())
    await job()
    assert len(asked) == 1, f"за один проход проверки — {len(asked)} запроса к списку релизов"


async def _noop():
    return None
