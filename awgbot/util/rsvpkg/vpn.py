"""
vpn.py — часть VPN пакета: интерфейсы клиентов основы и сборка конфига клона.

Из живого конфига интерфейса берутся только поля, без которых клон не
поднять: адрес, порт, закрытый ключ, MTU, обфускация (Jc…H4 и прочие ключи
протокола, кроме I1–I5), пиры — открытый ключ, PSK, адрес в туннеле. Хуки
(`PreUp`/`PostUp`/`PreDown`/`PostDown`), `Table`, `DNS`, пути и комментарии не
передаются никогда: пакет — данные, а конфиг клона резерв собирает сам из
проверенных полей (`clone_conf`). Пир с адресом вне подсети интерфейса
отбрасывается.
"""
from __future__ import annotations

import ipaddress
import re

from awgbot.infra.awg.parsers import _OBFUSCATION_KEYS, _extract_param, _split_conf, parse_interface_params

_B64 = re.compile(r"[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=\Z")
_EXCLUDED_OBF = ("I1", "I2", "I3", "I4", "I5")
MAX_PEERS = 4096


class VpnPartError(ValueError):
    """Конфиг интерфейса не годится для пакета."""


def _key(value: str, what: str) -> str:
    v = str(value or "").strip()
    if not _B64.match(v):
        raise VpnPartError(f"{what}: не похож на ключ")
    return v


def _peer_ip(lines: list[str]) -> str:
    for l in lines:
        s = l.strip()
        if s.startswith("AllowedIPs"):
            first = s.split("=", 1)[1].split(",")[0].strip()
            return first.split("/", 1)[0]
    return ""


def _peer_psk(lines: list[str]) -> str:
    for l in lines:
        s = l.strip()
        if s.startswith("PresharedKey"):
            return s.split("=", 1)[1].strip()
    return ""


def iface_part(name: str, conf_text: str) -> dict:
    """Один интерфейс из живого конфига → поле `ifaces[]` пакета."""
    header, peers = _split_conf(conf_text)
    params = parse_interface_params(header)          # обфускация и ListenPort
    addr = str(_extract_param(header, "Address") or "").split(",")[0].strip()
    try:
        net = ipaddress.ip_interface(addr).network
    except ValueError as e:
        raise VpnPartError(f"{name}: адрес интерфейса не разобрался") from e
    try:
        port = int(params.get("ListenPort", ""))
    except (TypeError, ValueError) as e:
        raise VpnPartError(f"{name}: нет ListenPort") from e
    obf = {k: str(params[k]) for k in _OBFUSCATION_KEYS if k in params and k not in _EXCLUDED_OBF}
    out = {
        "name": str(name), "addr": addr, "port": port,
        "priv": _key(_extract_param(header, "PrivateKey") or "", f"{name}: PrivateKey"),
        "obf": obf, "peers": [],
    }
    mtu = str(_extract_param(header, "MTU") or "").strip()
    if mtu.isdigit():
        out["mtu"] = int(mtu)
    for p in peers[:MAX_PEERS]:
        ip = _peer_ip(p["lines"])
        try:
            if not ip or ipaddress.ip_address(ip) not in net:
                continue
        except ValueError:
            continue
        pub, psk = p.get("pubkey") or "", _peer_psk(p["lines"])
        if not (_B64.match(pub) and (not psk or _B64.match(psk))):
            continue
        out["peers"].append({"pub": pub, "psk": psk, "ip": ip})
    return out


def build(ifaces: list[tuple[str, str]], *, admin: list[str], dns: list[str], upstreams: list[str],
          tz: str) -> dict:
    """Часть VPN целиком: интерфейсы (имя, текст конфига) и поля зоны."""
    return {
        "ifaces": [iface_part(n, t) for n, t in ifaces],
        "admin": [str(a) for a in admin][:64],
        "dns": [str(d) for d in dns][:8],
        "upstreams": [str(u) for u in upstreams][:8],
        "tz": str(tz or ""),
    }


def check(part: dict) -> dict:
    """Проверить часть VPN, пришедшую с другой стороны: типы, ключи, адреса.
    Негодное — VpnPartError."""
    if not isinstance(part, dict) or not isinstance(part.get("ifaces"), list) or not part["ifaces"]:
        raise VpnPartError("часть VPN без интерфейсов")
    for i in part["ifaces"]:
        if not isinstance(i, dict):
            raise VpnPartError("интерфейс не того вида")
        if not re.fullmatch(r"[a-z][a-z0-9]{1,14}", str(i.get("name", ""))):
            raise VpnPartError("имя интерфейса не годится")
        net = ipaddress.ip_interface(str(i.get("addr", ""))).network
        if not 1 <= int(i.get("port", 0)) <= 65535:
            raise VpnPartError("порт интерфейса не годится")
        _key(i.get("priv", ""), "PrivateKey")
        if not isinstance(i.get("obf"), dict) or any(k in _EXCLUDED_OBF or k not in _OBFUSCATION_KEYS for k in i["obf"]):
            raise VpnPartError("обфускация не того вида")
        if not isinstance(i.get("peers"), list) or len(i["peers"]) > MAX_PEERS:
            raise VpnPartError("пиры не того вида")
        for p in i["peers"]:
            _key(p.get("pub", ""), "PublicKey")
            if p.get("psk"):
                _key(p["psk"], "PresharedKey")
            if ipaddress.ip_address(str(p.get("ip", ""))) not in net:
                raise VpnPartError("адрес пира вне подсети интерфейса")
    for k in ("admin", "dns", "upstreams"):
        for a in part.get(k, []):
            ipaddress.ip_address(str(a))
    return part


def clone_conf(iface: dict) -> str:
    """Конфиг клона из проверенного поля `ifaces[]`: без хуков, без Table,
    пиры — ровно те, что в пакете."""
    lines = ["[Interface]", f"Address = {iface['addr']}", f"ListenPort = {int(iface['port'])}",
             f"PrivateKey = {iface['priv']}"]
    if iface.get("mtu"):
        lines.append(f"MTU = {int(iface['mtu'])}")
    for k in _OBFUSCATION_KEYS:
        if k in iface.get("obf", {}):
            lines.append(f"{k} = {iface['obf'][k]}")
    out = "\n".join(lines)
    for p in iface.get("peers", []):
        block = ["[Peer]", f"PublicKey = {p['pub']}"]
        if p.get("psk"):
            block.append(f"PresharedKey = {p['psk']}")
        block.append(f"AllowedIPs = {p['ip']}/32")
        out += "\n\n" + "\n".join(block)
    return out + "\n"
