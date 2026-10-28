"""Тексты хвоста старой эпохи (domain/notices/reserve.py): строки Т-Ж4 по записи
спорного, заголовок со склонениями, «… и ещё N», Т-Ж5 трёх вариантов, Т-Ж6.

Строка собирается из записи отчёта перебазирования (rsvjrn.rebase) — поля по
схеме, значения по смыслу колонки: даты — годом двумя цифрами, флаги —
«вкл/выкл», байты — объёмом, 0 — «без ограничения», статусы — словами; ключи и
коды — без значений.

Цена ошибки: имя колонки или сырое значение в чате — админ не понимает, что
потерялось; значение ключа или кода в тексте — секрет в истории Telegram;
«здесь» и «на «Beta»» наоборот — админ правит не на той стороне; «и ещё N»
неверным числом — правки теряются из виду; точка в конце уведомления —
против правила проекта.
"""
from __future__ import annotations

import pytest

from awgbot.domain.notices import reserve as rn


def line(d: dict, *, profile: str = "Иван", device: str = "", taken: str = "", variant: str = "crash") -> str:
    return rn.tail_line(d, profile, device, taken, "Beta", variant)


def col(table: str, c: str, tail, kept) -> dict:
    return {"kind": "column", "table": table, "col": c, "tail": tail, "kept": kept}


# ── строки по колонкам ──────────────────────────────────────────────────────

def test_a_name_dispute_names_both_values_and_where_the_kept_one_is():
    assert line(col("clients", "name", "Маша", "Мария К.")) == \
        "профиль Иван: имя «Маша» — на «Beta» осталось «Мария К.»"


def test_in_c9_the_kept_value_is_here():
    assert line(col("clients", "name", "Маша", "Мария К."), variant="c9") == \
        "профиль Иван: имя «Маша» — здесь осталось «Мария К.»"


def test_a_date_goes_with_a_two_digit_year():
    got = line(col("client_subscription", "period_end", "2026-11-15T00:00:00+03:00", "2026-11-01T00:00:00+03:00"))
    assert got == "профиль Иван: срок до 15.11.26 — на «Beta» осталось до 01.11.26", got


@pytest.mark.parametrize("table, c, tail, kept, want", [
    ("clients", "routing_allowed", 1, 0, "РФ-доступ вкл — на «Beta» осталось выкл"),
    ("device_traffic", "traffic_limit", 5 * 1024 ** 3, 0, "лимит трафика 5 ГБ — на «Beta» осталось без ограничения"),
    ("client_quota", "bonus_bytes", 0, 5 * 1024 ** 3, "бонус без ограничения — на «Beta» осталось 5 ГБ"),
    ("clients", "activation_status", "active", "pending", "вход выполнен — на «Beta» осталось не выполнен"),
    ("client_subscription", "status", "expired", "active", "подписка истекла — на «Beta» осталось активна"),
    ("clients", "device_limit", 5, 3, "устройств не больше 5 — на «Beta» осталось 3"),
])
def test_values_are_shown_by_their_meaning(table, c, tail, kept, want):
    got = line(col(table, c, tail, kept))
    assert got == f"профиль Иван: {want}", got


def test_an_unknown_column_is_a_general_line_without_values():
    got = line(col("clients", "kind", "owner-x", "owner-y"))
    assert "owner" not in got and "kind" not in got, got
    assert got == "профиль Иван: правку поменяли и там, и здесь — на «Beta» осталась своя", got


def test_a_mask_dispute_has_no_bits():
    got = line(col("clients", "block_reason", 3, 5))
    assert got == "профиль Иван: блокировку поменяли и там, и здесь — на «Beta» осталась своя", got


@pytest.mark.parametrize("c", ["private_key", "preshared_key", "public_key"])
def test_keys_are_named_without_values_and_ask_for_a_new_config(c):
    d = {"kind": "column", "table": "devices", "col": c, "tail": "DUMMY-tail", "kept": "DUMMY-kept", "secret": True}
    got = line(d, device="iPad")
    assert "DUMMY" not in got, got
    assert got == "устройство «iPad» профиля Иван: ключи поменяли и там, и здесь — на «Beta» остались свои. " \
                  "Выдай конфигурацию заново", got


def test_a_code_is_named_without_its_value():
    d = {"kind": "column", "table": "clients", "col": "invite_code", "tail": None, "kept": None, "secret": True}
    assert line(d) == "профиль Иван: код поменяли и там, и здесь — на «Beta» остался свой"


# ── строки по виду спорного ─────────────────────────────────────────────────

def test_an_address_taken_names_who_took_it_and_asks_for_a_new_config():
    d = {"kind": "unique", "table": "devices", "col": "address"}
    got = line(d, profile="Олег", device="iPad", taken="«Pixel» профиля Анна")
    assert got == "устройство «iPad» профиля Олег — его адрес занял «Pixel» профиля Анна, заведённый на «Beta». " \
                  "Выдай «iPad» конфигурацию заново", got


def test_an_address_taken_in_c9_was_made_here():
    d = {"kind": "unique", "table": "devices", "col": "address"}
    assert "заведённый здесь" in line(d, profile="Олег", device="iPad", taken="«Pixel»", variant="c9")


def test_a_disputed_device_removal_asks_for_a_new_config():
    """Откат удаления вернул устройство без закрытого ключа — конфигурацию выдать заново."""
    got = line({"kind": "delete", "table": "devices", "col": "name"}, device="Ноутбук")
    assert got == "устройство «Ноутбук» профиля Иван: удаление не перенесено — на «Beta» его успели поменять. " \
                  "Выдай конфигурацию заново", got


def test_a_gone_row_says_the_record_is_no_more_there():
    assert line({"kind": "gone", "table": "clients"}) == \
        "профиль Иван: правка не перенесена — на «Beta» этой записи уже нет"


def test_a_device_without_an_owner_is_not_signed_by_its_own_name():
    got = line(col("devices", "name", "A", "B"), profile="", device="iPad")
    assert got.startswith("устройство «iPad»: ") and "профиля" not in got, got


# ── заголовок, «и ещё N» ────────────────────────────────────────────────────

@pytest.mark.parametrize("n, head", [
    (1, "⚠️ Не перенесена 1 правка, сделанная здесь"),
    (2, "⚠️ Не перенесены 2 правки, сделанные здесь"),
    (5, "⚠️ Не перенесены 5 правок, сделанные здесь"),
    (21, "⚠️ Не перенесена 21 правка, сделанная здесь"),
])
def test_the_head_agrees_with_the_number(n, head):
    text = rn.tail_disputed("crash", "Beta", ["x"] * min(n, 5), n)
    assert text.startswith(head + " перед переездом на «Beta»: те же поля там успели поменять по-своему"), text


def test_the_c9_head():
    text = rn.tail_disputed("c9", "Beta", ["x", "y"], 2)
    assert text.startswith("⚠️ Не перенесены 2 правки бота на «Beta», сделанные после потери связи: здесь те же поля "
                           "успели поменять по-своему"), text


def test_twelve_disputes_show_five_lines_and_seven_more():
    lines = [f"профиль П{n}: правка" for n in range(12)]
    text = rn.tail_disputed("crash", "Beta", lines, 12)
    assert text.count("\n• ") == 5 and text.endswith("\n… и ещё 7"), text
    assert "П5" not in text, "шестая строка в тексте"


def test_five_disputes_have_no_more():
    text = rn.tail_disputed("crash", "Beta", ["a"] * 5, 5)
    assert "и ещё" not in text and text.count("\n• ") == 5


# ── Т-Ж5, Т-Ж6 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant, want", [
    ("crash", "ℹ️ Правки, сделанные здесь перед переездом на «Beta», перенесены туда: 4"),
    ("c9", "ℹ️ Правки бота на «Beta» после потери связи перенесены сюда: 12"),
    ("self", "ℹ️ Правки, сделанные здесь перед переездом на «Beta», перенесены после возврата: 4"),
])
def test_tj5_variants(variant, want):
    assert rn.tail_moved(variant, "Beta", 12 if variant == "c9" else 4) == want


def test_tj6_texts_and_no_final_period():
    stalled = rn.follow_stalled("Beta", "здесь — по 14:02")
    assert stalled == "⚠️ Данные здесь перестали догонять бот на «Beta»: здесь — по 14:02 — возврат подождёт"
    ok = rn.follow_ok("Beta")
    assert ok == "🟢 Данные здесь снова идут за ботом на «Beta»"
    for t in (stalled, ok, rn.tail_moved("crash", "Beta", 1), rn.tail_disputed("crash", "Beta", ["a"], 1)):
        assert not t.rstrip().endswith("."), t
