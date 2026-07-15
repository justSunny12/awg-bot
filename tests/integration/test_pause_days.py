from awgbot.core import settings
def test_pause_day_choice_and_days(services, fake_awg):
    from awgbot.bot import keyboards as kb
    from awgbot.util import timeutil
    from datetime import datetime, timedelta
    # клиент с остатком лимита 10 (используем 4 из 14 single, но total ограничит)
    end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=300))
    cid = services.db.create_client("P", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    avail = services.pause_available_days(cid)
    # клавиатура: если avail<14, кнопки «14 дн.» быть не должно
    labels = [b.text for r in kb.pause_day_choice(cid, avail).inline_keyboard for b in r]
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


def test_pause_counter_shows_period_end(services, fake_awg):
    """Пункт 3: счётчик приостановки содержит дату конца подписки."""
    from awgbot.bot import texts
    from awgbot.util import timeutil
    from datetime import datetime
    end = timeutil.to_iso(datetime(2027, 3, 15, 12, 0, 0, tzinfo=timeutil.TZ))
    cid = services.db.create_client("X", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    c = services.db.get_client(cid)
    block = texts.subscription_block(c, for_admin=True)
    line = [l for l in block.split("\n") if "Приостановка" in l][0]
    assert "до 15.03.2027" in line


def test_pause_limit_exhausted_text():
    from awgbot.bot import texts
    assert texts.pause_limit_exhausted() == "Лимит дней приостановки в текущем периоде исчерпан."


def test_pause_not_capped_by_subscription_remainder(services, fake_awg):
    """Приостановка НЕ ограничена остатком подписки: даже если до конца 2 дня,
    доступно всё, что позволяет single/total лимит."""
    from awgbot.util import timeutil
    from datetime import datetime, timedelta
    # подписка кончается через 2 дня
    end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=2))
    cid = services.db.create_client("Short", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    avail = services.pause_available_days(cid)
    # не должно быть 2 (остаток подписки) — должно быть весь суммарный лимит
    import awgbot.core.config as cfg
    assert avail == settings.get_int("pause.pause_max_total_days", 28)
    # и реально можно поставить на весь лимит
    ok, reserved, _, _ = services.enter_pause(cid, settings.get_int("pause.pause_max_total_days", 28))
    assert ok and reserved == settings.get_int("pause.pause_max_total_days", 28)


def test_email_resume_code_cycle(services, fake_awg):
    """Email-выход: код генерится при входе, снимает паузу, обнуляется (replay)."""
    from awgbot.core.blocks import ClientBlock
    from awgbot.util import timeutil
    from datetime import datetime, timedelta
    end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=200))
    cid = services.db.create_client("Vac", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    ok, reserved, notes, code = services.enter_pause(cid, 7)
    assert ok and code and len(code) == 8
    assert services.db.find_client_by_resume_code(code) == cid
    assert services.db.find_client_by_resume_code("WRONGXXX") is None
    ok2, notes2 = services.resume_by_email_code(code)
    assert ok2
    c = services.db.get_client(cid)
    assert not (int(c.block_reason) & int(ClientBlock.PAUSED))
    assert services.db.find_client_by_resume_code(code) is None   # replay-защита


def test_email_resume_wrong_code_noop(services, fake_awg):
    """Неизвестный код — no-op (ok=False), паузу не трогает."""
    ok, notes = services.resume_by_email_code("NEVERSET")
    assert ok is False and notes == []


def test_resume_code_safe_alphabet():
    """Код без похожих символов (0/O, 1/l/I)."""
    from awgbot.infra import email_resume
    for _ in range(50):
        c = email_resume.generate_code()
        assert not (set(c) & set("0O1lI"))


def test_friend_panel_hides_pause_counter(services, fake_awg):
    """Друг не видит счётчик приостановки — он ей не управляет."""
    from awgbot.bot import texts
    from awgbot.util import timeutil
    from datetime import datetime, timedelta
    end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=200))
    cid = services.db.create_client("Host", 2, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    c = services.db.get_client(cid)
    assert "Приостановка" in texts.subscription_block(c, for_admin=True)   # админ видит
    assert "Приостановка" not in texts.subscription_block(c, show_pause=False)  # друг нет
