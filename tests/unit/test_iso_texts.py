"""Момент из ISO-строки в текстах — timeutil.iso_clock / iso_dt / iso_date_yy и
один набор помощников текстов резерва.

Что стережём: строка из БД, состояния или линка показывается в поясе показа
(пояс бота, в тестах — UTC+5), какое бы смещение в ней ни стояло; пустая или
битая строка даёт заданную заглушку, а не трассировку посреди уведомления.
Копии этих помощников по модулям (свои _when/_date/_n в каждом тексте резерва,
в бэкапе, в вопросах шлюза) разъезжались: где-то ловили только ValueError, где-то
дата шла без пояса. Цена ошибки — время «с …» в чужом поясе (час-другой мимо,
у полуночи — другая дата) или упавшее уведомление вместо строки без момента.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from awgbot.util import timeutil as t

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2] / "awgbot"
LATE = "2026-11-15T22:30:00+03:00"          # в поясе показа (UTC+5) — уже 16.11, 00:30


def test_the_moment_is_shown_in_the_display_zone_whatever_offset_it_was_written_with():
    assert t.iso_clock(LATE) == "00:30 (UTC+5)"
    assert t.iso_clock("2026-11-15T19:30:00+00:00") == "00:30 (UTC+5)"
    assert t.iso_clock(LATE, seconds=True) == "00:30:00 (UTC+5)"
    assert t.iso_dt(LATE, year=True) == "16.11.26 00:30 (UTC+5)"
    assert t.iso_date_yy(LATE) == "16.11.26", "дата у полуночи — по поясу показа, а не по смещению строки"


@pytest.mark.parametrize("bad", ["", None, "вчера", "2026-13-01T00:00:00+03:00", 0])
def test_an_empty_or_broken_moment_gives_the_fallback(bad):
    assert t.iso_clock(bad) == "" and t.iso_dt(bad) == "" and t.iso_date_yy(bad) == ""
    assert t.iso_clock(bad, "?") == "?" and t.iso_dt(bad, "?", year=True) == "?" and t.iso_date_yy(bad, "?") == "?"


def test_texts_take_the_moment_through_the_shared_helpers():
    """Тексты резерва, бэкапа, вопросов шлюза и строки главной — одним путём: те же
    строки, тот же пояс, та же заглушка."""
    from awgbot.bot.texts.gateway import ask
    from awgbot.bot.texts.reserve import c9wait, common, peeradmin, pult
    from awgbot.domain.notices import backup, reserve_fmt, reserve_tail, reserve_tg
    from awgbot.domain.services.reserve import away
    assert common._when(LATE) == reserve_fmt._when(LATE) == away._when(LATE) == "00:30 (UTC+5)"
    assert common._date(LATE) == away._date(LATE) == "16.11 00:30 (UTC+5)"
    assert backup._when(LATE) == "16.11.26 00:30 (UTC+5)"
    assert backup._date(LATE) == ask._date(LATE) == reserve_tail._date_yy(LATE) == "16.11.26"
    assert common._when("") == reserve_fmt._when(None) == "" and away._when("") == away._date("x") == "?"
    assert backup._date("") == backup._when("") == ask._date(None) == "?"
    assert reserve_tail._date_yy("2026-11-15") == "15.11.26" and reserve_tail._date_yy("Х") == "Х"
    assert "с 16.11" in reserve_tg.tg_pool_stale(LATE), "дата застоя анонсов — по поясу показа"
    assert pult._when is common._when and peeradmin._when is common._when and c9wait._date is common._date


def _defs(path: pathlib.Path) -> set[str]:
    return {n.name for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)}


def test_reserve_texts_keep_one_set_of_name_and_time_helpers():
    """Имя «…» и время в текстах резерва — из bot/texts/reserve/common (бот) и
    domain/notices/reserve_fmt (уведомления домена: экранирование без кавычек);
    своих копий у соседей нет, и уведомления берут их не через фасад reserve."""
    texts = ROOT / "bot" / "texts" / "reserve"
    for path in sorted(texts.rglob("*.py")):
        if path.name != "common.py":
            assert not _defs(path) & {"_n", "_when", "_date"}, f"своя копия помощника в {path.relative_to(ROOT)}"
    notices = ROOT / "domain" / "notices"
    for path in sorted(notices.glob("reserve*.py")):
        if path.name != "reserve_fmt.py":
            assert not _defs(path) & {"_e", "_n", "_when"}, f"своя копия помощника в {path.relative_to(ROOT)}"
    for name in ("reserve_keep.py", "reserve_pult.py", "reserve_tail.py"):
        tree = ast.parse((notices / name).read_text())
        assert not [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module == "reserve"], \
            f"{name} берёт помощники через фасад reserve — встречный импорт пакета"


def test_the_return_mode_is_read_in_one_place():
    """Ключ рычага возврата читает только reserve/away.return_mode (метод сервиса
    reserve_return_mode — тот же вызов); больше ни один модуль."""
    allowed = {ROOT / "domain" / "services" / "reserve" / "away.py"}
    hits = [p for p in ROOT.rglob("*.py") if '"app.reserve.return"' in p.read_text() and p not in allowed]
    assert not hits, f"ключ рычага возврата читают мимо return_mode: {hits}"


def test_gateway_and_routing_texts_show_the_moment_or_drop_it():
    """Остальные тексты с моментом из строки — шлюз (панель резерва, обход
    Telegram, кворум, обновление списков), карточка и экран слотов, снимок
    списка, уведомление о настройках шлюза, набор обновлений: живая строка —
    в поясе показа, битая — заглушка или строка без «с …», а не трассировка."""
    from awgbot.bot.texts import fleet
    from awgbot.bot.texts.gateway import panel, transit
    from awgbot.bot.texts.routing import card, slots
    from awgbot.domain.notices import gateway as gn
    assert fleet._clock(LATE) == "00:30 (UTC+5)" and fleet._clock("x") == "?"
    assert "с 00:30 (UTC+5)" in panel.reserve_panel_line({"seen": "down", "since": LATE, "name": "B"})
    assert panel.reserve_panel_line({"seen": "down", "since": "x", "name": "B"}).startswith("⚠️ Резерв «B»: нет связи — ")
    assert "на нём с 00:30" in panel.reserve_panel_line({"open": True, "since_open": LATE, "name": "B"})
    assert panel.reserve_tg_lines({"tg_via": "B", "tg_since": "x", "name": "A"}) == [
        "📨 Telegram — через резерв: аплинк до «A» молчит"]
    assert panel.reserve_tg_lines({"tg_via": "B", "tg_since": LATE, "name": "A"})[0].endswith(" с 00:30 (UTC+5)")
    assert "не вижу с 00:30 (UTC+5)" in panel.quorum_panel_line({"w": "uns", "since": LATE, "name": "A"})
    assert "не вижу —" in panel.quorum_panel_line({"w": "uns", "since": None, "name": "A"})
    assert transit._lists_updated_short(LATE) == "обн. 16.11 00:30 (UTC+5)"
    assert transit._lists_updated_short("x") == "обн. ?"
    assert card.cfg_more_lines({"gen": 3, "applied_at": LATE}) == ["конфигурация №3 применена 16.11 00:30 (UTC+5)"]
    assert card.cfg_more_lines({"gen": 3, "applied_at": "x"}) == card.cfg_more_lines({"applied_at": LATE}) == []
    assert card.keep_line({"state": "saved", "at": LATE}) == "💾 Настройки бота шлюза в бэкапе: на 16.11.26 00:30 (UTC+5)"
    assert card.keep_line({"state": "saved"}) == card.keep_line({"state": "saved", "at": "x"}) == ""
    head = card.channel_lines({"ever": True, "online": False, "seen": LATE}, True)[0]
    assert head.endswith("🔴 нет связи с 16.11 00:30 (UTC+5)")
    assert card.channel_lines({"ever": True, "online": False, "seen": "x"}, True)[0].endswith("🔴 нет связи")
    from types import SimpleNamespace
    st = [{"gateway": SimpleNamespace(id=i, label="", lan_subnets=[], lan_mode=1),
           "device": SimpleNamespace(name=f"Pi{i}"), "active": i == 1, "link_ok": True} for i in (1, 2)]
    assert "Переключение: 16.11 00:30 (UTC+5), авто" in slots.gateways_text(st, switched_at=LATE)
    assert "Переключение" not in slots.gateways_text(st, switched_at="x")
    assert gn.keep_received([], LATE).endswith("как на 16.11.26 00:30 (UTC+5)")
    assert gn.keep_received([], "x").endswith("как на ?")


def test_no_text_keeps_its_own_try_around_parse_iso():
    """Своя обёртка try: fmt_*(parse_iso(…)) except … — копия iso_clock / iso_dt /
    iso_date_yy; у копий разъезжались ловимые исключения и заглушки."""
    wrapped = {"fmt_clock_ui", "fmt_dt_ui", "fmt_date_yy"}

    def name(f):
        return getattr(f, "attr", getattr(f, "id", ""))

    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        if "timeutil" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Try):
                continue
            for sub in (n for stmt in node.body for n in ast.walk(stmt)):
                if (isinstance(sub, ast.Call) and name(sub.func) in wrapped and sub.args
                        and isinstance(sub.args[0], ast.Call) and name(sub.args[0].func) == "parse_iso"):
                    hits.append(f"{path.relative_to(ROOT)}:{sub.lineno}")
    assert not hits, f"своя копия разбора момента в try: {hits}"


def test_profile_and_update_dates_survive_a_broken_mark():
    """Дата периода профиля и «последний раз виден» — через timeutil.iso_date_ui/iso_dt:
    битая отметка в базе даёт «?», а не падение всего экрана профиля."""
    from types import SimpleNamespace
    from awgbot.util import timeutil
    assert timeutil.iso_date_ui("мусор", "?") == "?" and timeutil.iso_date_ui("") == ""
    assert timeutil.iso_date_ui("2026-11-15T10:00:00+03:00") == "15.11" or timeutil.iso_date_ui("2026-11-15T10:00:00+03:00").startswith("15.11")
    from awgbot.bot.texts.admin import profiles
    c = SimpleNamespace(name="Иван", period_start="мусор", period_end="мусор")
    assert "сейчас ?" in profiles.period_start_prompt(c) and "сейчас ?" in profiles.period_end_prompt(c)
