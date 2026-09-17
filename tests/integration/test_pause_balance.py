"""Счёт дней паузы (v2.22.0): годовая — 28 за период, ежемесячная — +2 за
своевременное продление до 24; вход списывает, досрочный выход возвращает;
переходы между типами; разовая миграция по истории продлений."""

import pytest

from awgbot.core import settings
from awgbot.core.enums import PauseMode
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def _year(): return settings.get_int("pause.pause_max_total_days", 28)
def _md(): return settings.get_int("pause.monthly_pause_days", 2)


def test_creation_credits_by_kind(services, make_active_client):
    y = make_active_client("Y", tg_id=7501, period_kind="year")
    m = make_active_client("M", tg_id=7502, period_kind="month")
    d = make_active_client("D", tg_id=7503, period_kind="day")
    n = make_active_client("N", tg_id=7504, period_kind="never")
    assert services.pause_available_days(y.id) == _year()
    assert services.pause_available_days(m.id) == _md(), "первый оплаченный месяц — тоже месяц"
    assert services.pause_available_days(d.id) == 0
    assert services.pause_available_days(n.id) == 0 and services.db.get_client(n.id).pause_balance_days == 0


def test_monthly_renewals_accumulate_to_the_cap(services, make_active_client):
    m = make_active_client("M", tg_id=7510, period_kind="month")
    for i in range(1, 15):
        services.extend_period(m.id, "month", keep_remainder=False)
        assert services.pause_available_days(m.id) == min((i + 1) * _md(), 12 * _md())
    assert services.pause_available_days(m.id) == 12 * _md()


def test_expired_or_graced_month_gets_no_credit_on_the_next_renewal(services, make_active_client):
    m = make_active_client("M", tg_id=7520, period_kind="month")
    services.db.update_client_fields(m.id, status="expired",
                                     period_end="2026-09-01T00:00:00+03:00")
    services.extend_period(m.id, "month", keep_remainder=False)
    assert services.pause_available_days(m.id) == _md(), "истёкшая — без бонуса, остаток цел"
    services.extend_period(m.id, "month", keep_remainder=False)
    assert services.pause_available_days(m.id) == 2 * _md(), "следующее своевременное — снова +2"
    services.db.update_client_fields(m.id, grace_used=1)
    services.extend_period(m.id, "month", keep_remainder=False)
    assert services.pause_available_days(m.id) == 2 * _md(), "отсрочка в периоде — без бонуса"


def test_yearly_renewals_accumulate_to_two_even_untimely(services, make_active_client):
    y = make_active_client("Y", tg_id=7525, period_kind="year")
    services.db.update_client_fields(y.id, status="expired", period_end="2026-09-01T00:00:00+03:00")
    services.extend_period(y.id, "year", keep_remainder=False)
    assert services.pause_available_days(y.id) == 2 * _year(), "годовое начисление — безусловное"
    # третье продление копит, но не выше порога: потратил 5 — вернутся ровно 5
    services.db.set_pause_balance(y.id, 2 * _year() - 5)
    services.extend_period(y.id, "year", keep_remainder=False)
    assert services.pause_available_days(y.id) == 2 * _year(), "третье копит не выше порога"
    services.extend_period(y.id, "year", keep_remainder=False)
    assert services.pause_available_days(y.id) == 2 * _year(), "на пороге начислять нечего"


def test_switching_kinds(services, make_active_client):
    m = make_active_client("M", tg_id=7530, period_kind="month")
    services.extend_period(m.id, "year", keep_remainder=False)
    assert services.pause_available_days(m.id) == _md() + _year(), "месяц → год: остаток + годовое"
    y = make_active_client("Y", tg_id=7531, period_kind="year")
    r = services.extend_period(y.id, "month", keep_remainder=False)
    assert services.pause_available_days(y.id) == _year(), \
        "год → месяц: накопленное сверх месячного порога не сгорает"
    assert r.pause.reason == "cap" and r.pause.added == 0, "выше порога не начисляется"
    services.db.set_pause_balance(y.id, 5)
    services.extend_period(y.id, "month", keep_remainder=False)
    assert services.pause_available_days(y.id) == 5 + _md()
    services.extend_period(y.id, "week", keep_remainder=False)
    assert services.pause_available_days(y.id) == 5 + _md(), "неделя: счёт не пополняется, живёт"
    services.extend_period(y.id, "never", keep_remainder=False)
    assert services.pause_available_days(y.id) == 0, "бессрочной останавливать нечего"
    assert services.db.get_client(y.id).pause_balance_days == 0, "у бессрочной счёт всегда ноль"


def test_pause_spends_and_refunds_the_balance(services, fake_awg, make_active_client):
    m = make_active_client("M", tg_id=7540, period_kind="month")
    for _ in range(4):
        services.extend_period(m.id, "month", keep_remainder=False)
    assert services.pause_available_days(m.id) == 5 * _md()          # 10
    ok, reserved, _, _ = services.enter_pause(m.id, 7)
    assert ok and reserved == 7
    assert services.db.get_client(m.id).pause_balance_days == 5 * _md() - 7
    ok, actual, _, _ = services.exit_pause(m.id, auto=False)          # через минуту — 1 день
    assert ok and actual == 1
    assert services.pause_available_days(m.id) == 5 * _md() - 1, "неиспользованный резерв вернулся"
    # админская пауза счёт не трогает
    services.enter_admin_pause(m.id, 3)
    assert services.db.get_client(m.id).pause_balance_days == 5 * _md() - 1
    services.exit_pause(m.id, auto=False)
    assert services.pause_available_days(m.id) == 5 * _md() - 1


def test_migration_from_history(services, fake_awg, make_active_client):
    """Разово после обновления: ежемесячным — по оплаченным месяцам (все
    своевременные), годовым — остаток старого лимита с учётом текущей паузы."""
    m = make_active_client("M", tg_id=7550, period_kind="month")
    services.extend_period(m.id, "month", keep_remainder=False)
    services.extend_period(m.id, "month", keep_remainder=False)
    y = make_active_client("Y", tg_id=7551, period_kind="year")
    services.enter_pause(y.id, 4)                                     # резерв 4 на счету уже списан
    # «до обновления»: счетов не было
    for c in (m, y):
        services.db.set_pause_balance(c.id, 0)
    services.db.update_client_fields(y.id, pause_used_days=3)
    assert services.migrate_pause_balances() == 2
    assert services.db.get_client(m.id).pause_balance_days == 3 * _md()
    assert services.db.get_client(y.id).pause_balance_days == _year() - 3 - 4
    services.db.set_pause_balance(m.id, 0)
    assert services.migrate_pause_balances() == 0, "миграция разовая"
    assert services.db.get_client(m.id).pause_balance_days == 0


def test_open_admin_pause_keeps_balance_and_period_none(services, make_active_client):
    y = make_active_client("Y", tg_id=7560, period_kind="year")
    services.enter_admin_pause(y.id, 0)
    c = services.db.get_client(y.id)
    assert c.period_end is None and c.pause_mode == PauseMode.ADMIN_OPEN
    assert c.pause_balance_days == _year()
    assert services.pause_available_days(y.id) == 0, "на открытой паузе входить некуда"


def test_credit_reasons_and_notifications(services, make_active_client):
    """Владельцу — вторая строка к «Подписка продлена до …»: что стало со
    счётом и почему; админу — та же строка коротко."""
    from awgbot.bot import texts
    m = make_active_client("M", tg_id=7570, period_kind="month")
    r = services.extend_period(m.id, "month", keep_remainder=False)
    own = [n.text for n in r.notifications if n.tg_id == 7570][0]
    assert own.startswith("Подписка продлена до ") and own.endswith(
        "\n⏸ Дней паузы добавлено: +2, доступно 4.")
    assert texts.pause_credit_admin(r.pause) == "Дней паузы: +2 → 4"

    services.db.set_pause_balance(m.id, 23)
    r = services.extend_period(m.id, "month", keep_remainder=False)
    assert r.pause.after == 24 and texts.pause_credit_line(r.pause) == \
        "⏸ Дней паузы добавлено: +1, доступно 24 (максимум для ежемесячной подписки)."
    assert texts.pause_credit_admin(r.pause) == "Дней паузы: +1 → 24 (максимум)"
    r = services.extend_period(m.id, "month", keep_remainder=False)
    assert texts.pause_credit_line(r.pause) == ("⏸ Дни паузы не добавлены: достигнуто максимальное "
                                                "количество для ежемесячной подписки (24).")
    assert texts.pause_credit_admin(r.pause) == "Дней паузы: не добавлены — максимум ежемесячной (24)"

    services.db.update_client_fields(m.id, status="expired", period_end="2026-09-01T00:00:00+03:00")
    services.db.set_pause_balance(m.id, 4)
    r = services.extend_period(m.id, "month", keep_remainder=False)
    assert texts.pause_credit_line(r.pause) == ("⏸ Дни паузы за этот период не начислены: подписка "
                                                "продлена после истечения. Доступно 4 дня.")
    assert texts.pause_credit_admin(r.pause) == "Дней паузы: не начислены — после истечения, доступно 4"
    services.db.update_client_fields(m.id, grace_used=1)
    services.db.set_pause_balance(m.id, 1)
    r = services.extend_period(m.id, "month", keep_remainder=False)
    assert texts.pause_credit_line(r.pause) == ("⏸ Дни паузы за этот период не начислены: в прошлом "
                                                "периоде использована отсрочка. Доступно 1 день.")

    y = make_active_client("Y", tg_id=7571, period_kind="year")
    services.db.set_pause_balance(y.id, 51)
    r = services.extend_period(y.id, "year", keep_remainder=False)
    assert texts.pause_credit_line(r.pause) == "⏸ Дней паузы добавлено: +5, доступно 56 (максимум)."
    r = services.extend_period(y.id, "year", keep_remainder=False)
    assert texts.pause_credit_line(r.pause) == ("⏸ Дни паузы не добавлены: достигнуто максимальное "
                                                "количество для годовой подписки (56).")
    r = services.extend_period(y.id, "never", keep_remainder=False)
    own = [n.text for n in r.notifications if n.tg_id == 7571][0]
    assert own == "Подписка теперь бессрочная 🎉" and texts.pause_credit_line(r.pause) == ""
    d = make_active_client("D", tg_id=7572, period_kind="day")
    r = services.extend_period(d.id, "week", keep_remainder=False)
    assert texts.pause_credit_line(r.pause) == "" and texts.pause_credit_admin(r.pause) == ""


def test_parse_dates_without_time():
    d = timeutil.parse_dt_sec("05.10.2026")
    assert (d.year, d.month, d.day, d.hour, d.minute, d.second) == (2026, 10, 5, 0, 0, 0)
    assert d.tzinfo == timeutil.TZ
    assert timeutil.parse_dt_sec("05.10.2026 18:30").minute == 30
    assert timeutil.parse_dt_sec(" 05.10.2026  18:30:15 ").second == 15
    with pytest.raises(ValueError):
        timeutil.parse_dt_sec("2026-10-05")
