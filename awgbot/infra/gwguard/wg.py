"""wg.py — аплинк этого шлюза, политика «Telegram → аплинк», маршрут по умолчанию
(интерфейс и роутер — `via`), файл статуса скрипта обвязки, подсеть клиентов,
хендшейк и конфиг линка режимом --link-apply."""
from __future__ import annotations

import ipaddress
import json
import re

from awgbot.core import config
from awgbot.util import kvfile
from awgbot.infra import gwguard as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде
from .common import LINK_APPLY_TIMEOUT



def _awg(args: list[str]) -> str:
    try:
        proc = pkg.subprocess.run(["awg", *args], capture_output=True, timeout=10)
    except (OSError, pkg.subprocess.SubprocessError):
        return ""
    return proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""


def _endpoint_host(iface: str) -> str:
    """Хост Endpoint первого пира интерфейса (`awg show <if> endpoints`)."""
    out = pkg._awg(["show", iface, "endpoints"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "(none)":
            return parts[1].rsplit(":", 1)[0].strip("[]")
    return ""


def uplink_interface() -> str:
    """Клиентский туннель к ВПС. Задан в conf — он; иначе то, что решил
    скрипт обвязки при применении (файл статуса; без трёх exec на тик);
    иначе не-линк интерфейс с тем же хостом Endpoint, что у линка (тот же
    ВПС), иначе единственный не-линк интерфейс."""
    if config.GW_UPLINK_IF:
        return config.GW_UPLINK_IF
    from_script = pkg.script_status().get("UPLINK_IF", "").strip()
    if from_script and re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", from_script):
        return from_script
    names = [n for n in pkg._awg(["show", "interfaces"]).split() if n != config.GW_LINK_IF]
    link_host = pkg._endpoint_host(config.GW_LINK_IF)
    same = [n for n in names if link_host and pkg._endpoint_host(n) == link_host]
    if len(same) == 1:
        return same[0]
    if len(names) == 1:
        return names[0]
    return ""


def uplink_pubkey() -> tuple[str, str]:
    """(интерфейс, публичный ключ) аплинка; пусто — не нашли."""
    iface = pkg.uplink_interface()
    if not iface:
        return "", ""
    return iface, pkg._awg(["show", iface, "public-key"])


def script_status() -> dict:
    """Что решил скрипт обвязки при последнем применении: GW_STATUS
    unmarked|confirmed|foreign|unconfirmed и ключ помеченного шлюза."""
    return kvfile.read(pkg.STATUS_FILE)


def _ip_json(args: list[str]) -> list:
    try:
        proc = pkg.subprocess.run(["ip", "-j", *args], capture_output=True, timeout=10)
    except (OSError, pkg.subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        return json.loads(proc.stdout.decode(errors="replace") or "[]")
    except ValueError:
        return []


def _via_of(hop: dict) -> str | None:
    """Шлюз записи маршрута: None — шлюза нет; "" — шлюз не IPv4 (via inet6)."""
    via = hop.get("gateway")
    if not via and isinstance(hop.get("via"), dict):       # via inet6 в маршруте v4
        via = hop["via"].get("host")
    if not via:
        return None
    try:
        return str(ipaddress.IPv4Address(str(via)))
    except ValueError:
        return ""


def default_route() -> dict:
    """Маршрут по умолчанию основной таблицы одним `ip -j route show default`:
    {'present': есть ли он вообще, 'dev': интерфейс, 'via': шлюз}. via — как
    VIA4 скрипта обвязки: первый шлюз, в том числе у первого nexthop, только
    IPv4; нет шлюза или он не IPv4 — пусто. Пусто во всём — не узнать."""
    routes = [r for r in pkg._ip_json(["route", "show", "default"]) if isinstance(r, dict)]
    dev = next((str(r.get("dev")) for r in routes if r.get("dev")), "")
    via = ""
    for r in routes:
        found = next((v for v in (_via_of(h) for h in
                                  [r, *(h for h in (r.get("nexthops") or []) if isinstance(h, dict))])
                      if v is not None), None)
        if found is not None:
            via = found
            break
    return {"present": bool(routes), "dev": dev, "via": via}


def default_route_dev() -> str:
    """Интерфейс маршрута по умолчанию основной таблицы; пусто — не узнать."""
    return pkg.default_route()["dev"]


def default_route_via() -> str:
    """Шлюз маршрута по умолчанию (IPv4, как VIA4 скрипта обвязки); пусто — нет."""
    return pkg.default_route()["via"]


def setup_script_has(marker: str) -> bool:
    """Есть ли строка в копии скрипта обвязки на шлюзе (/usr/local/sbin): по
    ней видно, умеет ли установленный скрипт то, чего нет в файле статуса."""
    try:
        return marker.encode() in pkg.Path(pkg.SETUP_SCRIPT).read_bytes()
    except OSError:
        return False


def _fwmark_of(rule: dict) -> int:
    """Метка правила из `ip -j rule`: «0x1», с маской — «0x1/0xff» (чужие
    правила на той же машине); нечитаемое — 0, а не исключение на весь тик."""
    raw = str(rule.get("fwmark", "0")).split("/", 1)[0].strip()
    try:
        return int(raw, 0)
    except ValueError:
        return 0


def uplink_policy(uplink_if: str) -> dict:
    """{'rule': bool, 'route': bool} — есть ли правило «метка → таблица» и
    маршрут по умолчанию в аплинк в этой таблице."""
    rule = any(pkg._fwmark_of(r) == pkg.TG_MARK and str(r.get("table")) == str(pkg.UPLINK_TABLE)
               for r in pkg._ip_json(["rule", "show"]))
    route = any(r.get("dst") == "default" and r.get("dev") == uplink_if
                for r in pkg._ip_json(["route", "show", "table", str(pkg.UPLINK_TABLE)]))
    return {"rule": rule, "route": route}


def uplink_policy_ensure(uplink_if: str, state: dict | None = None) -> list[str]:
    """Перевыставить недостающее. state — уже снятое pkg.uplink_policy (тик
    снимает его для проверок и отдаёт сюда, чтобы не ходить в ip дважды).
    Возвращает, что было восстановлено."""
    state = state if state is not None else pkg.uplink_policy(uplink_if)
    fixed: list[str] = []
    if not state["rule"]:
        if pkg.subprocess.run(["ip", "rule", "add", "fwmark", str(pkg.TG_MARK), "lookup", str(pkg.UPLINK_TABLE)],
                          capture_output=True, timeout=10).returncode == 0:
            fixed.append("правило по метке")
    if not state["route"]:
        if pkg.subprocess.run(["ip", "route", "replace", "default", "dev", uplink_if,
                           "table", str(pkg.UPLINK_TABLE)], capture_output=True, timeout=10).returncode == 0:
            fixed.append("маршрут в аплинк")
    return fixed


def client_subnet() -> str:
    """Подсеть клиентов ВПС: из conf агента, иначе из юнита обвязки, куда её
    вшил бандл. Установщику спрашивать её незачем."""
    if config.GW_CLIENT_SUBNET:
        return config.GW_CLIENT_SUBNET
    text = pkg.unit_text()
    if not text:
        return ""
    m = re.search(r'^Environment="?CLIENT_SUBNET=([0-9./]+)"?', text, re.M)
    return m.group(1) if m else ""


def handshake_age(iface: str) -> int | None:
    """Секунд с последнего хендшейка пира интерфейса (`awg show <if>
    latest-handshakes`); None — хендшейка не было или интерфейса нет."""
    state, age = pkg.handshake_state(iface)
    return age if state == "ok" else None


def handshake_state(iface: str) -> tuple[str, int | None]:
    """("ok", секунд с хендшейка) | ("never", None) — интерфейс есть, пир ни
    разу не отвечал | ("absent", None) — интерфейса нет | ("unknown", None) —
    `awg show` не ответил. «unknown» — не «мёртв»: разовый сбой команды под
    нагрузкой не должен решать за линк и аплинк."""
    import time
    proc_out = pkg._awg(["show", iface, "latest-handshakes"])
    if not proc_out:
        return ("unknown" if pkg._ip_json(["link", "show", "dev", iface]) else "absent"), None
    best = 0
    for line in proc_out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            best = max(best, int(parts[1]))
    if not best:
        return "never", None
    return "ok", max(0, int(time.time()) - best)


def link_apply(path: str, timeout: int = LINK_APPLY_TIMEOUT, iface_nets=()) -> tuple[bool, str]:
    """Поставить конфиг линка из файла режимом обвязки `--link-apply`: скрипт
    сам проверяет его белым списком и правилом AllowedIPs (второй рубеж), а
    дальше — syncconf без перезапуска или down/up. (ok, вывод): в выводе строка
    `link: unchanged|syncconf|restarted`."""
    env = pkg._setup_env()
    env["IFACE_NETS"] = " ".join(str(n) for n in iface_nets)   # подсети интерфейсов — те же, что сверял агент
    try:
        proc = pkg.subprocess.run([pkg.SETUP_SCRIPT, "--link-apply", path], capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return False, "отсутствует скрипт обвязки — «🚑 Восстановить»"
    except pkg.subprocess.TimeoutExpired:
        return False, f"скрипт обвязки не ответил за {timeout} с"
    except (OSError, pkg.subprocess.SubprocessError) as e:
        return False, str(e)
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    return proc.returncode == 0, out


def link_applied_how(out: str) -> str:
    """Что сделал --link-apply по его выводу: unchanged | syncconf | restarted | пусто."""
    for line in reversed(out.splitlines()):
        if line.startswith("link: "):
            return line[6:].strip()
    return ""


def _setup_env() -> dict:
    """Окружение для режимов скрипта обвязки, которые агент зовёт напрямую.
    Скрипт читает значения из окружения — его обычно даёт юнит обвязки; здесь
    оно собирается из строк того же юнита: к этому моменту транзакция уже
    переписала их под документ, и проверки скрипта сверяют ровно то, с чем
    стартует обвязка."""
    import os
    env = dict(os.environ)
    for key in ("CLIENT_SUBNET", "LINK_IF", "ADMIN_IPS", "LAN_MODE", "HOME_SUBNETS", "RESOLVER",
                "PEER_HOME_NETS", "LINK_CHANNEL_PORT", "GATEWAY_PUBKEY", "GATEWAY_PREV_PUBKEY",
                "TG_MARK", "UPLINK_TABLE"):
        val = pkg.unit_env(key)
        if val or key not in env:
            env[key] = val
    env["LINK_IF"] = env.get("LINK_IF") or config.GW_LINK_IF
    env.setdefault("HOST_CONF_DIR", config.GW_CONF_DIR)
    for key in ("TG_MARK", "UPLINK_TABLE"):
        if not env.get(key):
            env.pop(key, None)                     # пусто — пусть скрипт возьмёт своё умолчание
    return env
