"""E2E: экраны переезда админа — обзор по ссылке «🚚 Переезд» с главной
(/start migration) и переезд одного профиля (/start migration-<id>).

Цена ошибки: в шапке обзора — неизменившиеся параметры, и админ ищет, что
же меняется; переехавшие профили вперемешку с отстающими — отстающих не
видно; имя переехавшего ссылкой ведёт на пустой по смыслу экран; дата
последнего коннекта не того пира (старого вместо двойника) — админ пишет
человеку «ты не переехал», хотя тот давно на новом интерфейсе; «⬅️ Назад»
с экрана профиля выбрасывает на главную вместо обзора.
"""
import datetime

import pytest
from aiogram.filters import CommandObject

from awgbot.bot import texts
from awgbot.bot.callbacks import Menu
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers.admin import panel
from awgbot.core import config
from awgbot.infra import awg, awglock
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
BOT = "awg_test_bot"


def _link(payload, label):
    return f'<a href="https://t.me/{BOT}?start={payload}">{label}</a>'


def _cmd(args):
    return CommandObject(prefix="/", command="start", args=args)


async def _open(services, bot, payload):
    """Экран по ссылке без живого меню — новым сообщением: (команда, текст,
    кнопки [(подпись, колбэк)])."""
    services.bot_username = BOT
    services.ensure_admin_client()
    services.db.set_nav_message_id(ADMIN, None)
    msg = FakeMessage(text=f"/start {payload}", chat_id=ADMIN, user_id=ADMIN, bot=bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd(payload))
    shown = [(t, m) for kind, t, m in msg.sent if kind == "answer"]
    assert shown, f"по ссылке {payload} ничего не пришло"
    text, markup = shown[-1]
    buttons = [(b.text, b.callback_data) for r in markup.inline_keyboard for b in r] if markup else []
    return msg, text, buttons


def _seen(services, device_id, ts):
    services.db.update_device_fields(device_id, last_handshake=ts)


def _params(monkeypatch, *, port=(51820, 51821), generation=(1, 2)):
    """Что меняет переезд: порт старого и нового интерфейса, поколение ядра."""
    monkeypatch.setattr(config, "SERVER_PORT", port[0])
    monkeypatch.setattr(awg, "read_server_params",
                        lambda force=False, iface=None: {"listen_port": port[1]})
    monkeypatch.setattr(awglock, "applied_generation", lambda: generation[0])
    monkeypatch.setattr(awglock, "target_generation", lambda: generation[1])


@pytest.fixture()
def world(services, mig, make_active_client):
    """Три профиля в когорте: у Ксюши переехало 1 из 3, у Пети 0 из 1, Коля
    переехал целиком (2 из 2). Возвращает профили и устройства по именам."""
    now = int(timeutil.now().timestamp())
    ksu = make_active_client("Ксюша", tg_id=9601)
    petya = make_active_client("Петя", tg_id=9602)
    kolya = make_active_client("Коля", tg_id=9603)
    devs = {}
    for c, names in ((ksu, ("iPhone", "MacBook", "iPad")), (petya, ("Android",)),
                     (kolya, ("Pixel", "Ноут"))):
        for n in names:
            devs[n] = services.add_device(c.id, n).device_id
            _seen(services, devs[n], now - 3600)
    services.migration_start()
    twins = services.db.twins_by_origin()
    for n in ("iPhone", "Pixel", "Ноут"):
        _seen(services, twins[devs[n]], now - 60)
    return {"ksu": ksu, "petya": petya, "kolya": kolya, "devs": devs, "twins": twins, "now": now}


# ── обзор ────────────────────────────────────────────────────────────────────

async def test_overview_lists_laggards_first_and_links_only_them(services, fake_bot, world, monkeypatch):
    """Шапка — только изменившиеся параметры; профили по убыванию оставшегося
    (Ксюше осталось 2, Пете 1), переехавший целиком — внизу, 🟢 и без ссылки;
    «⬅️ В меню»; команда /start из чата убрана."""
    _params(monkeypatch)
    msg, text, buttons = await _open(services, fake_bot, "migration")
    ksu, petya = world["ksu"], world["petya"]
    assert text.split("\n\n") == [
        "🚚 Переезд (порт: 51820 → 51821, подсеть: 10.8.1 → 10.9.1, awg: gen1 → gen2)",
        f"🔴 {_link(f'migration-{ksu.id}', 'Ксюша')}: 1/3 устройств",
        f"🔴 {_link(f'migration-{petya.id}', 'Петя')}: 0/1 устройств",
        "🟢 Коля: 2/2 устройств",
    ], text
    assert buttons == [("⬅️ В меню", Menu(action="main").pack())], buttons
    assert msg.deleted, "команда /start migration осталась в чате"


async def test_overview_head_drops_parameters_that_do_not_change(services, fake_bot, world, monkeypatch):
    """Порт тот же и ядро то же — в шапке только подсеть: «порт: 51820 → 51820»
    читается как ошибка настройки."""
    _params(monkeypatch, port=(51820, 51820), generation=(2, 2))
    _, text, _ = await _open(services, fake_bot, "migration")
    assert text.split("\n")[0] == "🚚 Переезд (подсеть: 10.8.1 → 10.9.1)", text


def test_overview_head_without_known_changes_and_without_profiles():
    """Ничего не известно о параметрах — голая шапка без скобок; когорта пуста
    — так и сказано, а не пустой экран."""
    assert texts.migration_overview_text({"rows": []}) == "🚚 Переезд\n\nПереезжать некому"
    d = {"port": ("", ""), "subnet": ("10.8.1", "10.8.1"), "generation": ("", ""), "rows": []}
    assert texts.migration_overview_text(d).startswith("🚚 Переезд\n"), \
        "одинаковые значения попали в шапку"


# ── профиль ──────────────────────────────────────────────────────────────────

async def test_profile_screen_shows_each_device_with_the_last_connect(services, fake_bot, world, monkeypatch):
    """«🚚 Переезд: [Ксюша], 1/3 устройств»; переехавшее — 🟢 с коннектом
    двойника, остальные — 🔴 с коннектом старого пира; «⬅️ Назад» — в обзор."""
    _params(monkeypatch)
    ksu, devs, now = world["ksu"], world["devs"], world["now"]

    def seen(ts):
        return timeutil.fmt_dt_ui(datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc))
    msg, text, buttons = await _open(services, fake_bot, f"migration-{ksu.id}")
    head, body = text.split("\n\n")
    assert head == f"🚚 Переезд: {_link(f'cl-{ksu.id}', 'Ксюша')}, 1/3 устройств", text
    lines = body.split("\n")
    assert sorted(lines) == sorted([
        f"🟢 iPhone — последний коннект: {seen(now - 60)}",
        f"🔴 MacBook — последний коннект: {seen(now - 3600)}",
        f"🔴 iPad — последний коннект: {seen(now - 3600)}",
    ]), text
    assert len(lines) == 3, "двойники задвоили список устройств"
    assert devs and buttons == [("⬅️ Назад", Menu(action="migration").pack())], buttons
    assert msg.deleted


def test_profile_screen_device_without_any_connect_says_so(services, make_active_client):
    """Нет ни одного коннекта — «не подключалось», а не «01.01 03:00» от нуля."""
    c = make_active_client("Ксюша", tg_id=9610)
    dev = services.db.get_device(services.add_device(c.id, "iPad").device_id)
    text = texts.migration_client_text(c, [(dev, False, 0)])
    assert text == "🚚 Переезд: Ксюша, 0/1 устройств\n\n🔴 iPad — последний коннект: не подключалось", text


async def test_back_from_the_profile_leads_to_the_overview(services, fake_bot, world, monkeypatch):
    """«⬅️ Назад» с экрана профиля — обзор на месте того же сообщения."""
    _params(monkeypatch)
    services.bot_username = BOT
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=fake_bot)
    await panel.admin_migration_overview(cb, services)
    shown = [s for s in nav.sent if s[0] in ("edit_text", "answer")]
    assert shown and shown[-1][1].startswith("🚚 Переезд (порт: 51820 → 51821"), shown


async def test_stale_profile_link_falls_back_to_the_home(services, fake_bot, world):
    """Профиль удалён, а ссылка осталась в истории чата — главная, не исключение."""
    _, text, _ = await _open(services, fake_bot, "migration-999999")
    assert text.startswith("🛠 "), text


@pytest.mark.parametrize("payload, link", [
    ("migration", ("migration", 0)), ("migration-12", ("migration_cl", 12)),
    ("migration-", None), ("migration-x", None), ("gwcfg-2", ("gwcfg", 2)), ("gwcfg-", None),
])
def test_parse_link_knows_migration_and_gateway_configuration(payload, link):
    assert panel.parse_link(payload) == link
