def test_admin_resume_pause_button_and_exit(services, fake_awg):
    from awgbot.core.blocks import ClientBlock, DeviceBlock
    from awgbot.bot import keyboards as kb
    from awgbot.util import timeutil
    from datetime import datetime, timedelta
    end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=200))
    cid = services.db.create_client("Отпускник", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 5)
    dev = services.add_device(cid, "phone")
    ok, reserved, _, _ = services.enter_pause(cid)
    assert ok and reserved > 0
    c = services.db.get_client(cid)
    assert int(c.block_reason) & int(ClientBlock.PAUSED)
    labels = [b.text for r in kb.admin_client_actions(c).inline_keyboard for b in r]
    assert "▶️ Вывести из приостановки" in labels
    ok, actual, new_end, _ = services.exit_pause(cid, auto=False)
    c2 = services.db.get_client(cid)
    assert not (int(c2.block_reason) & int(ClientBlock.PAUSED))
    d = services.db.get_device(dev.device_id)
    assert not (int(d.block_reason) & int(DeviceBlock.PAUSED))
