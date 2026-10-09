from awgbot.core import settings
def test_pause_day_choice_and_days(services, fake_awg):
    from awgbot.bot import keyboards as kb
    from awgbot.util import timeutil
    from datetime import datetime, timedelta
    # клиент с остатком лимита 10 (используем 4 из 14 single, но total ограничит)
    end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=300))
    cid = services.db.create_client("P", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    services.db.set_pause_balance(cid, settings.get_int("pause.pause_max_total_days", 28))
    avail = services.pause_available_days(cid)
    # клавиатура: если avail<14, кнопки «14 дн.» быть не должно
    labels = [b.text for r in kb.pause_kb(cid, avail).inline_keyboard for b in r]
    if avail < 14:
        assert not any("14 дн." in l for l in labels)
    if avail >= 7:
        assert any("7 дн." in l for l in labels)
    assert any("Другое" in l for l in labels)
    # enter_pause с явным числом
    ok, reserved, _, _ = services.enter_pause(cid, 5)
    assert ok and reserved == 5
    # сверх доступного — капается до доступного
    services.exit_pause(cid, auto=False)
    avail_now = services.pause_available_days(cid)
    ok, reserved, _, _ = services.enter_pause(cid, 9999)
    assert ok and reserved == avail_now


def test_pause_counter_shows_balance_of_type_max(services, fake_awg):
    """Счётчик у админа: накопленное из максимума типа (год — 56, месяц — 24),
    без срока — дни не сгорают; у недели максимума нет — строки нет."""
    from awgbot.bot.texts.admin import _pause_of
    from awgbot.util import timeutil
    from datetime import datetime
    end = timeutil.to_iso(datetime(2027, 3, 15, 12, 0, 0, tzinfo=timeutil.TZ))
    cid = services.db.create_client("X", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    services.db.set_pause_balance(cid, settings.get_int("pause.pause_max_total_days", 28))
    assert _pause_of(services.db.get_client(cid)) == "⏸️ Пауза: 28 из 56 дн."
    services.db.set_pause_balance(cid, 21)
    services.db.update_client_fields(cid, period_kind="month")
    assert _pause_of(services.db.get_client(cid)) == "⏸️ Пауза: 21 из 24 дн."
    services.db.update_client_fields(cid, period_kind="week")
    assert _pause_of(services.db.get_client(cid)) == "", "у недели максимума нет — строки нет"


def test_pause_limit_exhausted_text():
    from awgbot.bot import texts
    assert texts.pause_limit_exhausted() == \
        "Дни паузы на счету закончились — пополнится при продлении подписки"


def test_pause_not_capped_by_subscription_remainder(services, fake_awg):
    """Приостановка НЕ ограничена остатком подписки: даже если до конца 2 дня,
    доступно всё, что позволяет single/total лимит."""
    from awgbot.util import timeutil
    from datetime import datetime, timedelta
    # подписка кончается через 2 дня
    end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=2))
    cid = services.db.create_client("Short", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    services.db.set_pause_balance(cid, settings.get_int("pause.pause_max_total_days", 28))
    avail = services.pause_available_days(cid)
    # не должно быть 2 (остаток подписки) — должно быть весь суммарный лимит
    assert avail == settings.get_int("pause.pause_max_total_days", 28)
    # и реально можно поставить на весь лимит
    ok, reserved, _, _ = services.enter_pause(cid, settings.get_int("pause.pause_max_total_days", 28))
    assert ok and reserved == settings.get_int("pause.pause_max_total_days", 28)
