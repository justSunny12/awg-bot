"""
Общие правила двух концов канала линка (util/gwlink): порт по умолчанию,
адреса сторон в /30, отпечаток фидов, таблица ключей конфигурации — и те же
значения в скриптах поставки.

Эти правила раньше жили в трёх-шести копиях и расходились. Цена расхождения
у каждого своя, и ни одна не видна сразу: шлюз стучится в порт, где никого
нет; слушатель встаёт не на тот адрес; шлюз отвергает каждую доставку фидов и
качает их сам с адреса квартиры; сверка установленного на ВПС молча не видит
поля, которое не попало в таблицу. Скрипты сверяем прогоном их настоящих строк
под sh, а не глазами.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from awgbot.domain import gwsnapshot
from awgbot.domain.services.gwchannel import GwChannelMixin
from awgbot.runtime import linkclient, linkserver
from awgbot.util import gwlink

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GW_SETUP = ROOT / "install" / "routing-gw-setup.sh"
LINK_SETUP = ROOT / "install" / "routing-link-setup.sh"


def _sh(prog: str, **env) -> str:
    r = subprocess.run(["sh", "-c", prog], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", **env})
    assert r.returncode == 0, r.stderr
    return r.stdout


# ── адреса сторон в /30 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("cidr, want", [
    ("10.99.99.0/30", ("10.99.99.1", "10.99.99.2")),
    ("10.99.99.4/30", ("10.99.99.5", "10.99.99.6")),
    ("10.99.99.6/30", ("10.99.99.5", "10.99.99.6")),   # адрес хоста вместо сети — та же /30
])
def test_link_hosts_are_the_first_and_the_second_host_of_the_slash_30(cidr, want):
    """Первый хост — ВПС, второй — шлюз: ровно так их раздаёт скрипт линка.
    Перепутай порядок — слушатель встанет на адрес шлюза и не поднимется."""
    assert gwlink.link_hosts(cidr) == want
    assert (linkserver.vps_address(cidr), linkserver.gw_address(cidr)) == want


@pytest.mark.parametrize("junk", ["", "мусор", "10.99.99.0/24", "10.99.99.0/31", "10.99.99.0/32",
                                  "10.99.99.0/29", "fd00::/126", "10.99.99.300/30", None])
def test_link_hosts_of_anything_but_a_v4_slash_30_are_empty(junk):
    """/24 даёт два «первых хоста» вместо пары сторон — слушатель встал бы на
    адрес, до которого шлюз никогда не постучится. Пусто честнее выдуманного:
    слот просто остаётся без канала до исправления."""
    try:
        got = gwlink.link_hosts(junk)
    except TypeError:
        pytest.fail(f"link_hosts упал на {junk!r} — мусор из БД уронил бы такт живости")
    assert got == ("", ""), f"{junk!r} → {got}"


def test_the_script_derives_the_same_addresses_as_the_bot():
    """Скрипт линка считает адреса сторон своей арифметикой. Разойдись она с
    ботом — ВПС слушает один адрес, а шлюзу выдан другой."""
    head = LINK_SETUP.read_text(encoding="utf-8")
    lines = [ln for ln in head.splitlines()
             if ln.startswith(("LINK_CIDR=", "_cidr_", "LINK_VPS_ADDR=", "LINK_GW_ADDR="))]
    assert len(lines) == 6, f"строки вывода адресов линка изменились: {lines}"
    for cidr in ("10.99.99.0/30", "10.99.99.4/30", "10.99.99.8/30"):
        out = _sh("\n".join(lines) + '\nprintf "%s %s" "$LINK_VPS_ADDR" "$LINK_GW_ADDR"', LINK_CIDR=cidr)
        assert tuple(out.split()) == gwlink.link_hosts(cidr), f"{cidr}: скрипт {out}, бот {gwlink.link_hosts(cidr)}"


# ── порт канала по умолчанию ─────────────────────────────────────────────────

def test_one_default_port_on_both_ends_and_in_both_scripts(monkeypatch):
    """Порт по умолчанию — одно число на четырёх местах: слушатель ВПС, клиент
    шлюза, скрипт линка (бандл) и скрипт обвязки (юнит). Разойдись одно — канал
    молча не поднимается, и увидят это только на живой малине."""
    from awgbot.core import settings
    from awgbot.infra import gwguard, nftguard
    real = settings.get_int
    monkeypatch.setattr(settings, "get_int", lambda k, d=None:
                        d if k == "app.routing.link_channel_port" else real(k, d))
    port = gwlink.DEFAULT_PORT
    assert linkserver.channel_port() == port, "слушатель ВПС без настройки встал на другой порт"
    monkeypatch.setattr(nftguard, "link_ifaces", lambda: ["awglink"])   # линк есть — порт открыт
    assert nftguard.link_channel_port() == port, "файервол ВПС открывает не тот порт, что слушает канал"
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    assert linkclient.server_port() == port, "клиент шлюза без строки в юните стучится не туда"
    link = LINK_SETUP.read_text(encoding="utf-8")
    line = next(ln for ln in link.splitlines() if ln.startswith("LINK_CHANNEL_PORT="))
    assert _sh("_cfg_chport=\n" + line + '\nprintf "%s" "$LINK_CHANNEL_PORT"') == str(port), (
        "бандл без настройки в app.yaml везёт другой порт")
    gw = GW_SETUP.read_text(encoding="utf-8")
    lines = [ln for ln in gw.splitlines()
             if ln.startswith("LINK_CHANNEL_PORT=") or ln.startswith('case "$LINK_CHANNEL_PORT"')]
    assert _sh("\n".join(lines) + '\nprintf "%s" "$LINK_CHANNEL_PORT"') == str(port), (
        "юнит обвязки без строки в бандле пишет другой порт")


# ── ключи конфигурации ───────────────────────────────────────────────────────

def test_every_bundle_key_has_a_human_name():
    """Сверка «у сервера / на шлюзе» подписывает строку человеческим именем
    ключа. Ключ без имени — KeyError посреди напоминания, и о расхождении не
    узнает никто."""
    assert set(gwlink.KEY_HUMAN) == set(gwlink.BUNDLE_KEYS), (
        f"без имени: {sorted(set(gwlink.BUNDLE_KEYS) - set(gwlink.KEY_HUMAN))}, "
        f"лишние: {sorted(set(gwlink.KEY_HUMAN) - set(gwlink.BUNDLE_KEYS))}")
    assert all(gwlink.KEY_HUMAN[k].strip() for k in gwlink.BUNDLE_KEYS)
    assert set(gwlink.BUNDLE_KEYS) == set(gwlink.SETTINGS_KEYS) | set(gwlink.BUNDLE_ONLY_KEYS)


def test_the_snapshot_bundle_block_is_the_bundle_keys_in_their_order():
    """Блок bundle снимка и сверка на ВПС берут поля из одной таблицы. Поле,
    которого нет в снимке, сверка молча считала бы совпавшим."""
    assert gwsnapshot.BUNDLE_FIELDS == tuple(k.lower() for k in gwlink.BUNDLE_KEYS)
    assert gwsnapshot.BUNDLE_FIELDS == ("admin_ips", "home_subnets", "lan_mode", "resolver",
                                        "peer_home_nets")
    assert [gwlink.snap_field(k) for k in gwlink.BUNDLE_KEYS] == list(gwsnapshot.BUNDLE_FIELDS)


# ── отпечаток фидов ──────────────────────────────────────────────────────────

def test_the_feeds_fingerprint_is_the_agreed_rule():
    """Одно правило на ВПС и на шлюзе. Разойдись оно — шлюз отвергал бы каждую
    доставку («отпечаток не сошёлся») и ходил бы за фидами сам. Правило
    записано здесь явно: тест обязан падать от его правки, а не переезжать
    вместе с ней."""
    d, n = "ipset=/a.org/vpn_domains\n", "91.108.4.0/22\n"
    assert gwlink.feeds_hash(d, n) == hashlib.sha256((d + "\n--\n" + n).encode()).hexdigest()
    assert gwlink.feeds_hash(d, n) != gwlink.feeds_hash(n, d), "домены и подсети не взаимозаменяемы"
    assert gwlink.feeds_hash(d + "x", n) != gwlink.feeds_hash(d, "x" + n), "граница между частями размыта"


# ── источники фидов: ВПС и скрипт списков шлюза ──────────────────────────────

def test_the_server_fetches_feeds_from_exactly_where_the_gateway_would():
    """Фиды для шлюзов качает ВПС. Если его адреса и набор сервисов разойдутся
    с тем, что шлюз качал сам, режим без VPN в квартире тихо поменяет состав
    списков при переходе на канал: сайты, ходившие через туннель, пойдут
    напрямую. Константы скрипта берём прогоном его строк."""
    script = GW_SETUP.read_text(encoding="utf-8")
    lists = script.split("cat > \"$LAN_LISTS\" <<'LISTSEOF'\n", 1)[1].split("\nLISTSEOF\n", 1)[0]
    lines = [ln for ln in lists.splitlines()
             if ln.startswith(("ITDOG=", "DOMAINS_URL=", "SUBNET_SERVICES=", "GOOG_URL="))]
    assert len(lines) == 4, f"строки источников в скрипте списков изменились: {lines}"
    out = _sh("\n".join(lines) + '\nprintf "%s\\n%s\\n%s\\n%s" "$ITDOG" "$DOMAINS_URL" '
              '"$SUBNET_SERVICES" "$GOOG_URL"').split("\n")
    itdog, domains, services, goog = out
    assert GwChannelMixin._LAN_ITDOG == itdog
    assert GwChannelMixin._LAN_DOMAINS_URL == domains
    assert tuple(GwChannelMixin._LAN_SERVICES) == tuple(services.split()), (
        f"сервисы подсетей: ВПС {GwChannelMixin._LAN_SERVICES}, шлюз {services.split()}")
    assert GwChannelMixin._LAN_GOOG_URL == goog
