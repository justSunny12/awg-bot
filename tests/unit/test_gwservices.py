"""
Сервисы соседних сетей: общий
модуль gwservices — разбор обзора mDNS, чистка недоверенных записей, сборка
файла dnsmasq — и гистерезис обзора на агенте.

Цена ошибки: разбор, пропустивший строку «+» или IPv6, публикует у соседа
сервер без адреса; чистка, пропустившая кавычку или запятую, — строку конфига
dnsmasq малины из данных с чужой машины; обратная зона не той маски — Mac не
находит домен обзора и Finder пуст; гистерезис без порога — уснувший NAS
роняет кэш DNS соседней сети рестартом dnsmasq каждые 15 минут.
"""
from __future__ import annotations

import json
import random
import time

import pytest

from awgbot.domain import gwservices as gs
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database

OWN = ["192.168.68.0/24"]


def _line(name="NASPi5", host="NASPi5.local", addr="192.168.68.222", port="445",
          proto="IPv4", kind="=", stype="_smb._tcp") -> str:
    """Строка `avahi-browse -rtpk` так, как её печатает avahi 0.8."""
    return f"{kind};end0;{proto};{name};{stype};local;{host};{addr};{port};"


def _rec(n="naspi5", h="naspi5", a="192.168.1.10", p=445, t="_smb._tcp") -> dict:
    return {"t": t, "n": n, "h": h, "p": p, "a": a}


# ── разбор avahi-browse -p ───────────────────────────────────────────────────

def test_parse_takes_only_resolved_ipv4_lines_and_strips_local():
    """Строка «+» идёт первой и адреса не несёт, IPv6 соседу не нужен: запись
    — только из «=» по IPv4, хост — без «.local» и в нижнем регистре."""
    text = "\n".join([
        "+;end0;IPv4;NASPi5;_smb._tcp;local",
        _line(proto="IPv6", addr="fe80::1"),
        _line(),
    ])
    assert gs.parse_avahi(text, OWN) == [
        {"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.68.222"}]


def test_parse_unescapes_decimal_escapes_and_drops_non_ascii():
    r"""avahi экранирует пробел как \032 и «;» как \059; апостроф-кавычка
    и кириллица в имени Finder показал бы как «xn--…» — их выбрасываем."""
    text = "\n".join([
        _line(name="Ivan’s\\032MacBook", host="ivans-mbp.local", addr="192.168.68.5"),
        _line(name="a\\059b\\032\\032c", host="ab.local", addr="192.168.68.6"),
    ])
    names = {r["a"]: r["n"] for r in gs.parse_avahi(text, OWN)}
    assert names == {"192.168.68.5": "Ivans MacBook", "192.168.68.6": "ab c"}, (
        f"экранирование или набор символов разобраны не так: {names}")


def test_parse_skips_addresses_outside_own_subnets_and_other_types():
    """Сервер из чужой подсети (Docker-мост, гостевой Wi-Fi) соседям не
    объявляем: адреса туда у них нет маршрута. Чужой тип — не наш."""
    text = "\n".join([
        _line(addr="172.17.0.2"),
        _line(addr="192.168.68.10", stype="_http._tcp"),
        _line(addr="192.168.68.11", port="0"),
        _line(addr="192.168.68.12", port="порт"),
    ])
    assert gs.parse_avahi(text, OWN) == []


def test_parse_merges_duplicates_by_address_and_port():
    """Один сервер виден по двум интерфейсам (end0 и wlan0) — одна запись."""
    text = "\n".join([_line(), _line().replace("end0", "wlan0"),
                      _line(port="139")])
    got = gs.parse_avahi(text, OWN)
    assert [(r["a"], r["p"]) for r in got] == [("192.168.68.222", 139), ("192.168.68.222", 445)]


def test_parse_caps_the_list_at_32():
    text = "\n".join(_line(name=f"nas{i}", host=f"nas{i}.local", addr=f"192.168.68.{i + 1}")
                     for i in range(40))
    assert len(gs.parse_avahi(text, OWN)) == gs.MAX_OWN == 32


def test_parse_of_empty_output_is_an_empty_list():
    assert gs.parse_avahi("", OWN) == [] and gs.parse_avahi(None, OWN) == []


def test_a_non_ascii_name_falls_back_to_the_host_label():
    """Имя целиком кириллицей — в Finder будет метка хоста, не пропажа."""
    got = gs.parse_avahi(_line(name="Сервер", host="naspi5.local"), OWN)
    assert [r["n"] for r in got] == ["naspi5"], got


def test_a_non_ascii_name_and_host_become_the_address_label():
    """Имя и хост оба вне ASCII: хост — «h-<адрес>», имя — метка хоста. Сервер есть в сети — он обязан быть и в Finder соседа, а не
    исчезнуть молча из-за языка имени."""
    got = gs.parse_avahi(_line(name="Сервер", host="сервер.local", addr="192.168.68.10"), OWN)
    assert got == [{"t": "_smb._tcp", "n": "h-192-168-68-10", "h": "h-192-168-68-10",
                    "p": 445, "a": "192.168.68.10"}], f"сервер с кириллическим именем потерян: {got}"


# ── чистка недоверенного ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    _rec(n='nas"5'), _rec(n="nas,5"), _rec(n="nas\n5"), _rec(n="<b>nas</b>"),
    _rec(n="сервер"), _rec(n="x" * 64), _rec(n=" nas"), _rec(n="nas  5"),
    _rec(h="Nas5"), _rec(h="-nas"), _rec(h="nas.evil"), _rec(h="x" * 64),
    _rec(t="_http._tcp"), _rec(a="192.168.2.10"), _rec(a="192.168.1.10/24"),
    _rec(p=0), _rec(p=70000), _rec(p="445;"), "не запись", None,
], ids=lambda b: repr(b)[:40])
def test_clean_throws_a_bad_record_away_and_keeps_its_neighbours(bad):
    """Запись с кавычкой, запятой, переводом строки, разметкой, кириллицей,
    лишней длиной, чужим типом или адресом вне подсети — выбрасывается целиком,
    не «чинится»; соседние живут."""
    good = _rec(n="backup", h="backup", a="192.168.1.20")
    got = gs.clean([bad, good], ["192.168.1.0/24"], gs.MAX_PEER)
    assert got == [good], f"{bad!r} прошла чистку или утащила соседа: {got}"


def test_clean_keeps_only_the_closed_set_of_fields():
    rec = {**_rec(), "x": "server=/awg.internal/1.1.1.1", "p": "445"}
    assert gs.clean([rec], ["192.168.1.0/24"], 8) == [_rec()], "лишнее поле или строковый порт пережили чистку"


def test_clean_respects_the_limit_and_duplicates():
    many = [_rec(n=f"nas{i}", h=f"nas{i}", a=f"192.168.1.{i + 1}") for i in range(70)]
    assert len(gs.clean(many, ["192.168.1.0/24"], gs.MAX_PEER)) == 64
    assert len(gs.clean([_rec(), _rec(n="dup")], ["192.168.1.0/24"], 8)) == 1, "дубль по (адрес, порт)"
    assert gs.clean([], ["192.168.1.0/24"], 8) == [] and gs.clean(None, [], 8) == []


# ── отпечаток ────────────────────────────────────────────────────────────────

def test_feed_hash_is_empty_for_nothing_and_stable_across_order():
    """Пустое к пустому не едет: отпечаток пустого — пустая строка. Порядок
    записей на отпечаток не влияет, иначе одинаковый список уезжал бы заново."""
    items = [_rec(a=f"192.168.1.{i}", h=f"n{i}", n=f"n{i}") for i in range(1, 6)]
    shuffled = items[:]
    random.Random(7).shuffle(shuffled)
    assert gs.feed_hash([]) == "" and gs.feed_hash(None) == ""
    assert gs.feed_hash(items) == gs.feed_hash(shuffled) != ""
    assert gs.feed_hash(items) != gs.feed_hash(items[:-1])


# ── обратная зона и сборка файла ─────────────────────────────────────────────

@pytest.mark.parametrize("net,zone", [
    ("192.168.68.0/24", "0.68.168.192.in-addr.arpa"),
    ("192.168.68.77/24", "0.68.168.192.in-addr.arpa"),
    ("192.168.68.0/23", "0.68.168.192.in-addr.arpa"),
    ("192.168.69.0/23", "0.68.168.192.in-addr.arpa"),
    ("192.168.0.0/16", "0.0.168.192.in-addr.arpa"),
])
def test_reverse_zone_is_the_network_address_reversed(net, zone):
    """Mac ищет домен обзора в обратной зоне своей подсети, считая её по маске
    от DHCP (RFC 6763 §11): часть хоста обнулена, четыре октета наоборот."""
    assert gs.reverse_zone(net) == zone


def test_render_publishes_the_browse_domain_in_every_own_subnet():
    text = gs.render_dnsmasq([_rec()], ["192.168.68.0/24", "10.20.0.0/16"])
    lines = text.splitlines()
    for zone in ("0.68.168.192.in-addr.arpa", "0.0.20.10.in-addr.arpa"):
        assert f"ptr-record=b._dns-sd._udp.{zone},awg.internal" in lines, zone
        assert f"ptr-record=lb._dns-sd._udp.{zone},awg.internal" in lines, zone
    assert "local=/awg.internal/" in lines, "без local= запросы домена обзора уходят апстримом"
    assert 'ptr-record=_smb._tcp.awg.internal,"naspi5._smb._tcp.awg.internal"' in lines
    assert 'srv-host="naspi5._smb._tcp.awg.internal",naspi5.awg.internal,445' in lines
    assert 'txt-record="naspi5._smb._tcp.awg.internal",""' in lines, "RFC 6763 §6: TXT у каждого инстанса"
    assert "host-record=naspi5.awg.internal,192.168.1.10" in lines


def test_every_rendered_line_passes_the_whitelist():
    """Файл, собранный Python-ом, обязан целиком проходить белый список —
    иначе помощник на малине отвергнет его, и записи не лягут никогда."""
    items = [_rec(n=f"NAS {i}", h="naspi5", a=f"192.168.1.{i}") for i in range(1, 6)]
    items.append(_rec(n="Time_Machine-2", h="tm", a="192.168.2.7", p=10445))
    text = gs.render_dnsmasq(items, ["192.168.68.0/23", "10.0.0.0/8"], "ab" * 32)
    bad = [ln for ln in text.splitlines() if not any(p.fullmatch(ln) for p in gs.LINE_RES)]
    assert bad == [], f"строки вне белого списка: {bad}"
    assert gs.lines_ok(text)


def test_colliding_host_labels_get_suffixes_and_stay_unique():
    """Две записи с одной меткой хоста — naspi5 и naspi5-2: иначе два
    host-record на одно имя, и Finder ведёт к случайному серверу."""
    items = [_rec(n="a", h="naspi5", a="192.168.1.1"), _rec(n="b", h="naspi5", a="192.168.1.2"),
             _rec(n="c", h="naspi5-2", a="192.168.1.3")]
    text = gs.render_dnsmasq(items, OWN)
    hosts = [ln.split("=", 1)[1].split(",")[0] for ln in text.splitlines() if ln.startswith("host-record=")]
    assert "naspi5.awg.internal" in hosts and "naspi5-2.awg.internal" in hosts
    assert len(hosts) == len(set(hosts)) == 3, f"метки хостов совпали: {hosts}"


def test_same_instance_names_on_different_servers_stay_two_servers():
    """Два сервера с одинаковым именем без учёта регистра («NAS» и «nas»):
    dnsmasq сводит имена к нижнему регистру, и без уникализации один инстанс
    получил бы две цели SRV — Finder вёл бы то к одному NAS, то к другому."""
    items = [_rec(n="NAS", h="nas-a", a="192.168.1.1"), _rec(n="nas", h="nas-b", a="192.168.1.2")]
    text = gs.render_dnsmasq(items, OWN)
    srv = [ln for ln in text.splitlines() if ln.startswith("srv-host=")]
    names = [ln.split("=", 1)[1].rsplit(",", 2)[0] for ln in srv]
    assert len(srv) == 2 and len({n.lower() for n in names}) == 2, f"один инстанс на два сервера: {srv}"
    assert names[0] == '"NAS._smb._tcp.awg.internal"' and names[1] == '"nas 2._smb._tcp.awg.internal"', names
    assert {ln.rsplit(",", 2)[1] for ln in srv} == {"nas-a.awg.internal", "nas-b.awg.internal"}, srv
    ptr = [ln for ln in text.splitlines() if ln.startswith("ptr-record=_smb._tcp.")]
    assert len(ptr) == 2 and len({ln.lower() for ln in ptr}) == 2, ptr
    assert gs.lines_ok(text), "уникализированное имя не проходит белый список помощника"


def test_render_of_nothing_is_nothing():
    """Пусто — файла нет вовсе (снимается), а не файл с одним local=."""
    assert gs.render_dnsmasq([], OWN) == ""
    assert gs.render_dnsmasq([_rec(n='x"y')], OWN) == "", "грязная запись собрала непустой файл"


def test_render_never_emits_a_directive_from_a_dirty_record():
    """Второй слой на получателе: сборка из шаблона по чистым записям. Имя с
    переводом строки и чужой директивой строки конфига не порождает."""
    evil = _rec(n='x",\nserver=/awg.internal/1.1.1.1\n#', a="192.168.1.99")
    text = gs.render_dnsmasq([evil, _rec()], OWN)
    assert "server=" not in text and "1.1.1.1" not in text
    assert gs.lines_ok(text)


def test_version_floor_for_the_card():
    assert gs.version_at_least("3.1.0", (3, 1, 0)) and gs.version_at_least("3.10.2", (3, 1, 0))
    assert not gs.version_at_least("3.0.9", (3, 1, 0))
    assert not gs.version_at_least("", (3, 1, 0)) and not gs.version_at_least("3.1.0-rc1", (3, 1, 0))


@pytest.mark.parametrize("ver,floor,ok", [
    ("3.1.0", (3, 1, 0), True),
    ("v3.1.0", (3, 1, 0), True),          # тег релиза с «v» — та же версия
    (" 3.1.0\n", (3, 1, 0), True),        # хвост строки из вывода агента
    ("3.1.0.2", (3, 1, 0), True),         # хотфикс поверх 3.1.0
    ("3.1.1", (3, 1, 0), True),
    ("3.10.0", (3, 1, 0), True),          # по числам, не по строке
    ("4.0.0", (3, 1, 0), True),
    ("3.0.9", (3, 1, 0), False),
    ("3.0.9.9", (3, 1, 0), False),        # хотфикс прежней версии — ещё не она
    ("3.1", (3, 1, 0), False),            # две цифры — не версия выпуска
    ("3", (3, 1, 0), False),
    ("3.1.0-rc1", (3, 1, 0), False),
    ("3.1.0.2.1", (3, 1, 0), False),
    ("abc", (3, 1, 0), False),
    ("", (3, 1, 0), False),
    (None, (3, 1, 0), False),
    # порог с хотфиксом: сравнение по длине порога
    ("3.1.0", (3, 1, 0, 2), False),
    ("3.1.0.1", (3, 1, 0, 2), False),
    ("3.1.0.2", (3, 1, 0, 2), True),
    ("3.1.1", (3, 1, 0, 2), True),
])
def test_version_floor_reads_versions_like_the_update_check(ver, floor, ok):
    """Карточка решает по версии агента, умеет ли он новое. Разбор — тот же,
    что у проверки обновлений: агент на хотфиксе 3.1.0.2 или с «v» в теге
    иначе считался бы старым и терял возможности, а «3.1» — новым."""
    assert gs.version_at_least(ver, floor) is ok, f"{ver!r} ≥ {floor}: ждали {ok}"


# ── гистерезис обзора на агенте ──────────────────────────────────────────────

class _Lan:
    """Шлюз для обзора: юнит (режим, подсети, соседи, канал) и что отвечает
    avahi-browse. `out` = None — обзор не удался (таймаут, нет утилиты)."""

    def __init__(self, monkeypatch):
        self.env = {"LAN_MODE": "1", "HOME_SUBNETS": "192.168.68.0/24",
                    "PEER_HOME_NETS": "192.168.1.0/24", "LINK_CHANNEL": "1"}
        self.out: str | None = ""
        self.browses = 0
        monkeypatch.setattr(gwguard, "unit_env", lambda k: self.env.get(k, ""))
        monkeypatch.setattr(gwguard, "lan_mode", lambda: self.env.get("LAN_MODE") == "1")
        monkeypatch.setattr(gwguard, "avahi_browse", self._browse)

    def _browse(self, timeout=15):
        self.browses += 1
        return self.out

    def see(self, *addrs: str) -> None:
        self.out = "\n".join(_line(name=f"nas{a.rsplit('.', 1)[1]}", host=f"nas{a.rsplit('.', 1)[1]}.local",
                                   addr=a) for a in addrs)


@pytest.fixture()
def agent(tmp_path):
    db = Database(tmp_path / "gw.db"); db.init_schema()
    yield GatewayServices(db)
    db.close()


def _local(agent) -> list[str]:
    return [r["a"] for r in agent.services_local()]


def test_a_new_server_appears_at_once(agent, monkeypatch):
    lan = _Lan(monkeypatch)
    lan.see("192.168.68.10")
    assert agent.services_scan() is True
    assert _local(agent) == ["192.168.68.10"]
    assert json.loads(agent.db.get_state("gw_svc_local"))[0]["h"] == "nas10"
    assert agent.services_scan() is False, "тот же обзор — не изменение"
    lan.see("192.168.68.10", "192.168.68.11")
    assert agent.services_scan() is True and _local(agent) == ["192.168.68.10", "192.168.68.11"]


def test_a_vanished_server_goes_away_on_the_third_miss(agent, monkeypatch):
    """Уснувший NAS не роняет кэш DNS соседей: первые два обзора без него
    список не меняют, третий подряд — снимает."""
    lan = _Lan(monkeypatch)
    lan.see("192.168.68.10", "192.168.68.11")
    agent.services_scan()
    lan.see("192.168.68.10")
    assert agent.services_scan() is False, "снят на первом промахе"
    assert agent.services_scan() is False, "снят на втором промахе"
    assert _local(agent) == ["192.168.68.10", "192.168.68.11"]
    assert agent.services_scan() is True
    assert _local(agent) == ["192.168.68.10"], "третий промах подряд не снял сервер"


def test_a_failed_scan_is_not_a_miss(agent, monkeypatch):
    """Обзор упал (таймаут, avahi перезапускался) — это не «сервера нет»."""
    lan = _Lan(monkeypatch)
    lan.see("192.168.68.10", "192.168.68.11")
    agent.services_scan()
    lan.see("192.168.68.10")
    agent.services_scan(); agent.services_scan()
    lan.out = None
    for _ in range(3):
        assert agent.services_scan() is False
    assert _local(agent) == ["192.168.68.10", "192.168.68.11"], "упавшие обзоры засчитаны промахами"
    lan.see("192.168.68.10")
    assert agent.services_scan() is True and _local(agent) == ["192.168.68.10"]


def test_a_server_back_before_the_third_miss_starts_the_count_over(agent, monkeypatch):
    lan = _Lan(monkeypatch)
    lan.see("192.168.68.10", "192.168.68.11")
    agent.services_scan()
    for _ in range(2):
        lan.see("192.168.68.10")
        agent.services_scan()
    lan.see("192.168.68.10", "192.168.68.11")
    assert agent.services_scan() is False
    for _ in range(2):
        lan.see("192.168.68.10")
        assert agent.services_scan() is False, "промахи до возвращения сервера не обнулились"
    assert _local(agent) == ["192.168.68.10", "192.168.68.11"]


def test_without_peer_access_the_scan_does_not_touch_mdns_and_clears_the_list(agent, monkeypatch):
    """Функция работает там, где работает доступ между подсетями:
    соседей в юните нет — mDNS не трогаем, а прежний список один раз уходит
    пустым, чтобы сервер перестал раздавать его соседям."""
    lan = _Lan(monkeypatch)
    lan.see("192.168.68.10")
    agent.services_scan()
    lan.env["PEER_HOME_NETS"] = ""
    n = lan.browses
    assert agent.services_scan() is True and _local(agent) == []
    assert agent.services_scan() is False, "пустой список «изменился» второй раз"
    assert lan.browses == n, "обзор mDNS без доступа между подсетями"
    lan.env.update(PEER_HOME_NETS="192.168.1.0/24", LINK_CHANNEL="")
    assert agent.services_scan() is False and lan.browses == n, "без канала обзор не нужен"


# ── отложенное применение записей соседей (помощника ещё нет) ───────────────
#
# Обновление агента положило новую обвязку, а юнит её ещё не перезапускал:
# помощника awg-lan-services.sh на диске нет. Сервер второй раз за сессию
# записи не пришлёт — агент обязан сохранить присланное и применить сам, как
# только помощник появится. Иначе Finder в соседней сети пуст до следующего
# переподключения канала, а карточка на ВПС навсегда «шлюз отказался принимать».

class _Peer:
    """Получатель записей соседей: юнит (режим, подсети), помощник на диске
    (`helper_there`), что отвечает помощник (`rc_ok`, `tail`), реассерт юнита
    обвязки. Помощник «ставит» файл: копирует присланный в dnsmasq.d."""

    def __init__(self, tmp_path, monkeypatch):
        self.env = {"LAN_MODE": "1", "HOME_SUBNETS": "192.168.68.0/24",
                    "PEER_HOME_NETS": "192.168.1.0/24", "LINK_CHANNEL": "1"}
        self.helper = tmp_path / "awg-lan-services.sh"
        self.conf = tmp_path / "dnsmasq.d" / gs.CONF_NAME
        self.conf.parent.mkdir()
        self.rc_ok, self.tail = True, ""
        self.runs: list[str] = []
        self.reasserts = 0
        self.reassert_ok = True
        monkeypatch.setattr(gwguard, "unit_env", lambda k: self.env.get(k, ""))
        monkeypatch.setattr(gwguard, "lan_mode", lambda: self.env.get("LAN_MODE") == "1")
        monkeypatch.setattr(gwguard, "LAN_SERVICES_SCRIPT", str(self.helper))
        monkeypatch.setattr(gwguard, "PEER_SERVICES_CONF", str(self.conf))
        monkeypatch.setattr(gwguard, "PEER_SERVICES_NEW", str(tmp_path / "lib" / "peer-services.conf.new"))
        monkeypatch.setattr(gwguard, "run_lan_services", self._run)
        monkeypatch.setattr(gwguard, "reassert", self._reassert)
        monkeypatch.setattr(gwguard, "unit_state", lambda: {"ActiveState": "active"})

    def _run(self, path: str = "", timeout: int = 90):
        self.runs.append(path)
        if not self.helper.exists():
            return False, "скрипта записей SMB нет — обвязка старого образца"
        if not self.rc_ok:
            return False, self.tail
        if path:
            self.conf.write_text(open(path, encoding="utf-8").read(), encoding="utf-8")
        elif self.conf.exists():
            self.conf.unlink()
        return True, ""

    def _reassert(self, timeout: int = 90):
        self.reasserts += 1
        return self.reassert_ok, "" if self.reassert_ok else "юнит не поднялся"

    def appear(self) -> None:
        self.helper.write_text("#!/bin/sh\n", encoding="utf-8")


NAS = _rec(n="NASPi5", h="naspi5", a="192.168.1.10")
H_NAS = gs.feed_hash([NAS])


@pytest.fixture()
def peer(tmp_path, monkeypatch):
    return _Peer(tmp_path, monkeypatch)


def _pending(agent) -> str:
    return agent.db.get_state(GatewayServices._SVC_PENDING_KEY) or ""


def _throttle(agent) -> None:
    """Реассерт был только что — следующий отложен троттлингом на 10 минут."""
    agent._last_reassert = time.monotonic()


def test_without_the_helper_and_a_throttled_reassert_the_records_wait(agent, peer):
    """Помощника нет, реассерт отложен троттлингом: агент отвечает отказом,
    честно говорящим «перевыставится», файла dnsmasq не трогает, а присланное
    хранит — ровно тем отпечатком, который ждёт сервер."""
    _throttle(agent)
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is False and res["n"] == 0, res
    assert res["error"] == "скрипта записей SMB нет — обвязка перевыставится в ближайшие минуты", res
    assert peer.reasserts == 0, "троттлинг реассерта не сработал"
    assert peer.runs == [] and not peer.conf.exists(), "без помощника тронули dnsmasq"
    assert json.loads(_pending(agent)) == {"hash": H_NAS, "items": [NAS]}, (
        "присланное сервером не сохранено — до переподключения его никто не повторит")
    assert agent.services_applied_hash() == "", "отказ записан как применённое"
    assert agent.services_retry() is None, "помощника всё ещё нет — повторять нечего"
    assert json.loads(_pending(agent))["hash"] == H_NAS, "пустой повтор съел отложенное"


def test_a_reassert_that_did_not_bring_the_helper_keeps_the_records_pending(agent, peer):
    """Реассерт прошёл, но помощник не появился (юнит ещё не дописал файл):
    отказ «перевыставляется», присланное ждёт повтора."""
    agent._last_reassert = time.monotonic() - 3600     # прошлый реассерт давно
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert peer.reasserts == 1
    assert res["ok"] is False and res["error"] == "скрипта записей SMB нет — обвязка перевыставляется", res
    assert json.loads(_pending(agent))["hash"] == H_NAS
    assert not peer.conf.exists()


def test_the_helper_that_appeared_later_gets_the_records_on_retry(agent, peer):
    """Помощник появился (юнит отработал на тике) — повтор ставит ровно то,
    что прислал сервер, и возвращает тот же отпечаток: по нему сервер
    перестаёт считать слот «не принявшим». Второй повтор — ничего."""
    _throttle(agent)
    agent.apply_peer_services(H_NAS, [NAS])
    peer.appear()
    res = agent.services_retry()
    assert res is not None, "появившийся помощник не получил отложенное"
    assert res["ok"] is True and res["n"] == 1 and res["hash"] == H_NAS, res
    assert "host-record=naspi5.awg.internal,192.168.1.10" in peer.conf.read_text(encoding="utf-8")
    assert _pending(agent) == "", "применённое осталось в очереди повтора"
    assert agent.services_applied_hash() == H_NAS, "в hello уйдёт старый отпечаток — сервер пришлёт снова"
    assert agent.db.get_state(GatewayServices._SVC_PEER_ERR_KEY) == "", "ошибка про помощника не снята"
    runs = len(peer.runs)
    assert agent.services_retry() is None, "повтор после успеха снова что-то применил"
    assert len(peer.runs) == runs


def test_a_reassert_that_brought_the_helper_applies_at_once(agent, peer, monkeypatch):
    """Реассерт прошёл и помощник на месте — записи ставятся тем же вызовом,
    без очереди и без отказа серверу."""
    def reassert(why):
        peer.appear()
        return True
    monkeypatch.setattr(agent, "_reassert_throttled", reassert)
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is True and res["n"] == 1 and res["error"] == "", res
    assert peer.conf.exists(), "записи не поставлены сразу после реассерта"
    assert _pending(agent) == "", "применённое сразу легло ещё и в очередь повтора"
    assert agent.services_applied_hash() == H_NAS
    assert agent.services_retry() is None


def test_the_retry_itself_reasserts_until_the_helper_appears(agent, peer, monkeypatch):
    """Помощника нет и первый реассерт его не принёс: повтор на тике сам
    перевыставляет обвязку (не чаще троттлинга) и ставит отложенные записи,
    как только помощник появился — без переподключения канала."""
    _throttle(agent)
    agent.apply_peer_services(H_NAS, [NAS])
    assert agent.services_retry() is None and peer.reasserts == 0, "троттлинг реассерта не соблюдён"
    agent._last_reassert = -1e9                          # прошло 10 минут

    def reassert(timeout=90):
        peer.reasserts += 1
        peer.appear()
        return True, ""
    monkeypatch.setattr(gwguard, "reassert", reassert)
    res = agent.services_retry()
    assert peer.reasserts == 1, "тик не перевыставил обвязку сам"
    assert res is not None and res["ok"] is True and res["hash"] == H_NAS, res
    assert peer.conf.exists() and _pending(agent) == ""


def test_a_file_outside_the_line_whitelist_never_reaches_the_helper(agent, peer, monkeypatch, tmp_path):
    """Второй рубеж на агенте: собранный файл не прошёл построчный белый
    список (шаблон разошёлся с помощником) — отказ с причиной, файл не пишется
    и помощник не зовётся."""
    peer.appear()
    monkeypatch.setattr(gs, "render_dnsmasq", lambda items, nets, digest="": "server=/awg.internal/1.1.1.1\n")
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is False and res["error"] == "записи SMB не прошли проверку строк", res
    assert peer.runs == [] and not (tmp_path / "lib" / "peer-services.conf.new").exists()
    assert agent.services_applied_hash() == ""


def test_an_unwritable_services_file_refuses_with_a_reason(agent, peer, monkeypatch, tmp_path):
    peer.appear()
    blocker = tmp_path / "blocker"; blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(gwguard, "PEER_SERVICES_NEW", str(blocker / "peer-services.conf.new"))
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is False and res["error"].startswith("файл записей SMB не записан: "), res
    assert peer.runs == [] and not peer.conf.exists()


def test_a_refusal_that_is_not_about_the_helper_is_not_retried(agent, peer):
    """Помощник есть и отверг файл (dnsmasq --test, rc≠0) — повтор того же
    файла дал бы тот же отказ и рестарт-попытку на каждом тике: в очередь не
    кладём."""
    peer.appear()
    peer.rc_ok, peer.tail = False, "dnsmasq: bad option at line 3"
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is False and res["error"] == "dnsmasq: bad option at line 3", res
    assert _pending(agent) == "", "отказ не про помощника попал в очередь повтора"
    runs = len(peer.runs)
    assert agent.services_retry() is None
    assert len(peer.runs) == runs, "повтор без очереди звал помощника"


def test_a_silent_refusal_still_says_something_to_the_server(agent, peer):
    """Скрипт записей отказал с пустым выводом — у сервера в карточке всё равно
    должна быть причина, а не «отказался принимать:» с пустотой."""
    peer.appear()
    peer.rc_ok, peer.tail = False, ""
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is False and res["error"] == "скрипт записей SMB отказал без объяснений", res


def test_a_pending_record_the_helper_then_refuses_is_dropped_after_one_try(agent, peer):
    """Отложили из-за помощника, он появился — и отверг файл. Итог уходит
    серверу один раз (с отпечатком), дальше повторять нечего: каждый тик иначе
    — попытка рестарта dnsmasq соседней сети."""
    _throttle(agent)
    agent.apply_peer_services(H_NAS, [NAS])
    peer.appear()
    peer.rc_ok, peer.tail = False, "rc=1"
    res = agent.services_retry()
    assert res is not None and res["ok"] is False and res["hash"] == H_NAS, res
    assert _pending(agent) == "", "тот же отказ остался в очереди — повтор на каждом тике"
    runs = len(peer.runs)
    assert agent.services_retry() is None and len(peer.runs) == runs


def test_a_newer_successful_delivery_drops_the_stale_pending(agent, peer):
    """Отложили список A, а потом сервер прислал B и тот встал: повтор не
    должен откатить сеть к устаревшему A."""
    _throttle(agent)
    agent.apply_peer_services(H_NAS, [NAS])
    peer.appear()
    other = _rec(n="backup", h="backup", a="192.168.1.20")
    h_other = gs.feed_hash([other])
    assert agent.apply_peer_services(h_other, [other])["ok"] is True
    assert _pending(agent) == ""
    assert agent.services_retry() is None, "повтор вернул устаревший список"
    assert "192.168.1.20" in peer.conf.read_text(encoding="utf-8")
    assert agent.services_applied_hash() == h_other


@pytest.mark.parametrize("raw", ["{не json", "[1, 2]", '"строка"'])
def test_a_broken_pending_record_is_dropped_quietly(agent, peer, raw):
    """Битая запись очереди (оборванная запись, чужой формат) — снять и
    ничего не применять: не падать на каждом тике."""
    peer.appear()
    agent.db.set_state(GatewayServices._SVC_PENDING_KEY, raw)
    assert agent.services_retry() is None
    assert _pending(agent) == "", "битая запись осталась — разбор на каждом тике"
    assert peer.runs == [], "из битой записи что-то применили"
