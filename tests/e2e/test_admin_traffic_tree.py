"""E2E: экраны трафика админа деревом — «📊 Трафик за ММ.ГГ» (A03) и трафик
профиля (A04), слитые с прежними экранами РФ-доступа; строка РФ в карточках
профиля и устройства; ссылки /start traffic…/traffic_local… на эти экраны;
скрытая команда /uitree.

Цена ошибки: РФ-итог не первой веткой или профили не по убыванию — крупный
потребитель теряется внизу; нулевые строки — шум, за которым не видно
живого; «└─» не у последней ветки — дерево читается как оборванное; старая
ссылка из истории чата (traffic_local…) ведёт в исключение вместо экрана.
"""
import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.filters import CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from awgbot.bot import texts
from awgbot.bot.callbacks import ClientCB, DeviceCB, Menu
from awgbot.bot.handlers import admin as ah
from awgbot.bot.texts import fmt
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
GB = 1024 ** 3
RF = "🇷🇺 РФ-доступ"


def _cmd(args):
    return CommandObject(prefix="/", command="start", args=args)


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


async def _deep(services, bot, payload: str):
    """Экран по ссылке /start <payload> без живого меню — новым сообщением:
    (текст, подписи кнопок, колбэки кнопок)."""
    services.db.set_nav_message_id(ADMIN, None)
    msg = _amsg(bot, f"/start {payload}")
    await ah.admin_start(msg, services, FakeState(), command=_cmd(payload))
    shown = [(t, m) for kind, t, m in msg.sent if kind == "answer"]
    assert shown, f"экран по ссылке {payload} не отрисован"
    text, markup = shown[-1]
    buttons = [b for row in markup.inline_keyboard for b in row] if markup else []
    return text, [b.text for b in buttons], [b.callback_data for b in buttons]


def _rf_feature(monkeypatch, services, fake_routing, enabled: bool) -> None:
    """Функция РФ-доступа развёрнута и включена/выключена — условие строк РФ."""
    monkeypatch.setattr(services, "routing_provisioned", lambda: True)
    fake_routing.enabled = enabled


def _rf_total(services, rx: int, tx: int) -> None:
    services.db.set_state("rf_month_rx", str(rx))
    services.db.set_state("rf_month_tx", str(tx))


def _profile(services, make_active_client, name, tg_id, *, traffic=(0, 0), rf=(0, 0),
             allowed=True, device="Телефон"):
    """Профиль с одним устройством: traffic — (↑, ↓) за месяц, rf — его
    РФ-часть, как их накопил бы опрос."""
    c = make_active_client(name, tg_id=tg_id)
    if allowed:
        services.db.update_client_fields(c.id, routing_allowed=1)
    dc = services.add_device(c.id, device)
    if any(traffic):
        services.db.add_traffic_bulk([(dc.device_id, *traffic)])
    if any(rf):
        services.db.rf_add_bulk([(dc.device_id, *rf)])
    return services.db.get_client(c.id), dc.device_id


# ── знаки дерева ─────────────────────────────────────────────────────────────

def test_tree_marks_the_last_branch_and_continues_under_the_others():
    """«├─» — не последняя ветка, «└─» — последняя; под не последней —
    «│», под последней — пробелы той же ширины. Перепутай — вложенная строка
    РФ читается как ветка соседнего профиля."""
    out = fmt.tree([("A", ["a1"]), ("B", []), ("C", ["c1", "c2"])], style="full")
    assert out.split("\n") == ["├─ A", "│  └─ a1", "├─ B", "└─ C", "   ├─ c1", "   └─ c2"], out


def test_tree_variants_for_the_choice_and_a_single_nested_line_follow_the_style():
    rows = [("A", ["a1"]), ("B", ["b1"])]
    assert fmt.tree(rows, style="bare").split("\n") == ["├ A", "│ └ a1", "└ B", "  └ b1"]
    assert fmt.tree(rows, style="flat") == "A\n└ a1\n\nB\n└ b1", "плоский список 3.1.0 — через пустую строку"
    assert fmt.tree([], style="full") == ""
    assert fmt.sub_line("x", style="full") == "└─ x"
    assert fmt.sub_line("x", style="flat") == "└ x"
    assert fmt.TREE_STYLE == "full", "знак по умолчанию — «├─ / └─ / │»"


# ── A03: трафик по профилям ──────────────────────────────────────────────────

async def test_traffic_tree_leads_with_rf_total_then_profiles_by_size(
        services, fake_bot, fake_routing, make_active_client, monkeypatch):
    """Раскладка A03: шапка и итог сервера, РФ-итог первой веткой, профили по
    убыванию общего трафика с РФ-веткой под своей записью, последняя ветка —
    «└─», под ней вложенная строка отступлена пробелами."""
    _rf_feature(monkeypatch, services, fake_routing, True)
    services.bot_username = "awg_test_bot"
    kolya, _ = _profile(services, make_active_client, "Коля", 7101, traffic=(0, GB), rf=(0, GB))
    ksu, _ = _profile(services, make_active_client, "Ксюша", 7102, traffic=(2 * GB, 10 * GB),
                      rf=(GB, 2 * GB))
    petya, _ = _profile(services, make_active_client, "Петя", 7103, traffic=(GB, 3 * GB))
    _rf_total(services, GB, 3 * GB)                    # ровно сумма устройств — «вне профилей» нет
    text, labels, cbs = await _deep(services, fake_bot, "traffic")

    def link(c):
        return f'<a href="https://t.me/awg_test_bot?start=traffic-{c.id}">{c.name}</a>'
    assert text.split("\n") == [
        f"📊 Трафик за {texts.month_label()}:",
        "17 ГБ (↑3 ↓14)",
        f"├─ {RF}: 4 ГБ (↑1 ↓3)",
        f"├─ 👤 {link(ksu)}: 12 ГБ (↑2 ↓10)",
        f"│  └─ {RF}: 3 ГБ",
        f"├─ 👤 {link(petya)}: 4 ГБ (↑1 ↓3)",
        f"└─ 👤 {link(kolya)}: 1 ГБ (↑0 ↓1)",
        f"   └─ {RF}: 1 ГБ",
    ], text
    assert (labels, cbs) == (["⬅️ В меню"], [Menu(action="main").pack()])


async def test_traffic_tree_drops_zero_profiles_zero_rf_and_zero_outside(
        services, fake_bot, fake_routing, make_active_client, monkeypatch):
    """Нули не выводятся: профиль без трафика, РФ-ветка с нулём у профиля,
    которому РФ разрешён, РФ-итог сервера с нулём и «Вне профилей» с нулём."""
    _rf_feature(monkeypatch, services, fake_routing, True)
    _profile(services, make_active_client, "Молчун", 7111)
    _profile(services, make_active_client, "Ксюша", 7112, traffic=(GB, GB))
    _rf_total(services, 0, 0)
    text, _, _ = await _deep(services, fake_bot, "traffic")
    assert "Молчун" not in text, "профиль без трафика в списке"
    assert RF not in text, f"нулевая РФ-строка выведена:\n{text}"
    assert "Вне профилей" not in text, text
    assert text.split("\n")[2:] == ["└─ 👤 Ксюша: 2 ГБ (↑1 ↓1)"], text


async def test_traffic_tree_without_any_traffic_says_so(services, fake_bot):
    services.ensure_admin_client()
    text, _, _ = await _deep(services, fake_bot, "traffic")
    assert text.split("\n") == [f"📊 Трафик за {texts.month_label()}:", "0 ГБ",
                                "Трафика за месяц ещё нет"], text


async def test_outside_profiles_nests_under_the_rf_total(services, fake_bot, make_active_client):
    """«🧐 Вне профилей» — итог РФ сервера минус устройства — часть РФ-итога:
    веткой под ним, а не отдельной записью среди профилей (иначе его читают
    как ещё один профиль)."""
    _profile(services, make_active_client, "Коля", 7121, traffic=(GB, GB), rf=(GB, 0))
    _rf_total(services, GB, GB)
    text, _, _ = await _deep(services, fake_bot, "traffic")
    assert text.split("\n")[2:] == [
        f"├─ {RF}: 2 ГБ (↑1 ↓1)",
        "│  └─ 🧐 Вне профилей: 1 ГБ — удалённые устройства и первые минуты новых",
        "└─ 👤 Коля: 2 ГБ (↑1 ↓1)",
        f"   └─ {RF}: 1 ГБ",
    ], text


@pytest.mark.parametrize("extra, shown", [
    (0, False),                      # сошлось — строки нет
    (GB // 100 - 1, False),          # меньше 0.01 ГБ — шум округления
    (GB // 100, True),               # ровно 0.01 ГБ — уже видно
])
async def test_outside_profiles_threshold(services, fake_bot, make_active_client, extra, shown):
    _profile(services, make_active_client, "Ксюша", 7122, traffic=(GB, GB), rf=(GB, GB))
    _rf_total(services, GB, GB + extra)
    text, _, _ = await _deep(services, fake_bot, "traffic")
    assert ("🧐 Вне профилей: 0.01 ГБ —" in text) is shown, text


async def test_deleted_device_moves_its_rf_into_outside(services, fake_bot, make_active_client):
    """Устройство удалили в середине месяца: РФ-итог сервера не меняется, его
    часть уходит во «Вне профилей», у профиля РФ уменьшается."""
    c, keep = _profile(services, make_active_client, "Ксюша", 7123, traffic=(GB, GB), rf=(GB, GB))
    gone = services.add_device(c.id, "Ноут")
    services.db.add_traffic_bulk([(gone.device_id, GB, GB)])
    services.db.rf_add_bulk([(gone.device_id, GB, GB)])
    _rf_total(services, 2 * GB, 2 * GB)
    before, _, _ = await _deep(services, fake_bot, "traffic")
    assert "Вне профилей" not in before and f"   └─ {RF}: 4 ГБ" in before, before
    services.remove_device(gone.device_id)
    after, _, _ = await _deep(services, fake_bot, "traffic")
    assert after.split("\n")[2:] == [
        f"├─ {RF}: 4 ГБ (↑2 ↓2)",
        "│  └─ 🧐 Вне профилей: 2 ГБ — удалённые устройства и первые минуты новых",
        "└─ 👤 Ксюша: 2 ГБ (↑1 ↓1)",
        f"   └─ {RF}: 2 ГБ",
    ], after


# ── A04: трафик профиля по устройствам ───────────────────────────────────────

async def test_profile_traffic_tree_sorts_devices_and_drops_zeros(
        services, fake_bot, fake_routing, make_active_client, monkeypatch):
    """A04: шапка с именем профиля, итог профиля (а не сервера), РФ профиля
    первой веткой, устройства по убыванию трафика с РФ-веткой; устройство без
    трафика и нулевая РФ-ветка не выводятся; «⬅️ Назад» — на A03."""
    _rf_feature(monkeypatch, services, fake_routing, True)
    c, mac = _profile(services, make_active_client, "Ксюша", 7131, traffic=(GB, GB), rf=(0, 0),
                      device="MacBook")
    phone = services.add_device(c.id, "iPhone").device_id
    idle = services.add_device(c.id, "Планшет").device_id
    services.db.add_traffic_bulk([(phone, GB, 5 * GB)])
    services.db.rf_add_bulk([(phone, GB, 2 * GB)])
    _profile(services, make_active_client, "Чужой", 7132, traffic=(9 * GB, 9 * GB))
    text, labels, cbs = await _deep(services, fake_bot, f"traffic-{c.id}")
    assert text.split("\n") == [
        f"📊 Трафик за {texts.month_label()}, Ксюша:",
        "8 ГБ (↑2 ↓6)",
        f"├─ {RF}: 3 ГБ (↑1 ↓2)",
        "├─ ⚪ iPhone: 6 ГБ (↑1 ↓5)",
        f"│  └─ {RF}: 3 ГБ",
        "└─ ⚪ MacBook: 2 ГБ (↑1 ↓1)",
    ], text
    assert idle and "Планшет" not in text
    assert (labels, cbs) == (["⬅️ Назад"], [Menu(action="traffic").pack()])

    cb, nav = _acb(fake_bot)
    await ah.admin_traffic_profiles(cb, services)
    back, _ = last_screen(nav)
    assert back.startswith(f"📊 Трафик за {texts.month_label()}:\n"), back


@pytest.fixture()
def rf_gateway(services, make_active_client, monkeypatch):
    """Профиль админа: телефон и устройство-шлюз, у обоих трафик и РФ-счётчики
    (у шлюза — как если бы байты на него всё-таки легли)."""
    monkeypatch.setattr(services, "gateway_ping", lambda slot: None)
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: "down")
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    phone = services.add_device(admin.id, "phone")
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.add_traffic_bulk([(phone.device_id, GB, GB), (pi.device_id, 3 * GB, 3 * GB)])
    services.db.rf_add_bulk([(phone.device_id, GB, GB), (pi.device_id, 5 * GB, 5 * GB)])
    return admin, phone.device_id, pi.device_id


async def test_gateway_gets_no_rf_branch_and_stays_out_of_the_profile_rf(
        services, fake_bot, rf_gateway):
    """Шлюз — не потребитель: РФ-ветки под ним нет, в РФ профиля он не входит
    (2 ГБ телефона, а не 12 со шлюзом)."""
    admin, _, _ = rf_gateway
    text, _, _ = await _deep(services, fake_bot, f"traffic-{admin.id}")
    lines = text.split("\n")
    assert lines[2] == f"├─ {RF}: 2 ГБ (↑1 ↓1)", text
    i = lines.index("├─ 🛰 NASPi: 6 ГБ (↑3 ↓3)")
    assert RF not in lines[i + 1], f"РФ-ветка под шлюзом:\n{text}"
    assert lines[-2:] == ["└─ ⚪ phone: 2 ГБ (↑1 ↓1)", f"   └─ {RF}: 2 ГБ"], text


# ── ссылки на экраны трафика ─────────────────────────────────────────────────

@pytest.mark.parametrize("payload", ["traffic", "traffic_local"])
async def test_both_home_links_open_the_traffic_tree_and_remove_the_command(
        services, fake_bot, make_active_client, payload):
    """«📊 Трафик» и «🇷🇺 РФ-доступ» главной, а также старые ссылки на экран РФ
    из истории чата ведут на один экран A03; команда /start убирается."""
    _profile(services, make_active_client, "Ксюша", 7141, traffic=(GB, GB))
    services.db.set_nav_message_id(ADMIN, 777)
    msg = _amsg(fake_bot, f"/start {payload}")
    await ah.admin_start(msg, services, FakeState(), command=_cmd(payload))
    assert msg.deleted, "команда /start осталась в чате"
    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits and edits[-1][2].startswith(f"📊 Трафик за {texts.month_label()}:\n"), edits
    assert not any(kind == "answer" for kind, _, _ in msg.sent), "второе меню вместо правки живого"


@pytest.mark.parametrize("fmt_", ["traffic-{id}", "traffic_local-{id}", "traffic_local-{id}-t",
                                  "traffic-{id}-t"])
async def test_profile_links_open_the_profile_traffic(services, fake_bot, make_active_client, fmt_):
    c, _ = _profile(services, make_active_client, "Ксюша", 7142, traffic=(GB, GB))
    text, labels, _ = await _deep(services, fake_bot, fmt_.format(id=c.id))
    assert text.startswith(f"📊 Трафик за {texts.month_label()}, Ксюша:\n"), text
    assert labels == ["⬅️ Назад"]


@pytest.mark.parametrize("payload", ["traffic_local-abc-t", "traffic_local--t", "traffic_local-t",
                                     "traffic_local-abc", "traffic-999999", "traffic_local-999999-t"])
async def test_broken_or_stale_traffic_links_fall_back_to_the_home(services, fake_bot, payload):
    """Суффикс без числа или удалённый профиль — главная, а не исключение
    разбора и не пустой экран."""
    text, labels, _ = await _deep(services, fake_bot, payload)
    assert text.startswith("🛠 "), text
    assert "👥 Профили" in labels, labels


async def test_old_rf_back_button_opens_the_traffic_tree(services, fake_bot):
    """Кнопка «Назад» прежнего экрана РФ профиля (Menu traffic_local) из
    истории чата открывает дерево трафика, а не молчит."""
    services.ensure_admin_client()
    cb, nav = _acb(fake_bot)
    cb.data = Menu(action="traffic_local").pack()
    await ah.admin_traffic_profiles(cb, services)
    text, _ = last_screen(nav)
    assert text.startswith(f"📊 Трафик за {texts.month_label()}:"), text


# ── строка РФ в карточках — прежнее правило ─────────────────────────────────

_PFX = f"└─ {RF}: "


def _rf_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith(_PFX)]


async def _client_card(services, bot, client_id: int) -> str:
    cb, nav = _acb(bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=client_id), services, FakeState())
    return last_screen(nav)[0]


async def _device_card(services, bot, device_id: int) -> str:
    cb, nav = _acb(bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=device_id), services, FakeState())
    return last_screen(nav)[0]


@pytest.mark.parametrize("enabled, allowed, rf, card, branch", [
    (True, True, (0, 0), "0 ГБ", None),        # разрешён и 0: карточка — строка, дерево — ноль не выводит
    (False, True, (0, 0), None, None),         # функция выключена
    (True, False, (0, 0), None, None),         # не разрешён и 0
    (True, False, (GB, 3 * GB), "4 ГБ", "4 ГБ"),   # не разрешён, но было
    (False, True, (GB, 3 * GB), "4 ГБ", "4 ГБ"),   # выключили, байты остались
])
async def test_profile_rf_line_in_the_card_and_the_tree(
        services, fake_bot, fake_routing, make_active_client, monkeypatch,
        enabled, allowed, rf, card, branch):
    """Карточка профиля: «└─ 🇷🇺 РФ-доступ» сразу под строкой трафика — при
    прежних условиях (разрешён и функция включена, или за месяц было). «0 ГБ»
    при включённой функции — ровно та подсказка, что маркировка не работает.
    В дереве трафика нулевой РФ-ветки нет."""
    _rf_feature(monkeypatch, services, fake_routing, enabled)
    c, _ = _profile(services, make_active_client, "Ксюша", 7151, traffic=(GB, GB), rf=rf,
                    allowed=allowed)
    text = await _client_card(services, fake_bot, c.id)
    if card is None:
        assert _rf_lines(text) == [], text
    else:
        lines = text.splitlines()
        assert _rf_lines(text) == [_PFX + card], text
        head = next(i for i, ln in enumerate(lines) if ln.startswith("📊 "))
        assert lines[head + 1] == _PFX + card, "строка РФ не сразу под трафиком профиля"
    tree_text, _, _ = await _deep(services, fake_bot, "traffic")
    subs = [ln for ln in tree_text.splitlines() if ln.endswith(f"{RF}: {branch}") and "└─" in ln]
    if branch is None:
        assert not [ln for ln in tree_text.splitlines() if ln.startswith("   └─ ")], tree_text
    else:
        assert subs, tree_text


@pytest.mark.parametrize("enabled, allowed, rf, card", [
    (True, True, (0, 0), "0 ГБ"),
    (False, True, (0, 0), None),
    (True, False, (0, 0), None),
    (True, False, (GB, GB), "2 ГБ"),
    (False, True, (GB, GB), "2 ГБ"),
])
async def test_device_rf_line_in_the_card(services, fake_bot, fake_routing, make_active_client,
                                          monkeypatch, enabled, allowed, rf, card):
    """Карточка устройства: «└─ 🇷🇺 РФ-доступ» под строкой «Был в сети … ·
    трафик» — по тому же правилу от владельца."""
    _rf_feature(monkeypatch, services, fake_routing, enabled)
    _, did = _profile(services, make_active_client, "Ксюша", 7152, rf=rf, allowed=allowed)
    text = await _device_card(services, fake_bot, did)
    if card is None:
        assert _rf_lines(text) == [], text
    else:
        assert _rf_lines(text) == [_PFX + card], text
        assert text.splitlines()[2] == _PFX + card, "строка РФ не сразу под строкой трафика"


async def test_rf_lines_stay_in_cards_when_the_routing_self_check_fails(
        services, fake_bot, fake_routing, make_active_client, monkeypatch):
    """Самопроверка обвязки отрицательная, функция развёрнута и включена:
    «0 ГБ» у профиля и устройства остаются — как строка на главной."""
    from awgbot.infra import routing as infra_routing
    _rf_feature(monkeypatch, services, fake_routing, True)
    monkeypatch.setattr(infra_routing, "available", lambda: False)
    c, did = _profile(services, make_active_client, "Ксюша", 7153)
    assert _rf_lines(await _client_card(services, fake_bot, c.id)) == [_PFX + "0 ГБ"], "строка профиля пропала"
    assert _rf_lines(await _device_card(services, fake_bot, did)) == [_PFX + "0 ГБ"], "строка устройства пропала"


async def test_no_rf_lines_on_a_server_without_the_feature(services, fake_bot, fake_routing):
    """Админу РФ-доступ разрешён всегда; без функции на сервере вечное «0 ГБ»
    в его карточке и в дереве — шум про то, чего нет."""
    fake_routing.enabled = False
    services.ensure_admin_client()
    admin = services.admin_client()
    dev = services.add_device(admin.id, "phone")
    services.db.add_traffic_bulk([(dev.device_id, GB, GB)])
    assert _rf_lines(await _client_card(services, fake_bot, admin.id)) == []
    assert RF not in (await _deep(services, fake_bot, "traffic"))[0]
    assert RF not in (await _deep(services, fake_bot, f"traffic-{admin.id}"))[0]


# ── /uitree ──────────────────────────────────────────────────────────────────

async def test_uitree_sends_two_messages_with_three_variants_and_restores_the_style(
        services, fake_bot, make_active_client):
    """Тестовая команда выбора знаков: два сообщения (общий трафик и первый
    профиль), в каждом по три варианта; после неё знаки экранов — прежние,
    иначе все деревья бота остались бы в последнем, «плоском» варианте."""
    fmt.TREE_STYLE = "full"
    _profile(services, make_active_client, "Ксюша", 7161, traffic=(GB, GB), rf=(GB, 0))
    msg = _amsg(fake_bot, "/uitree")
    await ah.uitree_probe(msg, services)
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert len(sent) == 2, sent
    for text in sent:
        for title in ("1) ├─ └─ │", "2) ├ └ │", "3) плоский список"):
            assert f"<b>{title}</b>" in text, text
    assert sent[0].count("📊 Трафик за") == 3 and sent[1].count(", Ксюша:") == 3
    assert fmt.TREE_STYLE == "full", "команда оставила чужой вариант знаков"


class _Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return Message(message_id=len(self.calls) + 100, date=datetime.datetime.now(),
                       chat=Chat(id=ADMIN, type="private"), text="ok")

    async def stream_content(self, *a, **k):              # pragma: no cover
        raise AssertionError("скачиваний нет")
        yield b""

    async def close(self):
        pass


@pytest.mark.parametrize("role, replies", [("admin", 2), ("client", 0), ("invited", 0)])
async def test_uitree_answers_only_the_admin(services, make_active_client, monkeypatch, role, replies):
    """Команда скрытая и только для админа: клиенту или гостю — ни слова (для
    них это чужой трафик — утечка)."""
    _profile(services, make_active_client, "Ксюша", 7162, traffic=(GB, GB))
    monkeypatch.setattr(ah.router, "_parent_router", None)
    dp = Dispatcher(storage=MemoryStorage())
    dp["services"] = services
    dp.include_router(ah.router)
    session = _Session()
    bot = Bot("42:DUMMY", session=session)
    msg = Message(message_id=7, date=datetime.datetime.now(), chat=Chat(id=ADMIN, type="private"),
                  text="/uitree", **{"from": User(id=ADMIN, is_bot=False, first_name="A")})
    await dp.feed_update(bot, Update(update_id=1, message=msg), role=role)
    sends = [m for m in session.calls if type(m).__name__ == "SendMessage"]
    assert len(sends) == replies, [type(m).__name__ for m in session.calls]
