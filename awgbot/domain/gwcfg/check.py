"""check.py — проверки значений документа по разделам; конфиги WireGuard — через util/wgconf."""
from __future__ import annotations

import ipaddress
import re

from awgbot.util import wgconf
from awgbot.util.wgconf import norm

from .doc import CfgError, ENV_KEYS, ENV_REQUIRED, MAIL_HINT

MIN_PHRASE = 8                                   # как backupcrypto.MIN_PASSPHRASE_LEN — сверяет тест
_MAX_ADMIN_IPS = 64
_MAX_SUBNETS = 64
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _ipv4(tok: str) -> bool:
    try:
        return ipaddress.ip_address(tok).version == 4
    except ValueError:
        return False


def _net4(tok: str, *, strict: bool = True):
    try:
        net = ipaddress.ip_network(tok, strict=strict)
    except ValueError:
        return None
    return net if net.version == 4 and "/" in tok else None


def _private(net) -> bool:
    return any(net.subnet_of(p) for p in _PRIVATE)


def _port(val, what: str) -> int:
    try:
        port = int(val)
    except (TypeError, ValueError):
        raise CfgError(f"{what}: порт — не число") from None
    if not 1 <= port <= 65535:
        raise CfgError(f"{what}: порт вне 1–65535")
    return port


# ── env ──────────────────────────────────────────────────────────────────────

def check_env(env: dict) -> dict:
    """Проверить раздел env по типу каждого значения; вернуть нормализованный
    словарь всех ключей. Отсутствует обязательный ключ или не прошло одно
    значение — CfgError: половина новых настроек хуже старых целиком."""
    if not isinstance(env, dict):
        raise CfgError("env — не словарь")
    out: dict[str, str] = {}
    for key in ENV_KEYS:
        if key not in env:
            if key in ENV_REQUIRED:
                raise CfgError(f"в env нет {key}")
            continue
        val = norm(env[key])
        toks = val.split()
        if len(toks) > _MAX_SUBNETS:
            raise CfgError(f"{key}: слишком много значений")
        if key == "CLIENT_SUBNET":
            net = _net4(val)
            if net is None or not _private(net) or not 16 <= net.prefixlen <= 28:
                raise CfgError("CLIENT_SUBNET: нужна частная IPv4-подсеть с префиксом /16–/28")
        elif key == "ADMIN_IPS":
            if len(toks) > _MAX_ADMIN_IPS or not all(_ipv4(t) for t in toks):
                raise CfgError("ADMIN_IPS: только IPv4-адреса, не больше 64")
        elif key == "LAN_MODE":
            if val not in ("0", "1"):
                raise CfgError("LAN_MODE: только 0 или 1")
        elif key == "RESOLVER":
            if val and not _ipv4(val):
                raise CfgError("RESOLVER: не IPv4-адрес")
        elif key == "LAN_SUBNETS":
            if not all(_net4(t, strict=False) for t in toks):
                raise CfgError("LAN_SUBNETS: не подсети IPv4")
        elif key == "PEER_LAN_NETS":
            nets = []
            for t in toks:
                net = _net4(t)
                if net is None or not _private(net):
                    raise CfgError(f"PEER_LAN_NETS: {t} — не частная IPv4-подсеть")
                if net.prefixlen < 16 or net.prefixlen > 30:
                    raise CfgError(f"PEER_LAN_NETS: {t} — префикс вне /16–/30")
                if any(net.overlaps(o) for o in nets):
                    raise CfgError(f"PEER_LAN_NETS: {t} пересекается с другой подсетью")
                nets.append(net)
        elif key == "LINK_CHANNEL_PORT":
            _port(val, "LINK_CHANNEL_PORT")
        out[key] = val
    return out


# ── прочие разделы ───────────────────────────────────────────────────────────

def check_mail(mail: dict) -> dict:
    """Почта: логин до 254 знаков, хосты [A-Za-z0-9.-], порты 1–65535,
    подсказка backup ∈ {email, telegram, пусто}."""
    if not isinstance(mail, dict):
        raise CfgError("mail — не словарь")
    login = str(mail.get("login") or "")
    if not 1 <= len(login) <= 254 or "\n" in login:
        raise CfgError("почта: логин пуст или длиннее 254 знаков")
    password = str(mail.get("password") or "")
    if not password or "\n" in password or len(password) > 512:
        raise CfgError("почта: пароль пуст")
    for host_key in ("imap_host", "smtp_host"):
        if not _HOST_RE.match(str(mail.get(host_key) or "")):
            raise CfgError(f"почта: {host_key} — недопустимое имя хоста")
    out = {"login": login, "password": password,
           "imap_host": str(mail["imap_host"]), "smtp_host": str(mail["smtp_host"]),
           "imap_port": _port(mail.get("imap_port"), "почта: imap_port"),
           "smtp_port": _port(mail.get("smtp_port"), "почта: smtp_port"),
           MAIL_HINT: norm(mail.get(MAIL_HINT))}
    if out[MAIL_HINT] not in ("", "email", "telegram"):
        raise CfgError("почта: backup — только email или telegram")
    return out


def check_backup(backup: dict) -> dict:
    """Фраза шифрования — те же правила, что у ввода на шлюзе."""
    if not isinstance(backup, dict):
        raise CfgError("backup — не словарь")
    phrase = str(backup.get("phrase") or "").strip()
    if len(phrase) < MIN_PHRASE:
        raise CfgError(f"фраза шифрования короче {MIN_PHRASE} знаков")
    if "\n" in phrase or len(phrase) > 512:
        raise CfgError("фраза шифрования: недопустимое значение")
    return {"phrase": phrase}


def check_token(token: dict) -> dict:
    """Токен бота — как при вводе на ВПС."""
    if not isinstance(token, dict):
        raise CfgError("token — не словарь")
    value = norm(token.get("value"))
    if not _TOKEN_RE.match(value):
        raise CfgError("токен: не похоже на токен бота")
    return {"value": value}


def check_server(server: dict) -> dict:
    if not isinstance(server, dict):
        raise CfgError("server — не словарь")
    name = norm(server.get("name"))
    if not _NAME_RE.match(name):
        raise CfgError("имя сервера: только [A-Za-z0-9._-], до 64 знаков")
    return {"name": name}


def check_link(conf_text: str, env: dict, *, link_cidr: str, address: str = "", fixed: dict | None = None,
               iface_nets=()) -> wgconf.Conf:
    """Конфиг линка из документа: белый список ключей, ровно один [Peer],
    только штамп в комментариях, Address — тот же адрес в той же /30, ключи,
    PSK и обфускация — те же, что стоят (другие меняются только переходом),
    AllowedIPs — по строгому правилу из того же документа. env — уже
    проверенный раздел env. Возвращает разобранный конфиг."""
    try:
        conf = wgconf.parse(conf_text)
        wgconf.check_keys(conf, "link")
    except wgconf.WgConfError as e:
        raise CfgError(f"конфиг линка: {e}") from None
    addr = conf.get("Interface", "Address")
    try:
        iface = ipaddress.ip_interface(addr)
    except ValueError:
        raise CfgError("конфиг линка: Address — не адрес") from None
    want_net = _net4(link_cidr)
    if want_net is None or iface.network != want_net or iface.ip in (want_net.network_address, want_net.broadcast_address):
        raise CfgError(f"конфиг линка: Address {addr} не в /30 линка {link_cidr}")
    if address and str(iface) != str(ipaddress.ip_interface(address)):
        raise CfgError("конфиг линка: Address другой — адрес линка каналом не меняется")
    for section, key in (("Interface", "PrivateKey"), ("Peer", "PublicKey"), ("Peer", "PresharedKey"),
                         *(("Interface", k) for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
                                                     "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5"))):
        if fixed and key in fixed and norm(conf.get(section, key)) != norm(fixed[key]):
            raise CfgError("конфиг линка: ключи, PSK и обфускация меняются только переходом ключей")
    if not conf.get("Peer", "Endpoint"):
        raise CfgError("конфиг линка: нет Endpoint")
    host, _, port = conf.get("Peer", "Endpoint").rpartition(":")
    if not host or not _HOST_RE.match(host.strip("[]")):
        raise CfgError("конфиг линка: Endpoint — недопустимый хост")
    _port(port, "конфиг линка: Endpoint")
    ka = norm(conf.get("Peer", "PersistentKeepalive"))
    if ka and not re.fullmatch(r"\d{1,5}(-\d{1,5})?", ka):
        raise CfgError("конфиг линка: PersistentKeepalive — число или диапазон")
    try:
        wgconf.check_allowed_ips(conf.get("Peer", "AllowedIPs"),
                                 client_subnet=env.get("CLIENT_SUBNET", ""), link_cidr=link_cidr,
                                 peer_nets=norm(env.get("PEER_LAN_NETS")).split(),
                                 resolver=norm(env.get("RESOLVER")),
                                 lan_subnets=norm(env.get("LAN_SUBNETS")).split(), iface_nets=iface_nets)
    except wgconf.WgConfError as e:
        raise CfgError(f"конфиг линка: {e}") from None
    return conf


def check_uplink(uplink: dict) -> wgconf.Conf:
    """Конфиг аплинка: белый список, Table = off, ключи признания — base64 32 байт."""
    if not isinstance(uplink, dict):
        raise CfgError("uplink — не словарь")
    try:
        conf = wgconf.parse(str(uplink.get("conf") or ""))
        wgconf.check_keys(conf, "uplink")
    except wgconf.WgConfError as e:
        raise CfgError(f"конфиг аплинка: {e}") from None
    if norm(conf.get("Interface", "Table")).lower() != "off":
        raise CfgError("конфиг аплинка: нужен Table = off")
    if not wgconf.is_pubkey(uplink.get("pubkey")):
        raise CfgError("конфиг аплинка: pubkey — не ключ")
    if uplink.get("prev_pubkey") and not wgconf.is_pubkey(uplink.get("prev_pubkey")):
        raise CfgError("конфиг аплинка: prev_pubkey — не ключ")
    return conf


def check_doc(parsed: dict, *, link_cidr: str, address: str = "", fixed: dict | None = None,
              iface_nets=()) -> dict:
    """Проверить разобранный документ целиком до первого действия. Возвращает
    {раздел: проверенное значение}; не прошло одно — CfgError, шлюз не тронут.
    Раздел env обязателен; остальные — какие приехали."""
    sections = parsed.get("sections") or {}
    if "env" not in sections:
        raise CfgError("в документе нет раздела env")
    out: dict = {"env": check_env(sections["env"])}
    if "link" in sections:
        out["link"] = check_link(sections["link"].get("conf", ""), out["env"], link_cidr=link_cidr,
                                 address=address, fixed=fixed, iface_nets=iface_nets)
    if "server" in sections:
        out["server"] = check_server(sections["server"])
    if "mail" in sections:
        out["mail"] = check_mail(sections["mail"])
    if "backup" in sections:
        out["backup"] = check_backup(sections["backup"])
    if "token" in sections:
        out["token"] = check_token(sections["token"])
    if "uplink" in sections:
        out["uplink"] = check_uplink(sections["uplink"])
    return out
