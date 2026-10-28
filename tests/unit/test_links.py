"""
Грамматика ссылок на экраны (bot/links): оба разборщика (админ и
пользователь), метка «-n» у ссылок из уведомлений — mark_new в
notifier._send — и её снятие strip_new при разборе /start.

Цена ошибки: ссылка из уведомления без метки перерисовывает живое меню
где-то выше в чате — человек нажал, «ничего не произошло»; метка на ссылке
экрана открывает экран с экрана новым сообщением, и меню в чате множатся;
метка, приписанная коду приглашения, или код, обрезанный по «-n», — друг
не активирует доступ по ссылке, которую ему переслали; метка, вытолкнувшая
параметр за 64 знака, — Telegram перестаёт считать текст ссылкой.
"""
from __future__ import annotations

import re

import pytest

from awgbot.bot import links

pytestmark = pytest.mark.unit

BOT = "test_bot"


def _a(payload: str, label: str = "x", bot: str = BOT) -> str:
    return f'<a href="https://t.me/{bot}?start={payload}">{label}</a>'


def _payloads(text: str) -> list[str]:
    return re.findall(r'\?start=([A-Za-z0-9_-]+)"', text)


# ── mark_new ─────────────────────────────────────────────────────────────────

SCREENS = ["cl-5", "dev-7", "gw-2", "traffic-3-t", "traffic-3", "traffic_local-4-t", "online", "expiring",
           "unassigned", "upd", "traffic", "migration", "migration-3", "rsv", "extend-4", "sub", "rf",
           "list-drift-2", "list-gwnets-2", "list-peernets", "list-snap-k3x9a2bq7d"]


@pytest.mark.parametrize("payload", SCREENS)
def test_every_screen_link_in_a_notification_gets_the_mark_once(payload):
    """Каждая ссылка на экран или список — с «-n» ровно один раз: повторный
    проход (уведомление, прошедшее через отправку дважды) ничего не меняет."""
    text = f"🛰 {_a(payload, 'NASPi')}: конфигурация неактуальна"
    once = links.mark_new(text)
    assert _payloads(once) == [payload + "-n"], once
    assert links.mark_new(once) == once, "второй проход дописал метку ещё раз"


def test_several_links_in_one_notification_are_all_marked_and_the_text_around_is_untouched():
    text = (f"🛰 {_a('gw-1', 'NASPi')}: конфигурация неактуальна — устройства админа\n"
            f" · …{_a('list-drift-1', 'и ещё 5')}\nпрофиль {_a('cl-3', 'Ксюша &amp; Co')}")
    got = links.mark_new(text)
    assert _payloads(got) == ["gw-1-n", "list-drift-1-n", "cl-3-n"], got
    assert re.sub(r"-n\"", '"', got) == text, "метка задела что-то кроме параметров ссылок"


@pytest.mark.parametrize("payload", ["CTEST0000001", "FAbCdEfGhIjK", "refresh", "whatever", "gwcfg-1",
                                     "list-", "list-gwnets-x", "cl-5-n-x"])
def test_codes_refresh_and_foreign_payloads_are_not_marked(payload):
    """Код приглашения, «обновлено … 🔄» (перерисовка на месте) и чужое — как
    есть: экраном они не открываются, а у пользователя код проверяется по
    исходному параметру."""
    text = f"ссылка: {_a(payload)}"
    assert links.mark_new(text) == text


def test_an_address_outside_href_is_not_touched():
    """Голый адрес текстом (приглашение другу, `friends.py`) — не ссылка
    разметки: дописанная к нему метка сломала бы код в тексте."""
    text = "Открой https://t.me/test_bot?start=cl-5 или <code>t.me/test_bot?start=dev-7</code>"
    assert links.mark_new(text) == text


def test_links_without_start_and_to_another_site_are_not_touched():
    text = '<a href="https://t.me/naspi_gw_bot">NASPi</a> · <a href="https://example.org/?start=cl-5">x</a>'
    assert links.mark_new(text) == text


def test_a_link_to_another_bot_screen_is_marked_too():
    """Ссылка на экран — по грамматике параметра, имя бота любое (бот шлюза
    рисует свои ссылки своим именем)."""
    assert _payloads(links.mark_new(_a("gw-2", bot="naspi_gw_bot"))) == ["gw-2-n"]


def test_the_mark_never_pushes_a_payload_past_64_characters():
    """Самый длинный параметр экрана с меткой — в 64 знаках Telegram (id — до
    девяти цифр); номер длиннее — не ссылка вовсе, и метку он не получает."""
    longest = f"{links.texts.TRAFFIC_PAYLOAD}-999999999-t"
    assert links.is_screen(longest) and len(longest) + len(links.NEW) <= 64, longest
    assert _payloads(links.mark_new(_a(longest))) == [longest + "-n"]
    edge = "cl-" + "1" * 61
    assert not links.is_screen(edge)
    assert links.mark_new(_a(edge)) == _a(edge)


@pytest.mark.parametrize("text", ["", None, "без ссылок", "start=cl-5"])
def test_texts_without_links_pass_as_they_are(text):
    assert links.mark_new(text) == text


# ── strip_new и оба разборщика ──────────────────────────────────────────────

@pytest.mark.parametrize("payload, bare", [
    ("cl-5-n", "cl-5"), ("dev-7-n", "dev-7"), ("gw-2-n", "gw-2"), ("traffic-3-t-n", "traffic-3-t"),
    ("online-n", "online"), ("sub-n", "sub"), ("rf-n", "rf"), ("list-drift-2-n", "list-drift-2"),
])
def test_strip_new_takes_the_mark_off_a_screen_link(payload, bare):
    assert links.strip_new(payload) == (bare, True)


@pytest.mark.parametrize("payload", ["cl-5", "traffic-3-t", "online", "CTEST0000001", "", "refresh"])
def test_strip_new_leaves_unmarked_links_and_codes_as_they_are(payload):
    assert links.strip_new(payload) == (payload, False)


@pytest.mark.parametrize("code", ["FAbCdEfGh-n", "CTEST000-n", "Fdev-7-n", "whatever-n", "cl-5-n-n", "-n", "refresh-n"])
def test_a_code_ending_in_the_mark_stays_whole(code):
    """Код, оканчивающийся на «-n», — не ссылка на экран: метку с него не
    снимаем, он уходит в проверку кода целиком."""
    assert links.strip_new(code) == (code, False)


@pytest.mark.parametrize("payload, want", [
    ("online", ("online", 0)), ("expiring", ("expiring", 0)), ("unassigned", ("unassigned", 0)),
    ("upd", ("upd", 0)), ("traffic", ("traffic", 0)), ("traffic_local", ("traffic", 0)),
    ("migration", ("migration", 0)), ("rsv", ("set_rsv", 0)),
    ("traffic-3", ("traffic_dev", 3)), ("traffic-3-t", ("traffic_dev", 3)), ("traffic_local-4-t", ("traffic_dev", 4)),
    ("traffic_local-4", ("traffic_dev", 4)),
    ("extend-4", ("extend", 4)), ("gw-2", ("gw", 2)), ("cl-5", ("cl", 5)), ("dev-7", ("dev", 7)),
    ("migration-3", ("migration_cl", 3)),
    ("cl-x", None), ("gw-", None), ("whatever", None), ("refresh", None), ("list-drift-2", None), ("sub", None),
])
def test_the_admin_grammar_is_what_it_was(payload, want):
    """Разборщик админа переехал без изменения поведения: шапка главной,
    карточки и уведомления открывают те же экраны."""
    assert links.parse_admin(payload) == want


@pytest.mark.parametrize("payload, want", [
    ("sub", ("sub", 0)), ("rf", ("rf", 0)), ("dev-7", ("dev", 7)),
    ("dev-x", None), ("cl-5", None), ("online", None), ("CTEST0000001", None), ("dev-7-t", None),
])
def test_the_client_grammar_is_what_it_was(payload, want):
    assert links.parse_client(payload) == want


@pytest.mark.parametrize("payload", ["cl-²", "cl-²-n", "dev-²", "dev-٣-n", "traffic-²-t", "cl-99999999999999999999",
                                     "dev-99999999999999999999", "gw-0000000000"])
def test_unicode_digits_and_endless_numbers_are_not_links_and_do_not_raise(payload):
    """Параметр /start — ввод человека: цифра Юникода проходила str.isdigit и
    роняла int(); число без предела роняло запрос к базе. У пользователя и
    гостя такой параметр идёт дальше как код приглашения, а не исключением."""
    assert links.parse_admin(payload.removesuffix("-n")) is None, payload
    assert links.parse_client(payload.removesuffix("-n")) is None, payload
    assert links.strip_new(payload) == (payload, False), payload
    assert not links.is_screen(payload)


@pytest.mark.parametrize("payload, admin, client", [
    ("cl-999999999", ("cl", 999999999), None), ("cl-1234567890", None, None),
    ("dev-999999999", ("dev", 999999999), ("dev", 999999999)), ("dev-1000000000", None, None),
    ("traffic-999999999-t", ("traffic_dev", 999999999), None), ("traffic-1000000000-t", None, None),
    ("gw-007", ("gw", 7), None), ("dev-0", ("dev", 0), ("dev", 0)),
])
def test_an_id_in_a_link_is_up_to_nine_ascii_digits(payload, admin, client):
    """Предел — девять цифр, у админа и у пользователя одинаковый: id базы в
    ссылку влезает, десятая цифра — уже не ссылка (у пользователя параметр
    идёт дальше кодом приглашения). Без предела двадцать цифр уходили в запрос
    к базе и роняли его переполнением целого SQLite."""
    assert links.parse_admin(payload) == admin, (payload, links.parse_admin(payload))
    assert links.parse_client(payload) == client, (payload, links.parse_client(payload))


@pytest.mark.parametrize("payload, want", [
    ("cl-5-n", ("cl", 5)), ("traffic-3-t-n", ("traffic_dev", 3)), ("gw-2-n", ("gw", 2)), ("online-n", ("online", 0)),
])
def test_the_admin_parser_reads_marked_links_after_strip(payload, want):
    bare, new = links.strip_new(payload)
    assert new and links.parse_admin(bare) == want, (bare, new)


@pytest.mark.parametrize("payload, want", [("dev-7-n", ("dev", 7)), ("sub-n", ("sub", 0)), ("rf-n", ("rf", 0))])
def test_the_client_parser_reads_marked_links_after_strip(payload, want):
    bare, new = links.strip_new(payload)
    assert new and links.parse_client(bare) == want, (bare, new)


def test_the_handlers_parse_with_the_same_grammar():
    """Обработчики /start и уведомитель знают одну грамматику: разборщик в
    обработчике — тот же, что метит уведомления (иначе метка ставилась бы
    ссылкам, которые /start не откроет, или наоборот)."""
    from awgbot.bot.handlers.admin import panel
    from awgbot.bot.handlers.client import parse_link as client_parse
    for p in SCREENS + ["whatever", "CTEST0000001"]:
        assert panel.parse_link(p) == links.parse_admin(p), p
        assert client_parse(p) == links.parse_client(p), p


# ── сторож меток эталонов (tests/screens/marks) не пустой ───────────────────

def test_the_reference_guard_sees_a_marked_screen_and_an_unmarked_notification():
    """Сторож меток в эталонах экранов ловит обе ошибки: экран с «-n» и
    уведомление без него; правильные — молчит. Иначе его зелёный цвет в
    test_screens ничего бы не значил."""
    from tests.screens import marks
    from tests.screens.harness import Call, Record
    note_ok = "🛰 " + _a("gw-1-n", "NASPi") + ": конфигурация неактуальна"
    note_bad = "🛰 " + _a("gw-1", "NASPi") + ": конфигурация неактуальна"
    note_alien = "код: " + _a("CTEST0000001")
    screen_ok = "🔗 Связь подсетей: с " + _a("gw-1", "NASPi") + " " + _a("list-gwpeers-2", "и ещё 1")
    screen_bad = "🔗 Связь подсетей: с " + _a("gw-1-n", "NASPi")
    rec = Record(action="> test", calls=[Call("+ send #2", note_ok), Call("= edit #1", screen_ok)])
    assert marks.problems("t", rec, {note_ok}) == []
    for body, notified, what in ((note_bad, {note_bad}, "без метки"), (note_alien, {note_alien}, "вне грамматики"),
                                 (screen_bad, set(), "вне уведомления")):
        got = marks.problems("t", Record(action="> test", calls=[Call("+ send #2", body)]), notified)
        assert len(got) == 1 and what in got[0], got
