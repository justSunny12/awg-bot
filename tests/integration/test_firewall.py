"""Файервол в потоке бота: реассерт set устройств админа в точках изменения и
разовое снятие старых ворот только при включённой таблице."""
import pytest

from awgbot.core import config
from awgbot.domain.services import ServiceError
from awgbot.infra import awg, nftguard


def test_admin_device_creation_reconciles_the_admin_set(services, fake_awg, make_active_client):
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    dc = services.add_device(admin.id, "phone")
    assert fake_awg.fw_admin_ips == [dc.address]


def test_nonadmin_device_creation_does_not_touch_the_firewall(services, fake_awg, make_active_client):
    client = make_active_client(name="Клиент", tg_id=777)
    services.add_device(client.id, "phone")
    assert fake_awg.fw_admin_ips is None


def test_reconcile_collects_only_admin_addresses(services, fake_awg, make_active_client):
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    client = make_active_client(name="Клиент", tg_id=777)
    a1 = services.add_device(admin.id, "a1")
    services.add_device(client.id, "c1")
    a2 = services.add_device(admin.id, "a2")
    fake_awg.fw_admin_ips = None
    services.reconcile_ssh_access()
    assert fake_awg.fw_admin_ips == sorted([a1.address, a2.address])


def test_reconcile_runs_even_when_firewall_disabled(services, fake_awg, monkeypatch):
    """Решение «фильтр или только NAT» принимает nftguard, а не вызывающий:
    на хосте с выключенным файерволом таблица всё равно нужна — без неё у
    клиентов нет выхода наружу."""
    monkeypatch.setattr(nftguard, "enabled", lambda: False)
    services.reconcile_ssh_access()
    assert fake_awg.fw_calls == 1


def test_legacy_gate_retired_once_and_only_when_enabled(services, fake_awg, monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "remove_legacy_ssh_gate", lambda: calls.append(1) or True)
    monkeypatch.setattr(nftguard, "enabled", lambda: False)
    services.retire_legacy_ssh_gate()
    assert calls == [], "без новой таблицы старые ворота не трогаем"
    monkeypatch.setattr(nftguard, "enabled", lambda: True)
    services.retire_legacy_ssh_gate()
    services.retire_legacy_ssh_gate()
    assert calls == [1], "снимаются один раз, метка в state"
    assert services.db.get_state("legacy_ssh_gate_removed") == "1"


# ── сервисный слой: включение, подтверждение, правка списка ──────────────────
# Единственная страховка от самозапирания жила без единого теста: экранные
# тесты подменяют эти методы целиком.

@pytest.fixture()
def fw(services, monkeypatch, tmp_path):
    """nftguard подменён на запись действий; настройки — в памяти."""
    from awgbot.core import settings
    store = {"app.firewall.enabled": False, "app.firewall.ssh_allow": []}
    acts: list = []
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(store.get(k, d)))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    monkeypatch.setattr(nftguard, "enabled", lambda: bool(store["app.firewall.enabled"]))
    monkeypatch.setattr(nftguard, "build_spec", lambda ips: "SPEC")
    monkeypatch.setattr(nftguard, "render", lambda spec: "TEXT")
    monkeypatch.setattr(nftguard, "ensure_persistence", lambda: [])
    monkeypatch.setattr(nftguard, "apply_text", lambda t: acts.append(("apply", t)))
    monkeypatch.setattr(nftguard, "arm_rollback", lambda s, c, e: acts.append(("arm", s, c)))
    monkeypatch.setattr(nftguard, "disarm_rollback", lambda: acts.append(("disarm",)) or True)
    monkeypatch.setattr(nftguard, "remove", lambda: acts.append(("remove",)) or ["снято"])
    return store, acts


def test_enable_applies_at_once_without_a_timer(services, fw):
    """Из чата — сразу: чат от SSH не зависит, выключается той же кнопкой.
    Таймер отката остался у CLI, где другого пути кроме терминала нет."""
    store, acts = fw
    store["app.firewall.ssh_allow"] = ["203.0.113.7"]
    assert services.firewall_enable() is None
    assert store["app.firewall.enabled"] is True
    assert [a[0] for a in acts] == ["apply"], "таймера из чата быть не должно"


def test_enable_without_addresses_is_refused(services, fw):
    """Фильтр без адресов открывает SSH всем и не фильтрует ничего — кнопки в
    разделе нет, а сервис не даёт включить и мимо кнопки."""
    store, acts = fw
    with pytest.raises(ServiceError, match="адрес"):
        services.firewall_enable()
    assert store["app.firewall.enabled"] is False and not acts


def test_enable_rolls_the_flag_back_when_the_table_is_refused(services, fw, monkeypatch):
    """Останься enabled=true при неприменённой таблице — следующий тик бота
    применил бы её сам, без человека."""
    store, acts = fw
    store["app.firewall.ssh_allow"] = ["203.0.113.7"]

    def boom(text):
        raise nftguard.GuardError("nft: синтаксис")
    monkeypatch.setattr(nftguard, "apply_text", boom)
    with pytest.raises(ServiceError):
        services.firewall_enable()
    assert store["app.firewall.enabled"] is False


def test_confirm_disarms_and_disable_keeps_nat(services, fw):
    """confirm — для таймера, который поставил CLI; disable снимает его же."""
    store, acts = fw
    store["app.firewall.ssh_allow"] = ["203.0.113.7"]
    services.firewall_enable()
    acts.clear()
    assert services.firewall_confirm() is True
    assert acts == [("disarm",)]
    acts.clear()
    services.firewall_disable()
    assert [a[0] for a in acts] == ["disarm", "remove"], \
        "таймер снимается ДО снятия таблицы, иначе сработает по пустому"
    assert store["app.firewall.enabled"] is False


def test_adding_an_address_never_arms_the_timer(services, fw):
    """Добавление запереть не может — обратный отсчёт на нём был бы пугалкой."""
    store, acts = fw
    store["app.firewall.enabled"] = True
    services.firewall_allow_add("203.0.113.7, home.example.org")
    assert store["app.firewall.ssh_allow"] == ["203.0.113.7", "home.example.org"]
    assert not any(a[0] == "arm" for a in acts)
    # дедупликация и дописывание, а не затирание
    services.firewall_allow_add("203.0.113.7 198.51.100.9")
    assert store["app.firewall.ssh_allow"] == ["203.0.113.7", "home.example.org", "198.51.100.9"]


def test_removing_an_address_applies_at_once(services, fw):
    store, acts = fw
    store["app.firewall.enabled"] = True
    store["app.firewall.ssh_allow"] = ["203.0.113.7", "198.51.100.9"]
    acts.clear()
    left = services.firewall_allow_remove("203.0.113.7")
    assert left == ["198.51.100.9"]
    assert [a[0] for a in acts] == ["apply"]


def test_bad_address_is_refused_before_it_reaches_the_config(services, fw):
    store, acts = fw
    with pytest.raises(ServiceError):
        services.firewall_allow_add("мусор")
    with pytest.raises(ServiceError):
        services.firewall_allow_add("   ")
    assert store["app.firewall.ssh_allow"] == [] and acts == []


def test_ipv6_survives_the_whole_path(services, fw):
    """IPv6 принимается nftguard, значит обязан пережить и запись, и показ, и
    кнопку удаления: раньше он ломал упаковку callback_data, и раздел
    «Файервол» переставал открываться навсегда."""
    from awgbot.bot import keyboards as kb
    store, acts = fw
    store["app.firewall.enabled"] = True
    services.firewall_allow_add("2001:db8::1")
    assert store["app.firewall.ssh_allow"] == ["2001:db8::1"]
    markup = kb.settings_firewall({"raw_allow": ["2001:db8::1"], "enabled": True})
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "➖ 2001:db8::1" in labels


# ── порт SSH из чата ─────────────────────────────────────────────────────────
# Порт живёт в conf и в sshd; сервис меняет оба и не оставляет их разными.

@pytest.fixture()
def sshd_fake(monkeypatch):
    from awgbot.infra import sshd
    st = {"busy": "", "fail": "", "set": []}
    monkeypatch.setattr(sshd, "port_busy", lambda p: st["busy"])

    def set_port(p):
        if st["fail"]:
            raise sshd.SshdError(st["fail"])
        st["set"].append(p)
        return [f"sshd слушает {p}"]
    monkeypatch.setattr(sshd, "set_port", set_port)
    return st


def test_port_change_writes_conf_first_and_then_moves_sshd(services, fw, sshd_fake, monkeypatch):
    from awgbot.core import settings
    store, _ = fw
    store["app.network.ssh_port"] = 22
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    assert services.ssh_port_change(2222) == 22
    assert store["app.network.ssh_port"] == 2222 and sshd_fake["set"] == [2222]


def test_port_change_restores_conf_when_sshd_refuses(services, fw, sshd_fake, monkeypatch):
    from awgbot.core import settings
    store, _ = fw
    store["app.network.ssh_port"] = 22
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    sshd_fake["fail"] = "sshd -t: Bad configuration option"
    with pytest.raises(ServiceError, match="Bad configuration"):
        services.ssh_port_change(2222)
    assert store["app.network.ssh_port"] == 22, "фильтр не должен ждать порт, на котором sshd нет"


def test_busy_and_same_port_are_refused_before_anything_changes(services, fw, sshd_fake, monkeypatch):
    from awgbot.core import settings
    store, _ = fw
    store["app.network.ssh_port"] = 22
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    sshd_fake["busy"] = 'tcp LISTEN 0.0.0.0:8443 users:(("nginx",pid=1,fd=6))'
    assert "nginx" in services.ssh_port_busy(8443)
    with pytest.raises(ServiceError, match="занят"):
        services.ssh_port_change(8443)
    sshd_fake["busy"] = ""
    with pytest.raises(ServiceError, match="текущий"):
        services.ssh_port_change(22)
    with pytest.raises(ServiceError, match="1 до 65535"):
        services.ssh_port_busy(0)
    assert store["app.network.ssh_port"] == 22 and not sshd_fake["set"]


# ── этап 2: владелец sshd_config и порт как факт на сервере ──────────────────

@pytest.fixture()
def sshd_owner_fake(monkeypatch):
    from awgbot.infra import sshd
    st = {"owner": sshd.SshdOwner(), "listening": [22]}
    monkeypatch.setattr(sshd, "owner", lambda: st["owner"])
    monkeypatch.setattr(sshd, "listening_ports", lambda: list(st["listening"]))
    return st


def test_port_change_refused_when_a_generator_owns_the_config(services, fw, sshd_fake, sshd_owner_fake, monkeypatch):
    from awgbot.core import settings
    from awgbot.domain.gwssh import SshOwnerRefusal
    from awgbot.infra import sshd
    store, _ = fw
    store["app.network.ssh_port"] = 22
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    sshd_owner_fake["owner"] = sshd.SshdOwner("generator", "", None, "Ansible managed: do not edit",
                                              ["/etc/ssh/sshd_config"])
    with pytest.raises(SshOwnerRefusal) as ei:
        services.ssh_port_change(2222)
    assert ei.value.owner.kind == "generator" and ei.value.listening == 22
    assert store["app.network.ssh_port"] == 22 and not sshd_fake["set"]


def test_port_drift_warns_once_when_the_bot_owns_the_config(services, fw, sshd_owner_fake, monkeypatch):
    """sshd_config правили руками при включённом фильтре — сервер заперт молча.
    Владелец — бот: предупреждаем, не следуем; один раз на значение."""
    from awgbot.core import settings
    store, _ = fw
    store["app.network.ssh_port"] = 22
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    assert services.ssh_port_drift_notes() == []
    sshd_owner_fake["listening"] = [2222]
    notes = services.ssh_port_drift_notes()
    assert len(notes) == 1 and "слушает порт 2222" in notes[0].text and "держит 22" in notes[0].text
    assert notes[0].critical is True
    assert store["app.network.ssh_port"] == 22, "не следуем"
    assert services.ssh_port_drift_notes() == [], "второй тик молчит"
    sshd_owner_fake["listening"] = [22]
    assert services.ssh_port_drift_notes() == []
    sshd_owner_fake["listening"] = [2222]
    assert len(services.ssh_port_drift_notes()) == 1, "новое расхождение — новое предупреждение"


def test_port_drift_is_followed_when_a_foreign_owner_sets_it(services, fw, sshd_owner_fake, monkeypatch):
    from awgbot.core import settings
    from awgbot.infra import sshd
    store, _ = fw
    store["app.network.ssh_port"] = 22
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    sshd_owner_fake["owner"] = sshd.SshdOwner("generator", "", None, "managed by ansible", ["/etc/ssh/sshd_config"])
    sshd_owner_fake["listening"] = [2222]
    notes = services.ssh_port_drift_notes()
    assert len(notes) == 1 and "22 → 2222" in notes[0].text and "другой процесс" in notes[0].text
    assert store["app.network.ssh_port"] == 2222, "чужой владелец — следуем, как агент"
    assert notes[0].critical is False
