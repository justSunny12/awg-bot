"""Подсказка в уведомлении о лежащем шлюзе: обе команды — выпустить файл
конфигурации шлюза на сервере AWG и применить его на шлюзе. Кнопки выпуска
файла для того же слота нет, и без команды подсказка вела в никуда."""
from __future__ import annotations


def test_the_hint_names_both_commands_for_the_slot(services, make_active_client):
    from awgbot.core import config
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    pi = services.add_device(admin.id, "Pi2")
    gw = services.db.gateway_add(pi.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    text = services._txt_rt_gw_down(gw)
    assert ("выпусти свежий файл конфигурации шлюза на сервере AWG (<code>awg-bot gw-bundle --link awglink2</code>) "
            "и примени его на шлюзе (<code>sudo sh awg-gw-bundle-awglink2.sh</code>)") in text, text
    assert "--install" not in text and not text.endswith(".")


def test_without_a_slot_the_hint_keeps_placeholders():
    from awgbot.domain.services import Services
    text = Services._txt_rt_bundle_hint(Services.__new__(Services))
    assert "<code>awg-bot gw-bundle --link &lt;интерфейс&gt;</code>" in text
    assert "<code>sudo sh &lt;файл&gt;</code>" in text
