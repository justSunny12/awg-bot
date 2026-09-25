"""
gwguard.py — сторона ШЛЮЗА: чтение таблицы `inet awg_gw_guard`, которую ставит
routing-gw-setup.sh (юнит awg-link-gw.service), и локальные добавки к ней.

Генератор таблицы — ОДИН, скрипт: он же реассертит её при загрузке и по
`systemctl restart awg-link-gw.service`. Агент таблицу не пишет — только
читает (проверки, панель) и просит перевыставить; единственное исключение —
наборы с динамикой (`ssh_allow4`, `server4`: резолв имён DynDNS), которые
агент наполняет сам через `set_sync`, как основной бот держит admin4.

Юнит обвязки агент правит в одном случае — настройки, пришедшие каналом
линка (`unit_set_env`/`unit_restore`): переписывает строки Environment= четырёх
ключей `gwlink.SETTINGS_KEYS` и перезапускает юнит, при отказе возвращает
прежний текст. Фиды, привезённые каналом, лежат в /var/lib/awg-gw/feed и
применяются тем же скриптом списков с AWG_LAN_FROM.

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
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from awgbot.core import config
from awgbot.domain import gwservices

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
_ADDR_CHARS_RE = re.compile(r"[0-9A-Fa-f:./]+")     # ipaddress пускает scope id (%…) — sh нет


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
            if _ADDR_CHARS_RE.fullmatch(tok):
                continue
        except ValueError:
            pass
        # имя — как nftguard.classify: с точкой, не с цифры, допустимые символы
        if key == "SSH_ALLOW" and _HOST_RE.fullmatch(tok) and "." in tok and not tok[0].isdigit():
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
    want = {_norm_elem(e) for e in collapse(desired)}
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


def collapse(entries) -> list[str]:
    """Схлопнуть пересекающиеся и смежные подсети: nft отвергает пересечения
    в наборе с flags interval («conflicting intervals») — и в транзакции, и в
    файле при загрузке. Не-адреса (имена) пропускаются."""
    nets = []
    for e in entries:
        try:
            nets.append(ipaddress.ip_network(str(e), strict=False))
        except ValueError:
            continue
    return [_norm_elem(str(n)) for n in ipaddress.collapse_addresses(nets)]


def overlaps(entry: str, others) -> str:
    """Чем из others пересекается entry (первое найденное), пусто — ничем.
    Для отказа «уже покрыт подсетью …» до записи в файл."""
    try:
        net = ipaddress.ip_network(str(entry), strict=False)
    except ValueError:
        return ""
    for o in others:
        try:
            on = ipaddress.ip_network(str(o), strict=False)
        except ValueError:
            continue
        if net.overlaps(on):
            return str(o)
    return ""


def plumbing_installed() -> bool:
    """Юнит обвязки на месте (после --rollback его нет — писать состояние и
    дёргать реассерт некуда)."""
    return Path(f"/etc/systemd/system/{config.GW_UNIT}").exists()


def lan_nets(iface: str = "") -> list[str]:
    """Подсети интерфейса квартиры (маршруты scope link) — то, что скрипт
    кладёт в lan4 при старте; агент сверяет по тику (DHCP мог сменить подсеть).
    Пусто — не определить (агент тогда набор не трогает)."""
    if not iface:
        for r in _ip_json(["route", "show", "default"]):
            if r.get("dev"):
                iface = str(r["dev"])
                break
    if not iface:
        return []
    out = []
    for r in _ip_json(["route", "show", "dev", iface, "scope", "link"]):
        dst = str(r.get("dst", ""))
        if "/" in dst and dst not in out:
            out.append(dst)
    return out


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


def unit_path() -> Path:
    return Path(f"/etc/systemd/system/{config.GW_UNIT}")


def unit_set_env(values: dict[str, str]) -> str:
    """Переписать строки `Environment=KEY=…` юнита обвязки для данных ключей и
    перечитать юниты. Возвращает прежний текст юнита — для отката.

    Настройки, пришедшие каналом, кладём ПРЯМО В ЮНИТ, а не отдельным файлом
    рядом: юнит — единственный источник, из которого скрипт обвязки стартует
    при каждой загрузке, и он же переписывает юнит при каждом применении. Файл
    рядом (EnvironmentFile) переживал бы применение старого бандла руками и
    тихо возвращал бы значения, которые человек только что заменил.

    Значения к этому моменту проверены строго (цифры, точки, косые, пробелы):
    кавычек и переводов строк в них нет, вывести из строки нечем.
    """
    path = unit_path()
    text = path.read_text(encoding="utf-8")
    new = text
    for key, val in values.items():
        if not re.fullmatch(r"[0-9./ ]*", val):
            raise GwGuardError(f"{key}: недопустимые символы в значении")
        line = f'Environment="{key}={val}"'
        pat = re.compile(rf'^Environment="?{re.escape(key)}=[^\n]*$', re.M)
        if pat.search(new):
            new = pat.sub(line, new, count=1)
        else:
            # ключа в юните нет (обвязка старого образца) — перед первым
            # EnvironmentFile или ExecStart, туда, где его и ставит скрипт
            anchor = re.search(r"^(EnvironmentFile=|ExecStart=)", new, re.M)
            if anchor is None:
                raise GwGuardError("юнит обвязки без ExecStart — не трогаю")
            new = new[:anchor.start()] + line + "\n" + new[anchor.start():]
    if new != text:
        tmp = path.with_suffix(".tmp")
        _write_private(tmp, new)                  # права юнита — 0600, как ставит скрипт
        tmp.replace(path)
        try:
            _daemon_reload()
        except GwGuardError:
            # Юнит переписан, а systemd о нём не узнал: «не применилось» при новых
            # значениях в файле — худший исход, следующая доставка увидела бы
            # «совпало» и не перезапустила бы ничего. Возвращаем прежний текст.
            _write_private(tmp, text)
            tmp.replace(path)
            raise
    return text


def _write_private(path: Path, text: str) -> None:
    """Файл с секретом: 0600 с первого байта, а не chmod после записи."""
    import os
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _daemon_reload() -> None:
    try:
        proc = subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise GwGuardError(f"systemctl daemon-reload не отработал: {e}") from e
    if proc is not None and getattr(proc, "returncode", 0) != 0:
        raise GwGuardError("systemctl daemon-reload отказал: "
                           + proc.stderr.decode(errors="replace").strip()[-200:])


def unit_restore(text: str) -> None:
    """Вернуть юнит к прежнему тексту (откат неудачного применения)."""
    path = unit_path()
    tmp = path.with_suffix(".tmp")
    _write_private(tmp, text)
    tmp.replace(path)
    _daemon_reload()


def reassert(timeout: int = 90) -> tuple[bool, str]:
    """Перевыставить таблицу: рестарт юнита — тот зовёт скрипт с окружением
    бандла. Линк скрипт не трогает, если конфиг не менялся."""
    try:
        proc = subprocess.run(["systemctl", "restart", config.GW_UNIT],
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"юнит обвязки не отработал за {timeout} с"
    except OSError as e:
        # systemctl нет или не запустился — тот же отказ: вызывающий обязан
        # откатить юнит, а не оставить новые значения без таблицы под ними
        return False, f"systemctl не запустился: {e}"
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


# ── локальная сеть без VPN ──────────────────
HOME_TABLE_NAME = "awg_home"
DNSMASQ_D = "/etc/dnsmasq.d"
LAN_STATUS_FILE = "/var/lib/awg-gw/lists.status"
LAN_LISTS_SCRIPT = "/usr/local/sbin/awg-lan-lists.sh"
LAN_DOMAIN_SCRIPT = "/usr/local/sbin/awg-lan-domain.sh"
OWN_LIST_FILES = ("awg-gw-vpn-user.conf", "awg-gw-ru-user.conf")   # свои списки в conf-dir
OWN_LISTS_NEW = "/var/lib/awg-gw/own-lists.new"   # файл для `sync` — собирает агент из канона
OWN_FILL_NEW = "/var/lib/awg-gw/own-fill.new"     # домены для `fill` — новые «в туннель» после sync
OWN_FILL_TIMEOUT = 1800                           # dig по каждому домену, до 500 доменов — фоном
# скрипт своих списков ждёт блокировку lists.lock до 120 с: таймауты вызовов
# длиннее, иначе отказ «занято» не доходил бы, а в панель шёл бы «timed out»
LAN_SCRIPT_TIMEOUT = 150


def unit_env(key: str) -> str:
    """Значение Environment=KEY=… из юнита обвязки — что приехало в бандле."""
    try:
        text = Path(f"/etc/systemd/system/{config.GW_UNIT}").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(rf'^Environment="?{re.escape(key)}=([^"\n]*)"?', text, re.M)
    return (m.group(1) if m else "").strip()


def lan_mode() -> bool:
    return unit_env("LAN_MODE") == "1"


def lists_status() -> dict:
    """Что записал скрипт списков: updated_at, domains, nets, rc."""
    out = {}
    try:
        for line in Path(LAN_STATUS_FILE).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def home_table_info() -> Optional[dict]:
    """{'sets': {имя: число элементов}, 'chains': set, 'lan_pkts': int, 'dns_pkts': int}
    или None — таблицы нет. lan_pkts — счётчик «из локальной сети наружу»
    (первое правило со счётчиком в prerouting), dns_pkts — «DNS с роутера»
    (input). Один exec, терсный (-t): без элементов наборов — lan_vpn4 растёт
    без конца, выгружать его каждые три минуты ради двух счётчиков незачем;
    число элементов поэтому всегда 0, счёты — из lists.status."""
    proc = _nft(["-j", "-t", "list", "table", TABLE_FAMILY, HOME_TABLE_NAME])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise GwGuardError(f"nft -j: {e}")
    sets: dict[str, int] = {}
    chains: set[str] = set()
    counters: dict[str, int] = {}
    for item in doc.get("nftables", []):
        if "set" in item:
            s = item["set"]
            sets[s["name"]] = len(s.get("elem") or [])
        elif "chain" in item:
            chains.add(item["chain"]["name"])
        elif "rule" in item:
            r = item["rule"]
            chain = r.get("chain", "")
            if chain in counters:
                continue
            for e in r.get("expr") or []:
                if isinstance(e, dict) and "counter" in e:
                    counters[chain] = int((e["counter"] or {}).get("packets", 0))
                    break
    return {"sets": sets, "chains": chains,
            "lan_pkts": counters.get("prerouting", 0), "dns_pkts": counters.get("input", 0)}


def lan_own_lists() -> tuple[int, int]:
    """(в туннель, напрямую) — персональные списки, строки nftset=."""
    def _count(name: str) -> int:
        try:
            text = Path(f"{DNSMASQ_D}/{name}").read_text(encoding="utf-8")
        except OSError:
            return 0
        return sum(1 for ln in text.splitlines() if ln.startswith("nftset="))
    return _count(OWN_LIST_FILES[0]), _count(OWN_LIST_FILES[1])


def dnsmasq_active() -> Optional[bool]:
    try:
        proc = subprocess.run(["systemctl", "is-active", "--quiet", "dnsmasq"],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def upstream_stats() -> Optional[dict[str, tuple[int, int]]]:
    """Статистика апстримов dnsmasq без единого запроса наружу: `servers.bind`
    в классе CHAOS отдаёт по каждому серверу «отправлено / отказов». None —
    dig нет или dnsmasq не ответил.

    Раньше апстрим проверялся живым запросом к github.com каждым тиком: при TTL
    в минуту dnsmasq почти каждый раз уходил наружу — один и тот же запрос раз
    в три минуты, тот самый периодический рисунок, от которого уходим."""
    try:
        proc = subprocess.run(["dig", "+short", "+time=3", "+tries=1", "@127.0.0.1",
                               "servers.bind", "CH", "TXT"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: dict[str, tuple[int, int]] = {}
    for m in re.finditer(r'"([0-9a-fA-F.:#@\w-]+)\s+(\d+)\s+(\d+)"',
                         proc.stdout.decode(errors="replace")):
        out[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return out


def forward_accepts() -> set[str]:
    """Интерфейсы, для которых в чужой ip filter FORWARD стоят наши ACCEPT
    (раздел 3a скрипта обвязки). Пусто — не прочиталось или ничего нет."""
    try:
        proc = subprocess.run(["iptables", "-w", "-S", "FORWARD"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    out = set()
    for line in proc.stdout.decode(errors="replace").splitlines():
        m = re.match(r"^-A FORWARD -(i|o) (\S+) -j ACCEPT$", line.strip())
        if m:
            out.add(f"{m.group(1)}:{m.group(2)}")
    return out


def resolve_via_local(name: str = "github.com") -> Optional[bool]:
    """Резолвит ли dnsmasq через аплинк: один запрос к 127.0.0.1. None — dig нет."""
    try:
        proc = subprocess.run(["dig", "+short", "+time=3", "+tries=1", "@127.0.0.1", name, "A"],
                              capture_output=True, timeout=15)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return False
    out = proc.stdout.decode(errors="replace")
    return proc.returncode == 0 and bool(re.search(r"^\d+\.\d+\.\d+\.\d+$", out, re.M))


def iface_for_subnet(net: str) -> Optional[tuple[str, str]]:
    """(интерфейс, адрес) с адресом из подсети — тем же правилом, что
    lan_iface_for в скрипте обвязки; None — нет или не прочиталось."""
    try:
        want = ipaddress.ip_network(net, strict=False)
        proc = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, timeout=10)
        links = json.loads(proc.stdout.decode(errors="replace") or "[]")
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    for link in links:
        name = link.get("ifname", "")
        if name == "lo" or name.startswith(("awg", "docker", "veth", "br-")):
            continue
        for a in link.get("addr_info") or []:
            try:
                if ipaddress.ip_address(a.get("local", "")) in want:
                    return name, a["local"]
            except ValueError:
                continue
    return None


LAN_FEED_DIR = "/var/lib/awg-gw/feed"


def run_lan_lists(timeout: int = 600, from_dir: str = "") -> tuple[bool, str]:
    """Обновить списки скриптом обвязки. (ok, хвост вывода). from_dir — фиды
    не качать, а взять готовыми оттуда (их привёз канал линка)."""
    import os
    env = dict(os.environ)
    if from_dir:
        env["AWG_LAN_FROM"] = from_dir
    try:
        proc = subprocess.run([LAN_LISTS_SCRIPT], capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return False, "скрипта списков нет — перевыпусти конфигурацию шлюза"
    except subprocess.TimeoutExpired:
        return False, "таймаут обновления списков"
    except OSError as e:
        return False, str(e)
    tail = (proc.stdout + proc.stderr).decode(errors="replace").strip().splitlines()[-3:]
    return proc.returncode == 0, "\n".join(tail)


# ── сервисы соседних сетей ─────────────────
LAN_SERVICES_SCRIPT = "/usr/local/sbin/awg-lan-services.sh"
PEER_SERVICES_CONF = f"{DNSMASQ_D}/{gwservices.CONF_NAME}"
PEER_SERVICES_NEW = "/var/lib/awg-gw/peer-services.conf.new"


def avahi_browse_available() -> bool:
    return shutil.which("avahi-browse") is not None


def avahi_active() -> Optional[bool]:
    """Запущен ли avahi-daemon: без него SMB-серверы этой сети соседям не
    видны. None — systemctl не ответил."""
    try:
        proc = subprocess.run(["systemctl", "is-active", "--quiet", "avahi-daemon"],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def avahi_browse(timeout: int = 15) -> Optional[str]:
    """`avahi-browse -rtpk _smb._tcp` — что объявляют по mDNS в локальной сети.
    Запрос не покидает её сегмент. None — утилиты нет или обзор не удался
    (промахом гистерезиса не считается)."""
    try:
        proc = subprocess.run(["avahi-browse", "-rtpk", "_smb._tcp"], capture_output=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):   # нет утилиты, не ответила
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace")


def run_lan_services(path: str = "", timeout: int = LAN_SCRIPT_TIMEOUT) -> tuple[bool, str]:
    """Установить файл записей соседей в dnsmasq помощником обвязки (проверка
    построчно, --test, рестарт с откатом); без пути — снять файл. (ok, хвост)."""
    try:
        proc = subprocess.run([LAN_SERVICES_SCRIPT, *([path] if path else [])],
                              capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return False, "скрипта записей SMB нет — перевыпусти конфигурацию шлюза"
    except subprocess.TimeoutExpired:
        return False, f"скрипт записей SMB не ответил за {timeout} с"
    except OSError as e:
        return False, str(e)
    tail = (proc.stdout + proc.stderr).decode(errors="replace").strip().splitlines()[-3:]
    return proc.returncode == 0, "\n".join(tail)


def dns_local(name: str, qtype: str = "PTR") -> Optional[list[str]]:
    """Что отдаёт свой dnsmasq по имени: `dig +short @127.0.0.1`. None — dig
    нет или не ответил; пустой список — записи нет."""
    try:
        proc = subprocess.run(["dig", "+short", "+time=3", "+tries=1", "@127.0.0.1", name, qtype],
                              capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip() for ln in proc.stdout.decode(errors="replace").splitlines() if ln.strip()]


def lan_domain_has_sync() -> bool:
    """Скрипт своих списков умеет `sync` (метка в шапке): без неё обвязка
    старого образца — канон применять нечем, нужен реассерт."""
    try:
        with open(LAN_DOMAIN_SCRIPT, encoding="utf-8", errors="replace") as f:
            return "# awg-lan-domain: sync" in f.read(8192)
    except OSError:
        return False


def run_lan_domain(cmd: str, domains: list[str], timeout: int = LAN_SCRIPT_TIMEOUT) -> tuple[bool, str]:
    """Свои списки: add | ru | del | list | sync. (ok, вывод)."""
    try:
        proc = subprocess.run([LAN_DOMAIN_SCRIPT, cmd, *domains], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return False, "скрипта своих списков нет — перевыпусти конфигурацию шлюза"
    except subprocess.TimeoutExpired:
        return False, f"скрипт своих списков не ответил за {timeout} с"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return proc.returncode == 0, (proc.stdout + proc.stderr).decode(errors="replace").strip()


_OS_ERRORS = {
    "ENOSPC": "нет места на диске", "EROFS": "диск только для чтения", "EACCES": "нет прав на запись",
    "EPERM": "нет прав на запись", "ENOENT": "нет каталога для файла", "EIO": "ошибка ввода-вывода диска",
    "EDQUOT": "исчерпана дисковая квота", "ENOTDIR": "нет каталога для файла",
    "EEXIST": "нет каталога для файла",   # на месте каталога — файл
}


def os_error_text(e: OSError) -> str:
    """Ошибка записи файла — человеческой строкой вместо «[Errno 28] No space
    left on device: '/var/lib/…'»: причина по коду, остальное человеку ни к чему."""
    import errno
    name = errno.errorcode.get(e.errno or 0, "")
    return _OS_ERRORS.get(name) or (f"ошибка записи ({name})" if name else "ошибка записи")
