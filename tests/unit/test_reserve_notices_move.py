"""Тексты уведомлений переезда и возврата (domain/notices/reserve.py): Т-РУ4,
4а, 5, 5б, 6, 11, 16, 17 и Т-РК1, Т-РК2.

Цена ошибки: имя сервера или причина с `<` в тексте без экранирования —
Telegram отвергает сообщение целиком, и админ не узнаёт о переезде; точка в
конце — нарушение формы уведомлений; адрес записи не моноширинным — его не
скопировать одним касанием, когда запись нужно поменять руками; «со звуком»
и «без звука» перепутаны — ночью будят не тем.
"""
from __future__ import annotations

import pytest

from awgbot.domain.notices import reserve as rn

pytestmark = pytest.mark.unit

SINCE = "2026-10-21T03:12:00+05:00"
EVIL = "<b>Бета</b>&"


def _all_texts() -> list[tuple[str, str]]:
    return [
        ("hosting_started.button", rn.hosting_started("Beta", "button", SINCE)),
        ("hosting_started.dead", rn.hosting_started("Beta", "dead", SINCE)),
        ("hosting_started.rf", rn.hosting_started("Beta", "rf", SINCE)),
        ("hosting_started.nogw", rn.hosting_started("Beta", "nogw", SINCE)),
        ("hosting_started.code", rn.hosting_started("Beta", "code", SINCE)),
        ("hosting_ended", rn.hosting_ended("Beta", "↑ 1 ГБ")),
        ("hosting_ended.empty", rn.hosting_ended("Beta", "")),
        ("hosting_failed", rn.hosting_failed("Beta", "порт занят")),
        ("moved.button", rn.moved_to_reserve("Beta", "button", SINCE)),
        ("moved.dead", rn.moved_to_reserve("Beta", "dead", SINCE, SINCE)),
        ("moved.rf", rn.moved_to_reserve("Beta", "rf", SINCE, SINCE, "198.51.100.7")),
        ("moved.nogw", rn.moved_to_reserve("Beta", "nogw", SINCE, SINCE)),
        ("moved.code", rn.moved_to_reserve("Beta", "code", SINCE, SINCE)),
        ("returned_home", rn.returned_home("Beta", {"clients": 1, "devices": 2, "extensions": 3, "blocks": 4}, "↑ 1 ГБ")),
        ("returned_home.empty", rn.returned_home("Beta", {}, "")),
        ("primary_back.auto", rn.primary_back(True)),
        ("primary_back.button", rn.primary_back(False)),
        ("move_failed", rn.move_failed("Beta", "запись не изменена")),
        ("move_failed.released", rn.move_failed("Beta", "запись не изменена", released=True)),
        ("return_failed", rn.return_failed("Beta", "нет снимка")),
        ("record_not_changed", rn.record_not_changed("vpn.example.org", "HTTP 403", "198.51.100.7")),
        ("record_foreign", rn.record_foreign("vpn.example.org", "198.51.100.77")),
        ("dns_token_rotate", rn.dns_token_rotate("Beta")),
        ("client_moved", rn.client_moved()),
        ("client_moved.rf", rn.client_moved(rf=True)),
        ("client_returned", rn.client_returned("2026-10-28T00:00:00+05:00")),
        ("client_returned.nodate", rn.client_returned("")),
    ]


@pytest.mark.parametrize("name,text", _all_texts(), ids=[n for n, _ in _all_texts()])
def test_no_notice_ends_with_a_period(name, text):
    assert text.strip() and not text.rstrip().endswith("."), f"{name}: точка в конце уведомления"


def test_server_names_and_reasons_are_html_escaped():
    for text in (rn.hosting_started(EVIL, "button", SINCE), rn.hosting_ended(EVIL, ""), rn.moved_to_reserve(EVIL, "button", SINCE),
                 rn.returned_home(EVIL, {}, ""), rn.move_failed(EVIL, EVIL, released=True), rn.hosting_failed(EVIL, EVIL),
                 rn.return_failed(EVIL, EVIL), rn.dns_token_rotate(EVIL), rn.record_not_changed(EVIL, EVIL, EVIL),
                 rn.record_foreign(EVIL, EVIL)):
        assert "<b>Бета</b>" not in text, text
        assert "&lt;b&gt;" in text, text


def test_addresses_of_the_record_are_monospace():
    t = rn.record_not_changed("vpn.example.org", "HTTP 403", "198.51.100.7")
    assert "<code>vpn.example.org</code>" in t and "<code>198.51.100.7</code>" in t
    f = rn.record_foreign("vpn.example.org", "198.51.100.77")
    assert "<code>vpn.example.org</code>" in f and "<code>198.51.100.77</code>" in f
    assert "<code>198.51.100.7</code>" in rn.moved_to_reserve("Beta", "rf", SINCE, SINCE, "198.51.100.7")


def test_the_hosting_notice_tells_the_reason_of_each_kind():
    assert "перевёл к тебе" in rn.hosting_started("Beta", "button", SINCE)
    assert "не отвечает" in rn.hosting_started("Beta", "dead", SINCE)
    assert "недоступен из РФ" in rn.hosting_started("Beta", "rf", SINCE)
    assert "шлюзы не на связи" in rn.hosting_started("Beta", "nogw", SINCE)
    assert "аварийному коду" in rn.hosting_started("Beta", "code", SINCE)


def test_the_primary_side_notice_lists_what_clients_were_told():
    for kind in ("button", "dead", "rf", "nogw", "code"):
        assert "переподключи VPN" in rn.moved_to_reserve("Beta", kind, SINCE, SINCE), kind


def test_the_return_summary_counts_only_what_happened_and_appends_lost_devices():
    full = rn.returned_home("Beta", {"clients": 1, "devices": 2, "extensions": 3, "blocks": 4}, "↑ 1 ГБ")
    for part in ("1 новых профиля", "2 новых устройства", "3 продления", "4 блокировки", "через резерв"):
        assert part in full, full
    assert "без новых профилей и устройств" in rn.returned_home("Beta", {"clients": 0}, "")
    line = rn.lost_device("Планшет", "Аня", "Ноутбук", "Вера", "Beta")
    text = rn.returned_home("Beta", {"lost": [line]}, "")
    assert text.count("Не встало устройство") == 1 and "Выдай «Планшет» конфигурацию заново" in text


def test_the_lever_decides_what_the_returning_notice_promises():
    assert "возвращаю пользователей и бот" in rn.primary_back(True)
    assert "верни пользователей кнопкой" in rn.primary_back(False)


def test_the_move_failure_notice_mentions_the_reserve_only_when_it_had_taken_the_clone():
    assert "отпустил" not in rn.move_failed("Beta", "x")
    assert "отпустил профили" in rn.move_failed("Beta", "x", released=True)


def test_the_return_failure_says_the_clients_stay_at_the_reserve():
    t = rn.return_failed("Beta", "нет снимка base-7.db")
    assert "остаются на «Beta»" in t and "base-7.db" in t


def test_the_client_notices_tell_what_to_do_and_name_the_forwarding_date():
    moved = rn.client_moved()
    assert "выключи и включи" in moved and "менять не нужно" in moved and "РФ" not in moved
    assert "недоступен из РФ" in rn.client_moved(rf=True)
    back = rn.client_returned("2026-10-28T00:00:00+05:00")
    assert "вернулся на основной" in back and "до " in back and "запасной путь" in back
    assert "пока он работает через запасной путь" in rn.client_returned("")
    assert "запасной путь" in rn.client_returned("не дата"), "битая дата не должна ломать уведомление"
