"""blocks.py — часть пакета awg (см. __init__): блокировка трафика пира в iptables
и снятие прежних ворот SSH (цепочка AWGBOT_SSH, PostUp-страж)."""
from __future__ import annotations

import threading

from awgbot.core import config
from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде

from .common import AwgError

# Каждый вызов iptables — с ожиданием замка xtables (-w): монитор, сверка и
# обработчики зовут его из разных потоков, и без -w занятый замок (второй
# iptables, nft, awg-quick) — это отказ «Another app is currently holding the
# xtables lock» вместо короткого ожидания. 5 с — в пределах таймаутов наружу.
_IPT = ["iptables", "-w", "5"]
# Проверка «есть ли правило» и само правило — не атомарны: два потока видели
# «нет» и ставили два DROP, а снятие убирало один — адрес оставался
# заблокированным. Замок на обе операции закрывает окно внутри процесса.
_LOCK = threading.Lock()
_MAX_DUPES = 32                               # предел цикла снятия: больше дублей одного адреса не бывает


# ─────────────────────────────────────────────────────────────────────────────
# Блокировка трафика пира (механика истечения) — iptables хоста
# ─────────────────────────────────────────────────────────────────────────────

def block_ip(ip: str) -> None:
    """DROP в НАЧАЛО FORWARD (перед широким ACCEPT). Идемпотентно."""
    pkg._validate_ip(ip)
    with _LOCK:
        if pkg.is_blocked(ip):
            return
        pkg._exec([*_IPT, "-I", "FORWARD", "1", "-s", f"{ip}/32", "-j", "DROP"])


def unblock_ip(ip: str) -> None:
    """Снимает DROP — все экземпляры, не один: дубли прежних гонок или ручного
    iptables иначе оставляли адрес заблокированным после «разблокировки».
    Идемпотентно."""
    pkg._validate_ip(ip)
    with _LOCK:
        for _ in range(_MAX_DUPES):
            if not pkg.is_blocked(ip):
                return
            pkg._exec([*_IPT, "-D", "FORWARD", "-s", f"{ip}/32", "-j", "DROP"])
    raise AwgError(f"DROP {ip} не снимается: правило возвращается")


def blocked_ip_counts() -> dict[str, int]:
    """Адреса с DROP в FORWARD и число правил на каждый — одним `iptables -S`.
    Больше одного — дубль, который сверка снимает."""
    proc = pkg._exec([*_IPT, "-S", "FORWARD"], check=False)
    out: dict[str, int] = {}
    for line in (proc.stdout or b"").decode(errors="replace").splitlines():
        parts = line.split()
        if "-s" in parts and "DROP" in parts:
            src = parts[parts.index("-s") + 1].split("/")[0]
            out[src] = out.get(src, 0) + 1
    return out


def blocked_ips() -> set[str]:
    """Все адреса с DROP в FORWARD — одним `iptables -S`, а не -C на каждый."""
    return set(pkg.blocked_ip_counts())


def is_blocked(ip: str) -> bool:
    """iptables -C FORWARD ... — код 0 = правило есть."""
    pkg._validate_ip(ip)
    proc = pkg._exec([*_IPT, "-C", "FORWARD", "-s", f"{ip}/32", "-j", "DROP"],
                 check=False)
    return proc.returncode == 0


# ─────────────────────────────────────────────────────────────────────────────
# SSH-к-хосту из туннеля — теперь ЕДИНСТВЕННАЯ точка: nft-таблица awg_bot_guard
# (awgbot/infra/nftguard/). Здесь осталось только снятие старых ворот: цепочки
# iptables AWGBOT_SSH с джампами и PostUp-стража в conf интерфейсов. Оба
# слоя дублировали то, что таблица держит с загрузки, и мешали разбору отказов.
# ─────────────────────────────────────────────────────────────────────────────
_LEGACY_SSH_CHAIN = "AWGBOT_SSH"


def gated_ifaces() -> list[str]:
    """Интерфейсы, через которые до хоста дотягивается трафик из туннеля.

    Их два в окне переезда, и оба обязаны фигурировать ВЕЗДЕ, где строится
    SSH-фильтр: и в целях, и в самих правилах. Разойдись эти два списка — и
    получается ровно то, что уже случалось с маркировкой: половина механизма
    знает про переезд, половина нет, а отказ выглядит необъяснимо («у меня
    просто перестал работать SSH»).
    """
    names = [config.AWG_INTERFACE]
    if config.MIGRATION_INTERFACE and config.MIGRATION_INTERFACE not in names:
        names.append(config.MIGRATION_INTERFACE)
    return names




def remove_legacy_ssh_gate() -> bool:
    """Снести прежний пер-пирный фильтр: PostUp-строки с AWGBOT_SSH в conf
    каждого интерфейса и цепочку AWGBOT_SSH с её джампами из INPUT/FORWARD.
    Идемпотентно; True — что-то действительно снято."""
    changed = False
    for iface in pkg.gated_ifaces():
        path = f"{config.AWG_DIR}/{iface}.conf"
        try:
            conf = pkg.read_file(path)
        except AwgError:
            continue                              # интерфейса переезда может не быть
        kept = [ln for ln in conf.splitlines(keepends=True)
                if not (ln.lstrip().startswith("PostUp") and _LEGACY_SSH_CHAIN in ln)]
        if len(kept) != len(conf.splitlines()):
            with pkg.writing():
                pkg._backup_conf(iface)
                pkg.write_file(path, "".join(kept))
            changed = True
    for hook in ("INPUT", "FORWARD"):
        for _ in range(_MAX_DUPES):                # предел: iptables, «снимающий» без конца, не вешает поток
            if pkg._exec([*_IPT, "-D", hook, "-j", _LEGACY_SSH_CHAIN], check=False).returncode != 0:
                break
            changed = True
    if pkg._exec([*_IPT, "-F", _LEGACY_SSH_CHAIN], check=False).returncode == 0:
        pkg._exec([*_IPT, "-X", _LEGACY_SSH_CHAIN], check=False)
        changed = True
    return changed
