"""Общие данные снимков агента шлюза: состояние хоста — тик, панель, SSH,
VPN-транзит, релиз, итог обвязки — фиксированными словарями.
"""
from __future__ import annotations

import base64
import copy

from awgbot.domain import gwchecks as gc
from awgbot.domain.gateway import GwCheck, GwStatus
from awgbot.infra import updates


# ── состояние хоста ──────────────────────────────────────────────────────────

def _x25519(seed: bytes) -> tuple[str, str]:
    """Пара X25519 из постоянного зерна (снимок детерминирован): (приватный, публичный).
    Зёрна — те же, что у раздела uplink в снимках карточки слота (shots_admin):
    отпечаток в приглашении совпадает с отпечатком строки К3."""
    import hashlib
    from nacl.public import PrivateKey
    key = PrivateKey(hashlib.sha256(seed).digest())
    return base64.b64encode(bytes(key)).decode(), base64.b64encode(bytes(key.public_key)).decode()


_UP_DEV_PRIV, _UP_DEV_PUB = _x25519(b"DUMMY-uplink-device")
_UP_SRV_PRIV, _UP_SRV_PUB = _x25519(b"DUMMY-uplink-server")
_UPLINK_VALUE = {
    "conf": (f"[Interface]\nPrivateKey = {_UP_DEV_PRIV}\nAddress = 10.8.1.7/32\nTable = off\n\n"
             f"[Peer]\nPublicKey = {_UP_SRV_PUB}\nPresharedKey = DUMMY-PSK=\n"
             "Endpoint = vpn.example.org:43125\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n"),
    "pubkey": _UP_DEV_PUB, "prev_pubkey": "DUMMY-PREV-PUB="}


_CHECKS_OK = [
    GwCheck(gc.CHECK_LINK, True, "хендшейк 40 с назад"),
    GwCheck(gc.CHECK_GUARD, True),
    GwCheck(gc.CHECK_IP_FORWARD, True),
    GwCheck(gc.CHECK_TG_ROUTE, True),
    GwCheck(gc.CHECK_EGRESS, True, "35 мс через провайдера"),
    GwCheck(gc.CHECK_MODULE, True, "собран для всех ядер (2)"),
]

_CHECKS_LAN = [
    GwCheck(gc.CHECK_TRANSIT, True, group="lan"),
    GwCheck(gc.CHECK_RESOLVER, True, group="lan"),
    GwCheck(gc.CHECK_LISTS, True, "обновлены 15.09 06:00", group="lan"),
    GwCheck(gc.CHECK_SMB_OWN, True, "nas", group="svc"),
]


def _ssh_panel(**kw) -> dict:
    """Строка SSH панели: порт, владелец, фильтр снаружи, адресов."""
    d = {"port": 22, "owner": "", "filter": False, "allow": 0, "new_plumbing": True}
    d.update(kw)
    return d


def _lan(**kw) -> dict:
    """VPN-транзит включён: интерфейс, адрес, резолвер, списки, свои, SMB."""
    d = {"iface": "end0", "addr": "192.168.1.10", "resolver": "10.8.0.1", "uplink": "awg0",
         "domains": 1234, "nets": 56, "updated_at": "2026-09-15T06:00:00+05:00",
         "own_vpn": 2, "own_ru": 1, "lan_pkts": 123456,
         "svc": {"active": True, "own": ["nas"], "peer": ["nas-2", "printer"], "ever": True}}
    d.update(kw)
    return d


def _status(**kw) -> GwStatus:
    """Живой замер здорового шлюза без VPN-транзита."""
    d = dict(link_up=True, handshake_age=40.0, rx=3 * 1024 ** 3, tx=512 * 1024 ** 2,
             checks=copy.deepcopy(_CHECKS_OK), temp=48.3, throttled={"now": [], "ever": []},
             cpu=12.0, ram=41.0, ram_free_mb=2345, disk=23.0, disk_free_gb=87.4, smart="OK",
             disks=[[26.1, 113.5, "/"], [1105.2, 1720.3, "/srv/dev-disk-by-uuid-a1"], [0.4, 931.5, "/srv/usb"]],
             uptime_seconds=12 * 86400 + 3 * 3600, hostname="pi", server_name="awg-srv",
             module_version="1.0.20260901", srcversion="ABCDEF0123456789", kernels_total=2,
             mark_status="confirmed", ssh=_ssh_panel())
    d.update(kw)
    return GwStatus(**d)


def _status_lan(**kw) -> GwStatus:
    d = dict(lan=_lan(), checks=copy.deepcopy(_CHECKS_OK + _CHECKS_LAN))
    d.update(kw)
    return _status(**d)


def _ssh(**kw) -> dict:
    """Раздел «🛡 SSH-доступ» (gwssh.ssh_screen): новая обвязка, фильтр выключен."""
    d = {"port": 22, "sshd_down": False, "ports": [22], "env_port": 22, "conf_ports": [22],
         "owner": "", "owner_where": "", "owner_port": None, "owner_detail": "", "owner_files": [],
         "filter": False, "allow": [], "resolved": [], "unresolved": [], "held": [],
         "admin_ips": ["10.8.1.2", "10.8.1.3"], "server": "203.0.113.10",
         "lan": ["192.168.1.0/24"], "peer_nets": [], "new_plumbing": True,
         "table_ports": {"tunnel_in": 22}, "ufw": False, "omv_rules": 0}
    d.update(kw)
    return d


_OWN_OFF = {"active": False, "state": "off"}


_RELEASE = updates.Release(
    tag="v1.2.4", version=(1, 2, 4), asset_url=None, sha256=None,
    body="### Шлюз\n- панель: строка SSH с владельцем порта\n- «🔀 VPN-транзит»: свои списки листаются\n"
         "### Исправлено\n- итог применения конфигурации не терял строку почты\n\n#requires_gw_1.2.0")


_TICK_LAN = _status_lan()
_MAIL = {"mail": True, "mail_last": "ok"}
