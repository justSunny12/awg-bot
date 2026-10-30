"""РФ-доступ через второй сервер на экранах: карточка «🛟 Резерв» обеих сторон
(Т-РТ1, Т-РТ2, Т-РТ2б, Т-РТ3, «▸ подробнее» Т-РТ13), тумблер «Мои шлюзы для
резерва» в эпизоде — диалог Т-РТ4 («Отмена» первой, «Выключить» снимает
согласие тем же нажатием), диалог удаления пары с Т-РТ5 у обеих сторон, экран
роли админа второго сервера (Т-РТ12).

Сервер — «Alpha» с двумя слотами и парой с «Beta» (tests/integration/_rfpeer_common.py),
хендлеры зовутся напрямую.

Цена ошибки: тумблер в эпизоде без диалога — админ одним нажатием оставляет
пользователей второго сервера без РФ-доступа, не зная об этом; «Выключить» без
такта — правило 151 и таблица открыты до следующего такта, вторая сторона не
знает об отзыве; карточка молчит об эпизоде — админ не понимает, почему
исходящий адрес у пользователей сменился; Т-РТ2 без причины — нечего чинить;
диалог удаления без Т-РТ5 — админ рвёт идущий транзит вслепую.
"""
from __future__ import annotations

import pytest

from awgbot.bot.handlers.peeradmin import _screen as peer_screen
from awgbot.bot.handlers.settings.reserve import card as card_h
from awgbot.bot.texts.reserve import section as st_t
from awgbot.core import config, settings
from awgbot.util import timeutil
from awgbot.util.textfmt.base import human_bytes
from tests.conftest import FakeCallback, FakeMessage, last_screen
from tests.integration import _rfpeer_common as rp

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID
rf = rp.rf
MB = 1 << 20


@pytest.fixture()
def ui(rf, monkeypatch):
    from awgbot.util import dnsapi
    monkeypatch.setattr(dnsapi, "read_system", lambda name, timeout=3.0: [])   # карточка не ходит в сеть за адресом
    return rf


def _nav(fake_bot) -> FakeMessage:
    return FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)


def _cb(nav, fake_bot) -> FakeCallback:
    return FakeCallback(message=nav, user_id=ADMIN, bot=fake_bot)


def _peer_episode(s, *, up=0, dn=0) -> str:
    """«Beta» ходит через мои шлюзы: согласие дано, `st.rfp.use` второй стороны пришёл."""
    rp.settle(s)
    s.k.up, s.k.dn = up, dn
    since = timeutil.now_iso()
    rp.peer_rfp(s, offer=False, why="peer", use=since)
    rp.tick(s)
    return since


def _at(iso: str) -> str:
    return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))


# ── «Beta» глазами транзитного: карточка и тумблер ──────────────────────────

async def test_the_card_shows_the_peer_using_my_gateways_with_the_volume(ui):
    since = _peer_episode(ui, up=MB, dn=MB)
    ui.k.up, ui.k.dn = MB + 120 * MB, MB + 1434 * MB
    text, _ = await card_h.reserve_screen(ui)
    want = (f"└ 🇷🇺 РФ-доступ пользователей «Beta» идёт через мои шлюзы с {_at(since)} · "
            f"(↑ {human_bytes(120 * MB)} | ↓ {human_bytes(1434 * MB)})")   # вид как на главной
    assert want in text.splitlines(), text
    lines = text.splitlines()
    assert lines.index(want) > lines.index("📥 Пользователи «Beta»: на его сервере"), "Т-РТ3 не в блоке «📥»"


async def test_no_episode_no_line_in_the_card(ui):
    rp.settle(ui)
    text, _ = await card_h.reserve_screen(ui)
    assert "идёт через мои шлюзы" not in text


async def test_the_switch_outside_an_episode_turns_off_at_once(ui, fake_bot):
    rp.settle(ui)
    nav = _nav(fake_bot)
    cb = _cb(nav, fake_bot)
    await card_h.rf_mine_toggle(cb, ui)
    assert settings.get_bool("app.reserve.rf_via_mine", True) is False
    assert cb.answers[-1][0] == "Мои шлюзы для резерва: выкл", cb.answers
    assert "Выключить «Мои шлюзы для резерва»?" not in last_screen(nav)[0]


async def test_the_switch_in_an_episode_asks_first_with_cancel_first(ui, fake_bot):
    since = _peer_episode(ui)
    nav = _nav(fake_bot)
    await card_h.rf_mine_toggle(_cb(nav, fake_bot), ui)
    text, labels = last_screen(nav)
    assert text == ("🇷🇺 <b>Выключить «Мои шлюзы для резерва»?</b>\n\n"
                    f"Сейчас через шлюзы этого сервера идёт РФ-доступ пользователей «Beta» — с {_at(since)}, "
                    "шлюзы «Beta» не отвечают. После выключения российские сервисы у них откроются с зарубежного "
                    "адреса и могут ругаться"), text
    assert labels[0].startswith("⬅️") and labels[1] == "Выключить", labels
    assert settings.get_bool("app.reserve.rf_via_mine", False) is True, "выключено без подтверждения"
    assert ui.k.rules == [rp.RULE151], "правило снято до подтверждения"


async def test_cancel_in_the_dialog_changes_nothing(ui, fake_bot):
    from awgbot.bot.handlers.settings.render import _render
    _peer_episode(ui)
    nav = _nav(fake_bot)
    await card_h.rf_mine_toggle(_cb(nav, fake_bot), ui)
    await _render(_cb(nav, fake_bot), "rsv", ui)                 # «⬅️ Отмена» — назад в карточку
    assert settings.get_bool("app.reserve.rf_via_mine", False) is True and ui.k.rules == [rp.RULE151]
    assert "Резерв «Beta»" in last_screen(nav)[0]


async def test_turn_off_in_the_dialog_withdraws_the_offer_in_the_same_press(ui, fake_bot):
    _peer_episode(ui)
    ui.reserve_st_if_changed()
    nav = _nav(fake_bot)
    cb = _cb(nav, fake_bot)
    await card_h.rf_mine_off_yes(cb, ui)
    assert settings.get_bool("app.reserve.rf_via_mine", True) is False
    assert ui.rsv_get("rsv_rfpeer")["offer"] == {"on": False, "why": "off", "since": ui.rsv_get("rsv_rfpeer")["offer"]["since"]}
    assert ui.k.rules == [] and ui.fw[-1] == {}, "правило 151 или таблица транзита пережили «Выключить»"
    body = ui.reserve_st_if_changed()
    assert body is not None and body["rfp"]["offer"] is False, "отзыв не ушёл второй стороне"
    assert cb.answers[-1][0] == "Мои шлюзы для резерва: выкл"
    assert "Резерв «Beta»" in last_screen(nav)[0], "после «Выключить» не карточка"


# ── экран роли админа второго сервера ──────────────────────────────────────

async def test_the_role_screen_says_allowed_and_since_when_it_is_used(ui):
    rp.settle(ui)
    text, _ = await peer_screen(ui)
    assert "🟢 РФ-доступ через шлюзы «Alpha»: разрешён" in text.splitlines(), text
    since = _peer_episode(ui)
    text, _ = await peer_screen(ui)
    assert f"🟢 РФ-доступ через шлюзы «Alpha»: разрешён · сейчас идёт через них с {_at(since)}" in text.splitlines(), text


# ── удаление пары: Т-РТ5 ───────────────────────────────────────────────────

async def test_the_unpair_dialog_warns_the_transit_side(ui, fake_bot):
    _peer_episode(ui)
    nav = _nav(fake_bot)
    await card_h.unpair_ask(_cb(nav, fake_bot), ui)
    text, labels = last_screen(nav)
    assert text.endswith("\n\nСейчас через твои шлюзы идёт РФ-доступ пользователей «Beta» — после удаления у них "
                         "он пропадёт"), text
    assert labels[0].startswith("⬅️")


async def test_the_unpair_dialog_warns_the_asking_side(ui, fake_bot):
    rp.settle(ui)
    ui.rsv_update("rsv_rfpeer", use={"since": timeutil.now_iso(), "from": 1})
    nav = _nav(fake_bot)
    await card_h.unpair_ask(_cb(nav, fake_bot), ui)
    text, _ = last_screen(nav)
    assert text.endswith("\n\nСейчас РФ-доступ твоих пользователей идёт через шлюзы «Beta» — после удаления его не "
                         "будет, пока не ответит свой шлюз"), text


async def test_the_unpair_dialog_without_transit_has_no_tail(ui, fake_bot):
    rp.settle(ui)
    nav = _nav(fake_bot)
    await card_h.unpair_ask(_cb(nav, fake_bot), ui)
    assert last_screen(nav)[0].endswith("Подключить заново можно только новым приглашением")


# ── «Alpha» глазами просящего: строки «📤» ─────────────────────────────────

SINCE = "2026-11-02T14:05:00+05:00"
T = timeutil.fmt_clock_ui(timeutil.parse_iso(SINCE))


def test_the_episode_line_names_the_peer_and_the_moment():
    assert st_t.rfpeer_mine_lines({"use": {"since": SINCE}}, "Beta") == \
        [f"└ 🇷🇺 РФ-доступ — через шлюзы «Beta» с {T}: свои шлюзы не отвечают"]


@pytest.mark.parametrize("code, reason", [
    ("off", "его админ выключил «Мои шлюзы для резерва»"),
    ("no_gw", "у «Beta» нет шлюзов"),
    ("rt_off", "у «Beta» не задействован РФ-доступ"),
    ("down", "шлюз «Beta» сейчас не отвечает"),
    ("peer", "«Beta» сам ходит в РФ через шлюзы этого сервера"),
])
async def test_no_offer_tells_why_there_is_no_backup_path(ui, code, reason):
    rp.settle(ui)
    rp.peer_rfp(ui, offer=False, why=code)
    text, _ = await card_h.reserve_screen(ui)
    mark = "☑️" if code == "off" else "⚠️"                     # ☑️ — выключенная настройка; условие не выполнено — ⚠️
    assert f"└ {mark} Запасного РФ-доступа через шлюзы «Beta» нет: {reason}" in text.splitlines(), text


async def test_no_answer_from_the_peer_yet_is_said_plainly(ui):
    rp.settle(ui)
    rp.peer_rfp(ui, offer=False, why="")
    text, _ = await card_h.reserve_screen(ui)
    assert "└ ⚠️ Запасного РФ-доступа через шлюзы «Beta» нет: второй сервер пока не ответил" in text.splitlines()


def test_the_probe_down_line_and_its_moment():
    assert st_t.rfpeer_mine_lines({"probe_down": SINCE}, "Beta") == \
        [f"└ ⚠️ Шлюзы «Beta» для РФ-доступа не отвечают с {T} — запасного пути сейчас нет"]
    assert st_t.rfpeer_mine_lines({"offer": True}, "Beta") == [], "согласие есть, слот жив — строки нет"


async def test_the_card_of_an_episode_shows_the_episode_line(ui):
    rp.peer_rfp(ui, offer=True)
    rp.settle(ui)
    rp.own_down(ui)
    rp.ticks(ui, ui._rt_fail_need())
    text, _ = await card_h.reserve_screen(ui)
    assert [ln for ln in text.splitlines() if ln.startswith("└ 🇷🇺 РФ-доступ — через шлюзы «Beta» с ")], text


async def test_the_card_tells_when_the_peer_slot_does_not_answer(ui):
    rp.peer_rfp(ui, offer=True)
    ui.probe[rp.VSLOT] = "down"
    rp.ticks(ui, ui._rt_window_size())
    text, _ = await card_h.reserve_screen(ui)
    assert [ln for ln in text.splitlines()
            if ln.startswith("└ ⚠️ Шлюзы «Beta» для РФ-доступа не отвечают")], text


def test_more_explains_the_rf_path_without_the_view_matrix():
    more = st_t.more_text()
    assert "РФ-доступ пользователей идёт через шлюзы резерва, пока его админ это разрешает" in more
    assert "не раньше чем через 10 минут после переключения" in more
    assert "15 минут" not in more and "хотя бы одному твоему шлюзу" not in more, "случаи матрицы взглядов остались"
