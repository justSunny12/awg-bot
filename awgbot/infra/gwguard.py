"""
gwguard.py — сторона ШЛЮЗА: чтение таблицы `inet awg_gw_guard`, которую ставит
routing-gw-setup.sh (юнит awg-link-gw.service), и локальные добавки к ней.

Генератор таблицы — ОДИН, скрипт: он же реассертит её при загрузке и по
`systemctl restart awg-link-gw.service`. Агент таблицу не пишет — только
читает (проверки, панель) и просит перевыставить; единственное исключение —
наборы с динамикой (`ssh_allow4`, `server4`: резолв имён DynDNS), которые
агент наполняет сам через `set_sync`, как основной бот держит admin4.

Локальное состояние файервола шлюза — /etc/awg-gw/firewall.env, его читают
скрипт и юнит (EnvironmentFile): ADMIN_IPS_EXTRA (доверенные из туннеля сверх
бандла, `awg-bot firewall allow`), SSH_PORT (факт: порт, который слушает
sshd — пишет агент), SSH_FILTER, SSH_ALLOW, SSH_ALLOW_RESOLVED (SSH снаружи,
раздел «Доступ по SSH» агента и `awg-bot ssh …`).
"""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from awgbot.core import config

TABLE_FAMILY = "inet"
TABLE_NAME = "awg_gw_guard"
TABLE = f"{TABLE_FAMILY} {TABLE_NAME}"
FW_ENV = "/etc/awg-gw/firewall.env"
CHAINS = ("input", "tunnel_in", "forward", "postrouting", "output")


class GwGuardError(RuntimeError):
    pass


def _nft(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["nft", *args], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise GwGuardError("nft не найден — установите пакет nftables")
    except subprocess.TimeoutExpired:
        raise GwGuardError("таймаут nft")


def _elem_str(el) -> str:
    if isinstance(el, str):
        return el
    if isinstance(el, dict):
        if "prefix" in el:
            return f"{el['prefix']['addr']}/{el['prefix']['len']}"
        if "elem" in el:
            return _elem_str(el["elem"].get("val"))
        if "val" in el:
            return _elem_str(el["val"])
    return str(el)


def table_info() -> Optional[dict]:
    """{'sets': {имя: set(str)}, 'chains': set(имя), 'masq_ifaces': set(str),
    'ssh_ports': {цепочка: порт}} или None — таблицы нет. masq_ifaces — в
    какие интерфейсы стоит masquerade (по oifname); ssh_ports — какой tcp
    dport держат правила про SSH (tunnel_in: сервер по линку; input: переход
    на ssh_in). Один exec: `nft -j list table`."""
    proc = _nft(["-j", "list", "table", TABLE_FAMILY, TABLE_NAME])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise GwGuardError(f"nft -j: {e}")
    sets: dict[str, set[str]] = {}
    chains: set[str] = set()
    masq: set[str] = set()
    ssh_ports: dict[str, int] = {}
    for item in doc.get("nftables", []):
        if "set" in item:
            s = item["set"]
            sets[s["name"]] = {_elem_str(e) for e in (s.get("elem") or [])}
        elif "chain" in item:
            chains.add(item["chain"]["name"])
        elif "rule" in item:
            expr = item["rule"].get("expr") or []
            if any("masquerade" in e for e in expr if isinstance(e, dict)):
                for e in expr:
                    m = e.get("match") if isinstance(e, dict) else None
                    if m and (m.get("left") or {}).get("meta", {}).get("key") == "oifname":
                        masq.add(str(m.get("right")))
            for e in expr:
                m = e.get("match") if isinstance(e, dict) else None
                left = (m or {}).get("left") or {}
                if m and left.get("payload", {}).get("field") == "dport" \
                        and left["payload"].get("protocol") == "tcp" and isinstance(m.get("right"), int):
                    ssh_ports[str(item["rule"].get("chain"))] = int(m["right"])
    return {"sets": sets, "chains": chains, "masq_ifaces": masq, "ssh_ports": ssh_ports}


def iptables_forward_policy() -> Optional[str]:
    """Политика чужой цепочки ip filter FORWARD: accept в нашей таблице не
    отменяет drop в ней. docker ставит DROP, если сам включал ip_forward."""
    proc = _nft(["-j", "list", "chain", "ip", "filter", "FORWARD"])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError:
        return None
    for item in doc.get("nftables", []):
        if "chain" in item:
            return str(item["chain"].get("policy") or "accept")
    return None


# ── локальное состояние: /etc/awg-gw/firewall.env ───────────────────────────
ENV_KEYS = ("ADMIN_IPS_EXTRA", "SSH_PORT", "SSH_FILTER", "SSH_ALLOW", "SSH_ALLOW_RESOLVED")
_ENV_HEAD = ("# awg-bot (шлюз): локальное состояние файервола. Правится из чата агента и\n"
             "# командами awg-bot firewall / awg-bot ssh; читают юнит awg-link-gw и скрипт\n"
             "# обвязки. ADMIN_IPS_EXTRA — доверенные из туннеля сверх бандла; SSH_PORT —\n"
             "# порт, который слушает sshd (факт, пишет агент); SSH_FILTER/SSH_ALLOW/\n"
             "# SSH_ALLOW_RESOLVED — фильтр SSH снаружи и его адреса.\n")
_ENV_LINE_RE = re.compile(r'^([A-Z_][A-Z0-9_]*)="([^"\n]*)"$', re.M)
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")


def read_env() -> dict[str, str]:
    """Все NAME="…" из файла; нет файла — пусто."""
    try:
        text = Path(FW_ENV).read_text(encoding="utf-8")
    except OSError:
        return {}
    return {m.group(1): m.group(2) for m in _ENV_LINE_RE.finditer(text)}


def _check_env_value(key: str, value: str) -> str:
    """Файл исполняется sh (`. "$FW_ENV"`): каждое значение — строгий набор
    символов. Списки адресов — IP/CIDR/имена через пробел, порт — число."""
    v = " ".join(str(value).split())
    if key == "SSH_PORT":
        if v and not (v.isdigit() and 1 <= int(v) <= 65535):
            raise ValueError(f"SSH_PORT: {v!r} не порт")
        return v
    if key == "SSH_FILTER":
        return "1" if v in ("1", "true", "True") else "0"
    for tok in v.split():
        try:
            ipaddress.ip_network(tok, strict=False)
            continue
        except ValueError:
            pass
        if key == "SSH_ALLOW" and _HOST_RE.fullmatch(tok):
            continue
        raise ValueError(f"{key}: {tok!r} не адрес" + (", не подсеть и не имя" if key == "SSH_ALLOW" else ""))
    return v


def write_env(**fields: str) -> dict[str, str]:
    """Обновить указанные ключи, остальное сохранить; запись целиком, атомарно.
    Возвращает итоговое содержимое."""
    cur = read_env()
    for k, v in fields.items():
        if k not in ENV_KEYS:
            raise ValueError(f"неизвестный ключ {k}")
        cur[k] = _check_env_value(k, v)
    p = Path(FW_ENV)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = _ENV_HEAD + "".join(f'{k}="{cur[k]}"\n' for k in ENV_KEYS if k in cur) \
        + "".join(f'{k}="{v}"\n' for k, v in cur.items() if k not in ENV_KEYS)
    fd, tmp = tempfile.mkstemp(prefix=".firewall.env.", dir=str(p.parent))
    with open(fd, "w", encoding="utf-8") as f:
        f.write(body)
    Path(tmp).chmod(0o644)
    Path(tmp).replace(p)
    return cur


def read_extra() -> list[str]:
    return [t for t in read_env().get("ADMIN_IPS_EXTRA", "").split() if t]


def write_extra(entries: list[str]) -> None:
    for e in entries:
        ipaddress.ip_network(e, strict=False)          # ValueError наружу
    write_env(ADMIN_IPS_EXTRA=" ".join(entries))


def set_sync(name: str, desired: set[str], info: dict | None = None) -> bool:
    """Привести живой набор таблицы к desired одной транзакцией
    (`flush set` + `add element`). Нет расхождения — ни одного exec.
    Возвращает, менял ли. Таблицы или набора нет — False (старая обвязка)."""
    if info is None:
        info = table_info()
    if not info or name not in info["sets"]:
        return False
    live = {_norm_elem(e) for e in info["sets"][name]}
    want = {_norm_elem(e) for e in desired}
    if live == want:
        return False
    lines = [f"flush set {TABLE} {name}"]
    if want:
        lines.append(f"add element {TABLE} {name} {{ {', '.join(sorted(want))} }}")
    fd, tmp = tempfile.mkstemp(prefix="awg-gw-set-", suffix=".nft")
    try:
        with open(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        proc = _nft(["-f", tmp])
        if proc.returncode != 0:
            raise GwGuardError("nft -f: " + proc.stderr.decode(errors="replace").strip()[-200:])
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass
    return True


def _norm_elem(e: str) -> str:
    """nft отдаёт /32 без маски и подсети с ней; сравниваем в одном виде."""
    try:
        net = ipaddress.ip_network(str(e), strict=False)
    except ValueError:
        return str(e)
    return str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)


def server_host() -> str:
    """Хост сервера снаружи: живой эндпоинт пира линка, запас — Endpoint из
    конфига линка. IP или имя; пусто — не узнать."""
    host = _endpoint_host(config.GW_LINK_IF)
    if host:
        return host
    try:
        text = Path(config.GW_LINK_CONF).read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^\s*Endpoint\s*=\s*(\S+)", text, re.M)
    if not m:
        return ""
    return m.group(1).rsplit(":", 1)[0].strip("[]")


def unit_admin_ips() -> list[str]:
    """ADMIN_IPS из юнита — что приехало в бандле (устройства админа)."""
    try:
        text = Path(f"/etc/systemd/system/{config.GW_UNIT}").read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r'^Environment="?ADMIN_IPS=([^"\n]*)"?', text, re.M)
    return [t for t in (m.group(1).split() if m else []) if t]


def unit_state() -> dict:
    """Состояние юнита обвязки: ActiveState / UnitFileState / Result. Нужно,
    чтобы отличать «таблицы нет, потому что обвязка старого образца» от «юнит
    не отработал» и «юнит выключен» — лечатся они по-разному."""
    try:
        proc = subprocess.run(["systemctl", "show", config.GW_UNIT, "--property=ActiveState",
                               "--property=UnitFileState", "--property=Result"],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def reassert() -> tuple[bool, str]:
    """Перевыставить таблицу: рестарт юнита — тот зовёт скрипт с окружением
    бандла. Линк скрипт не трогает, если конфиг не менялся."""
    proc = subprocess.run(["systemctl", "restart", config.GW_UNIT],
                          capture_output=True, timeout=90)
    ok = proc.returncode == 0
    return ok, "" if ok else proc.stderr.decode(errors="replace").strip()[-300:]


# ── шлюзовое устройство: аплинк этой машины и решение скрипта ────────────────
STATUS_FILE = "/etc/awg-gw/gateway.status"


def _awg(args: list[str]) -> str:
    try:
        proc = subprocess.run(["awg", *args], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""


def _endpoint_host(iface: str) -> str:
    """Хост Endpoint первого пира интерфейса (`awg show <if> endpoints`)."""
    out = _awg(["show", iface, "endpoints"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "(none)":
            return parts[1].rsplit(":", 1)[0].strip("[]")
    return ""


def uplink_interface() -> str:
    """Клиентский туннель к ВПС. Задан в conf — он; иначе не-линк интерфейс с
    тем же хостом Endpoint, что у линка (тот же ВПС), иначе единственный
    не-линк интерфейс."""
    if config.GW_UPLINK_IF:
        return config.GW_UPLINK_IF
    names = [n for n in _awg(["show", "interfaces"]).split() if n != config.GW_LINK_IF]
    link_host = _endpoint_host(config.GW_LINK_IF)
    same = [n for n in names if link_host and _endpoint_host(n) == link_host]
    if len(same) == 1:
        return same[0]
    if len(names) == 1:
        return names[0]
    return ""


def uplink_pubkey() -> tuple[str, str]:
    """(интерфейс, публичный ключ) аплинка; пусто — не нашли."""
    iface = uplink_interface()
    if not iface:
        return "", ""
    return iface, _awg(["show", iface, "public-key"])


def script_status() -> dict:
    """Что решил скрипт обвязки при последнем применении: GW_STATUS
    unmarked|confirmed|foreign|unconfirmed и ключ помеченного шлюза."""
    out = {}
    try:
        for line in Path(STATUS_FILE).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


# ── политика «Telegram → аплинк»: ip rule по метке + маршрут в таблице ──────
# Ставит PostUp аплинка при подъёме; systemd-networkd при своём (пере)запуске
# по умолчанию сносит ЧУЖИЕ ip rule и маршруты — интерфейс жив, метка стоит,
# а пакеты Telegram уходят домашнему провайдеру. Агент проверяет каждый тик и
# перевыставляет сам: это два idempotent-вызова ip, юнит дёргать незачем.

UPLINK_TABLE = 100
TG_MARK = 1


def _ip_json(args: list[str]) -> list:
    try:
        proc = subprocess.run(["ip", "-j", *args], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        return json.loads(proc.stdout.decode(errors="replace") or "[]")
    except ValueError:
        return []


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
    rule = any(_fwmark_of(r) == TG_MARK and str(r.get("table")) == str(UPLINK_TABLE)
               for r in _ip_json(["rule", "show"]))
    route = any(r.get("dst") == "default" and r.get("dev") == uplink_if
                for r in _ip_json(["route", "show", "table", str(UPLINK_TABLE)]))
    return {"rule": rule, "route": route}


def uplink_policy_ensure(uplink_if: str, state: dict | None = None) -> list[str]:
    """Перевыставить недостающее. state — уже снятое uplink_policy (тик
    снимает его для проверок и отдаёт сюда, чтобы не ходить в ip дважды).
    Возвращает, что было восстановлено."""
    state = state if state is not None else uplink_policy(uplink_if)
    fixed: list[str] = []
    if not state["rule"]:
        if subprocess.run(["ip", "rule", "add", "fwmark", str(TG_MARK), "lookup", str(UPLINK_TABLE)],
                          capture_output=True, timeout=10).returncode == 0:
            fixed.append("правило по метке")
    if not state["route"]:
        if subprocess.run(["ip", "route", "replace", "default", "dev", uplink_if,
                           "table", str(UPLINK_TABLE)], capture_output=True, timeout=10).returncode == 0:
            fixed.append("маршрут в аплинк")
    return fixed


def client_subnet() -> str:
    """Подсеть клиентов ВПС: из conf агента, иначе из юнита обвязки, куда её
    вшил бандл. Установщику спрашивать её незачем."""
    if config.GW_CLIENT_SUBNET:
        return config.GW_CLIENT_SUBNET
    try:
        text = Path(f"/etc/systemd/system/{config.GW_UNIT}").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^Environment="?CLIENT_SUBNET=([0-9./]+)"?', text, re.M)
    return m.group(1) if m else ""
