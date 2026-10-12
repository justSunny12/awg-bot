"""Двойник агента шлюза и его хоста для снимков; построитель снимка `_shot`.

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

Здесь и в shots_gateway_common — то, чем пользуются снимки нескольких
разделов; построитель и данные одного раздела живут в модуле раздела.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import datetime as _dt
import ipaddress
import types
from dataclasses import dataclass, field

from awgbot.bot import texts
from awgbot.bot.handlers import gateway as gateway_handlers
from awgbot.runtime import hostboot, linkclient
from awgbot.core import config
from awgbot.domain.backupcrypto import BackupKeyMissing
from awgbot.domain import gwcfg
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.domain.gwssh import SshOwnerRefusal
from awgbot.domain.services import ServiceError
from awgbot.infra import mail, sshd, updates
from awgbot.util import timeutil

from tests.screens.base import NOW, Shot
from tests.screens.shots_gateway_common import (_APPLY_STATUS, _BACKUP_KEY, _OWN_OFF, _RELEASE,
                                                _UPLINK_VALUE, _ssh, _status)


async def _no_sleep(_s=0):
    """Ожидание канала после применения — мгновенно: связи в снимке не будет."""


_FAST_ASYNCIO = types.SimpleNamespace(sleep=_no_sleep, get_running_loop=asyncio.get_running_loop)


@dataclass
class _Host:
    """Что агент увидел бы на хосте, если бы это был шлюз."""
    live: GwStatus = field(default_factory=_status)        # живой замер (status)
    tick: GwStatus | None = None                           # снимок тика в БД, tick_age с назад
    tick_age: int = 120
    channel: str = ""                                      # "" — клиента канала нет (нет связи); active | standby | offline
    slot_name: str = "NASPi"                               # имя слота по слову сервера
    standby: bool | None = None                            # есть ли другой шлюз (по слову сервера)
    carries: bool = False                                  # несёт трафик и линк жив
    op: tuple = (True, "")                                 # итог restart_link / reassert
    ssh: dict = field(default_factory=_ssh)
    ssh_after: tuple = ()                                  # (N, правка): после N-го ssh_screen хост меняется
    busy: str = ""                                         # ssh_port_busy
    port_error: str = ""                                   # ssh_port_change: ServiceError
    port_refusal: bool = False                             # ssh_port_change: OMV успел переписать конфиг
    filter_error: str = ""                                 # ssh_filter_on: ServiceError
    own_items: list = field(default_factory=list)          # свои домены (vpn|ru, домен)
    own: dict = field(default_factory=lambda: dict(_OWN_OFF))
    lan_fail: str = ""                                     # скрипт своих списков отказал
    router: tuple = ("192.168.1.0/24", "192.168.1.10", ["192.168.2.0/24"])   # подсеть, адрес, соседи
    late_answer: bool = False                              # итог «➖» дольше, чем Telegram держит нажатие
    mail: bool = False                                     # ящик подключён
    mail_last: str = ""                                    # ok | fail — последняя проверка
    mail_error: str = ""                                   # проверка/тест-письмо падают
    passphrase: bool = False                               # фраза шифрования бэкапов задана
    release: updates.Release | None = None                 # цель обновления
    scan_fails: bool = False                               # список релизов недоступен
    update_error: str = ""                                 # apply_update падает
    update_tag: str = ""                                   # «⬆️ Доступна vX» с прошлой проверки
    waiting: tuple = ()                                    # (тег, нужная версия сервера): релиз ждёт сервер AWG
    server_version: str = ""                               # версия сервера из role: "" | old | X.Y.Z
    hold: bool = False                                     # окно ожидания после старта: role ещё не было
    muted: bool = False
    # файл конфигурации шлюза
    bundle: dict = field(default_factory=lambda: {"ok": True, "mail": False, "passphrase": False,
                                                  "passphrase_differs": False, "link_changed": True})
    apply: tuple = (True, "")                              # итог скрипта применения
    apply_status: dict = field(default_factory=lambda: dict(_APPLY_STATUS))   # файл статуса скрипта
    bundle_mail: dict = field(default_factory=dict)        # проверка почты из файла
    mark: dict = field(default_factory=lambda: {"status": "confirmed", "claim": None})
    cfg_ask: tuple = ()                                    # висящие вопросы документа: uplink | mail | backup, по порядку
    server_caps: tuple = ()                                # возможности сервера из его role (cfg1 — шлёт документ сам)
    rekey: dict = field(default_factory=dict)              # переход ключей линка (link.txn): {"id", "state", "fp"}
    uplink_mine: str = "f00d" * 4                          # отпечаток стоящего аплинка (свой, не серверный)
    uplink_after: str = ""                                 # аплинк поправили руками, пока висит вопрос
    uplink_ok: bool = True                                 # новый конфиг аплинка поднялся (проба прошла)
    uplink_fail: tuple = ("reverted", "аплинк не поднялся за 60 с")   # как не вышло: состояние и причина
    uplink_pending: str = ""                               # конфиг ожидающего аплинка (uplink.pending)
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

    async def send_rekey_answer(self, reply):
        return True


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
        """Раздел SSH; ssh_after — хост меняется между показом и вводом (гонка)."""
        self._ssh_reads = getattr(self, "_ssh_reads", 0) + 1
        if self.h.ssh_after and self._ssh_reads == self.h.ssh_after[0] + 1:
            self.h.ssh.update(self.h.ssh_after[1])
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
        if self.h.filter_error:
            raise ServiceError(self.h.filter_error)
        self.h.ssh["filter"] = True

    def ssh_filter_off(self):
        self.h.ssh["filter"] = False

    def ssh_port_busy(self, port):
        return self.h.busy

    def ssh_port_change(self, port):
        if self.h.port_error:
            raise ServiceError(self.h.port_error)
        if self.h.port_refusal:
            # OMV переписал sshd_config между вводом и сменой — отказ сервиса
            self.h.ssh.update(owner="omv", owner_port=22)
            raise SshOwnerRefusal(sshd.SshdOwner("omv", "OMV: Службы → SSH", 22), 22)
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
        # сверка файлов (own_reconcile) видит правку, только если список изменился
        self._own_changed = items != self.h.own_items
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
        return getattr(self, "_own_changed", True)

    def own_unsent(self):
        return False

    def lan_router_params(self):
        net, addr, peers = self.h.router
        return net, addr, list(peers)

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

    # ── документ: ключи линка и аплинк ──────────────────────────────────────
    def rekey_state(self):
        """link.txn — память хоста снимка, а не файл в /etc/awg-gw."""
        return dict(self.h.rekey)

    def _rk_save(self, st):
        self.h.rekey = dict(st or {})

    def cfg_uplink_print(self, cached=False):
        """Отпечаток стоящего аплинка — свой, отличается от серверного."""
        return self.h.uplink_mine

    def cfg_uplink_apply(self, value):
        """Итог транзакции: applied | reverted | revert_failed | refused | busy."""
        if self.h.uplink_ok:
            return {"ok": True, "error": "", "state": "applied"}
        state, why = self.h.uplink_fail
        return {"ok": False, "error": why, "state": state}

    def _cfg_uplink_pending_put(self, value):
        """Ожидающий конфиг аплинка — в памяти хоста снимка, а не в /etc/awg-gw."""
        self.h.uplink_pending = str(value.get("conf") or "")
        return {k: v for k, v in value.items() if k != "conf"}

    def _cfg_uplink_pending_fill(self, value):
        return value if "conf" in value else {**value, "conf": self.h.uplink_pending}

    def _cfg_uplink_pending_drop(self):
        self.h.uplink_pending = ""

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
            st.ts = timeutil.to_iso(NOW - _dt.timedelta(seconds=h.tick_age))
            services.db.set_state(services._SNAPSHOT_KEY, st.to_json())
            if h.own_items:
                services.lan_own_counts_refresh()
        if h.channel in ("active", "standby"):
            services.set_link_role(h.channel == "active", standby=h.standby, name=h.slot_name)
        elif h.standby is not None:
            services.db.set_state(services._LINK_STANDBY_KEY, "1" if h.standby else "0")
        if h.server_caps:
            services.db.set_state(services._LINK_SERVER_CAPS_KEY, " ".join(h.server_caps))
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
        if h.server_version:
            services.db.set_state(services._LINK_SERVER_VERSION_KEY, h.server_version)
        if h.hold:
            services.update_hold_start()
        if h.waiting:
            import json
            services.db.set_state(services._WAITING_KEY, json.dumps({"tag": h.waiting[0], "need": h.waiting[1]}))
        if h.backup_key:
            services.db.set_state(services._BK_KEY_KEY, base64.b64encode(_BACKUP_KEY).decode())
        for section in h.cfg_ask:
            # сервер прислал своё, отличное от стоящего: вопрос повис (как после документа)
            if section == "uplink":
                value = dict(_UPLINK_VALUE)
                server_print = gwcfg.uplink_print(value["conf"], value["pubkey"], value["prev_pubkey"])
            elif section == "mail":
                value = {"login": "box@example.org", "password": "DUMMY-server", "imap_host": "imap.example.org",
                         "imap_port": 993, "smtp_host": "smtp.example.org", "smtp_port": 465, "backup": "email"}
                server_print = gwcfg.mail_print(value)
            else:
                value = {"phrase": "DUMMY-server-phrase"}
                server_print = gwcfg.backup_print(value["phrase"])
            services.cfg_hold(section, value, server_print, 18, "9c41" * 4)
        if h.uplink_after:
            h.uplink_mine = h.uplink_after
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
        if h.late_answer:
            _late_removal_toast(mp)
        return config.ADMIN_ID, "Админ"
    return build


def _late_removal_toast(mp) -> None:
    """Всплывашка итога «➖» опоздала: скрипт ответил дольше, чем Telegram
    держит нажатие, — ответ на колбэк отвергнут (остальные ответы — как были)."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import CallbackQuery
    real = CallbackQuery.answer

    def answer(self, text=None, **kw):
        if text and ": убран" in text:
            async def too_old():
                raise TelegramBadRequest(method=None, message="query is too old and response timeout expired")
            return too_old()
        return real(self, text=text, **kw)
    mp.setattr(CallbackQuery, "answer", answer)


def _shot(id_: str, *, press=(), text=None, conf=None, start=None, title="", steps=(), call=None,
          **opts) -> Shot:
    return Shot(id_, role="gateway", press=press, text=text, conf=conf or {}, start=start,
                title=title, steps=steps, call=call, data=_agent(**opts))


def _p(cb) -> tuple:
    return ("press", cb)


def _t(text: str) -> tuple:
    return ("text", text)
