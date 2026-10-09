"""Атомы общего слоя экранов — правила оформления проверяются здесь один раз:
шапка, статус, всплывашка, тумблер, числа, «и ещё N», «безлимит», и словарь
роли — оба экземпляра заполнены целиком."""
from dataclasses import fields

import pytest

from awgbot.bot import roles, ui
from awgbot.bot.texts import fmt

pytestmark = pytest.mark.unit


# ── шапка и статус ───────────────────────────────────────────────────────────

def test_head_puts_the_icon_outside_bold_and_the_status_next_to_the_name():
    assert ui.head("🔔 Уведомления") == "🔔 <b>Уведомления</b>"
    assert ui.head("🔀 VPN-транзит", ui.st("ok", "работает")) == "🔀 <b>VPN-транзит</b> 🟢 работает"
    assert ui.head("⬆️ Обновления", meta=["v3.2.0 🟢 актуальна"]) == "⬆️ <b>Обновления</b> · v3.2.0 🟢 актуальна"
    assert ui.head("💾 Бэкапы", "✅ вкл", meta=["🔐 фраза задана", ""]) == "💾 <b>Бэкапы</b> ✅ вкл · 🔐 фраза задана"
    assert ui.head("Настройки") == "<b>Настройки</b>", "без значка — всё имя жирным"


def test_status_table_is_icons_only():
    assert ui.STATUS == {"ok": "🟢", "warn": "🟡", "bad": "🔴", "wait": "⏳", "off": "⚪"}
    assert ui.st("off", "нет связи") == "⚪ нет связи" and ui.st("wait") == "⏳"
    with pytest.raises(KeyError):
        ui.st("unknown")


def test_tick_matches_the_button_toggle():
    from awgbot.bot.keyboards.common import _chk
    assert ui.tick(True) == _chk(True) == "✅" and ui.tick(False) == _chk(False) == "☑️"


def test_screen_skips_none_keeps_empty_lines_and_puts_the_note_first():
    text = ui.screen("🩺 Мониторинг", lines=["строка", None, "", "ещё"], details="подробности")
    assert text == "🩺 <b>Мониторинг</b>\nстрока\n\nещё\n<blockquote expandable>подробности</blockquote>"
    assert ui.screen("🩺 Мониторинг", note="✅ Готово") == "✅ Готово\n\n🩺 <b>Мониторинг</b>"
    assert ui.screen("🩺 Мониторинг") == "🩺 <b>Мониторинг</b>", "без строк и подробностей — одна шапка"


# ── всплывашка ───────────────────────────────────────────────────────────────

def test_confirm_puts_the_question_first_and_the_cost_on_the_next_line():
    assert ui.confirm("🔁 Перезапустить AWG?", "Все соединения оборвутся") == "🔁 Перезапустить AWG?\nВсе соединения оборвутся"
    assert ui.confirm("🗑 Удалить?") == "🗑 Удалить?"


def test_result_has_both_forms_and_escapes_the_reason():
    assert ui.result(True, "AWG перезапущен", "AWG не перезапущен") == "✅ AWG перезапущен"
    assert ui.result(False, "AWG перезапущен", "AWG не перезапущен", "awg <quick> & dirty") == \
        "🔴 AWG не перезапущен: awg &lt;quick&gt; &amp; dirty"
    assert ui.fail("Порт не изменён") == "🔴 Порт не изменён"


def test_changed_shows_old_arrow_new_with_the_unit():
    assert ui.changed("Частота опроса", 3, 5, " мин") == "✅ Частота опроса: 3 → 5 мин"
    assert ui.changed("Бонус", "—", 10, " ГБ") == "✅ Бонус: — → 10 ГБ"


def test_toast_table_is_plain_short_and_without_duplicates():
    table = {k: v for k, v in vars(ui.Toast).items() if not k.startswith("_")}
    assert table and all(v == ui.toast(v) for v in table.values()), "теги или сущности во всплывашке"
    assert len(set(table.values())) == len(table), "одна строка под двумя именами"


def test_toast_strips_tags_unescapes_and_caps_at_200():
    assert ui.toast("<b>Готово</b> &amp; точка") == "Готово & точка"
    long = "x" * 300
    out = ui.toast(long)
    assert len(out) == ui.TOAST_MAX == 200 and out.endswith("…")
    assert ui.toast("y" * 200) == "y" * 200, "ровно предел — без многоточия"
    assert ui.toast(None) == ""


def test_note_budget_counts_visible_text_only():
    plain = "a" * 1000
    assert ui.note_budget(plain) == 4096 - 1000 - 300
    assert ui.note_budget("<b>" + plain + "</b>") == ui.note_budget(plain), "теги не в счёт"
    assert ui.note_budget("z" * 5000) == 400, "не меньше 400"


# ── форматтеры ───────────────────────────────────────────────────────────────

def test_num_groups_thousands_with_a_space():
    assert fmt.num(0) == "0" and fmt.num(None) == "0"
    assert fmt.num(1234567) == "1 234 567" and fmt.num("42") == "42"


def test_more_lists_up_to_twelve_then_counts_the_rest():
    items = [f"h{i}.example.org" for i in range(12)]
    assert fmt.more(items) == ", ".join(f"<code>{x}</code>" for x in items), "на границе 12 — без хвоста"
    assert fmt.more(items + ["x"]).endswith("</code> и ещё 1")
    assert fmt.more(["a<b"], code=False) == "a&lt;b", "имена — текстом, но экранированы"
    assert fmt.more([]) == ""
    assert fmt.more(["a", "b", "c"], shown=2) == "<code>a</code>, <code>b</code> и ещё 1"


def test_unlimited_shows_zero_and_terabytes():
    assert fmt.unlimited(0) == "0 ГБ (безлимит)"
    assert fmt.unlimited(1024 ** 4 + 1024 ** 3 * 512) == "1.5 ТБ (безлимит)"


# ── словарь роли ─────────────────────────────────────────────────────────────

def _flat(obj, prefix=""):
    for f in fields(obj):
        v = getattr(obj, f.name)
        if hasattr(v, "__dataclass_fields__"):
            yield from _flat(v, f"{prefix}{f.name}.")
        else:
            yield f"{prefix}{f.name}", v


@pytest.mark.parametrize("br", [roles.MAIN, roles.GATEWAY], ids=lambda r: r.name)
def test_both_role_dictionaries_are_filled(br):
    """Пустое поле — забытая роль; пустым может быть только то, что в OPTIONAL,
    и для каждого поля из OPTIONAL должна существовать роль, у которой оно пусто."""
    empty = {name for name, v in _flat(br) if v in ("", (), None)}
    assert empty <= roles.OPTIONAL, f"{br.name}: не заполнено {empty - roles.OPTIONAL}"
    assert br.name in ("main", "gateway")


def test_optional_fields_are_really_optional_somewhere():
    names = {n for n, _ in _flat(roles.MAIN)}
    assert roles.OPTIONAL <= names, "OPTIONAL называет несуществующее поле"
    for name in roles.OPTIONAL:
        assert any(dict(_flat(br))[name] in ("", ()) for br in (roles.MAIN, roles.GATEWAY)), \
            f"{name} нигде не пусто — исключение лишнее"


def test_role_phrases_are_whole_sentences_not_fragments():
    """Слово роли — законченная фраза: не начинается с пробела, «не» не
    приклеивается снаружи, падежи — отдельные поля."""
    for br in (roles.MAIN, roles.GATEWAY):
        for name, v in _flat(br):
            if isinstance(v, str) and v and not name.startswith("keys."):
                assert v == v.strip() or name == "ssh_port_tail", (br.name, name, v)
    assert roles.MAIN.host_gen.endswith("а") and roles.GATEWAY.host_loc.endswith("е")


def test_current_and_pick_follow_the_installation_role(monkeypatch):
    from awgbot.core import config
    monkeypatch.setattr(config, "ROLE", "gateway")
    assert roles.current() is roles.GATEWAY
    monkeypatch.setattr(config, "ROLE", "client")
    assert roles.current() is roles.MAIN
    assert roles.pick(True) is roles.GATEWAY and roles.pick(False) is roles.MAIN
