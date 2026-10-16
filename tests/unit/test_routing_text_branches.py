"""Развилки текстов РФ-доступа и карточки шлюза: хвост на главной при
лежащем активном и живом резерве, уведомление при включении после «🟢»,
подсеть вне туннеля, итоги «Применить» счётом, ожидание SMB без канала."""

from __future__ import annotations

from awgbot.bot import texts
from awgbot.infra import routing as rt


def test_dead_active_with_live_standby_tells_what_to_do():
    info = {"ok": False, "active": "NASPi", "active_slot": 1,
            "standby": [{"name": "Pi4", "slot": 2, "state": "alive"}]}
    line = texts.routing_admin_status_line(info)
    assert line.endswith("резерв жив — переключи на резерв в «🛰 Шлюзы»"), line


def test_enable_time_warning_does_not_contradict_the_green_screen():
    """При включении замер идёт после «🟢»: «не отвечает» спорило бы с экраном."""
    w = texts.routing_gateway_warning(rt.PROBE_DOWN, at_start=False, gw="NASPi")
    assert w.startswith("⚠️ Шлюз NASPi не пропускает трафик наружу: "), w
    assert "не отвечает на старте" in texts.routing_gateway_warning(rt.PROBE_DOWN, at_start=True)


def test_a_subnet_without_a_twin_is_marked_outside_the_tunnel():
    from awgbot.bot.texts.routing.slots import lan_pairs
    line = lan_pairs(["192.168.1.0/24", "10.0.2.0/24"], [("192.168.1.0/24", "10.240.1.0/24")])
    assert line == ("<code>192.168.1.0/24</code> · в туннеле <code>10.240.1.0/24</code>; "
                    "<code>10.0.2.0/24</code> · вне туннеля"), line


def test_apply_toasts_count_with_a_noun():
    assert texts.routing_devices_toast({"a": True, "b": False, "c": False}) == \
        "РФ-доступ включён на 1, выключен на 2 устройствах"
    assert texts.routing_allowed_toast({"a": True, "b": True}) == "РФ-доступ разрешён 2 профилям"
    assert texts.routing_allowed_toast({"Боря": True}) == "Профиль Боря: РФ-доступ разрешён"


def test_smb_pending_with_the_channel_down_waits_for_the_link():
    from awgbot.bot.texts.routing import services_line
    line = services_line({"own": 0, "peer": 1, "state": "pending"}, online=False)
    assert line.endswith("\n🟡 Записи SMB уйдут на шлюз, когда он выйдет на связь"), line


def test_domain_example_in_a_refusal_is_monospaced():
    out = texts.routing_add_report([], [("x", "нужен адрес вида sberbank.ru")], 0, 100)
    assert out.endswith("— нужен адрес вида <code>sberbank.ru</code>"), out
