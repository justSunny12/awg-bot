"""common.py — константы, ошибка, вызов nft, подпись ошибок ОС."""
from __future__ import annotations

import re
import subprocess

from awgbot.infra import nftjson


TABLE_FAMILY = "inet"
TABLE_NAME = "awg_gw_guard"
TABLE = f"{TABLE_FAMILY} {TABLE_NAME}"
FW_ENV = "/etc/awg-gw/firewall.env"
CHAINS = ("input", "tunnel_in", "forward", "postrouting", "output")


class GwGuardError(RuntimeError):
    pass


def _nft(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return nftjson.run(args, timeout=timeout, error=GwGuardError)


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


TIMEOUT_MARK = "не отработал за"


# ── шлюзовое устройство: аплинк этой машины и решение скрипта ────────────────
STATUS_FILE = "/etc/awg-gw/gateway.status"


# ── политика «Telegram → аплинк»: ip rule по метке + маршрут в таблице ──────
# Ставит PostUp аплинка при подъёме; systemd-networkd при своём (пере)запуске
# по умолчанию сносит ЧУЖИЕ ip rule и маршруты — интерфейс жив, метка стоит,
# а пакеты Telegram уходят провайдеру локальной сети. Агент проверяет каждый тик и
# перевыставляет сам: это два idempotent-вызова ip, юнит дёргать незачем.

UPLINK_TABLE = 100
TG_MARK = 1


# ── локальная сеть без VPN ──────────────────
LAN_TABLE_NAME = "awg_lan"
# прежнее имя таблицы (агенты 3.3.0 и раньше): скрипт обвязки переносит наборы
# в новую на первом прогоне после обновления — снимается со следующим сдвигом минимума
LAN_TABLE_OLD = "awg_home"
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


LAN_FEED_DIR = "/var/lib/awg-gw/feed"


# ── сервисы соседних сетей ─────────────────
LAN_SERVICES_SCRIPT = "/usr/local/sbin/awg-lan-services.sh"
SETUP_SCRIPT = "/usr/local/sbin/routing-gw-setup.sh"      # скрипт обвязки: режим --link-apply зовёт агент
LINK_APPLY_TIMEOUT = 180                                    # с — установка конфига линка (syncconf или down/up)
GW_ETC = "/etc/awg-gw"                                      # link.prev / link.next — точки отката документа
PEER_SERVICES_CONF_NAME = "awg-gw-peer-services.conf"   # файл в /etc/dnsmasq.d на получателе
PEER_SERVICES_CONF = f"{DNSMASQ_D}/{PEER_SERVICES_CONF_NAME}"
PEER_SERVICES_NEW = "/var/lib/awg-gw/peer-services.conf.new"


_OS_ERRORS = {
    "ENOSPC": "нет места на диске", "EROFS": "диск только для чтения", "EACCES": "нет прав на запись",
    "EPERM": "нет прав на запись", "ENOENT": "директория не найдена", "EIO": "ошибка ввода-вывода диска",
    "EDQUOT": "исчерпана дисковая квота", "ENOTDIR": "директория не найдена",
    "EEXIST": "директория не найдена",    # на месте каталога — файл
}


def os_error_text(e: OSError) -> str:
    """Ошибка записи файла — человеческой строкой вместо «[Errno 28] No space
    left on device: '/var/lib/…'»: причина по коду, остальное человеку ни к чему."""
    import errno
    name = errno.errorcode.get(e.errno or 0, "")
    return _OS_ERRORS.get(name) or (f"ошибка записи ({name})" if name else "ошибка записи")
