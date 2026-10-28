"""Уведомление о лежащем шлюзе — без подсказки «выпусти свежий файл
конфигурации шлюза»: файл нужен только для первоначальной настройки, параметры
линка меняются только по каналу (решение пользователя 17.10.2026); аварийный
путь описан в README, в разделе неполадок."""
from __future__ import annotations


def test_the_down_alert_has_no_file_hint(services, make_active_client):
    from awgbot.core import config
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    pi = services.add_device(admin.id, "Pi2")
    gw = services.db.gateway_add(pi.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    text = services._txt_rt_gw_down(gw)
    assert text.startswith("🔴 Шлюз") and "gw-bundle" not in text and "файл конфигурации" not in text, text
