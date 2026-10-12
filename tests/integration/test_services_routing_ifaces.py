"""Условная маршрутизация при переезде профилей: бот переживает
существование второго интерфейса.
"""
import pytest

pytestmark = pytest.mark.integration


# ── переезд профилей: бот переживает существование второго интерфейса ─────────

def _iface_env(monkeypatch, services, second="awg1"):
    """Второй интерфейс задан конфигом; conf'ы обоих подменены в памяти."""
    from awgbot.core import config as cfg
    from awgbot.infra import awg as infra_awg
    monkeypatch.setattr(cfg, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(cfg, "AWG_DIR", "/opt/awg")
    monkeypatch.setattr(cfg, "CONF_PATH", "/opt/awg/awg0.conf")
    monkeypatch.setattr(cfg, "MIGRATION_INTERFACE", second)
    return infra_awg


def test_second_interface_does_not_orphan_its_peers(services, make_active_client,
                                                    monkeypatch):
    """Пиры соседнего интерфейса НЕ считаются пропавшими.

    Сверка читала один конфиг и сверяла его со всеми устройствами базы. Подними
    второй интерфейс — и каждый живущий на нём пир становится «пропавшим», а
    через MISSING_SWEEPS_THRESHOLD сверок бот удаляет его сам: молча,
    необратимо и у всех сразу. Это главный риск переезда.
    """
    from awgbot.core import config as cfg
    infra_awg = _iface_env(monkeypatch, services)
    c = make_active_client(name="c", tg_id=8100)
    dc = services.add_device(c.id, "Телефон")
    services.db.update_device_fields(dc.device_id, iface="awg1")
    dev = services.db.get_device(dc.device_id)

    confs = {
        "/opt/awg/awg0.conf": "[Interface]\nPrivateKey = X\n",
        "/opt/awg/awg1.conf": (f"[Interface]\nPrivateKey = Y\n\n[Peer]\n"
                               f"PublicKey = {dev.public_key}\n"
                               f"AllowedIPs = {dev.address}/32\n"),
    }
    monkeypatch.setattr(infra_awg, "read_file", lambda p: confs[p])
    monkeypatch.setattr(infra_awg, "read_server_params",
                        lambda force=False, iface=None: {"psk": "PSK=="})

    for _ in range(cfg.MISSING_SWEEPS_THRESHOLD + 2):
        services.reconcile_peers()
    assert services.db.get_device(dc.device_id) is not None, "устройство удалено сверкой"
    assert services.db.get_device(dc.device_id).missing_count == 0


def test_unreadable_interface_does_not_accumulate_misses(services, make_active_client,
                                                         monkeypatch):
    """Интерфейс задан, но конфига нет (ещё не подняли, лежит) — пропускаем
    ЦЕЛИКОМ, не трогая missing_count.

    Отсутствие конфига — это «не знаю», а не «пиров нет». Накапливать по нему
    пропажи значит готовить то же самое удаление, только медленнее.
    """
    from awgbot.core import config as cfg
    infra_awg = _iface_env(monkeypatch, services)
    c = make_active_client(name="c", tg_id=8101)
    dc = services.add_device(c.id, "Телефон")
    services.db.update_device_fields(dc.device_id, iface="awg1")

    def only_default(path):
        if path != "/opt/awg/awg0.conf":
            raise infra_awg.AwgError("нет такого файла")
        return "[Interface]\nPrivateKey = X\n"

    monkeypatch.setattr(infra_awg, "read_file", only_default)
    monkeypatch.setattr(infra_awg, "read_server_params",
                        lambda force=False, iface=None: {"psk": "PSK=="})

    for _ in range(cfg.MISSING_SWEEPS_THRESHOLD + 2):
        services.reconcile_peers()
    assert services.db.get_device(dc.device_id) is not None
    assert services.db.get_device(dc.device_id).missing_count == 0, \
        "по нечитаемому конфигу копятся пропажи"


def test_unknown_peer_is_quarantined_with_its_own_interface(services, monkeypatch):
    """Карантинная запись обязана помнить, ГДЕ живёт пир: снимать его пойдут по
    этому полю, и с чужого конфига снять не выйдет."""
    infra_awg = _iface_env(monkeypatch, services)
    pub = "C" * 43 + "="
    confs = {
        "/opt/awg/awg0.conf": "[Interface]\nPrivateKey = X\n",
        "/opt/awg/awg1.conf": (f"[Interface]\nPrivateKey = Y\n\n[Peer]\n"
                               f"PublicKey = {pub}\nAllowedIPs = 10.9.1.7/32\n"),
    }
    monkeypatch.setattr(infra_awg, "read_file", lambda p: confs[p])
    monkeypatch.setattr(infra_awg, "read_server_params",
                        lambda force=False, iface=None: {"psk": "PSK=="})

    notes = services.reconcile_peers()
    assert any("10.9.1.7" in n.text for n in notes), "тревоги о чужом пире не было"
    dev = [d for d in services.db.list_all_devices() if d.public_key == pub][0]
    assert dev.iface == "awg1"


def test_traffic_poll_covers_every_interface(services, make_active_client, monkeypatch):
    """Счётчики живут в ядре по интерфейсам: опрос одного не увидит пиров другого.

    Без обхода всех у непереехавших замерло бы потребление, лимиты перестали бы
    срабатывать, а недоучтённое потерялось бы вместе со старым интерфейсом.
    """
    infra_awg = _iface_env(monkeypatch, services)
    c = make_active_client(name="c", tg_id=8102)
    old = services.add_device(c.id, "Старое")
    new = services.add_device(c.id, "Новое")
    services.db.update_device_fields(new.device_id, iface="awg1")
    o, n = services.db.get_device(old.device_id), services.db.get_device(new.device_id)

    dumps = {
        "awg0": [{"public_key": o.public_key, "rx": 100, "tx": 200, "last_handshake": 0}],
        "awg1": [{"public_key": n.public_key, "rx": 300, "tx": 400, "last_handshake": 0}],
    }
    asked: list = []
    monkeypatch.setattr(infra_awg, "show_dump",
                        lambda iface=None: asked.append(iface) or dumps[iface])

    services.poll_traffic()          # первый проход — базовые точки
    dumps["awg0"][0].update(rx=150, tx=250)
    dumps["awg1"][0].update(rx=500, tx=600)
    services.poll_traffic()

    assert set(asked) == {"awg0", "awg1"}
    assert services.db.get_device(old.device_id).traffic_rx_month == 50
    assert services.db.get_device(new.device_id).traffic_rx_month == 200, \
        "трафик соседнего интерфейса не учтён"
