"""Снимки экранов агента шлюза (gateway.txt): жмёт админ, диспетчер агента.

Состояние агента — построителем `_agent(...)`: обвязка создаёт обычный
GatewayServices, построитель подменяет ему класс на `_Agent` — те же
сервисы, но всё, что ходит на хост (ip, nft, ss, sshd, скрипт обвязки,
почта, список релизов), отвечает из `_Host` снимка. БД и conf — настоящие
и свежие на каждый снимок, поэтому настройки, почта, фраза шифрования,
мьют обновлений и снимок тика лежат там, где их ищет код.

Упр. канал до сервера AWG: обвязка гасит `linkclient.enabled` и снимает
клиента канала (`linkclient._client`) на каждом снимке, построитель ставит
их monkeypatch'ем снимка — подмена откатывается в конце снимка и до
следующего не доживает.

Файл конфигурации шлюза — заглушка с подписью формата (bundlecrypt.MAGIC):
расшифровку и прогон скрипта отвечает `_Host` (inspect_bundle, apply_bundle,
статус скрипта, итог почты, пометка). Резервные копии — настоящие архивы,
зашифрованные случайным ключом (`backup_key` в БД): их разбирает настоящий
inspect_backup — метка роли, схема базы, конфиги интерфейсов. События без
действия человека (первая панель, обещание перезапуска, итоги обновления и
восстановления, перезагрузка хоста) — шагом call теми же функциями, что
зовёт старт агента.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import datetime as _dt
import io
import ipaddress
import json
import sqlite3
import tarfile
import types
from dataclasses import dataclass, field

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.handlers import gateway as gateway_handlers
from awgbot.bot.handlers import restore as restore_handlers
from awgbot.runtime import hostboot
from awgbot.util import bundlecrypt, secrets_util
from awgbot.bot.callbacks import CancelCB, GwCB, HideCB, PageCB, UpdateCB
from awgbot.core import config
from awgbot.domain.backupcrypto import BackupKeyMissing
from awgbot.domain.gateway import GatewayServices, GwCheck, GwStatus
from awgbot.domain.services import ServiceError
from awgbot.infra import mail, updates
from awgbot.runtime import linkclient
from awgbot.util import timeutil

from tests.screens.base import NOW, Shot

# ── состояние хоста ──────────────────────────────────────────────────────────

_CHECKS_OK = [
    GwCheck("линк", True, "хендшейк 40 с назад"),
    GwCheck("таблица awg_gw_guard", True),
    GwCheck("ip_forward", True),
    GwCheck("маршрут к Telegram", True),
    GwCheck("выход наружу", True, "35 мс через канал"),
    GwCheck("ядра", True, "модуль собран для 2 из 2"),
]

_CHECKS_LAN = [
    GwCheck("применение локальной сети", True, group="lan"),
    GwCheck("резолвер", True, group="lan"),
    GwCheck("списки", True, "обновлены 15.09 06:00", group="lan"),
    GwCheck("SMB этой подсети", True, "nas", group="lan"),
    GwCheck("SMB подсетей других шлюзов", None, "avahi не установлен", group="lan"),
]

_CHECKS_BAD = [
    GwCheck("линк", False, "хендшейка нет 14 мин"),
    GwCheck("таблица awg_gw_guard", False, "нет цепочки ssh_in"),
    GwCheck("ip_forward", True),
    GwCheck("маршрут к Telegram", None, "нечем проверить: нет dig"),
    GwCheck("выход наружу", False, "таймаут 5 с <проба>"),
    GwCheck("ядра", True, "модуль собран для 2 из 2"),
]


def _ssh_panel(**kw) -> dict:
    """Строка SSH панели: порт, владелец, фильтр снаружи, адресов."""
    d = {"port": 22, "owner": "", "filter": False, "allow": 0, "new_plumbing": True}
    d.update(kw)
    return d


def _lan(**kw) -> dict:
    """VPN-транзит включён: интерфейс, адрес, резолвер, списки, свои, SMB."""
    d = {"iface": "end0", "addr": "192.168.1.10", "resolver": "10.8.0.1", "uplink": "awg0",
         "domains": 1234, "nets": 56, "updated_at": "2026-09-15T06:00:00+03:00",
         "own_vpn": 2, "own_ru": 1, "lan_pkts": 123456,
         "svc": {"active": True, "own": ["nas"], "peer": ["nas-2", "printer"], "ever": True}}
    d.update(kw)
    return d


def _status(**kw) -> GwStatus:
    """Живой замер здорового шлюза без VPN-транзита."""
    d = dict(link_up=True, handshake_age=40.0, rx=3 * 1024 ** 3, tx=512 * 1024 ** 2,
             checks=copy.deepcopy(_CHECKS_OK), temp=48.3, throttled={"now": [], "ever": []},
             cpu=12.0, ram=41.0, ram_free_mb=2345, disk=23.0, disk_free_gb=87.4, smart="OK",
             uptime_seconds=12 * 86400 + 3 * 3600, hostname="pi", server_name="awg-srv",
             module_version="1.0.20260901", srcversion="ABCDEF0123456789", kernels_total=2,
             mark_status="confirmed", ssh=_ssh_panel())
    d.update(kw)
    return GwStatus(**d)


def _status_lan(**kw) -> GwStatus:
    d = dict(lan=_lan(), checks=copy.deepcopy(_CHECKS_OK + _CHECKS_LAN))
    d.update(kw)
    return _status(**d)


def _status_bad(**kw) -> GwStatus:
    """Линк лежит, обвязка разошлась, питание проседает сейчас."""
    d = dict(link_up=False, handshake_age=None, checks=copy.deepcopy(_CHECKS_BAD),
             throttled={"now": ["недонапряжение"], "ever": ["недонапряжение", "троттлинг"]},
             cpu=97.0, temp=81.0, ram=88.0, disk=91.0, ram_free_mb=312, disk_free_gb=3.2,
             smart="FAIL", mark_status="foreign", ssh=_ssh_panel(sshd_down=True))
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


_OWN_ITEMS = [("vpn", "example.com"), ("vpn", "example.net"), ("ru", "sber.ru")]
_OWN_OFF = {"active": False, "state": "off", "no_channel": False}
_OWN_SYNCED = {"active": True, "state": "synced", "pending": 0, "err": "", "rej": [], "synced": True}

_RELEASE = updates.Release(
    tag="v1.2.4", version=(1, 2, 4), asset_url=None, sha256=None,
    body="### Шлюз\n- панель: строка SSH с владельцем порта\n- «🔀 VPN-транзит»: свои списки листаются\n"
         "### Исправлено\n- итог применения конфигурации не терял строку почты\n\n#requires_gw_1.2.0")


_APPLY_STATUS = {"UPLINK": "installed", "LINK": "up", "GW_STATUS": "confirmed", "SSH_FILTER": "1",
                 "SSH_ALLOW_COUNT": "2", "LAN": "1", "LAN_IF": "end0", "LAN_ADDR": "192.168.1.10"}

# ── файлы в чат ──────────────────────────────────────────────────────────────

_BUNDLE = bundlecrypt.MAGIC + b"DUMMY-bundle"          # формат проверяется по подписи
_BUNDLE_NAME = "awg-gw-NASPi.bin"
_BACKUP_KEY = b"DUMMY-backup-key-0123456789abcdef"[:32]


def _archive(role: str = "gw", *, db: bytes | None = None, iface: bool = False) -> bytes:
    """Архив резервной копии: метка роли и даты, база, конфиг интерфейса."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, raw):
            ti = tarfile.TarInfo(name)
            ti.size = len(raw)
            ti.mtime = int(NOW.timestamp())
            tar.addfile(ti, io.BytesIO(raw))
        add("state/backup-meta.json",
            json.dumps({"role": role, "created_at": "2026-09-14T03:00:00+03:00"}).encode())
        if db is not None:
            add("state/bot.db", db)
        if iface:
            add("awg/awg0.conf", b"[Interface]\nPrivateKey = DUMMY\nAddress = 10.8.0.2/30\n")
    return buf.getvalue()


def _old_db() -> bytes:
    """База схемы ниже 3.2.0: в clients нет колонки kind."""
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE clients(id INTEGER PRIMARY KEY, tg_name TEXT);"
                      "CREATE TABLE devices(id INTEGER PRIMARY KEY, holder_client_id INTEGER);")
    raw = con.serialize()
    con.close()
    return bytes(raw)


def _enc(plain: bytes) -> bytes:
    return secrets_util.encrypt(plain, key=_BACKUP_KEY)


_COPY = _enc(_archive())
_COPY_IFACE = _enc(_archive(iface=True))
_COPY_MAIN = _enc(_archive("main"))
_COPY_OLD = _enc(_archive(db=_old_db()))
_COPY_NAME = "awg-gw-pi-2026-09-14.tgz.enc"


async def _no_sleep(_s=0):
    """Ожидание канала после применения — мгновенно: связи в снимке не будет."""


_FAST_ASYNCIO = types.SimpleNamespace(sleep=_no_sleep, get_running_loop=asyncio.get_running_loop)


@dataclass
class _Host:
    """Что агент увидел бы на хосте, если бы это был шлюз."""
    live: GwStatus = field(default_factory=_status)        # живой замер (status)
    tick: GwStatus | None = None                           # снимок тика в БД, 2 мин назад
    channel: str = ""                                      # "" — канала нет; active | standby | offline
    standby: bool | None = None                            # есть ли другой шлюз (по слову сервера)
    carries: bool = False                                  # несёт трафик и линк жив
    op: tuple = (True, "")                                 # итог restart_link / reassert
    ssh: dict = field(default_factory=_ssh)
    busy: str = ""                                         # ssh_port_busy
    port_error: str = ""                                   # ssh_port_change: ServiceError
    own_items: list = field(default_factory=list)          # свои домены (vpn|ru, домен)
    own: dict = field(default_factory=lambda: dict(_OWN_OFF))
    lan_fail: str = ""                                     # скрипт своих списков отказал
    mail: bool = False                                     # ящик подключён
    mail_last: str = ""                                    # ok | fail — последняя проверка
    mail_error: str = ""                                   # проверка/тест-письмо падают
    passphrase: bool = False                               # фраза шифрования бэкапов задана
    release: updates.Release | None = None                 # цель обновления
    scan_fails: bool = False                               # список релизов недоступен
    update_error: str = ""                                 # apply_update падает
    update_tag: str = ""                                   # «⬆️ Доступна vX» с прошлой проверки
    muted: bool = False
    # файл конфигурации шлюза
    bundle: dict = field(default_factory=lambda: {"ok": True, "mail": False, "passphrase": False,
                                                  "passphrase_differs": False, "link_changed": True})
    apply: tuple = (True, "")                              # итог скрипта применения
    apply_status: dict = field(default_factory=lambda: dict(_APPLY_STATUS))   # файл статуса скрипта
    bundle_mail: dict = field(default_factory=dict)        # проверка почты из файла
    mark: dict = field(default_factory=lambda: {"status": "confirmed", "claim": None})
    # резервные копии и старт процесса
    backup_key: bool = False                               # ключ шифрования копий задан
    restore_done: dict | None = None                       # маркер awg-bot restore
    update_pending: str = ""                               # перед рестартом шло обновление до …
    update_wait: bool = False                              # «дождись завершения» — #1
    restart_wait: bool = False                             # «бот перезапускается» — #1


class _Channel:
    """Клиент упр. канала с открытой сессией: роль — из БД, отправки — в никуда."""

    def __init__(self, services):
        self.services = services
        self._writer = object()
        self._task = None
        self._own_lock = asyncio.Lock()

    def start(self):
        pass

    async def push(self):
        pass

    async def push_own(self):
        pass

    async def flush_applied(self):
        pass


def _channel_on() -> bool:
    return True


class _Agent(GatewayServices):
    """GatewayServices без хоста: пробы, скрипты и сеть — из _Host."""

    h: _Host

    # ── линк и панель ───────────────────────────────────────────────────────
    def status(self):
        return GwStatus.from_json(self.h.live.to_json())

    def carries_traffic(self):
        return self.h.carries

    def restart_link(self):
        return self.h.op

    def reassert(self):
        return self.h.op

    def restart_bot(self):
        pass

    def services_scan(self):
        return False

    # ── SSH ─────────────────────────────────────────────────────────────────
    def ssh_screen(self, info=None, fact=None, conf=True):
        return copy.deepcopy(self.h.ssh)

    def ssh_allow_current(self):
        return list(self.h.ssh["allow"])

    def ssh_allow_add(self, raw):
        """Как сервис: IPv6 и мусор — отказ; подсеть поглощает свои адреса."""
        allow = list(self.h.ssh["allow"])
        for tok in raw.split():
            if ":" in tok:
                raise ServiceError(f"{tok} — IPv6, на шлюзе фильтр только по IPv4")
            if "." not in tok:
                raise ServiceError(f"«{tok}» не адрес, не подсеть и не имя")
            if "/" in tok:
                net = ipaddress.ip_network(tok, strict=False)
                allow = [a for a in allow if not _inside(a, net)]
            if tok not in allow:
                allow.append(tok)
        self.h.ssh["allow"] = allow
        return list(allow)

    def ssh_allow_remove(self, entry):
        self.h.ssh["allow"] = [a for a in self.h.ssh["allow"] if a != entry]
        return list(self.h.ssh["allow"])

    def ssh_filter_on(self):
        self.h.ssh["filter"] = True

    def ssh_filter_off(self):
        self.h.ssh["filter"] = False

    def ssh_port_busy(self, port):
        return self.h.busy

    def ssh_port_change(self, port):
        if self.h.port_error:
            raise ServiceError(self.h.port_error)
        old = self.h.ssh["port"]
        self.h.ssh.update(port=port, ports=[port], conf_ports=[port], env_port=port)
        return old

    # ── VPN-транзит и свои списки ───────────────────────────────────────────
    def lan_own_lists(self):
        return list(self.h.own_items)

    def lan_domains(self, cmd, domains):
        """Строки итога — как у routing-gw-setup.sh (домен в <code>)."""
        if self.h.lan_fail:
            return False, self.h.lan_fail
        out = []
        items = list(self.h.own_items)
        for d in domains:
            if "." not in d:
                out.append(f"{d}: не похоже на домен, пропущен")
                continue
            have = [k for k, x in items if x == d]
            if cmd == "del":
                if have:
                    items = [(k, x) for k, x in items if x != d]
                    out.append(f"<code>{d}</code>: убран")
                else:
                    out.append(f"<code>{d}</code>: в списках нет")
                continue
            kind = "vpn" if cmd == "add" else "ru"
            if kind in have:
                out.append(f"<code>{d}</code>: уже в списке")
                continue
            items = [(k, x) for k, x in items if x != d] + [(kind, d)]
            out.append(f"<code>{d}</code>: добавлен")
        self.h.own_items = items
        self.lan_own_counts_refresh()
        return True, "\n".join(out)

    def lan_own_counts_refresh(self):
        """Счётчики своих списков в снимке тика — по своим доменам, как у
        сервиса после правки (там их считает скрипт)."""
        raw = self.db.get_state(self._SNAPSHOT_KEY)
        if not raw:
            return
        st = GwStatus.from_json(raw)
        if st.lan:
            st.lan["own_vpn"] = sum(1 for k, _d in self.h.own_items if k == "vpn")
            st.lan["own_ru"] = sum(1 for k, _d in self.h.own_items if k == "ru")
            self.db.set_state(self._SNAPSHOT_KEY, st.to_json())

    def own_status(self):
        return dict(self.h.own), []

    def own_reconcile(self):
        return True

    def own_unsent(self):
        return False

    def lan_router_params(self):
        return "192.168.1.0/24", "192.168.1.10", ["192.168.2.0/24"]

    # ── почта и бэкапы ──────────────────────────────────────────────────────
    def email_check(self, acc=None):
        if self.h.mail_error:
            self.db.set_state(self._MAIL_CHECK_KEY, f"fail|{timeutil.now_iso()}|{self.h.mail_error}")
            return False, self.h.mail_error
        self.db.set_state(self._MAIL_CHECK_KEY, f"ok|{timeutil.now_iso()}")
        return True, "IMAP и SMTP отвечают, вход выполнен"

    def email_send_test(self):
        if self.h.mail_error:
            raise mail.MailError(self.h.mail_error)
        self._email_mark_ok()

    def email_send_backup(self, paths):
        pass

    def make_backup(self):
        """Копия шлюза — только шифрованная: без фразы отказ, как у сервиса."""
        if not self.backup_encryption_enabled():
            raise BackupKeyMissing("нет фразы")
        return ["/var/lib/awg-gw/backups/awg-gw-pi-2026-09-15.tgz.enc"]

    # ── обновления ──────────────────────────────────────────────────────────
    def update_next(self):
        self.update_scan_failed = self.h.scan_fails
        return None if self.h.scan_fails else self.h.release

    def update_block_reason(self, release=None):
        return ""

    def apply_update(self, release):
        if self.h.update_error:
            raise updates.UpdateError(self.h.update_error)

    # ── файл конфигурации шлюза ─────────────────────────────────────────────
    def inspect_bundle(self, blob):
        return dict(self.h.bundle)

    def apply_bundle(self, blob, overwrite_passphrase=False):
        return self.h.apply

    def gateway_apply_report(self):
        return texts.gateway_apply_report(self.h.apply_status)

    def bundle_mail_check(self):
        return dict(self.h.bundle_mail)

    def gateway_mark_outcome(self):
        out = {"uplink": "awg1", "pubkey": "DUMMYPUB=", **self.h.mark}
        self.db.set_state(self._GW_MARK_KEY, out["status"])
        return out

    def lan_lists_needed(self):
        return False

    # ── восстановление ──────────────────────────────────────────────────────
    def prepare_restore(self, plain):
        return "/var/lib/awg-bot/backups/restore-pending.tgz"

    def launch_restore(self, path):
        pass

    def pop_restore_done(self):
        return self.h.restore_done


def _inside(entry: str, net) -> bool:
    try:
        return ipaddress.ip_network(entry, strict=False).subnet_of(net)
    except ValueError:
        return False


def _agent(**opts):
    """Построитель снимка: агент с хостом _Host(**opts); снимок тика, канал,
    почта, фраза, мьют — в БД и conf, где их читает код."""
    def build(services, mp):
        h = _Host(**copy.deepcopy(opts))
        services.__class__ = _Agent
        services.h = h
        if h.tick is not None:
            st = GwStatus.from_json(h.tick.to_json())
            st.ts = timeutil.to_iso(NOW - _dt.timedelta(minutes=2))
            services.db.set_state(services._SNAPSHOT_KEY, st.to_json())
            if h.own_items:
                services.lan_own_counts_refresh()
        if h.channel in ("active", "standby"):
            services.set_link_role(h.channel == "active", standby=h.standby, name="NASPi")
        elif h.standby is not None:
            services.db.set_state(services._LINK_STANDBY_KEY, "1" if h.standby else "0")
        if h.channel:
            mp.setattr(linkclient, "enabled", _channel_on)
        mp.setattr(linkclient, "_client", _Channel(services) if h.channel in ("active", "standby") else None)
        if h.mail:
            services.email_save("admin@example.org", "DUMMY", "imap.example.org", 993,
                                "smtp.example.org", 587)
            if h.mail_last == "ok":
                services.db.set_state(services._MAIL_CHECK_KEY, "ok|2026-09-14T09:30:00+03:00")
            elif h.mail_last == "fail":
                services.db.set_state(services._MAIL_CHECK_KEY,
                                      "fail|2026-09-14T09:30:00+03:00|SMTP: авторизация отклонена")
        if h.passphrase:
            services.backup_set_passphrase("DUMMY-passphrase")
        if h.update_tag:
            services.db.set_state(services._AVAILABLE_KEY, h.update_tag)
        if h.muted:
            services.mute_updates()
        if h.backup_key:
            services.db.set_state(services._BK_KEY_KEY, base64.b64encode(_BACKUP_KEY).decode())
        if h.update_pending:
            services.db.set_state("update_pending", h.update_pending)
        if h.update_wait:
            services.set_update_wait(config.ADMIN_ID, 1)
        if h.restart_wait:
            services.set_restart_wait(config.ADMIN_ID, 1)
        # хост и сеть — мимо: конфиги интерфейсов копии сверяются с пустым
        # каталогом, тело релиза — из каталога, перезагрузка — «была»,
        # ожидание канала после применения — мгновенно
        mp.setattr(config, "GW_CONF_DIR", "/nonexistent/DUMMY-awg-gw")
        mp.setattr(updates, "release_body", lambda tag: _RELEASE.body)
        mp.setattr(hostboot, "reboot_detected", lambda db: True)
        mp.setattr(gateway_handlers, "asyncio", _FAST_ASYNCIO)
        return config.ADMIN_ID, "Админ"
    return build


def _shot(id_: str, *, press=(), text=None, conf=None, start=None, title="", steps=(), call=None,
          **opts) -> Shot:
    return Shot(id_, role="gateway", press=press, text=text, conf=conf or {}, start=start,
                title=title, steps=steps, call=call, data=_agent(**opts))


def _file(name: str, blob: bytes) -> tuple:
    return ("file", name, blob)


def _p(cb) -> tuple:
    return ("press", cb)


def _t(text: str) -> tuple:
    return ("text", text)


async def _first_panel(services, bot):
    await gateway_handlers.send_first_panel(bot, services)


async def _restart_panel(services, bot):
    await gateway_handlers.restore_panel_after_restart(bot, services)


async def _update_result(services, bot):
    from awgbot.runtime import main
    await main.report_update_result(bot, services)


async def _restore_result(services, bot):
    await restore_handlers.report_restore_result(bot, services)


async def _rebooted(services, bot):
    from awgbot.runtime import main
    await main._announce_reboot(bot, services.db, "агента")


def _lan_rm(items, dom: str) -> GwCB:
    """Кнопка «➖ домен» — номер в отсортированном списке и метка записи."""
    srt = kb.lan_own_sorted(items)
    i = next(n for n, (_k, d) in enumerate(srt) if d == dom)
    return GwCB(action="lan_rm", val=f"{i}.{kb.lan_own_tag(*srt[i])}")


def _ssh_del(allow, entry: str) -> GwCB:
    i = allow.index(entry)
    return GwCB(action="ssh_del!", val=f"{i}.{kb.entry_tag(entry)}")


_MANY_OWN = [("vpn", f"site{n:02d}.example.com") for n in range(1, 13)] + [("ru", "sber.ru")]
_ALLOW2 = ["203.0.113.7", "home.example.net"]
_ALLOW_MANY = [f"203.0.113.{n}" for n in range(1, 13)]
_TICK_LAN = _status_lan()
_MAIL = {"mail": True, "mail_last": "ok"}

# ── снимки ───────────────────────────────────────────────────────────────────

SHOTS = [
    # ── W00 панель ──────────────────────────────────────────────────────────
    _shot("gw.panel.start", start="", title="по /start: снимка тика нет — живой замер, без VPN-транзита"),
    _shot("gw.panel", press=[GwCB(action="panel")], tick=_TICK_LAN, update_tag="v1.2.4",
          title="«В меню»: снимок тика 2 мин назад, VPN-транзит, SMB, доступно обновление"),
    _shot("gw.panel.refresh", press=[GwCB(action="refresh")], tick=_TICK_LAN,
          live=_status_lan(month_rx=0), title="«🔄 Обновить» — живой замер поверх снимка тика"),
    _shot("gw.panel.channel.active", press=[GwCB(action="panel")], channel="active", standby=True,
          tick=_status_lan(), title="упр. канал на связи, шлюз несёт трафик"),
    _shot("gw.panel.channel.standby", press=[GwCB(action="panel")], channel="standby", standby=True,
          tick=_status(ssh=_ssh_panel(filter=True, allow=2)),
          title="упр. канал на связи, шлюз в резерве; фильтр SSH на 2 адреса"),
    _shot("gw.panel.channel.offline", press=[GwCB(action="panel")], channel="offline",
          tick=_status(mark_status="unmarked"),
          title="канал включён, но нет связи; шлюз не назначен — переслать сообщение"),
    _shot("gw.panel.unmarked.online", press=[GwCB(action="panel")], channel="active",
          tick=_status(mark_status="unmarked"), title="шлюз не назначен, запрос ушёл по каналу"),
    _shot("gw.panel.unconfirmed", press=[GwCB(action="panel")],
          tick=_status(mark_status="unconfirmed", ssh=_ssh_panel(owner="omv", new_plumbing=False)),
          title="аплинк не найден; SSH под OMV, обвязка старого образца"),
    _shot("gw.panel.broken", press=[GwCB(action="panel")], tick=_status_bad(),
          title="конфигурация расходится: чужой слот, линк лежит, проблемы, sshd не запущен"),
    _shot("gw.panel.own_pending", press=[GwCB(action="panel")],
          tick=_status_lan(lan=_lan(own={"active": True, "state": "no_link"},
                                    svc={"active": True, "own": [], "peer": [], "ever": False}),
                           checks=copy.deepcopy(_CHECKS_OK) + [
                               GwCheck("резолвер", False, "dnsmasq не отвечает", group="lan")]),
          title="VPN-транзит: резолвер упал, свои списки ждут синхронизации, SMB ещё не пришли"),

    # ── W01 здоровье ────────────────────────────────────────────────────────
    _shot("gw.health", press=[GwCB(action="health")],
          live=_status_lan(throttled={"now": [], "ever": ["недонапряжение"]}),
          title="всё в порядке, просадка питания с загрузки"),
    _shot("gw.health.broken", press=[GwCB(action="health")], live=_status_bad(),
          title="проблемы, «не проверено», питание сейчас"),

    # ── W02 мастер восстановления ───────────────────────────────────────────
    _shot("gw.reassert.ask", press=[GwCB(action="reassert")], title="с панели, шлюз трафик не несёт"),
    _shot("gw.reassert.ask.carries", press=[GwCB(action="reassert", val="health")], carries=True,
          title="со здоровья, шлюз несёт трафик — отмена ведёт в здоровье"),
    _shot("gw.reassert.done", press=[GwCB(action="reassert"), GwCB(action="reassert!")],
          title="выполнено: итог остаётся, панель следом"),
    _shot("gw.reassert.failed", press=[GwCB(action="reassert"), GwCB(action="reassert!")],
          op=(False, "routing-gw-setup.sh: не уложилось в 120 с"), title="отказ: хвост вывода"),

    # ── W10–W14 VPN-транзит и свои списки ───────────────────────────────────
    _shot("gw.lan.empty", press=[GwCB(action="lan")], tick=_status_lan(lan=_lan(own_vpn=0, own_ru=0)),
          own={"active": False, "state": "off", "no_channel": True},
          title="своих доменов нет, упр. канала нет — списки только этого шлюза"),
    _shot("gw.lan", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED,
          title="три своих домена, списки общие и синхронизированы"),
    _shot("gw.lan.pending", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": True, "state": "no_link", "pending": 2},
          title="правки ждут синхронизации — нет связи с сервером"),
    _shot("gw.lan.failed", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": True, "state": "failed", "err": "dnsmasq отверг конфиг <vpn.conf>"},
          title="свои списки не применились (ошибка с хоста — экранирована)"),
    _shot("gw.lan.rejected", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": True, "state": "rejected", "rej": [["awg-srv.example.com", "это хост сервера"]]},
          title="сервер не принял домен"),
    _shot("gw.lan.many", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_MANY_OWN, own=_OWN_SYNCED,
          title="13 своих доменов — первая страница"),
    _shot("gw.lan.page2", press=[GwCB(action="lan"),
                                 PageCB(screen="lanlist", ref=0, page=1, back=GwCB(action="lan").pack())],
          tick=_TICK_LAN, own_items=_MANY_OWN, own=_OWN_SYNCED, title="листание: вторая страница"),
    _shot("gw.lan.add.ask", press=[GwCB(action="lan"), GwCB(action="lan_add")], tick=_TICK_LAN,
          title="приглашение «➕ В туннель»"),
    _shot("gw.lan.ru.ask", press=[GwCB(action="lan"), GwCB(action="lan_ru")], tick=_TICK_LAN,
          title="приглашение «➕ Напрямую»"),
    _shot("gw.lan.add.done", press=[GwCB(action="lan"), GwCB(action="lan_add")],
          text="example.org example.com localhost", tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": False, "state": "off", "no_channel": True},
          title="итог первой строкой: добавлен / уже в списке / не домен; канала нет"),
    _shot("gw.lan.add.online", press=[GwCB(action="lan"), GwCB(action="lan_add")], text="example.org",
          tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="active", standby=True,
          title="итог с хвостом «синхронизируются» — канал на связи"),
    _shot("gw.lan.ru.solo", press=[GwCB(action="lan"), GwCB(action="lan_ru")], text="gosuslugi.ru",
          tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="active", standby=False,
          title="другого шлюза нет — итог без хвоста"),
    _shot("gw.lan.add.blank", press=[GwCB(action="lan"), GwCB(action="lan_add")], text=" ",
          tick=_TICK_LAN, title="пустой ввод — переспрос"),
    _shot("gw.lan.add.failed", press=[GwCB(action="lan"), GwCB(action="lan_add")], text="example.org",
          tick=_TICK_LAN, own_items=_OWN_ITEMS, lan_fail="dnsmasq не перезапустился — откатываю",
          title="скрипт отказал"),
    _shot("gw.lan.cancel", press=[GwCB(action="lan"), GwCB(action="lan_add"), CancelCB(kind="set_lan")],
          tick=_TICK_LAN, own_items=_OWN_ITEMS, title="«✖️ Отмена» под приглашением — экран на месте"),
    _shot("gw.lan.rm", press=[GwCB(action="lan"), _lan_rm(_OWN_ITEMS, "example.net")], tick=_TICK_LAN,
          own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="active", standby=True,
          title="«➖ домен» — сразу, всплывашка с хвостом синхронизации"),
    _shot("gw.lan.rm.stale", press=[GwCB(action="lan"), GwCB(action="lan_rm", val="0.deadbeef")],
          tick=_TICK_LAN, own_items=_OWN_ITEMS, title="список изменился — переспрос, экран заново"),
    _shot("gw.lan.rm.failed", press=[GwCB(action="lan"), _lan_rm(_OWN_ITEMS, "sber.ru")], tick=_TICK_LAN,
          own_items=_OWN_ITEMS, lan_fail="<code>sber.ru</code>: dnsmasq не перезапустился — откатываю",
          title="скрипт отказал — alert без разметки"),
    _shot("gw.lan.router", press=[GwCB(action="lan"), GwCB(action="lan_router")], tick=_TICK_LAN,
          title="❓ Роутер: вкладка MikroTik"),
    _shot("gw.lan.router.ow", press=[GwCB(action="lan"), GwCB(action="lan_router", val="ow")],
          tick=_TICK_LAN, channel="active", title="❓ Роутер: вкладка OpenWrt, имя слота с сервера"),

    # ── W20 корень настроек ─────────────────────────────────────────────────
    _shot("gw.set", press=[GwCB(action="settings")], title="корень настроек"),

    # ── W21 уведомления ─────────────────────────────────────────────────────
    _shot("gw.set.notify", press=[GwCB(action="notify")], title="по умолчанию"),
    _shot("gw.set.notify.quiet", press=[GwCB(action="notify")],
          conf={"quiet_hours.quiet_hours_enabled": True, "notifications.email_fallback": True},
          title="тихие часы и аварии на e-mail включены"),
    _shot("gw.set.notify.alerts_off", press=[GwCB(action="notify")], conf={"resource_alerts.enabled": False},
          title="алерты хоста выключены"),
    _shot("gw.set.notify.toggle", press=[GwCB(action="notify"),
                                         GwCB(action="tgl", val="quiet_hours.quiet_hours_enabled")],
          title="тумблер тихих часов — раздел на месте"),
    _shot("gw.set.notify.fallback.no_mail", press=[GwCB(action="notify"),
                                                   GwCB(action="tgl", val="notifications.email_fallback")],
          title="аварии на e-mail без ящика — предложение настроить"),
    _shot("gw.set.notify.fallback.on", press=[GwCB(action="notify"),
                                              GwCB(action="tgl", val="notifications.email_fallback")],
          **_MAIL, title="аварии на e-mail с ящиком — включено"),
    _shot("gw.set.notify.edit.ask", press=[GwCB(action="notify"),
                                           GwCB(action="edit", val="resource_alerts.thresholds_percent.cpu")],
          title="приглашение: порог CPU"),
    _shot("gw.set.notify.edit.done", press=[GwCB(action="notify"),
                                            GwCB(action="edit", val="app.gateway.temp_alert_c")],
          text="70", title="итог первой строкой раздела"),
    _shot("gw.set.notify.edit.bad", press=[GwCB(action="notify"),
                                           GwCB(action="edit", val="app.gateway.temp_alert_c")],
          text="150", title="вне границ — переспрос"),
    _shot("gw.set.notify.edit.cancel", press=[GwCB(action="notify"),
                                              GwCB(action="edit", val="resource_alerts.thresholds_percent.cpu"),
                                              CancelCB(kind="set_notify")],
          title="«✖️ Отмена» — раздел на месте приглашения"),

    # ── W22 почта ───────────────────────────────────────────────────────────
    _shot("gw.set.email", press=[GwCB(action="email")], title="ящик не подключён"),
    _shot("gw.set.email.configured", press=[GwCB(action="email")], **_MAIL,
          title="ящик подключён, последняя проверка удачная"),
    _shot("gw.set.email.check_failed_before", press=[GwCB(action="email")], mail=True, mail_last="fail",
          title="ящик подключён, последняя проверка — отказ"),
    _shot("gw.set.email.setup.ask", press=[GwCB(action="email"), GwCB(action="em_setup")],
          title="мастер: адрес"),
    _shot("gw.set.email.setup.change", press=[GwCB(action="email"), GwCB(action="em_setup")], **_MAIL,
          title="мастер: смена ящика"),
    _shot("gw.set.email.setup.known", press=[GwCB(action="email"), GwCB(action="em_setup")],
          text="admin@gmail.com", title="мастер: провайдер знаком — сразу пароль"),
    _shot("gw.set.email.setup.unknown", press=[GwCB(action="email"), GwCB(action="em_setup")],
          text="admin@example.org", title="мастер: провайдер незнаком — IMAP"),
    _shot("gw.set.email.setup.bad", press=[GwCB(action="email"), GwCB(action="em_setup")],
          text="не адрес", title="мастер: не адрес — переспрос"),
    _shot("gw.set.email.setup.cancel", press=[GwCB(action="email"), GwCB(action="em_setup"),
                                              CancelCB(kind="set_email")], title="мастер: отмена"),
    _shot("gw.set.email.setup.imap", steps=[_p(GwCB(action="email")), _p(GwCB(action="em_setup")),
                                            _t("admin@example.org"), _t("imap.example.org:993")],
          title="мастер: IMAP с портом — дальше SMTP"),
    _shot("gw.set.email.setup.imap_port", steps=[_p(GwCB(action="email")), _p(GwCB(action="em_setup")),
                                                 _t("admin@example.org"), _t("imap.example.org")],
          title="мастер: IMAP без порта — порт отдельным шагом"),
    _shot("gw.set.email.setup.bad_host", steps=[_p(GwCB(action="email")), _p(GwCB(action="em_setup")),
                                                _t("admin@example.org"), _t("imap:99999")],
          title="мастер: не сервер — переспрос"),
    _shot("gw.set.email.setup.smtp", steps=[_p(GwCB(action="email")), _p(GwCB(action="em_setup")),
                                            _t("admin@example.org"), _t("imap.example.org:993"),
                                            _t("smtp.example.org:587")],
          title="мастер: SMTP — дальше пароль"),
    _shot("gw.set.email.setup.saved", steps=[_p(GwCB(action="email")), _p(GwCB(action="em_setup")),
                                             _t("admin@example.org"), _t("imap.example.org:993"),
                                             _t("smtp.example.org:587"), _t("DUMMY-password")],
          title="мастер: пароль удалён, вход проверен, ящик сохранён"),
    _shot("gw.set.email.setup.saved_known", steps=[_p(GwCB(action="email")), _p(GwCB(action="em_setup")),
                                                   _t("admin@gmail.com"), _t("DUMMY-password")],
          title="мастер: знакомый провайдер — адрес и пароль"),
    _shot("gw.set.email.setup.login_failed", steps=[_p(GwCB(action="email")), _p(GwCB(action="em_setup")),
                                                    _t("admin@gmail.com"), _t("DUMMY-password")],
          mail_error="IMAP: вход отклонён — нужен пароль приложения",
          title="мастер: вход не прошёл — ящик не сохранён"),
    _shot("gw.set.email.check", press=[GwCB(action="email"), GwCB(action="em_check")], **_MAIL,
          title="«🔍 Проверить» — итог первой строкой"),
    _shot("gw.set.email.check.fail", press=[GwCB(action="email"), GwCB(action="em_check")], mail=True,
          mail_error="IMAP imap.example.org:993 недоступен: timed out", title="проверка не прошла"),
    _shot("gw.set.email.test", press=[GwCB(action="email"), GwCB(action="em_test")], **_MAIL,
          title="тест-письмо ушло"),
    _shot("gw.set.email.test.fail", press=[GwCB(action="email"), GwCB(action="em_test")], mail=True,
          mail_error="SMTP: авторизация отклонена", title="тест-письмо не ушло"),
    _shot("gw.set.email.forget.ask", press=[GwCB(action="email"), GwCB(action="em_forget")], **_MAIL,
          title="отключить ящик?"),
    _shot("gw.set.email.forget.done", press=[GwCB(action="email"), GwCB(action="em_forget"),
                                             GwCB(action="em_forget!")], **_MAIL, title="ящик отключён"),

    # ── W23–W26 SSH-доступ ──────────────────────────────────────────────────
    _shot("gw.ssh", press=[GwCB(action="ssh")], title="фильтр снаружи выключен, адресов нет"),
    _shot("gw.ssh.filter", press=[GwCB(action="ssh")],
          ssh=_ssh(filter=True, allow=list(_ALLOW2), peer_nets=["192.168.2.0/24"]),
          title="фильтр включён, два адреса, подсети других шлюзов связаны"),
    _shot("gw.ssh.old_plumbing", press=[GwCB(action="ssh")], ssh=_ssh(new_plumbing=False),
          title="обвязка старого образца — тумблера нет"),
    _shot("gw.ssh.omv", press=[GwCB(action="ssh")],
          ssh=_ssh(owner="omv", owner_port=2222, conf_ports=[2222], ports=[22, 2200], omv_rules=3, ufw=True,
                   filter=True, allow=["203.0.113.7", "dyn.example.net"], unresolved=["dyn.example.net"],
                   held=["203.0.113.99"]),
          title="порт под OMV и все предупреждения"),
    _shot("gw.ssh.generator", press=[GwCB(action="ssh")],
          ssh=_ssh(owner="generator", owner_detail="managed by <cloud-init>",
                   owner_files=["/etc/ssh/sshd_config"]),
          title="порт держит другой процесс — его пометка в конфиге"),
    _shot("gw.ssh.sshd_down", press=[GwCB(action="ssh")], ssh=_ssh(sshd_down=True, ports=[]),
          title="sshd не запущен"),
    _shot("gw.ssh.many", press=[GwCB(action="ssh")], ssh=_ssh(filter=True, allow=list(_ALLOW_MANY)),
          title="12 адресов — листание"),
    _shot("gw.ssh.on", press=[GwCB(action="ssh"), GwCB(action="ssh_on!")], ssh=_ssh(allow=list(_ALLOW2)),
          title="включить фильтр — сразу, предупреждение alert"),
    _shot("gw.ssh.off", press=[GwCB(action="ssh"), GwCB(action="ssh_off!")],
          ssh=_ssh(filter=True, allow=list(_ALLOW2)), title="выключить фильтр — alert"),
    _shot("gw.ssh.del", press=[GwCB(action="ssh"), _ssh_del(_ALLOW2, "home.example.net")],
          ssh=_ssh(filter=True, allow=list(_ALLOW2)), title="«➖ адрес» — сразу, всплывашка"),
    _shot("gw.ssh.del.stale", press=[GwCB(action="ssh"), GwCB(action="ssh_del!", val="5.deadbeef")],
          ssh=_ssh(allow=list(_ALLOW2)), title="список изменился — переспрос"),
    _shot("gw.ssh.port.ask", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], title="приглашение: порт"),
    _shot("gw.ssh.port.done", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="2222",
          title="порт изменён — итог первой строкой раздела"),
    _shot("gw.ssh.port.same", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="22",
          title="тот же порт — финишер"),
    _shot("gw.ssh.port.busy", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="8080",
          busy="nginx", title="порт занят — финишер"),
    _shot("gw.ssh.port.bad", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="70000",
          title="не порт — переспрос"),
    _shot("gw.ssh.port.error", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="2222",
          port_error="sshd не поднялся на 2222 — вернул 22", title="смена не прошла"),
    _shot("gw.ssh.port.owner", press=[GwCB(action="ssh"), GwCB(action="ssh_port")],
          ssh=_ssh(owner="omv", owner_port=22), title="порт под OMV — отказ сразу по кнопке"),
    _shot("gw.ssh.port.retry", steps=[_p(GwCB(action="ssh")), _p(GwCB(action="ssh_port")), _t("22"),
                                      _p(GwCB(action="ssh_port_retry"))],
          title="финишер после ввода → «✏️ Другой порт»: финишер со «Скрыть», приглашение новым"),
    _shot("gw.ssh.port.back", steps=[_p(GwCB(action="ssh")), _p(GwCB(action="ssh_port")), _t("8080"),
                                     _p(GwCB(action="ssh_port_back"))], busy="nginx",
          title="финишер после ввода → «⬅️ Назад»: финишер со «Скрыть», раздел новым"),
    _shot("gw.ssh.add.ask", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], title="приглашение: адреса"),
    _shot("gw.ssh.add.done", press=[GwCB(action="ssh"), GwCB(action="ssh_add")],
          text="198.51.100.4 home.example.net", ssh=_ssh(filter=True), title="добавлено"),
    _shot("gw.ssh.add.merged", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], text="203.0.113.0/24",
          ssh=_ssh(allow=["203.0.113.7", "203.0.113.9", "home.example.net"]),
          title="подсеть поглотила адреса"),
    _shot("gw.ssh.add.already", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], text="203.0.113.7",
          ssh=_ssh(allow=list(_ALLOW2)), title="всё уже в списке"),
    _shot("gw.ssh.add.bad", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], text="2001:db8::1",
          title="IPv6 — переспрос"),
    _shot("gw.ssh.add.cancel", press=[GwCB(action="ssh"), GwCB(action="ssh_add"), CancelCB(kind="set_ssh")],
          title="«✖️ Отмена» — раздел на месте"),

    # ── W31 мониторинг ──────────────────────────────────────────────────────
    _shot("gw.set.mon", press=[GwCB(action="mon")], title="по умолчанию"),
    _shot("gw.set.mon.quiet", press=[GwCB(action="mon")],
          conf={"app.gateway.link_alert_loud": False, "app.gateway.handshake_max_age": 90},
          title="звук по тихим часам; 90 с — «2 мин»"),
    _shot("gw.set.mon.toggle", press=[GwCB(action="mon"), GwCB(action="tgl", val="app.gateway.link_alert_loud")],
          title="тумблер «Звук 24/7»"),
    _shot("gw.set.mon.edit.ask", press=[GwCB(action="mon"),
                                        GwCB(action="edit", val="app.gateway.handshake_max_age")],
          title="приглашение: порог линка в минутах"),
    _shot("gw.set.mon.edit.done", press=[GwCB(action="mon"),
                                         GwCB(action="edit", val="app.gateway.handshake_max_age")],
          text="10", title="итог: минуты, в conf — секунды"),
    _shot("gw.set.mon.edit.unknown", press=[GwCB(action="mon"), GwCB(action="edit", val="app.nope")],
          title="ключ из старой клавиатуры — alert"),

    # ── W32–W33 бэкапы и шифрование ─────────────────────────────────────────
    _shot("gw.set.backup", press=[GwCB(action="backup")], title="по умолчанию, фразы нет"),
    _shot("gw.set.backup.off", press=[GwCB(action="backup")], conf={"app.scheduler.backup_enabled": False},
          title="автобэкапы выключены — раздел свёрнут"),
    _shot("gw.set.backup.email", press=[GwCB(action="backup")], passphrase=True, **_MAIL,
          conf={"app.scheduler.backup_channel": "email"}, title="на e-mail, фраза задана"),
    _shot("gw.set.backup.channel.no_mail", press=[GwCB(action="backup"),
                                                  GwCB(action="cyc", val="app.scheduler.backup_channel")],
          title="«Куда» → e-mail без ящика — предложение настроить"),
    _shot("gw.set.backup.channel.no_enc", press=[GwCB(action="backup"),
                                                 GwCB(action="cyc", val="app.scheduler.backup_channel")],
          **_MAIL, title="«Куда» → e-mail без фразы — alert"),
    _shot("gw.set.backup.channel.email", press=[GwCB(action="backup"),
                                                GwCB(action="cyc", val="app.scheduler.backup_channel")],
          passphrase=True, **_MAIL, title="«Куда» → e-mail"),
    _shot("gw.set.backup.when.ask", press=[GwCB(action="backup"), GwCB(action="edit", val="backup_when")],
          title="приглашение: день и час"),
    _shot("gw.set.backup.when.done", press=[GwCB(action="backup"), GwCB(action="edit", val="backup_when")],
          text="5 3", title="итог первой строкой"),
    _shot("gw.set.backup.when.bad", press=[GwCB(action="backup"), GwCB(action="edit", val="backup_when")],
          text="31 25", title="вне границ — переспрос"),
    _shot("gw.set.backup.now.nokey", press=[GwCB(action="backup"), GwCB(action="backup!")],
          title="«Сделать сейчас» без фразы — отказ сообщением"),
    _shot("gw.set.backup.now", press=[GwCB(action="backup"), GwCB(action="backup!")], passphrase=True,
          title="«Сделать сейчас» — файл в чат"),
    _shot("gw.set.backup.now.mailed", press=[GwCB(action="backup"), GwCB(action="backup!")], passphrase=True,
          **_MAIL, conf={"app.scheduler.backup_channel": "email"}, title="«Сделать сейчас» — на ящик"),
    _shot("gw.set.enc", press=[GwCB(action="backup"), GwCB(action="enc")], title="фразы нет"),
    _shot("gw.set.enc.set", press=[GwCB(action="backup"), GwCB(action="enc")], passphrase=True,
          title="фраза задана"),
    _shot("gw.set.enc.ask", press=[GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set")],
          title="приглашение: фраза"),
    _shot("gw.set.enc.first", press=[GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set")],
          text="DUMMY-passphrase", title="первая фраза принята и удалена — повтор"),
    _shot("gw.set.enc.short", press=[GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set")],
          text="DUMMY", title="короткая фраза — переспрос"),
    _shot("gw.set.enc.saved", steps=[_p(GwCB(action="backup")), _p(GwCB(action="enc")),
                                     _p(GwCB(action="enc_set")), _t("DUMMY-passphrase"), _t("DUMMY-passphrase")],
          title="повтор совпал — фраза задана, итог первой строкой бэкапов"),
    _shot("gw.set.enc.mismatch", steps=[_p(GwCB(action="backup")), _p(GwCB(action="enc")),
                                        _p(GwCB(action="enc_set")), _t("DUMMY-passphrase"), _t("DUMMY-other")],
          title="повтор не совпал — заново с первой фразы"),
    _shot("gw.set.enc.cancel", press=[GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set"),
                                      CancelCB(kind="set_backup")], title="«✖️ Отмена» — бэкапы"),

    # ── W34 сервис: перезапуски ─────────────────────────────────────────────
    _shot("gw.set.svc", press=[GwCB(action="svc")], title="🔧 Сервис"),
    _shot("gw.svc.restart.ask", press=[GwCB(action="svc"), GwCB(action="restart")], carries=True,
          title="перезапустить AWG? шлюз несёт трафик"),
    _shot("gw.svc.restart.ask.idle", press=[GwCB(action="svc"), GwCB(action="restart")],
          title="перезапустить AWG? трафика нет"),
    _shot("gw.svc.restart.done", press=[GwCB(action="svc"), GwCB(action="restart"), GwCB(action="restart!")],
          title="выполнено: итог и панель"),
    _shot("gw.svc.restart.failed", press=[GwCB(action="svc"), GwCB(action="restart"),
                                          GwCB(action="restart!")],
          op=(False, "awg-quick: <awg0> не поднялся"), title="отказ"),
    _shot("gw.svc.botrestart.ask", press=[GwCB(action="svc"), GwCB(action="botrestart")],
          title="перезапустить бота?"),
    _shot("gw.svc.botrestart.done", press=[GwCB(action="svc"), GwCB(action="botrestart"),
                                           GwCB(action="botrestart!")], title="обещание на месте меню"),

    # ── W40–W41 обновления ──────────────────────────────────────────────────
    _shot("gw.upd", press=[GwCB(action="updates")], title="версия актуальна"),
    _shot("gw.upd.available", press=[GwCB(action="updates")], release=_RELEASE,
          title="доступна новая версия: список изменений"),
    _shot("gw.upd.scan_failed", press=[GwCB(action="updates")], scan_fails=True,
          title="список релизов недоступен"),
    _shot("gw.upd.notify", press=[GwCB(action="updates"), GwCB(action="upd_toggle")], muted=True,
          title="«Уведомлять» — включены"),
    _shot("gw.upd.schedule", press=[GwCB(action="updates"), GwCB(action="cyc", val="updates.poll_schedule")],
          title="«📅 Проверка» по кругу: день → неделя"),
    _shot("gw.upd.schedule.never", press=[GwCB(action="updates")], conf={"updates.poll_schedule": "never"},
          title="«никогда» из старого conf → «месяц» и уведомления выкл"),
    _shot("gw.upd.install", press=[GwCB(action="updates"), UpdateCB(action="install")], release=_RELEASE,
          title="«⬆️ Обновить» — шаг убран, «дождись завершения»"),
    _shot("gw.upd.install.nothing", press=[UpdateCB(action="install")], title="обновлять не на что — alert"),
    _shot("gw.upd.install.failed", press=[GwCB(action="updates"), UpdateCB(action="install")],
          release=_RELEASE, update_error="sha256 не сошёлся", title="отказ до апдейтера — итог и панель"),
    _shot("gw.upd.menu", press=[UpdateCB(action="menu")], title="«В меню» на итоге обновления"),
    _shot("gw.upd.mute", press=[UpdateCB(action="mute")], title="«Не уведомлять» на уведомлении"),

    # ── W50–W60 файл конфигурации и восстановление: кнопки без файла ────────
    _shot("gw.bundle.apply.nofile", press=[GwCB(action="apply!")], title="«📦 Применить», файла в памяти нет"),
    _shot("gw.bundle.keep.nofile", press=[GwCB(action="apply_keep!")],
          title="«Оставить свою», файла в памяти нет"),
    _shot("gw.bundle.drop", press=[GwCB(action="drop")], title="«⬅️ Отмена» — файл отброшен, панель"),
    _shot("gw.restore.nofile", press=[GwCB(action="restore!")], title="восстановить — файла в памяти нет"),
    _shot("gw.restore.drop", press=[GwCB(action="restore_drop")], title="отказ от восстановления"),

    # ── W50–W52 файл конфигурации шлюза ─────────────────────────────────────
    _shot("gw.bundle.received", steps=[_file(_BUNDLE_NAME, _BUNDLE)],
          title="файл принят: линк перезапустится, трафик шлюз не несёт"),
    _shot("gw.bundle.received.carries", steps=[("start", ""), _file(_BUNDLE_NAME, _BUNDLE)], carries=True,
          tick=_status(), title="шлюз несёт трафик — прежняя панель удалена, предупреждение об обрыве"),
    _shot("gw.bundle.received.same_link", steps=[_file(_BUNDLE_NAME, _BUNDLE)],
          bundle={"ok": True, "link_changed": False}, title="конфиг линка тот же — без обрыва"),
    _shot("gw.bundle.first_run", steps=[_file("awg-gw-bundle.sh", b"#!/bin/sh\n# DUMMY\n")],
          title="файл первого применения по имени — убран из чата"),
    _shot("gw.bundle.first_run.renamed", steps=[_file("setup.sh", b"#!/bin/sh\n# awg-gw-bundle DUMMY\n")],
          title="файл первого применения по содержимому"),
    _shot("gw.bundle.not_ours", steps=[_file("photo.bin", b"DUMMY")], title="не конфигурация шлюза"),
    _shot("gw.bundle.too_big", steps=[_file("video.bin", b"\0" * (600 * 1024))],
          title="крупнее любого файла конфигурации — не скачивается"),
    _shot("gw.bundle.passphrase", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!"))],
          bundle={"ok": True, "passphrase": True, "passphrase_differs": True, "link_changed": True},
          title="фраза бэкапов в файле отличается — вопрос"),
    _shot("gw.bundle.applied", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!"))],
          title="применено: отчёт строками, адреса моноширинным, панель следом"),
    _shot("gw.bundle.applied.keep", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!")),
                                           _p(GwCB(action="apply_keep!"))],
          bundle={"ok": True, "passphrase": True, "passphrase_differs": True, "link_changed": True},
          apply_status={"UPLINK": "unchanged", "LINK": "up", "GW_STATUS": "confirmed", "SSH_FILTER": "0"},
          title="«Оставить свою»: аплинк без изменений, фильтр SSH выключен"),
    _shot("gw.bundle.applied.overwrite", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!")),
                                                _p(GwCB(action="apply_ow!"))],
          bundle={"ok": True, "passphrase": True, "passphrase_differs": True, "link_changed": True},
          apply_status={"LINK": "up", "GW_STATUS": "confirmed", "SSH_FILTER": "1", "SSH_ALLOW_COUNT": "0",
                        "LAN": "1", "LAN_ERROR": "dnsmasq не установлен"},
          title="«🔐 Перезаписать»: фильтр только для сервера, VPN-транзит не применён"),
    _shot("gw.bundle.applied.mail_ok", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!"))],
          bundle_mail={"state": "ok", "backup": True},
          title="почта из файла проверена, бэкапы переключены на e-mail"),
    _shot("gw.bundle.applied.mail_fail", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!"))],
          bundle_mail={"state": "fail", "why": "IMAP imap.example.org:993: вход отклонён"},
          title="почта из файла не прошла проверку"),
    _shot("gw.bundle.failed", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!"))],
          apply=(False, "routing-gw-setup.sh: аплинк <awg1> не поднялся — откатываю"),
          title="не применено: общая маска и причина"),
    _shot("gw.bundle.claim.channel", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!"))],
          channel="active", mark={"status": "unmarked", "claim": "DUMMY-claim-token"},
          apply_status={"UPLINK": "installed", "LINK": "up", "GW_STATUS": "unmarked"},
          title="шлюз не назначен, канал на связи — запрос ушёл сам"),
    _shot("gw.bundle.claim.forward", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!"))],
          mark={"status": "foreign", "claim": "DUMMY-claim-token"},
          apply_status={"UPLINK": "installed", "LINK": "foreign", "GW_STATUS": "foreign"},
          title="слот за другим устройством, канала нет — переслать сообщение"),
    _shot("gw.bundle.claim.unconfirmed", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="apply!"))],
          mark={"status": "unconfirmed", "claim": "DUMMY-claim-token"},
          apply_status={"UPLINK": "unchanged", "LINK": "unconfirmed", "GW_STATUS": "unconfirmed"},
          title="аплинк не найден, канала нет — переслать сообщение"),
    _shot("gw.bundle.dropped", steps=[_file(_BUNDLE_NAME, _BUNDLE), _p(GwCB(action="drop"))],
          title="«⬅️ Отмена» после файла — панель"),

    # ── W60 восстановление из копии ─────────────────────────────────────────
    _shot("gw.restore.offer", steps=[_file(_COPY_NAME, _COPY)], backup_key=True,
          title="копия шлюза — предложение"),
    _shot("gw.restore.offer.ifaces", steps=[_file(_COPY_NAME, _COPY_IFACE)], backup_key=True, carries=True,
          title="копия перезапишет конфиг линка — предупреждение о перезапуске AWG"),
    _shot("gw.restore.run", steps=[_file(_COPY_NAME, _COPY), _p(GwCB(action="restore!"))], backup_key=True,
          title="«Восстановить» — кнопки сняты, отчёт пришлёт новый процесс"),
    _shot("gw.restore.cancel", steps=[_file(_COPY_NAME, _COPY), _p(GwCB(action="restore_drop"))],
          backup_key=True, title="отказ после файла"),
    _shot("gw.restore.rejected.plain", steps=[_file("awg-gw-pi-2026-09-14.tgz", _archive())],
          title="открытый архив — на шлюзе только шифрованные"),
    _shot("gw.restore.rejected.no_key", steps=[_file(_COPY_NAME, _COPY)],
          title="шифрованная копия, а фразы здесь нет"),
    _shot("gw.restore.rejected.garbage", steps=[_file("notes.tgz.enc", b"DUMMY-not-a-backup")], backup_key=True,
          title="не расшифровалась"),
    _shot("gw.restore.rejected.not_copy", steps=[_file("notes.tgz.enc", _enc(b"DUMMY-not-a-tar"))],
          backup_key=True, title="расшифровалась, но не копия awg-bot"),
    _shot("gw.restore.rejected.main", steps=[_file("awg-bot-2026-09-14.tgz.enc", _COPY_MAIN)], backup_key=True,
          title="копия основного бота"),
    _shot("gw.restore.rejected.old_schema", steps=[_file(_COPY_NAME, _COPY_OLD)], backup_key=True,
          title="схема базы ниже 3.2.0"),

    # ── события старта процесса ─────────────────────────────────────────────
    _shot("gw.event.first_panel", call=("send_first_panel", _first_panel),
          title="первый запуск после установки — панель сама"),
    _shot("gw.event.restarted", call=("restore_panel_after_restart", _restart_panel), restart_wait=True,
          title="обещание «вернётся через несколько секунд» исполнено, панель следом"),
    _shot("gw.event.updated", call=("report_update_result", _update_result), update_pending="v1.2.3",
          update_wait=True, title="обновление применилось: «дождись» убрано, итог со списком изменений"),
    _shot("gw.event.update_failed", call=("report_update_result", _update_result), update_pending="v1.2.4",
          update_wait=True, title="обновление не применилось"),
    _shot("gw.event.restored", call=("report_restore_result", _restore_result),
          restore_done={"created_at": "2026-09-14T03:00:00+03:00"}, title="восстановлено из копии"),
    _shot("gw.event.rebooted", call=("_announce_reboot", _rebooted), title="хост перезагружен, агент запущен"),

    # ── общее: «Скрыть» на итоге ────────────────────────────────────────────
    _shot("gw.hide", press=[HideCB()], title="«Скрыть» убирает сообщение"),
]

# Длинная подпись в ряду из 2+ разрешена макетом экрана — {id снимка: подписи};
# длина считается без селектора варианта («☑️ Аварии на e-mail» — 18).
LABEL_EXCEPTIONS: dict[str, set[str]] = {}
