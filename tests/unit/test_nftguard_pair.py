"""Файервол хоста и линк пары серверов (infra/nftguard: collect.pair_link,
render._pair_input/_pair_forward).

С линка пары на хост — только TCP канала пары с адреса второй стороны в /30,
всё остальное с него — drop; пересылки через линк пары нет никогда (ни из
него, ни в него), до кода и после. Конфиг пары не опознаётся линком слота
(у него нет `Table = off`), а его UDP-порт открыт снаружи, как у любого
интерфейса AWG.

Цена ошибки: второй сервер пары ходит с линка к SSH, резолверу или пользователям
этого сервера — у него другой админ, и доверие ему — только канал пары;
конфиг пары в линках слотов — к нему едут исключения маскарада и правила
канала шлюзов; закрытый UDP-порт — линк пары не встаёт вовсе.
"""
from __future__ import annotations

import pytest

from awgbot.core import config, settings
from awgbot.infra import nftguard

pytestmark = pytest.mark.unit

PAIR_STAGED = """# Линк пары серверов AWG (резерв). Сгенерировано reserve-pair-setup.sh.
# awg-bot: пара 0123456789abcdef
[Interface]
Address = 10.99.98.2/30
ListenPort = 47101
PrivateKey = DUMMY-PRIV=
Jc = 5

[Peer]
PublicKey = DUMMY-PUB=
AllowedIPs = 10.99.98.1/32
PersistentKeepalive = 25-35
"""
PAIR_LINKED = PAIR_STAGED.replace("# awg-bot: пара 0123456789abcdef\n", "").replace(
    "AllowedIPs", "PresharedKey = DUMMY-PSK=\nAllowedIPs")
LINK = "[Interface]\nAddress = 10.99.99.1/30\nListenPort = 443\nTable = off\n\n[Peer]\nAllowedIPs = 0.0.0.0/0\n"
CLIENTS = "[Interface]\nAddress = 10.9.1.1/24\nListenPort = 42755\n"

Q = 'iifname { "awgpair" }'


@pytest.fixture()
def awgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "")
    monkeypatch.setattr(config, "SUBNET_PREFIX", "10.9.1")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    monkeypatch.setattr(config, "SERVER_PORT", 42755)
    monkeypatch.setattr(nftguard, "_tunnel_ifs", lambda: ["awg1"])
    monkeypatch.setattr(nftguard, "wan_ifaces", lambda: [])
    (tmp_path / "awg1.conf").write_text(CLIENTS, encoding="utf-8")
    (tmp_path / "awglink.conf").write_text(LINK, encoding="utf-8")
    return tmp_path


def _conf(monkeypatch, **kw):
    vals = {"app.firewall.enabled": True, "app.firewall.ssh_allow": [], "app.firewall.open_tcp": [8080],
            "app.firewall.open_udp": [5353], "app.network.ssh_port": 2222, "app.reserve.pair_channel_port": 8788}
    vals.update(kw)
    monkeypatch.setattr(settings, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(vals.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(vals.get(k, d)))


def _chain(text: str, name: str) -> list[str]:
    body = text.split(f"chain {name} {{", 1)[1].split("\n    }", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _spec(awgdir, monkeypatch, conf: str | None = PAIR_LINKED, **kw):
    _conf(monkeypatch, **kw)
    if conf is not None:
        (awgdir / "awgpair.conf").write_text(conf, encoding="utf-8")
    return nftguard.build_spec(["10.9.1.2"])


# ── сбор из конфига ──────────────────────────────────────────────────────────

def test_the_pair_link_is_read_from_its_config(awgdir, monkeypatch):
    spec = _spec(awgdir, monkeypatch)
    assert (spec.pair_if, spec.pair_peer4, spec.pair_port) == ("awgpair", "10.99.98.1", 8788)


def test_the_channel_port_follows_the_setting(awgdir, monkeypatch):
    spec = _spec(awgdir, monkeypatch, **{"app.reserve.pair_channel_port": 9900})
    assert spec.pair_port == 9900


def test_no_pair_config_means_no_pair_rules(awgdir, monkeypatch):
    spec = _spec(awgdir, monkeypatch, conf=None)
    assert (spec.pair_if, spec.pair_peer4, spec.pair_port) == ("", "", 0)
    assert "awgpair" not in nftguard.render(spec)


@pytest.mark.parametrize("junk", ["", "[Peer]\nAllowedIPs = мусор\n", "[Peer]\nAllowedIPs = 999.1.1.1/32\n"])
def test_a_broken_pair_config_gives_no_rules_and_no_crash(awgdir, monkeypatch, junk):
    spec = _spec(awgdir, monkeypatch, conf=junk)
    assert spec.pair_if == "", "из битого конфига взят адрес второй стороны"


def test_the_pair_config_is_not_a_slot_link_but_its_port_is_open(awgdir, monkeypatch):
    """`link_ifaces` — только конфиги с `Table = off`: конфиг пары туда не
    попадает (иначе ему достались бы правила канала шлюзов и исключение
    маскарада); его ListenPort — в открытых UDP-портах AWG."""
    (awgdir / "awgpair.conf").write_text(PAIR_STAGED, encoding="utf-8")
    assert nftguard.link_ifaces() == ["awglink"], "конфиг пары опознан линком слота"
    assert 47101 in nftguard.listen_ports(), "UDP-порт линка пары не открыт — линк не встанет"
    assert "awgpair" not in nftguard.quarantine_ifaces(nftguard.link_ifaces()), \
        "штамп пары принят за штамп сопряжения шлюза"


# ── вход: только канал пары с адреса второй стороны ──────────────────────────

@pytest.mark.parametrize("conf", [PAIR_STAGED, PAIR_LINKED], ids=["до кода", "после кода"])
def test_from_the_pair_link_only_the_channel_from_the_peer_reaches_the_host(awgdir, monkeypatch, conf):
    text = nftguard.render(_spec(awgdir, monkeypatch, conf=conf))
    lines = _chain(text, "input")
    accept = 'iifname { "awgpair" } ip saddr 10.99.98.1 tcp dport 8788 accept'
    assert accept in lines, lines
    at = lines.index(f"{Q} drop")
    assert lines[at - 1] == accept, "между каналом пары и drop линка пары что-то есть"


@pytest.mark.parametrize("conf", [PAIR_STAGED, PAIR_LINKED], ids=["до кода", "после кода"])
def test_the_pair_drop_stands_before_every_general_accept(awgdir, monkeypatch, conf):
    """Drop линка пары — раньше общих разрешений входа: ICMP, UDP-портов AWG,
    firewall.open_tcp/open_udp и SSH. Стоит он
    после них — второй сервер с линка пары достаёт до каждого из этих портов
    хоста, а «только канал» остаётся словами."""
    lines = _chain(nftguard.render(_spec(awgdir, monkeypatch, conf=conf)), "input")
    at = lines.index(f"{Q} drop")
    # правила с источником из подсети туннеля адрес /30 второй стороны не пропустят — не в счёт
    general = [ln for ln in lines if ln == "ip protocol icmp accept" or ln.startswith(("udp dport", "tcp dport"))]
    later = [ln for ln in general if lines.index(ln) < at]
    assert not later, f"общие разрешения раньше drop линка пары — с него они открыты: {later}"


# ── пересылка: никогда ───────────────────────────────────────────────────────

def test_forwarding_through_the_pair_link_is_dropped_before_any_accept(awgdir, monkeypatch):
    lines = _chain(nftguard.render(_spec(awgdir, monkeypatch)), "forward")
    i_in, i_out = lines.index(f"{Q} drop"), lines.index('oifname { "awgpair" } drop')
    first_accept = next(i for i, ln in enumerate(lines) if ln.endswith("accept"))
    assert max(i_in, i_out) < first_accept, f"пересылка с линка пары разрешена раньше drop: {lines}"


def test_forwarding_through_the_pair_link_is_dropped_without_the_filter_too(awgdir, monkeypatch):
    """Файервол выключен (умолчание) — таблица в форме NAT-only, как у
    карантина сопряжения шлюза: пересылки с линка пары и на него нет и в ней.
    Иначе второй сервер с адреса /30 ходит к пользователям этого сервера —
    AllowedIPs его адрес пропускает, а маршрут к пользователям у ядра есть."""
    text = nftguard.render(_spec(awgdir, monkeypatch, **{"app.firewall.enabled": False}))
    assert "chain forward {" in text, "в форме NAT-only нет цепочки пересылки вовсе"
    lines = _chain(text, "forward")
    assert f"{Q} drop" in lines and 'oifname { "awgpair" } drop' in lines, lines


def test_the_pair_input_stands_without_the_filter_too(awgdir, monkeypatch):
    """Форма NAT-only: на хост с линка пары — только канал, как вход линка
    сопряжения шлюза в той же форме (`_quarantine_input`)."""
    text = nftguard.render(_spec(awgdir, monkeypatch, **{"app.firewall.enabled": False}))
    assert "chain input {" in text, "в форме NAT-only вход с линка пары не ограничен"
    lines = _chain(text, "input")
    assert lines[-2:] == ['iifname { "awgpair" } ip saddr 10.99.98.1 tcp dport 8788 accept', f"{Q} drop"], lines


def test_the_rendering_is_stable(awgdir, monkeypatch):
    """Сверка по тику живёт сравнением текста: правила пары не дают шума."""
    a = nftguard.render(_spec(awgdir, monkeypatch))
    b = nftguard.render(_spec(awgdir, monkeypatch))
    assert a == b
