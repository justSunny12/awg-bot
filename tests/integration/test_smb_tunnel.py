"""
«SMB в туннеле» на сервере AWG: какой DNS получают конфиги устройств,
файл записей резолвера админа из SMB всех слотов, тумблер и его отказы,
монитор резолвера админа, старт бота и приём `svc` каналом.

БД настоящая, awg-слой фейковый; скрипт резолвера админа подменён
`_AdminDns` — он ведёт «конфиг» (по нему бот решает, поставлен ли резолвер)
и запоминает, какие записи ему отдали.

Цена ошибки: адрес резолвера админа в конфиге клиента — клиент без DNS (его
:53 закрыт файерволом); у шлюзового устройства — DNS всей локальной сети за
шлюзом идёт в резолвер, которому шлюз не админ; записи замолчавшего слота —
Finder ведёт на сервер, до которого маршрута нет; скрипт на каждом такте —
рестарт экземпляра и сброшенный кэш у админа раз в минуту.
"""
from __future__ import annotations

import datetime

import pytest

from awgbot.core import config, settings
from awgbot.domain import gwservices
from awgbot.domain.services import ServiceError
from awgbot.infra import admindns, resolver
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
ADDR = "10.255.53.53"
LAN1, LAN2 = "192.168.1.0/24", "192.168.68.0/24"
NAS = {"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.1.10"}
NAS2 = {"t": "_smb._tcp", "n": "Backup", "h": "backup", "p": 445, "a": "192.168.68.20"}
SPLIT = "10.8.1.0/24"


class _AdminDns:
    """Резолвер админа на подставном хосте: install пишет конфиг (installed()
    видит его), records запоминает текст; fail — режим, который откажет."""

    def __init__(self, monkeypatch, tmp_path, installed=True):
        self.conf = tmp_path / "admin-dns.conf"
        self.calls: list[tuple[str, str]] = []
        self.records: str | None = None
        self.fail = ""
        self.alive = True
        monkeypatch.setattr(admindns, "CONF_PATH", self.conf)
        monkeypatch.setattr(admindns, "run", self._run)
        monkeypatch.setattr(admindns, "probe", lambda timeout=2.0: self.alive)
        monkeypatch.setattr(admindns, "service_active", lambda: self.alive)
        # перехват :53 обвязки (iptables) — журналом, а не на хосте
        self.exempt: list[list[str]] = []
        monkeypatch.setattr(admindns, "nat_exempt", lambda subnets: self.exempt.append(list(subnets)) or [])
        # свой резолвер клиентов стоит (по умолчанию); тест может убрать
        monkeypatch.setattr(resolver, "installed", lambda: True)
        monkeypatch.setattr(resolver, "listen_addrs", lambda path=None: ["10.8.1.1"])
        if installed:
            self.conf.write_text(f"listen-address={ADDR}\nserver=10.8.1.1\n", encoding="utf-8")

    def _run(self, mode, arg=""):
        if mode == "records" and arg != "-":
            with open(arg, encoding="utf-8") as f:
                arg = f.read()
        self.calls.append((mode, arg))
        if self.fail == mode:
            raise admindns.AdminDnsError("подставной отказ")
        if mode == "install":
            self.conf.write_text(f"listen-address={ADDR}\nserver={arg}\n", encoding="utf-8")
        if mode == "records":
            self.records = None if arg == "-" else arg
        return ""

    def modes(self) -> list[str]:
        return [m for m, _ in self.calls]


@pytest.fixture()
def adm(monkeypatch, tmp_path):
    return _AdminDns(monkeypatch, tmp_path)


@pytest.fixture()
def toggle(monkeypatch):
    """Тумблер «SMB в туннеле» — словарь; прочие настройки — настоящие."""
    store = {"on": False}
    real_get, real_set = settings.get, settings.set_value

    def get(k, d=None):
        return store["on"] if k == "app.routing.smb_tunnel" else real_get(k, d)

    def set_value(k, v):
        if k == "app.routing.smb_tunnel":
            store["on"] = v
            return [k]
        return real_set(k, v)
    monkeypatch.setattr(settings, "get", get)
    monkeypatch.setattr(settings, "set_value", set_value)
    return store


@pytest.fixture()
def host(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "SUBNET_PREFIX", "10.8.1")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")


@pytest.fixture()
def two_slots(services, make_active_client, host):
    """Админ с двумя шлюзами: у NASPi сеть 192.168.1.0/24, у Pi2 — 192.168.68.0/24."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.gateway_set_lan_subnets(1, LAN1)
    services.gateway_set_lan_subnets(2, LAN2)
    return admin


def _seen(services, slot: int, hours: float) -> None:
    at = timeutil.now() - datetime.timedelta(hours=hours)
    services.db.set_state(services._gwlink_key(services._GWLINK_SEEN_KEY, slot), timeutil.to_iso(at))


def _lines(conf: str, key: str) -> list[str]:
    return [ln.split("=", 1)[1].strip() for ln in conf.splitlines() if ln.startswith(f"{key} =")]


# ── конфиги устройств ────────────────────────────────────────────────────────

def test_a_new_admin_device_gets_the_admin_resolver_and_the_space(services, make_active_client, adm, host,
                                                                   monkeypatch):
    """Раздельный туннель: DNS — резолвер админа в оба поля, в AllowedIPs —
    пространство адресов в туннеле (10.240.0.0/12): оно ведёт в туннель и
    адреса сетей шлюзов, и резолвер админа (он в служебной /16 пространства) —
    отдельный /32 не нужен. Тумблер выключен — всё равно: конфиг, выданный до
    включения, увидит имена сразу после него."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    created = services.add_device(admin.id, "MacBook")
    for conf in (created.conf, services.generate_config(created.device_id)["conf"]):
        assert _lines(conf, "DNS") == [f"{ADDR}, {ADDR}"], conf
        assert _lines(conf, "AllowedIPs") == [f"{SPLIT}, 10.240.0.0/12"], conf


def test_a_broken_space_leaves_the_admin_device_only_the_resolver_32(services, make_active_client, adm, host,
                                                                       monkeypatch):
    """Адреса в туннеле выключены неисправным пространством — строки
    пространства в конфиге нет, резолверу админа — свой /32."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    monkeypatch.setattr(config, "ROUTING_LAN_SPACE", "10.0.0.0/12")
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    created = services.add_device(admin.id, "MacBook")
    assert _lines(created.conf, "AllowedIPs") == [f"{SPLIT}, {ADDR}/32"], created.conf


def test_a_client_device_keeps_the_common_dns(services, make_active_client, adm, host, monkeypatch):
    """:53 резолвера админа клиентам закрыт файерволом — с его адресом в конфиге
    клиент остался бы без DNS."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    client = make_active_client(name="Ксюша", tg_id=2001)
    created = services.add_device(client.id, "Телефон")
    for conf in (created.conf, services.generate_config(created.device_id)["conf"]):
        assert ADDR not in conf, conf
        assert _lines(conf, "AllowedIPs") == [SPLIT]


def test_the_gateway_device_of_the_admin_keeps_the_common_dns(services, two_slots, adm, monkeypatch):
    """DNS шлюзового устройства — апстрим dnsmasq шлюза для всей его локальной
    сети: шлюз не в наборе admin4 по смыслу, и имена SMB ему ни к чему."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    gw = services.db.gateway(1)
    conf = services.generate_config(gw.device_id, for_bundle=True)["conf"]
    assert ADDR not in conf, "шлюзовому устройству — резолвер админа"
    assert _lines(conf, "AllowedIPs") == [SPLIT]


def test_without_the_admin_resolver_the_admin_device_still_gets_the_space(services, make_active_client, host,
                                                                  monkeypatch, tmp_path):
    """docker-режим или резолвер клиентов не свой — резолвера админа нет:
    DNS прежний, а строка пространства адресов в туннеле от резолвера не
    зависит — она есть."""
    _AdminDns(monkeypatch, tmp_path, installed=False)
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    created = services.add_device(admin.id, "MacBook")
    assert ADDR not in created.conf and _lines(created.conf, "AllowedIPs") == [f"{SPLIT}, 10.240.0.0/12"]


# ── файл записей ─────────────────────────────────────────────────────────────

def test_records_of_both_slots_reach_the_admin_resolver_through_the_line_whitelist(services, two_slots,
                                                                                   adm, toggle):
    """Тумблер включён — записи SMB обеих сетей под awg.internal в адресах в
    туннеле (слот 1 — 10.240.x.x, слот 2 — 10.241.x.x), обратная зона —
    подсеть клиентов (по ней Mac находит домен обзора); каждая строка — из
    белого списка шлюза."""
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    services.gwlink_services_in(2, [NAS2]); _seen(services, 2, 1)
    toggle["on"] = True
    assert services.smb_tunnel_sync() is True
    text = adm.records
    assert text and gwservices.lines_ok(text), text
    assert "host-record=naspi5.awg.internal,10.240.1.10" in text
    assert "host-record=backup.awg.internal,10.241.68.20" in text
    assert "192.168." not in text, "настоящий адрес сети шлюза через туннель не открывается"
    assert "ptr-record=b._dns-sd._udp.0.1.8.10.in-addr.arpa,awg.internal" in text, \
        "нет обратной зоны подсети клиентов — Finder не найдёт домен обзора"
    assert text.splitlines()[0].startswith("# awg-bot (сервер AWG)")
    assert services.smb_tunnel_slot(services.db.gateway(1)) and services.smb_tunnel_slot(services.db.gateway(2))


def test_the_same_address_behind_two_slots_gives_two_records(services, two_slots, adm, toggle):
    """Одинаковые подсети и одинаковые адреса за двумя шлюзами больше не
    сливаются при чистке: у каждого свой адрес в туннеле, видны оба."""
    services.gateway_set_lan_subnets(2, LAN1)
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    services.gwlink_services_in(2, [{**NAS, "n": "Other", "h": "other"}]); _seen(services, 2, 1)
    toggle["on"] = True
    assert [(r["h"], r["a"]) for r in services.smb_tunnel_items()] == [
        ("naspi5", "10.240.1.10"), ("other", "10.241.1.10")]
    assert services.smb_tunnel_sync() is True
    assert "host-record=other.awg.internal,10.241.1.10" in adm.records


def test_the_same_list_again_does_not_touch_the_script(services, two_slots, adm, toggle):
    """Отпечаток: такт монитора и повторный `svc` без изменений — ни рестарта
    экземпляра, ни записи на диск."""
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    toggle["on"] = True
    services.smb_tunnel_sync()
    n = len(adm.calls)
    assert services.smb_tunnel_sync() is True
    assert len(adm.calls) == n, f"повтор дёрнул скрипт: {adm.calls[n:]}"
    services.gwlink_services_in(1, [NAS, {**NAS, "n": "Media", "h": "media", "a": "192.168.1.11"}])
    services.smb_tunnel_sync()
    assert len(adm.calls) == n + 1 and "media.awg.internal" in adm.records


def test_a_slot_silent_for_over_a_day_falls_out_of_the_records(services, two_slots, adm, toggle):
    """Слот молчит больше суток — его серверы выпадают: Finder иначе вёл бы на
    машину за шлюзом, которого, может, уже нет."""
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 23)
    services.gwlink_services_in(2, [NAS2]); _seen(services, 2, 1)
    toggle["on"] = True
    services.smb_tunnel_sync()
    assert "naspi5.awg.internal" in adm.records
    _seen(services, 1, 25)
    services.smb_tunnel_sync()
    assert "naspi5.awg.internal" not in adm.records and "backup.awg.internal" in adm.records


def test_nothing_to_publish_takes_the_records_down(services, two_slots, adm, toggle):
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    toggle["on"] = True
    services.smb_tunnel_sync()
    services.gwlink_services_in(1, [])
    services.smb_tunnel_sync()
    assert adm.calls[-1] == ("records", "-") and adm.records is None


def test_lines_outside_the_whitelist_take_the_file_down_instead(services, two_slots, adm, toggle, monkeypatch):
    """Последний рубеж: строка, не прошедшая белый список (server=, conf-file=),
    не доезжает до dnsmasq — файл снимается целиком."""
    toggle["on"] = True
    monkeypatch.setattr(services, "smb_tunnel_text",
                        lambda: "local=/awg.internal/\nserver=/awg.internal/203.0.113.66\n")
    services.smb_tunnel_sync(force=True)
    assert adm.calls == [("records", "-")], adm.calls


def test_a_refused_records_file_is_retried_on_the_next_sync(services, two_slots, adm, toggle):
    """Скрипт отказал — отпечаток не записан, следующий такт пробует снова."""
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    toggle["on"] = True
    adm.fail = "records"
    assert services.smb_tunnel_sync() is False
    adm.fail = ""
    assert services.smb_tunnel_sync() is True and adm.records and "naspi5" in adm.records


def test_without_the_admin_resolver_sync_does_nothing(services, two_slots, toggle, monkeypatch, tmp_path):
    a = _AdminDns(monkeypatch, tmp_path, installed=False)
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    toggle["on"] = True
    assert services.smb_tunnel_sync(force=True) is False and a.calls == []


# ── тумблер ──────────────────────────────────────────────────────────────────

def test_turning_on_writes_the_setting_and_the_records_at_once(services, two_slots, adm, toggle):
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    assert services.smb_tunnel_unavailable() == ""
    assert services.smb_tunnel_set(True) is True
    assert toggle["on"] is True and "naspi5.awg.internal" in adm.records
    assert services.smb_tunnel_set(False) is True
    assert toggle["on"] is False and adm.calls[-1] == ("records", "-"), "выключили — имена остались"


def test_a_failed_records_file_is_reported_by_the_toggle(services, two_slots, adm, toggle):
    """Тумблер переставлен, а записи не встали — итог об этом говорит (False),
    а не «включён»."""
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    adm.fail = "records"
    assert services.smb_tunnel_set(True) is False and toggle["on"] is True


_WHY = {
    "docker": "SMB в туннеле — только при VPN прямо на хосте, без docker",
    "no_client_resolver": "Нужен свой резолвер: ⚙️ → 🖥 Сервер AWG → «🔒 Свой резолвер»",
    "no_resolver": "Резолвер админа не поднят — на сервере: awg-bot resolver admin status",
    "dead_resolver": "Резолвер админа не поднят — на сервере: awg-bot resolver admin status",
    "no_firewall": "Нужен фильтр снаружи: ⚙️ → 🛡 SSH-доступ → «☑️ Фильтр снаружи» — без него имена увидят все клиенты",
}


@pytest.mark.parametrize("why", list(_WHY))
def test_turning_on_is_refused_where_names_would_leak_or_not_resolve(services, two_slots, toggle,
                                                                       monkeypatch, tmp_path, why):
    """Без резолвера админа (или лежащего) включать нечего; без фильтра
    снаружи имена увидели бы все клиенты — отказ с причиной, настройка и
    записи не тронуты."""
    from awgbot.infra import nftguard
    a = _AdminDns(monkeypatch, tmp_path, installed=(why != "no_resolver"))
    if why == "docker":
        monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    if why == "no_client_resolver":
        monkeypatch.setattr(resolver, "installed", lambda: False)
    if why == "dead_resolver":
        a.alive = False
    if why == "no_firewall":
        monkeypatch.setattr(nftguard, "enabled", lambda: False)
    assert services.smb_tunnel_unavailable() == _WHY[why]
    with pytest.raises(ServiceError) as e:
        services.smb_tunnel_set(True)
    assert str(e.value) == _WHY[why]
    assert toggle["on"] is False and a.calls == []


def test_turning_off_is_never_refused(services, two_slots, toggle, monkeypatch, tmp_path):
    """Выключить можно всегда — даже когда включить уже нельзя."""
    from awgbot.infra import nftguard
    _AdminDns(monkeypatch, tmp_path)
    toggle["on"] = True
    monkeypatch.setattr(nftguard, "enabled", lambda: False)
    services.smb_tunnel_set(False)
    assert toggle["on"] is False


def test_the_card_says_in_the_tunnel_only_where_the_names_really_are(services, two_slots, adm, toggle,
                                                                     monkeypatch):
    """«в туннеле ✅» — только когда записи слота действительно в резолвере
    админа: тумблер, фильтр, резолвер поставлен и слот на связи."""
    from awgbot.infra import nftguard
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    gw = services.db.gateway(1)
    assert services.gwlink_services_card(gw)["tunnel"] is False
    toggle["on"] = True
    assert services.gwlink_services_card(gw)["tunnel"] is True
    _seen(services, 1, 25)
    assert services.gwlink_services_card(gw)["tunnel"] is False, "слот молчит сутки — его записей нет"
    _seen(services, 1, 1)
    monkeypatch.setattr(nftguard, "enabled", lambda: False)
    assert services.gwlink_services_card(gw)["tunnel"] is False, "фильтр выключен — записей нет"


def test_turning_the_filter_off_takes_the_records_down_at_once(services, two_slots, adm, toggle, monkeypatch):
    """Без фильтра снаружи :53 резолвера админа открыт всем — записи снимаются
    сразу при выключении фильтра, а не тактом монитора."""
    from awgbot.infra import nftguard
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    toggle["on"] = True
    services.smb_tunnel_sync()
    assert adm.records
    monkeypatch.setattr(nftguard, "enabled", lambda: False)
    services._smb_tunnel_follow()
    assert adm.records is None and adm.calls[-1] == ("records", "-")


# ── монитор ──────────────────────────────────────────────────────────────────

def _deliver(notes):
    for n in notes:
        if n.on_sent:
            n.on_sent()
    return notes


def test_the_monitor_says_once_when_the_admin_resolver_dies_and_once_when_back(services, two_slots, adm):
    """От резолвера админа — DNS всех новых конфигов админа, тумблер тут ни при
    чём. Отметка «сказано» — по доставке: недоставленное повторяется,
    доставленное — нет."""
    assert services.admin_dns_health_tick() == []
    adm.alive = False
    notes = services.admin_dns_health_tick()
    assert [n.text for n in notes] == [
        f"🔴 Резолвер админа <code>{ADDR}</code> не отвечает — у устройств админа нет DNS и имён SMB в туннеле"]
    assert notes[0].tg_id == ADMIN and not notes[0].critical and not notes[0].force_sound, \
        "ломает DNS только устройствам админа — без звука, не авария для всех"
    assert len(services.admin_dns_health_tick()) == 1, "не доставленное уведомление потеряно"
    _deliver(notes)
    assert services.admin_dns_health_tick() == [], "повторный отказ — повторное уведомление"
    adm.alive = True
    back = services.admin_dns_health_tick()
    assert [n.text for n in back] == ["✅ Резолвер админа снова отвечает"]
    _deliver(back)
    assert services.admin_dns_health_tick() == []


def test_the_monitor_is_silent_without_the_admin_resolver(services, two_slots, toggle, monkeypatch, tmp_path):
    a = _AdminDns(monkeypatch, tmp_path, installed=False)
    a.alive = False
    toggle["on"] = True
    assert services.admin_dns_health_tick() == [] and a.calls == []


def test_the_monitor_tick_drops_a_slot_that_went_silent(services, two_slots, adm, toggle):
    """Слот замолчал без единого `svc` — выпадает по такту монитора."""
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 23)
    toggle["on"] = True
    services.admin_dns_health_tick()
    assert "naspi5" in adm.records
    _seen(services, 1, 25)
    services.admin_dns_health_tick()
    assert adm.calls[-1] == ("records", "-")


# ── старт бота ───────────────────────────────────────────────────────────────

def _client_resolver(monkeypatch, addrs):
    monkeypatch.setattr(resolver, "listen_addrs", lambda path=None: list(addrs))
    monkeypatch.setattr(resolver, "installed", lambda: bool(addrs))


def test_start_installs_the_admin_resolver_and_redraws_the_firewall_once(services, two_slots, toggle,
                                                                         monkeypatch, tmp_path):
    """Живой хост установщик заново не проходит — ставит бот; правило :53 в
    таблицу сразу: до перерисовки общее «:53 из туннеля» пускало бы всех."""
    a = _AdminDns(monkeypatch, tmp_path, installed=False)
    _client_resolver(monkeypatch, ["10.8.1.1"])
    redraws = []
    monkeypatch.setattr(services, "_gw_firewall_refresh", lambda: redraws.append(1))
    services.admin_dns_ensure()
    assert a.calls[0] == ("install", "10.8.1.1") and redraws == [1]
    assert a.exempt == [["10.8.1.0/24"]], "адрес резолвера админа не выведен из перехвата :53 обвязки"
    services.admin_dns_ensure()
    assert redraws == [1], "уже стоящий резолвер админа перерисовывал файервол на каждом старте"


def test_start_does_nothing_without_the_client_resolver_or_in_docker(services, two_slots, monkeypatch,
                                                                     tmp_path):
    a = _AdminDns(monkeypatch, tmp_path, installed=False)
    _client_resolver(monkeypatch, [])
    services.admin_dns_ensure()
    _client_resolver(monkeypatch, ["10.8.1.1"])
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    services.admin_dns_ensure()
    assert a.calls == []


def test_a_failed_install_on_start_does_not_break_the_start(services, two_slots, monkeypatch, tmp_path):
    a = _AdminDns(monkeypatch, tmp_path, installed=False)
    a.fail = "install"
    _client_resolver(monkeypatch, ["10.8.1.1"])
    monkeypatch.setattr(services, "_gw_firewall_refresh", lambda: pytest.fail("перерисовка без резолвера"))
    services.admin_dns_ensure()
    assert a.modes() == ["install"]


def test_start_puts_back_the_records_of_a_toggle_left_on(services, two_slots, adm, toggle, monkeypatch):
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    toggle["on"] = True
    _client_resolver(monkeypatch, ["10.8.1.1"])
    adm.conf.write_text(f"listen-address={ADDR}\nserver=10.8.1.1\n", encoding="utf-8")
    services.admin_dns_ensure()
    assert adm.records and "naspi5" in adm.records


# ── приём `svc` каналом ──────────────────────────────────────────────────────

from tests.integration import test_gwlink_services as _svc_pair  # noqa: E402
from tests.integration.test_gwlink_channel import _until  # noqa: E402

pair = _svc_pair.pair            # фикстура: два слота на боевом LinkServer
_hello = _svc_pair._hello


async def test_a_changed_svc_from_a_gateway_reaches_the_admin_resolver_once(pair, adm, toggle, host):
    """Новый SMB-сервер за шлюзом — в резолвер админа сразу по `svc`, без
    ожидания такта; тот же список повторно — скрипт не трогается."""
    toggle["on"] = True
    x = await _hello(pair, 1)
    await x.send("svc", {"items": [NAS]})
    assert await _until(lambda: adm.records and "naspi5.awg.internal" in adm.records), \
        f"svc не дошёл до резолвера админа: {adm.calls}"
    n = len(adm.calls)
    await x.send("svc", {"items": [NAS]})
    await x.send("svc", {"items": [NAS, {**NAS, "n": "Media", "h": "media", "a": "192.168.1.11"}]})
    assert await _until(lambda: adm.records and "media.awg.internal" in adm.records)
    assert len(adm.calls) == n + 1, f"тот же список дёрнул скрипт: {adm.calls[n:]}"
    await x.close()


# ── снятие шлюза ─────────────────────────────────────────────────────────────

@pytest.fixture()
def removal(services, two_slots, adm, monkeypatch):
    """Снятие слота без хоста: скрипт линка — пустышка; все записи настроек —
    в словарь (снятие последнего выключает маршрутизацию, в conf писать нельзя)."""
    store = {"app.routing.smb_tunnel": True}
    real_get, real_bool = settings.get, settings.get_bool
    monkeypatch.setattr(settings, "get", lambda k, d=None: store[k] if k in store else real_get(k, d))
    monkeypatch.setattr(settings, "get_bool",
                        lambda k, d=False: bool(store[k]) if k in store else real_bool(k, d))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(services, "_gw_firewall_refresh", lambda: None)
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    services.gwlink_services_in(2, [NAS2]); _seen(services, 2, 1)
    services.smb_tunnel_sync()
    assert "naspi5" in adm.records and "backup" in adm.records, "сцена собрана не так"
    return store


def test_removing_one_of_two_slots_takes_its_smb_out_at_once(services, removal, adm):
    """Снятый шлюз уносит свои имена сразу, а не через сутки молчания: Finder
    иначе вёл бы на машину, до которой маршрута больше нет. Тумблер остаётся —
    второй шлюз на месте."""
    n = len(adm.calls)
    services.gateway_remove(2)
    assert removal["app.routing.smb_tunnel"] is True, "снятие одного из двух выключило SMB в туннеле"
    assert len(adm.calls) > n and adm.calls[-1][0] == "records"
    assert adm.records and "naspi5.awg.internal" in adm.records and "backup" not in adm.records, adm.records


def test_removing_the_last_slot_switches_the_toggle_off_silently(services, removal, adm):
    """Шлюзов нет — показывать нечего: тумблер выключается молча, записи
    снимаются, уведомлений нет (монитор после этого тоже молчит о записях)."""
    services.gateway_remove(2)
    out = services.gateway_remove(1)
    assert out is not None
    assert removal["app.routing.smb_tunnel"] is False, "без шлюзов «SMB в туннеле» остался включённым"
    assert adm.calls[-1] == ("records", "-") and adm.records is None
    assert services.admin_dns_health_tick() == [], "снятие шлюза породило уведомление"


def test_a_failing_records_script_does_not_break_the_removal(services, removal, adm):
    """Скрипт резолвера админа отказал — шлюз всё равно снят (иначе слот не
    убрать вовсе из-за побочной функции)."""
    adm.fail = "records"
    services.gateway_remove(2)
    assert services.db.gateway(2) is None
    assert [g.id for g in services.db.gateways()] == [1]


def test_an_exception_in_sync_does_not_break_the_removal(services, removal, monkeypatch):
    def boom(force=False):
        raise RuntimeError("подставной сбой")
    monkeypatch.setattr(services, "smb_tunnel_sync", boom)
    services.gateway_remove(1)
    assert services.db.gateway(1) is None
