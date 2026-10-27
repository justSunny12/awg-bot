"""unit.py — локальное состояние файервола и юнит обвязки: окружение, переписывание настроек с ВПС, реассерт."""
from __future__ import annotations

import ipaddress
import re
import tempfile
from pathlib import Path

from awgbot.core import config
from awgbot.infra import gwguard as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде



def read_env() -> dict[str, str]:
    """Все NAME="…" из файла; нет файла — пусто."""
    try:
        text = pkg.Path(pkg.FW_ENV).read_text(encoding="utf-8")
    except OSError:
        return {}
    return {m.group(1): m.group(2) for m in pkg._ENV_LINE_RE.finditer(text)}


def _check_env_value(key: str, value: str) -> str:
    """Файл исполняется sh (`. "$pkg.FW_ENV"`): каждое значение — строгий набор
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
            if pkg._ADDR_CHARS_RE.fullmatch(tok):
                continue
        except ValueError:
            pass
        # имя — как nftguard.classify: с точкой, не с цифры, допустимые символы
        if key == "SSH_ALLOW" and pkg._HOST_RE.fullmatch(tok) and "." in tok and not tok[0].isdigit():
            continue
        raise ValueError(f"{key}: {tok!r} не адрес" + (", не подсеть и не имя" if key == "SSH_ALLOW" else ""))
    return v


def write_env(**fields: str) -> dict[str, str]:
    """Обновить указанные ключи, остальное сохранить; запись целиком, атомарно.
    Возвращает итоговое содержимое."""
    cur = pkg.read_env()
    for k, v in fields.items():
        if k not in pkg.ENV_KEYS:
            raise ValueError(f"неизвестный ключ {k}")
        cur[k] = pkg._check_env_value(k, v)
    p = pkg.Path(pkg.FW_ENV)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = pkg._ENV_HEAD + "".join(f'{k}="{cur[k]}"\n' for k in pkg.ENV_KEYS if k in cur) \
        + "".join(f'{k}="{v}"\n' for k, v in cur.items() if k not in pkg.ENV_KEYS)
    fd, tmp = tempfile.mkstemp(prefix=".firewall.env.", dir=str(p.parent))
    with open(fd, "w", encoding="utf-8") as f:
        f.write(body)
    pkg.Path(tmp).chmod(0o644)
    pkg.Path(tmp).replace(p)
    return cur


def read_extra() -> list[str]:
    return [t for t in pkg.read_env().get("ADMIN_IPS_EXTRA", "").split() if t]


def write_extra(entries: list[str]) -> None:
    for e in entries:
        ipaddress.ip_network(e, strict=False)          # ValueError наружу
    pkg.write_env(ADMIN_IPS_EXTRA=" ".join(entries))


def plumbing_installed() -> bool:
    """Юнит обвязки на месте (после --rollback его нет — писать состояние и
    дёргать реассерт некуда)."""
    return pkg.Path(f"/etc/systemd/system/{config.GW_UNIT}").exists()


def lan_nets(iface: str = "") -> list[str]:
    """Подсети локального интерфейса (маршруты scope link) — то, что скрипт
    кладёт в lan4 при старте; агент сверяет по тику (DHCP мог сменить подсеть).
    Пусто — не определить (агент тогда набор не трогает)."""
    if not iface:
        for r in pkg._ip_json(["route", "show", "default"]):
            if r.get("dev"):
                iface = str(r["dev"])
                break
    if not iface:
        return []
    out = []
    for r in pkg._ip_json(["route", "show", "dev", iface, "scope", "link"]):
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
    host = pkg._endpoint_host(config.GW_LINK_IF)
    if host:
        return host
    try:
        text = pkg.Path(config.GW_LINK_CONF).read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^\s*Endpoint\s*=\s*(\S+)", text, re.M)
    if not m:
        return ""
    return m.group(1).rsplit(":", 1)[0].strip("[]")


_unit_cache: tuple[tuple[int, int, int], str] | None = None


def unit_text() -> str:
    """Текст юнита обвязки с кэшем по mtime, размеру и inode: за тик его
    читали больше десяти раз регуляркой, а перезапись из потока канала посреди
    тика давала снимок из двух версий. Inode в ключе обязателен: замена файла
    через tmp.replace даёт новый inode, а mtime на Linux грубый — две записи
    подряд того же размера иначе неразличимы, и unit_env вернул бы прежний
    текст. Нет файла — пусто."""
    global _unit_cache
    p = pkg.unit_path()
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        _unit_cache = None
        return ""
    if _unit_cache is not None and _unit_cache[0] == key:
        return _unit_cache[1]
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    _unit_cache = (key, text)
    return text


def unit_admin_ips() -> list[str]:
    """ADMIN_IPS из юнита — что приехало в бандле (устройства админа)."""
    text = pkg.unit_text()
    if not text:
        return []
    m = re.search(r'^Environment="?ADMIN_IPS=([^"\n]*)"?', text, re.M)
    return [t for t in (m.group(1).split() if m else []) if t]


def unit_state() -> dict:
    """Состояние юнита обвязки: ActiveState / UnitFileState / Result. Нужно,
    чтобы отличать «таблицы нет, потому что обвязка старого образца» от «юнит
    не отработал» и «юнит выключен» — лечатся они по-разному."""
    try:
        proc = pkg.subprocess.run(["systemctl", "show", config.GW_UNIT, "--property=ActiveState",
                               "--property=UnitFileState", "--property=Result"],
                              capture_output=True, timeout=10)
    except (OSError, pkg.subprocess.SubprocessError):
        return {}
    out = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def unit_path() -> Path:
    return pkg.Path(f"/etc/systemd/system/{config.GW_UNIT}")


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
    path = pkg.unit_path()
    text = path.read_text(encoding="utf-8")
    new = text
    for key, val in values.items():
        # ключи признания аплинка — base64 (32 байта), прочее — цифры, точки, косые, пробелы:
        # кавычек и переводов строк нет ни там, ни там — вывести из строки нечем
        pattern = r"(?:[A-Za-z0-9+/]{43}=)?" if key in ("GATEWAY_PUBKEY", "GATEWAY_PREV_PUBKEY") else r"[0-9./ ]*"
        if not re.fullmatch(pattern, val):
            raise pkg.GwGuardError(f"{key}: недопустимые символы в значении")
        line = f'Environment="{key}={val}"'
        pat = re.compile(rf'^Environment="?{re.escape(key)}=[^\n]*$', re.M)
        if pat.search(new):
            new = pat.sub(line, new, count=1)
        else:
            # ключа в юните нет (обвязка старого образца) — перед первым
            # EnvironmentFile или ExecStart, туда, где его и ставит скрипт
            anchor = re.search(r"^(EnvironmentFile=|ExecStart=)", new, re.M)
            if anchor is None:
                raise pkg.GwGuardError("скрипт обвязки без ExecStart — не трогаю")
            new = new[:anchor.start()] + line + "\n" + new[anchor.start():]
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()                                  # остаток прерванной записи (упало между шагами)
    if new != text:
        pkg._write_private(tmp, new)                  # права юнита — 0600, как ставит скрипт
        tmp.replace(path)
        try:
            pkg._daemon_reload()
        except pkg.GwGuardError:
            # Юнит переписан, а systemd о нём не узнал: «не применилось» при новых
            # значениях в файле — худший исход, следующая доставка увидела бы
            # «совпало» и не перезапустила бы ничего. Возвращаем прежний текст.
            pkg._write_private(tmp, text)
            tmp.replace(path)
            raise
    return text


def _write_private(path: Path, text: str) -> None:
    """Файл с секретом: 0600 с первого байта, атомарно (util/fsatomic)."""
    from awgbot.util.fsatomic import write_private
    write_private(path, text)


def _daemon_reload() -> None:
    try:
        proc = pkg.subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
    except (OSError, pkg.subprocess.SubprocessError) as e:
        raise pkg.GwGuardError(f"systemctl daemon-reload не отработал: {e}") from e
    if proc is not None and getattr(proc, "returncode", 0) != 0:
        raise pkg.GwGuardError("systemctl daemon-reload отказал: "
                           + proc.stderr.decode(errors="replace").strip()[-200:])


def unit_restore(text: str) -> None:
    """Вернуть юнит к прежнему тексту (откат неудачного применения)."""
    path = pkg.unit_path()
    tmp = path.with_suffix(".tmp")
    pkg._write_private(tmp, text)
    tmp.replace(path)
    pkg._daemon_reload()


def is_timeout(err: str) -> bool:
    """Отказ реассерта по времени: юнит ещё работает, а не отказал — откатывать
    файлы и перезапускать его поверх нельзя."""
    return pkg.TIMEOUT_MARK in (err or "")


def reassert(timeout: int = 90) -> tuple[bool, str]:
    """Перевыставить таблицу: рестарт юнита — тот зовёт скрипт с окружением
    бандла. Линк скрипт не трогает, если конфиг не менялся."""
    try:
        proc = pkg.subprocess.run(["systemctl", "restart", config.GW_UNIT],
                              capture_output=True, timeout=timeout)
    except pkg.subprocess.TimeoutExpired:
        return False, f"скрипт обвязки {pkg.TIMEOUT_MARK} {timeout} с"
    except OSError as e:
        # systemctl нет или не запустился — тот же отказ: вызывающий обязан
        # откатить юнит, а не оставить новые значения без таблицы под ними
        return False, f"systemctl не запустился: {e}"
    ok = proc.returncode == 0
    return ok, "" if ok else proc.stderr.decode(errors="replace").strip()[-300:]


def unit_env(key: str) -> str:
    """Значение Environment=KEY=… из юнита обвязки — что приехало в бандле."""
    text = pkg.unit_text()
    if not text:
        return ""
    m = re.search(rf'^Environment="?{re.escape(key)}=([^"\n]*)"?', text, re.M)
    return (m.group(1) if m else "").strip()


def lan_mode() -> bool:
    return pkg.unit_env("LAN_MODE") == "1"
