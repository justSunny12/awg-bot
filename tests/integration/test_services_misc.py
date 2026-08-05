"""Integration: прочие методы services — вьюхелперы, удаление/перепривязка
устройств, предпросчёт паузы, перезапуск сервиса, слоты и онлайн.
"""
import pytest

from awgbot.core.blocks import DeviceBlock
from awgbot.domain.services import LimitReached, ServiceError
from awgbot.infra import awg
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def test_has_free_slot_variants(services, make_active_client):
    unlimited = make_active_client(tg_id=1100, device_limit=0)
    assert services.has_free_slot(unlimited.id) is True
    limited = make_active_client(tg_id=1101, device_limit=1)
    assert services.has_free_slot(limited.id) is True
    services.add_device(limited.id, "d")
    assert services.has_free_slot(limited.id) is False
    assert services.has_free_slot(999999) is False


def test_device_slots_and_remaining(services, make_active_client):
    client = make_active_client(tg_id=1102, device_limit=3, period_kind="year")
    services.add_device(client.id, "d")
    assert services.device_slots(client.id) == (1, 3)
    assert services.device_slots(999999) == (0, 0)
    assert services.remaining_for(client.id) > 0
    never = make_active_client(tg_id=1103, period_kind="never")
    assert services.remaining_for(never.id) == 0


def test_is_only_device_and_count_unassigned(services, make_active_client):
    client = make_active_client(tg_id=1104)
    d1 = services.add_device(client.id, "a")
    assert services.is_only_device(d1.device_id) is True
    services.add_device(client.id, "b")
    assert services.is_only_device(d1.device_id) is False
    assert services.is_only_device(999999) is False
    svc = services.db.get_service_client_id()
    services.db.create_device(svc, "app", "PUBX", "PSK", "10.8.0.40")
    assert services.count_unassigned_devices() == 1


def test_client_is_online_by_handshake(services, make_active_client):
    client = make_active_client(tg_id=1105)
    dc = services.add_device(client.id, "d")
    assert services.client_is_online(client.id) is False
    services.db.update_device_fields(dc.device_id, last_handshake=int(timeutil.now().timestamp()))
    assert services.client_is_online(client.id) is True


def test_ensure_admin_client_idempotent(services):
    a = services.ensure_admin_client()
    b = services.ensure_admin_client()
    assert a == b
    assert services.admin_client() is not None


def test_generate_config_bot_ok_app_fails(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1106)
    dc = services.add_device(client.id, "d")
    cfg = services.generate_config(dc.device_id)
    assert cfg["vpn"].startswith("vpn://") and cfg["conf"]
    app_id = services.db.create_device(client.id, "app", "PUBA", "PSK", "10.8.0.41", private_key=None)
    with pytest.raises(ServiceError):
        services.generate_config(app_id)


def test_remove_device_returns_friend_and_clears_drop(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1107)
    dc = services.add_device(client.id, "d")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=91107)
    services._device_set_block(dc.device_id, DeviceBlock.ADMIN_SILENT)
    friend_tg = services.remove_device(dc.device_id)
    assert friend_tg == 91107
    assert services.db.get_device(dc.device_id) is None
    assert dc.address not in fake_awg.blocked


def test_remove_device_no_friend_returns_none(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1108)
    dc = services.add_device(client.id, "d")
    assert services.remove_device(dc.device_id) is None
    assert services.remove_device(999999) is None


def test_remove_device_peer_failure_raises(services, fake_awg, make_active_client, monkeypatch):
    client = make_active_client(tg_id=1109)
    dc = services.add_device(client.id, "d")

    def boom(pub):
        raise awg.AwgError("нет связи")
    monkeypatch.setattr(awg, "remove_peer", boom, raising=False)
    with pytest.raises(ServiceError):
        services.remove_device(dc.device_id)
    assert services.db.get_device(dc.device_id) is not None


def test_reassign_basic_moves_device(services, fake_awg, make_active_client):
    a = make_active_client(tg_id=1110, device_limit=3)
    b = make_active_client(tg_id=1111, device_limit=3)
    dc = services.add_device(a.id, "d")
    res = services.reassign_device(dc.device_id, b.id)
    assert services.db.get_device(dc.device_id).client_id == b.id
    assert res["donor"]["tg_id"] == 1110 and res["recipient"]["tg_id"] == 1111
    assert res["added_slot"] is False


def test_reassign_no_slot_raises(services, fake_awg, make_active_client):
    a = make_active_client(tg_id=1112, device_limit=3)
    b = make_active_client(tg_id=1113, device_limit=1)
    services.add_device(b.id, "occupied")
    dc = services.add_device(a.id, "d")
    with pytest.raises(LimitReached):
        services.reassign_device(dc.device_id, b.id)


def test_reassign_add_slot_bumps_limit(services, fake_awg, make_active_client):
    a = make_active_client(tg_id=1114, device_limit=3)
    b = make_active_client(tg_id=1115, device_limit=1)
    services.add_device(b.id, "occupied")
    dc = services.add_device(a.id, "d")
    res = services.reassign_device(dc.device_id, b.id, add_slot=True)
    assert res["added_slot"] is True
    assert services.db.get_client(b.id).device_limit == 2


def test_reassign_add_slot_unlimited_not_bumped(services, fake_awg, make_active_client):
    a = make_active_client(tg_id=1116, device_limit=3)
    b = make_active_client(tg_id=1117, device_limit=0)
    dc = services.add_device(a.id, "d")
    res = services.reassign_device(dc.device_id, b.id, add_slot=True)
    assert res["added_slot"] is False
    assert services.db.get_client(b.id).device_limit == 0


def test_reassign_from_service_donor_is_none(services, fake_awg, make_active_client):
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "app", "PUBR", "PSK", "10.8.0.42")
    b = make_active_client(tg_id=1118, device_limit=3)
    res = services.reassign_device(did, b.id)
    assert res["donor"] is None


def test_preview_exit_pause_states(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1119, period_kind="year")
    assert services.preview_exit_pause(client.id) is None
    ok, reserved, _, _ = services.enter_pause(client.id)
    assert ok
    actual, rsv = services.preview_exit_pause(client.id)
    assert rsv == reserved and actual >= 0


def test_preview_exit_pause_admin_open(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1120, period_kind="year")
    services.enter_admin_pause(client.id, 0)
    actual, rsv = services.preview_exit_pause(client.id)
    assert rsv == 0


def test_restart_service_reapplies_blocks(services, fake_awg, make_active_client, monkeypatch):
    client = make_active_client(tg_id=1121)
    dc = services.add_device(client.id, "d")
    services._device_set_block(dc.device_id, DeviceBlock.EXPIRY)
    monkeypatch.setattr(awg, "restart_server", lambda: None, raising=False)
    fake_awg.blocked.discard(dc.address)
    services.restart_service()
    assert dc.address in fake_awg.blocked


def test_admin_client_cannot_be_blocked(services, fake_awg, make_active_client):
    """Клиент админа не блокируется (defense-in-depth в сервисе)."""
    from awgbot.core import config
    from awgbot.core.blocks import ClientBlock
    admin = make_active_client(tg_id=config.ADMIN_ID, name="Админ")
    notes = services.block_client_manual(admin.id, ClientBlock.ADMIN_SILENT, notify=False)
    assert notes == []
    fresh = services.db.get_client(admin.id)
    assert int(fresh.block_reason) == 0        # не заблокирован


def test_admin_client_keyboard_has_no_dangerous_buttons(make_active_client):
    from awgbot.bot import keyboards as kb
    from awgbot.core import config

    class _C:
        id = 1; activation_status = "active"; block_reason = 0
        tg_id = config.ADMIN_ID
    m = kb.admin_client_actions(_C(), has_devices=True, is_admin_owner=True)
    labels = " ".join(b.text for row in m.inline_keyboard for b in row)
    for forbidden in ("Удалить", "Лимит", "Продлить", "лок"):   # блок/Блок/…
        assert forbidden not in labels, f"кнопка '{forbidden}' не должна быть у админ-клиента"
    assert "Имя" in labels and "Устройства" in labels


def test_rename_device_writes_db_only(services, fake_awg):
    """Имя живёт только в нашей БД: сервер про имена не знает."""
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "old", "PUBRN", "PSK", "10.8.1.5")
    services.rename_device(did, "Новое имя")
    assert services.db.get_device(did).name == "Новое имя"


def test_rename_device_empty_rejected(services, fake_awg):
    from awgbot.domain.services import ServiceError
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "old", "PUBRN2", "PSK", "10.8.1.6")
    with pytest.raises(ServiceError):
        services.rename_device(did, "   ")


def test_rename_device_survives_server_down(services, fake_awg, monkeypatch):
    """Сервер недоступен → имя в БД всё равно меняется: оно только наше."""
    from awgbot.infra import awg
    def _boom(*a, **k):
        raise awg.AwgError("awg down")
    monkeypatch.setattr(awg, "apply_config", _boom, raising=False)
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "old", "PUBRN3", "PSK", "10.8.1.7")
    services.rename_device(did, "БезСервера")                # не падает
    assert services.db.get_device(did).name == "БезСервера"


def test_is_only_device_false_for_service_pool(services, fake_awg):
    """Устройство «без профиля» (служебный клиент) не считается «единственным» —
    предупреждение о потере доступа к боту для него неприменимо."""
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "app", "PUBSRV", "PSK", "10.8.1.5")
    assert services.is_only_device(did) is False


def test_is_only_device_true_for_sole_client_device(services, fake_awg, make_active_client):
    """Единственное устройство обычного клиента — предупреждение уместно."""
    cl = make_active_client(tg_id=9100)
    dc = services.add_device(cl.id, "phone")
    assert services.is_only_device(dc.device_id) is True


def test_set_subscription_dates_heals_never_deadlock(services, fake_awg):
    """Дедлок: бессрочную (period_end=None) можно сделать срочной через прямую
    правку дат; status пересчитывается, period_kind сохраняется."""
    from awgbot.domain.services import SubStatus
    from awgbot.util import timeutil
    from datetime import datetime
    cid = services.db.create_client("p", 1, timeutil.now_iso(), None, "c", period_kind="never")
    services.db.activate_client("c", 900)
    ns = datetime(2025, 10, 10, 15, 33, 21, tzinfo=timeutil.TZ)
    ne = datetime(2026, 10, 10, 15, 33, 21, tzinfo=timeutil.TZ)
    s, e, _ = services.set_subscription_dates(cid, ns, ne)
    c = services.db.get_client(cid)
    assert c.period_end is not None                 # дедлок вылечен
    assert c.status == SubStatus.ACTIVE             # будущая дата
    assert c.period_kind == "never"                 # kind не тронут


def test_set_subscription_dates_past_end_expired(services, fake_awg):
    from awgbot.domain.services import SubStatus
    from awgbot.util import timeutil
    from datetime import datetime
    cid = services.db.create_client("p2", 1, timeutil.now_iso(),
                                    timeutil.to_iso(datetime(2030, 1, 1, tzinfo=timeutil.TZ)),
                                    "c2", period_kind="year")
    services.db.activate_client("c2", 901)
    services.set_subscription_dates(cid, datetime(2024, 1, 1, tzinfo=timeutil.TZ),
                                    datetime(2024, 6, 1, tzinfo=timeutil.TZ))
    assert services.db.get_client(cid).status == SubStatus.EXPIRED


def test_set_subscription_dates_reactivation_unblocks_devices(services, fake_awg):
    """Ревью-фикс: expired→active через правку дат снимает EXPIRY-блок с устройств."""
    from awgbot.domain.services import SubStatus
    from awgbot.core.blocks import DeviceBlock, ClientBlock
    from awgbot.util import timeutil
    from datetime import datetime
    cid = services.db.create_client("rb", 1, timeutil.now_iso(),
                                    timeutil.to_iso(datetime(2024, 1, 1, tzinfo=timeutil.TZ)),
                                    "crb", period_kind="year")
    services.db.activate_client("crb", 950)
    dc = services.add_device(cid, "phone")
    # эмулируем истечение: блокируем как watchdog
    fresh = services.db.get_client(cid)
    services._block_client(fresh)
    services.db.update_client_fields(cid, status=SubStatus.EXPIRED)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.EXPIRY)
    # реактивируем правкой дат на будущее
    services.set_subscription_dates(cid, datetime(2025, 1, 1, tzinfo=timeutil.TZ),
                                    datetime(2030, 1, 1, tzinfo=timeutil.TZ))
    assert not (int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.EXPIRY))
    assert services.db.get_client(cid).status == SubStatus.ACTIVE


def test_set_subscription_dates_forever(services, fake_awg):
    """Новая семантика: new_end=None → бессрочная (period_end=NULL, active)."""
    from awgbot.domain.services import SubStatus
    from awgbot.util import timeutil
    from datetime import datetime
    cid = services.db.create_client("pf", 1,
                                    timeutil.to_iso(datetime(2025, 1, 1, tzinfo=timeutil.TZ)),
                                    timeutil.to_iso(datetime(2026, 1, 1, tzinfo=timeutil.TZ)),
                                    "cf", period_kind="year")
    services.db.activate_client("cf", 960)
    s, e, notes = services.set_subscription_dates(
        cid, datetime(2025, 1, 1, tzinfo=timeutil.TZ), None)
    c = services.db.get_client(cid)
    assert c.period_end is None            # бессрочная
    assert c.status == SubStatus.ACTIVE
    assert e is None                        # отчёт покажет «бессрочно»


def test_client_created_report_variants():
    """Констатирующий результат создания: лимиты + срок, без секунд в дате."""
    from awgbot.bot import texts
    from awgbot.util import timeutil
    from datetime import datetime
    end = datetime(2027, 3, 15, 14, 30, 45, tzinfo=timeutil.TZ)
    r = texts.client_created_report("X", device_limit=3, traffic_limit_bytes=50 * 1024**3,
                                    period_kind="year", period_end=end)
    assert "до 3 устройств" in r and "до 50 ГБ" in r
    assert "подписка на год до 15.03.2027 14:30" in r and ":45" not in r   # без секунд
    assert "Повторный выпуск приглашения" in r
    # безлимиты + бессрочно
    r2 = texts.client_created_report("Y", device_limit=0, traffic_limit_bytes=0,
                                     period_kind="never", period_end=None)
    assert "количество устройств не ограничено" in r2
    assert "потребление не ограничено" in r2 and "бессрочная подписка" in r2
