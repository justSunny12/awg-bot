"""Снимки экранов админа (эталон admin.txt): жмёт админ, диспетчер основного бота.

Группы — как в перечне экранов: главная и ссылки с неё, устройства, РФ-доступ,
профили, новый профиль и объявление, настройки, вне меню, ссылки /start.
Состояние БД каждого снимка строят построители ниже (_people, _slots, …):
порядок создания в них фиксирован, поэтому номера профилей, устройств и
слотов в колбэках снимков — константы (сверяются assert'ом в построителе).
Хост (sshd, файервол, ядро AWG, сеть шлюза) в снимках не трогается: его
ответы — фиксированные словари и подмены методов сервисов.
"""
from __future__ import annotations

import base64

import datetime as _dt
import json


from awgbot.bot.callbacks import (AdminSelfCB, BlockCB, BroadcastCB, CancelCB, ClientCB, DelDeviceCB,
                                  DeviceCB, GwMarkCB, GwSlotCB, Menu, NoteCB, PageCB, PeriodCB, PresetCB,
                                  ReassignCB, RoutingCB, SetCB, UpdateCB)
from awgbot.core import config

from tests.screens.base import NOW, Shot

GB = 1024 ** 3

# номера, которые выдаёт свежая БД при порядке создания из _people
SVC, ADM = 1, 2                         # служебный профиль, профиль админа
KS, BR, PT = 3, 4, 5                    # Ксюша, Боря, Петя (не активирован)
IPHONE, MAC, PHONE, LAPTOP, TABLET, ALIEN = 1, 2, 3, 4, 5, 6


# ── построители состояния ────────────────────────────────────────────────────

def _iso(dt: _dt.datetime) -> str:
    from awgbot.util import timeutil
    return timeutil.to_iso(dt)


def _codes(services) -> None:
    """Коды приглашений — по порядку, а не случайные: ссылка приглашения
    попадает в текст экрана."""
    seq = iter(range(1, 1000))
    services._gen_code_body = lambda: f"DUMMY{next(seq):06d}"


def _base(services, *, status: bool = True):
    """Профиль админа, детерминированные коды приглашений, без живых
    замеров хоста; status — шапка «сервер работает, аптайм, метрики»."""
    from awgbot.infra import hostmetrics
    _codes(services)
    # файервол хоста (nft, /etc/nftables.conf) — не трогаем: его перерисовку
    # зовут назначение и снятие шлюза и завершение переезда
    services._firewall_apply = lambda rollback=False: None
    services.refresh_status_now = lambda: []
    services.update_scan = lambda: None
    services.ensure_admin_client()
    assert services.admin_client().id == ADM
    if status:
        services.db.set_state("server_ok_view", "1")
        services.db.set_state("container_started_at", "2026-09-12T09:00:00Z")
        services.db.set_state(hostmetrics.STATE_METRICS,
                              json.dumps({"cpu": 12, "ram": 41, "disk": 23, "ts": _iso(NOW)}))
    return config.ADMIN_ID, "Админ"


def _client(services, name: str, tg: int | None, *, kind: str = "year", limit: int = 3,
            traffic_gb: int = 0, devices=(), routing: bool = False) -> int:
    created = services.create_client(name, limit, kind, traffic_gb * GB)
    if tg is not None:
        res = services.activate_client(created.invite_code, tg)
        assert res.ok, res.reason
    if routing:
        services.db.update_client_fields(created.client_id, routing_allowed=1)
    for d in devices:
        services.add_device(created.client_id, d)
    return created.client_id


def _online(services, *device_ids) -> None:
    now = int(NOW.timestamp())
    for d in device_ids:
        services.db.update_device_fields(d, last_handshake=now)


def _people(services, *, status: bool = True, admin_devices=("iPhone", "MacBook")):
    """Типичная установка: у админа два устройства; Ксюша — годовая подписка,
    два устройства, РФ-доступ разрешён, трафик и онлайн; Боря — месяц,
    истекает через два дня, лимит устройств исчерпан; Петя не активировал
    приглашение; один пир без профиля, созданный мимо бота."""
    who = _base(services, status=status)
    for d in admin_devices:
        services.add_device(ADM, d)
    assert _client(services, "Ксюша", 2001, devices=("Телефон", "Ноутбук"), routing=True) == KS
    assert _client(services, "Боря", 2002, kind="month", limit=1, devices=("Планшет",)) == BR
    assert _client(services, "Петя", None, limit=2) == PT
    services.db.update_client_fields(BR, period_start=_iso(NOW - _dt.timedelta(days=28)),
                                     period_end=_iso(NOW + _dt.timedelta(days=2)))
    services.db.create_device(SVC, "app", "DUMMYPUB=", "DUMMYPSK=", "10.8.1.70")
    services.db.add_traffic_bulk([(PHONE, 3 * GB, int(0.6 * GB)), (LAPTOP, GB // 2, GB // 10),
                                  (IPHONE, 2 * GB, GB // 5), (TABLET, GB // 4, GB // 20)])
    services.db.rf_add_bulk([(PHONE, GB // 2, GB // 10)])
    services.db.set_state("rf_month_rx", str(GB // 2))
    services.db.set_state("rf_month_tx", str(GB // 10))
    _online(services, PHONE, IPHONE)
    return who


def _quiet(services):
    """Только что установленный бот: профиль админа, больше никого."""
    return _base(services)


def _fake_config(services) -> None:
    """Ссылка и файл устройства — заглушки: ключи в тексте выдачи зависели бы
    от порядка снимков (счётчик ключей фейкового awg общий на тест)."""
    services.generate_config = lambda dev_id, **k: {
        "vpn": "vpn://RFVNTVktQ09ORklH", "conf": "[Interface]\nPrivateKey = DUMMY\n"}


def _people_gen(services):
    who = _people(services)
    _fake_config(services)
    return who


def _admin_one(services):
    """У админа одно устройство: выдача с главной — сразу, без выбора."""
    who = _base(services)
    services.add_device(ADM, "iPhone")
    _fake_config(services)
    return who


UNMANAGED = 7                           # «Роутер» админа без ключа (после _people)


def _unmanaged(services):
    """Пир у админа, созданный мимо бота: ключа нет — выдавать нечего."""
    who = _people(services)
    assert services.db.create_device(ADM, "Роутер", "DUMMYPUB2=", "DUMMYPSK=", "10.8.1.71") == UNMANAGED
    return who


def _dev_blocked(services):
    from awgbot.core.blocks import DeviceBlock
    who = _people(services)
    services.block_device_manual(PHONE, DeviceBlock.ADMIN_NOTIFIED, False)
    return who


def _dev_blocked_twice(services):
    """Две ручные причины: админ-блок и блок владельца — снимать по выбору."""
    from awgbot.core.blocks import DeviceBlock
    who = _people(services)
    services.block_device_manual(PHONE, DeviceBlock.ADMIN_NOTIFIED, False)
    services.block_device_manual(PHONE, DeviceBlock.USER, False)
    return who


def _admin_many(services):
    """Двенадцать устройств у админа — список листается."""
    names = ("iPhone", "iPad", "MacBook", "Mac mini", "Apple TV", "Android", "Kindle", "Pixel",
             "ThinkPad", "Steam Deck", "Xbox", "Роутер")
    return _people(services, admin_devices=names)


def _ks_paused(services):
    who = _people(services)
    ok, *_ = services.enter_pause(KS, 7)
    assert ok
    return who


def _ks_blocked(services):
    from awgbot.core.blocks import ClientBlock
    who = _people(services)
    services.block_client_manual(KS, ClientBlock.ADMIN_SILENT, False)
    return who


def _ks_blocked_twice(services):
    from awgbot.core.blocks import ClientBlock
    who = _people(services)
    services.block_client_manual(KS, ClientBlock.ADMIN_SILENT, False)
    services.block_client_manual(KS, ClientBlock.USER, False)
    return who


def _br_expired(services):
    """Подписка Бори кончилась вчера."""
    who = _people(services)
    services.db.update_client_fields(BR, period_start=_iso(NOW - _dt.timedelta(days=31)),
                                     period_end=_iso(NOW - _dt.timedelta(days=1)))
    return who


def _br_grace(services):
    """Боря брал отсрочку: её дни вычтутся из следующего периода."""
    who = _people(services)
    services.db.update_client_fields(BR, grace_pending_cut=3 * 86400)
    return who


def _ks_many(services):
    """У Ксюши девять устройств — в карточку не влезают."""
    who = _people(services)
    services.db.update_client_fields(KS, device_limit=10)
    for n in ("Планшет", "Часы", "ТВ", "Приставка", "Рабочий", "Ноут 2", "Колонка"):
        services.add_device(KS, n)
    return who


def _many_clients(services):
    """Двенадцать профилей — список листается."""
    who = _people(services)
    for i, n in enumerate(("Аня", "Вера", "Гоша", "Даша", "Егор", "Женя", "Зоя", "Иван", "Катя"), 1):
        _client(services, n, 2100 + i)
    return who


def _ks_sites(services):
    """У Ксюши три своих сайта в РФ-доступе."""
    who = _people(services)
    services.routing_add_domains(KS, "kinopoisk.ru ozon.ru gosuslugi.ru")
    return who


def _tag(text: str) -> str:
    from awgbot.bot.keyboards import entry_tag
    return entry_tag(text)


def _no_others(services):
    """Только админ с устройствами — переносить некуда."""
    who = _base(services)
    services.add_device(ADM, "iPhone")
    services.add_device(ADM, "MacBook")
    return who


# ── построители настроек: хост — фиксированными ответами ───────────────────

def _fw_state(**over) -> dict:
    """Ответ firewall_screen без похода в sshd и nft: SSH на 22, фильтр
    выключен, адресов нет, конфигом sshd никто чужой не владеет."""
    st = {"enabled": False, "present": False, "rollback": False, "ufw": False, "firewalld": False,
          "listening": 22, "drift": False, "owner": "", "owner_where": "", "owner_port": None,
          "owner_detail": "", "owner_files": [], "sshd_down": False, "ssh_port": 22,
          "allow": [], "raw_allow": [], "unresolved": [], "admin_ips": ["10.8.1.2"], "nat": False}
    st.update(over)
    return st


class _FwHost:
    """SSH-доступ хоста для снимков: firewall_screen отдаёт это состояние,
    кнопки раздела меняют его, как поменяли бы sshd и nft."""

    def __init__(self, **over):
        self.st = _fw_state(**over)

    def screen(self) -> dict:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in self.st.items()}

    def add(self, raw: str) -> list[str]:
        for tok in str(raw).replace(",", " ").split():
            if tok not in self.st["raw_allow"]:
                self.st["raw_allow"].append(tok)
                self.st["allow"].append(tok)
        return list(self.st["raw_allow"])

    def remove(self, entry: str) -> None:
        self.st["raw_allow"].remove(entry)
        self.st["allow"].remove(entry)

    def turn(self, on: bool) -> None:
        self.st["enabled"] = self.st["present"] = on

    def port(self, port: int) -> int:
        old, self.st["ssh_port"] = self.st["ssh_port"], int(port)
        self.st["listening"] = int(port)
        return old

    def install(self, services) -> None:
        from awgbot.core import settings
        settings.set_value("app.firewall.ssh_allow", list(self.st["raw_allow"]))
        services.firewall_screen = self.screen
        services.ssh_port_busy = lambda port: "nginx" if port == 8080 else ""
        services.ssh_port_change = self.port
        services.firewall_allow_add = self.add
        services.firewall_allow_remove = self.remove
        services.firewall_enable = lambda: self.turn(True)
        services.firewall_disable = lambda: self.turn(False)


def _fw(**over):
    def build(services):
        who = _people(services)
        _FwHost(**{k: (list(v) if isinstance(v, list) else v) for k, v in over.items()}).install(services)
        return who
    return build


_FW_ALLOW = {"allow": ["203.0.113.5", "198.51.100.0/24"], "raw_allow": ["203.0.113.5", "198.51.100.0/24"]}


def _srv(host: str = "vpn.example.org", dns: str = "10.8.1.1", **over):
    """«🖥 Сервер AWG» из настроек снимка; ядро AWG и поколение — константы
    (на хосте их читает modinfo), over — поверх (расхождение порта, переезд)."""
    def build(services):
        from awgbot.core import settings
        who = _people(services)
        d1, _, d2 = dns.partition(",")
        settings.set_value("app.network.server_host", host)
        settings.set_value("app.client_config.server_name", "AWG-SRV")
        settings.set_value("app.client_config.dns1", d1.strip())
        settings.set_value("app.client_config.dns2", (d2 or d1).strip())
        real = services.server_screen
        services.server_screen = lambda: {**real(), "kernel": "3.1.20260812", "generation": 1, **over}
        return who
    return build


def _email_on(services):
    """Ящик подключён (креды в БД, серверы в настройках); вход и отправка
    письма — без сети."""
    from awgbot.core import settings
    who = _people(services)
    services.db.set_state("email_login", "admin@example.org")
    services.db.set_state("email_password", "DUMMY")
    services.db.set_state("email_check", f"ok|{_iso(NOW - _dt.timedelta(minutes=10))}")
    for k, v in (("email.imap_host", "imap.example.org"), ("email.imap_port", 993),
                 ("email.smtp_host", "smtp.example.org"), ("email.smtp_port", 465),
                 ("email.resume_address", "")):
        settings.set_value(k, v)
    services.email_check = lambda: (True, "")
    services.email_send_test = lambda: None
    return who


def _restarts(services):
    """Перезапуски AWG и бота — без systemctl."""
    who = _people(services)
    services.restart_service = lambda: None
    services.restart_bot = lambda: None
    return who


def _upd_found(services):
    """Проверка обновлений нашла следующую версию."""
    import types
    who = _people(services)
    found = types.SimpleNamespace(tag="v1.3.0", body="- исправлено одно\n- добавлено другое",
                                  name="v1.3.0", url="https://example.org/r/v1.3.0")
    services.update_scan = lambda: found
    services.update_block_reason = lambda f: ""
    services.update_next = lambda: found
    services.update_available_tag = lambda: "v1.3.0"
    services.apply_update = lambda nxt: None
    return who


def _bc_ready(services):
    """Для рассылки: кулдаун прошлых отправок того же процесса не мешает."""
    from awgbot.bot.handlers.admin import broadcast as bc
    bc._last_broadcast_at.clear()
    return _people(services)


def _backup_files(services):
    """«Сделать сейчас» в чат: файл копии — во временном каталоге."""
    import tempfile
    import os
    who = _people(services)
    d = tempfile.mkdtemp(prefix="awg-shot-")
    path = os.path.join(d, "awg-bot-backup.tgz.enc")
    with open(path, "wb") as f:
        f.write(b"DUMMY")
    services.make_backup = lambda: [path]
    return who


# ── построители РФ-доступа и шлюзов ─────────────────────────────────────────

NASPI, PI4 = 7, 8                       # устройства админа под слоты (после _people)
PRIV = "RERERERERERERERERERERERERERERERERERERERERERE="   # ключ линка — заглушка
TOKEN = "1234567890:DUMMYDUMMYDUMMYDUMMYDUMMY"


def _routing(services, mp, *, enabled: bool = True, awake: bool = True) -> None:
    """Обвязка РФ-доступа развёрнута (awake — интерфейс линка прочитан при
    старте) и функция включена или выключена; сеть линка — заглушки."""
    from awgbot.core import settings
    from awgbot.infra import routing as rt
    mp.setattr(config, "ROUTING_ENABLED", awake)
    services.routing_provisioned = lambda: True
    # выключатель — настоящая настройка снимка (фейк маршрутизации держит его
    # константой): кнопки «Включить» и «Выключить» меняют экран, как в бою
    settings.set_value("app.routing.enabled", enabled)
    orig = settings.get_bool
    mp.setattr(settings, "get_bool", lambda k, d=None: (
        str(settings.get(k, d)).lower() in ("1", "true", "yes") if k == "app.routing.enabled" else orig(k, d)))
    mp.setattr(rt.probes, "ping_peer", lambda iface="", **k: 43 if iface == "awglink2" else 61)
    mp.setattr(rt.probes, "link_peer_endpoint",
           lambda iface="": "198.51.100.7" if iface == "awglink2" else "203.0.113.10")
    mp.setattr(rt.marking, "switch_active", lambda iface: None)
    services.routing_status = lambda: (True, "ок")
    services.routing_link_ok = lambda: True
    services._link_privkey = lambda gw=None: PRIV
    services._run_link_script = lambda mode, env=None: None
    services.gw_bundle_encrypted = lambda slot=None: (b"DUMMY-ENC", "awg-gw-bundle.enc")
    services.gw_bundle_plain = lambda slot=None: (b"DUMMY-PLAIN", "awg-gw-bundle.sh")
    services.routing_update_lists = lambda force=False: 12345
    services.routing_provision = lambda: "dnsmasq: ok\nlink: awglink up"
    tokens: dict[int, str] = {}
    services.gw_bot_token = lambda slot=None: tokens.get(int(slot or 1), "")
    services.set_gw_bot_token = lambda t, slot=None: tokens.__setitem__(int(slot or 1), t)
    services.forget_gw_bot_token = lambda slot=None: tokens.pop(int(slot or 1), None)
    services._shot_tokens = tokens


def _slots(n: int = 1, *, standby: str = "alive", lan: bool = False, subnets: bool = False,
           token: bool = True, enabled: bool = True, failover: bool = True):
    """Шлюзы: n слотов на NASPi и Pi4 (активен первый); standby — резерв жив
    (alive) или лежит (down); subnets — у первого локальная подсеть, lan —
    ещё и VPN-транзит;
    token — бот шлюза известен (ссылка в карточке); failover —
    автопереключение на резерв."""
    def build(services, mp):
        from awgbot.core import settings
        who = _people(services)
        settings.set_value("app.routing.failover.enabled", failover)
        services.add_device(ADM, "NASPi")
        services.add_device(ADM, "Pi4")
        _routing(services, mp, enabled=enabled)
        if n >= 1:
            services.db.gateway_add(NASPI, "awglink", 443, "10.99.99.0/30", slot_id=1)
            if lan or subnets:
                services.db.gateway_update(1, home_subnets=["192.168.1.0/24"], lan_mode=int(lan))
        if n >= 2:
            services.db.gateway_add(PI4, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
            if standby == "alive":
                services.db.set_state("routing_gw_2_up_streak", str(services._RT_UP_STREAK))
            else:
                services._probe_slot = lambda g, active=False: "ok" if g.id == 1 else "down"
                services.db.set_state("routing_gw_2_down_since", str(int(NOW.timestamp()) - 600))
        if token:
            for slot in range(1, n + 1):
                services.set_gw_bot_token(TOKEN, slot)
                services.set_gw_bot_identity(slot, "naspi_gw_bot" if slot == 1 else "pi4_gw_bot",
                                             "NASPi" if slot == 1 else "Pi4")
        return who
    return build


def _rt_off(services, mp):
    """РФ-доступ развёрнут, но выключен."""
    who = _people(services)
    _routing(services, mp, enabled=False)
    return who


def _rt_asleep(services, mp):
    """Обвязка развёрнута, бот ещё не перезапущен — функция спит."""
    who = _people(services)
    _routing(services, mp, awake=False)
    return who


def _rt_none(services, mp):
    """Включено, шлюз ещё не назначен; у админа есть устройства-кандидаты."""
    who = _people(services)
    _routing(services, mp)
    return who


def _rt_none_bare(services, mp):
    """Включено, кандидатов нет: у админа ни одного устройства."""
    who = _base(services)
    _routing(services, mp)
    return who


def _rt_unavailable(services):
    """РФ-доступ админу не виден: функция недоступна."""
    who = _people(services)
    services.routing_available = lambda: False
    return who


def _claim(services, mp):
    """Пересланное сообщение агента: подпись проверена, устройство назначено."""
    who = _slots(1)(services, mp)
    dev = services.db.get_device(NASPI)
    gw = services.db.gateways()[0]
    services.gateway_claim = lambda text: {"status": "marked", "device": dev, "gateway": gw}
    return who


def _claim_already(services, mp):
    who = _slots(1)(services, mp)
    dev = services.db.get_device(NASPI)
    gw = services.db.gateways()[0]
    services.gateway_claim = lambda text: {"status": "already", "device": dev, "gateway": gw}
    return who


def _claim_bad(services, mp):
    who = _slots(1)(services, mp)

    def bad(text):
        raise ValueError("подпись не сходится — сообщение не от этого шлюза или ключ линка другой")
    services.gateway_claim = bad
    return who


CLAIM = "✅ Шлюз настроен. Перешли это сообщение основному боту:\nGW1:RFVNTVk.RFVNTVk"


# ── переезд профилей: второй интерфейс настроен ─────────────────────────────

def _mig_ready(services, mp):
    """Переезд настроен (awg1, 10.9.1), но не начат; параметры старого и
    нового интерфейса и поколение ядра — константы, а не чтение с хоста."""
    from awgbot.infra import awglock
    who = _people(services)
    mp.setattr(config, "AWG_INTERFACE", "awg0")
    mp.setattr(config, "MIGRATION_INTERFACE", "awg1")
    mp.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")
    mp.setattr(config, "SERVER_PORT", 43125)
    mp.setattr(awglock, "applied_generation", lambda: 1)
    mp.setattr(awglock, "target_generation", lambda: 1)
    return who


def _mig_running(services, mp):
    """Переезд идёт: двойники рождены, Ксюшин телефон уже переехал."""
    who = _mig_ready(services, mp)
    services.migration_start()
    twins = services.db.twins_by_origin()
    _online(services, twins[PHONE])
    return who


def _mig_finishing(services, mp):
    """Переезд идёт, и его можно завершить: смена основного интерфейса на
    хосте (гашение старого, файл поколения) — заглушками."""
    from awgbot.infra import awglock
    who = _mig_running(services, mp)
    services._retire_interface = lambda *a, **k: None
    mp.setattr(awglock, "write_state", lambda **k: None)
    mp.setattr(awglock, "needs_migration", lambda: False)
    return who


def _mig_cancelled(services, mp):
    """Переезд отменён, а один двойник уже успел переехать."""
    who = _mig_running(services, mp)
    services.migration_cancel()
    return who


SHOTS = [
    # ── 1.1 главная и ссылки с неё ──────────────────────────────────────────
    Shot("adm.main.quiet", role="admin", start="", data=_quiet, title="главная, никого нет"),
    Shot("adm.main", role="admin", start="", data=_people, title="главная с профилями, трафиком и онлайн"),
    Shot("adm.main.refresh", role="admin", press=[Menu(action="refresh")], data=_people,
         title="«🔄 Обновить»"),
    Shot("adm.online", role="admin", press=[Menu(action="online")], data=_people, title="онлайн"),
    Shot("adm.online.empty", role="admin", press=[Menu(action="online")], data=_quiet, title="онлайн, никого"),
    Shot("adm.expiring", role="admin", press=[Menu(action="expiring")], data=_people, title="истекают"),
    Shot("adm.expiring.empty", role="admin", press=[Menu(action="expiring")], data=_quiet,
         title="истекают, никого"),
    Shot("adm.traffic", role="admin", press=[Menu(action="traffic")], data=_people, title="трафик по профилям"),
    Shot("adm.traffic.empty", role="admin", press=[Menu(action="traffic")], data=_quiet, title="трафик, пусто"),
    Shot("adm.unassigned", role="admin", press=[Menu(action="unassigned")], data=_people,
         title="без профиля"),
    Shot("adm.unassigned.empty", role="admin", press=[Menu(action="unassigned")], data=_quiet,
         title="без профиля, пусто"),
]

_DEVICES = [
    # ── 1.2 устройства: свои, чужие, без профиля ────────────────────────────
    Shot("adm.devices", role="admin", press=[Menu(action="devices")], data=_people, title="мои устройства"),
    Shot("adm.devices.empty", role="admin", press=[Menu(action="devices")], data=_quiet,
         title="мои устройства, пусто"),
    Shot("adm.devices.page2", role="admin",
         press=[Menu(action="devices"), PageCB(screen="devices", page=1, back=Menu(action="devices").pack())],
         data=_admin_many, title="листание: вторая страница"),
    Shot("adm.gen.pick", role="admin", press=[Menu(action="gen_link")], data=_people_gen,
         title="ссылка с главной: выбор устройства"),
    Shot("adm.gen.link", role="admin", press=[Menu(action="gen_link")], data=_admin_one,
         title="одно устройство: ссылка сразу"),
    Shot("adm.gen.qr", role="admin", press=[DeviceCB(action="gen_qr", device_id=IPHONE)], data=_people_gen,
         title="QR устройства"),
    Shot("adm.gen.file", role="admin", press=[DeviceCB(action="gen_file", device_id=PHONE)],
         data=_people_gen, title="файл устройства профиля"),
    Shot("adm.gen.unmanaged", role="admin", press=[DeviceCB(action="gen_link", device_id=UNMANAGED)],
         data=_unmanaged, title="выдача пиру без ключа"),
    Shot("adm.self.add", role="admin", press=[AdminSelfCB(action="add")], data=_people,
         title="своё устройство: имя"),
    Shot("adm.self.add.done", role="admin", press=[AdminSelfCB(action="add")], text="iPad", data=_people,
         title="своё устройство создано"),
    Shot("adm.dev.own", role="admin", press=[DeviceCB(action="open", device_id=IPHONE)], data=_people,
         title="карточка своего устройства"),
    Shot("adm.dev.client", role="admin", press=[DeviceCB(action="open", device_id=PHONE)], data=_people,
         title="карточка устройства профиля (РФ, онлайн)"),
    Shot("adm.dev.blocked", role="admin", press=[DeviceCB(action="open", device_id=PHONE)], data=_dev_blocked,
         title="карточка заблокированного"),
    Shot("adm.dev.alien", role="admin", press=[DeviceCB(action="open", device_id=ALIEN)], data=_people,
         title="карточка пира без профиля"),
    Shot("adm.dev.name", role="admin", press=[DeviceCB(action="edit_name", device_id=PHONE)], data=_people,
         title="имя устройства: приглашение"),
    Shot("adm.dev.name.done", role="admin", press=[DeviceCB(action="edit_name", device_id=PHONE)],
         text="Телефон Ксюши", data=_people, title="имя устройства: итог"),
    Shot("adm.dev.limit", role="admin", press=[DeviceCB(action="edit_traffic", device_id=PHONE)], data=_people,
         title="лимит устройства: пресеты"),
    Shot("adm.dev.limit.preset", role="admin",
         press=[DeviceCB(action="edit_traffic", device_id=PHONE), PresetCB(kind="devlimit", ref=PHONE, val=50)],
         data=_people, title="лимит пресетом"),
    Shot("adm.dev.limit.other", role="admin",
         press=[DeviceCB(action="edit_traffic", device_id=PHONE), PresetCB(kind="devlimit", ref=PHONE, val=-1)],
         data=_people, title="лимит: «✏️ Другое»"),
    Shot("adm.dev.limit.other.done", role="admin",
         press=[DeviceCB(action="edit_traffic", device_id=PHONE), PresetCB(kind="devlimit", ref=PHONE, val=-1)],
         text="30", data=_people, title="лимит своим числом"),
    Shot("adm.dev.reassign", role="admin", press=[DeviceCB(action="reassign", device_id=ALIEN)], data=_people,
         title="перенос: в какой профиль"),
    Shot("adm.dev.reassign.go", role="admin",
         press=[ReassignCB(device_id=ALIEN, client_id=KS, stage="go")], data=_people,
         title="перенос в профиль со свободным местом"),
    Shot("adm.dev.reassign.slot_ask", role="admin",
         press=[ReassignCB(device_id=ALIEN, client_id=BR, stage="go")], data=_people,
         title="перенос: лимит исчерпан — добавить слот?"),
    Shot("adm.dev.reassign.slot_yes", role="admin",
         press=[ReassignCB(device_id=ALIEN, client_id=BR, stage="slot_yes")], data=_people,
         title="перенос со слотом"),
    Shot("adm.dev.block", role="admin", press=[BlockCB(target="dev", action="menu_block", ref=PHONE)],
         data=_people, title="блок устройства: как"),
    Shot("adm.dev.block.do", role="admin",
         press=[BlockCB(target="dev", action="block", ref=PHONE, kind="notified")], data=_people,
         title="блок с уведомлением"),
    Shot("adm.dev.unblock", role="admin", press=[BlockCB(target="dev", action="menu_unblock", ref=PHONE)],
         data=_dev_blocked, title="разблок одной причины — сразу"),
    Shot("adm.dev.unblock.reasons", role="admin",
         press=[BlockCB(target="dev", action="menu_unblock", ref=PHONE)], data=_dev_blocked_twice,
         title="разблок: какую причину"),
    Shot("adm.dev.del", role="admin", press=[DelDeviceCB(device_id=LAPTOP)], data=_people,
         title="удалить устройство?"),
    Shot("adm.dev.del.only", role="admin", press=[DelDeviceCB(device_id=TABLET)], data=_people,
         title="удалить единственное устройство профиля?"),
    Shot("adm.dev.del.confirm", role="admin", press=[DelDeviceCB(device_id=LAPTOP, stage="confirm")],
         data=_people, title="удалено — карточка профиля"),
]

_RF = [
    # ── 1.3 РФ-доступ: свой и профиля ───────────────────────────────────────
    Shot("adm.rf.own", role="admin", press=[RoutingCB(action="panel", ref=ADM)], data=_people,
         title="свой РФ-доступ"),
    Shot("adm.rf.own.empty", role="admin", press=[RoutingCB(action="panel", ref=ADM)], data=_quiet,
         title="свой РФ-доступ без устройств"),
    Shot("adm.rf.client", role="admin", press=[RoutingCB(action="panel", ref=KS)], data=_people,
         title="РФ-доступ профиля из карточки"),
    Shot("adm.rf.denied", role="admin", press=[RoutingCB(action="panel", ref=BR)], data=_people,
         title="профилю РФ-доступ не выдан"),
    Shot("adm.rf.dev", role="admin", press=[RoutingCB(action="dev", ref=PHONE)], data=_people,
         title="переключить устройство"),
    Shot("adm.rf.sites.empty", role="admin", press=[RoutingCB(action="sites", ref=KS)], data=_people,
         title="сайты, пусто"),
    Shot("adm.rf.add", role="admin", press=[RoutingCB(action="add", ref=KS, tag="panel")], data=_people,
         title="добавить сайты: приглашение"),
    Shot("adm.rf.add.done", role="admin", press=[RoutingCB(action="add", ref=KS, tag="panel")],
         text="kinopoisk.ru\nhttps://www.ozon.ru/catalog\nне домен", data=_people,
         title="добавить сайты: отчёт и раздел"),
    Shot("adm.rf.sites", role="admin", press=[RoutingCB(action="sites", ref=KS)], data=_ks_sites,
         title="сайты списком"),
    Shot("adm.rf.del", role="admin", press=[RoutingCB(action="del", ref=KS, idx=1, tag=_tag("kinopoisk.ru"))],
         data=_ks_sites, title="убрать сайт"),
    Shot("adm.rf.clear", role="admin", press=[RoutingCB(action="clear", ref=KS)], data=_ks_sites,
         title="очистить список?"),
    Shot("adm.rf.clear.yes", role="admin", press=[RoutingCB(action="clear_yes", ref=KS)], data=_ks_sites,
         title="список очищен"),
]

_PROFILES = [
    # ── 1.4 профили ─────────────────────────────────────────────────────────
    Shot("adm.clients", role="admin", press=[Menu(action="clients")], data=_people, title="профили"),
    Shot("adm.clients.empty", role="admin", press=[Menu(action="clients")], data=_quiet, title="профилей нет"),
    Shot("adm.clients.states", role="admin", press=[Menu(action="clients")], data=_ks_blocked,
         title="значки: блок, ожидание, онлайн"),
    Shot("adm.clients.page2", role="admin",
         press=[Menu(action="clients"), PageCB(screen="clients", page=1, back=Menu(action="clients").pack())],
         data=_many_clients, title="листание профилей"),
    Shot("adm.cl", role="admin", press=[ClientCB(action="open", client_id=KS)], data=_people,
         title="карточка: активен, РФ, устройства"),
    Shot("adm.cl.pending", role="admin", press=[ClientCB(action="open", client_id=PT)], data=_people,
         title="карточка: приглашение не принято"),
    Shot("adm.cl.expiring", role="admin", press=[ClientCB(action="open", client_id=BR)], data=_people,
         title="карточка: истекает, лимит исчерпан"),
    Shot("adm.cl.expired", role="admin", press=[ClientCB(action="open", client_id=BR)], data=_br_expired,
         title="карточка: подписка истекла"),
    Shot("adm.cl.paused", role="admin", press=[ClientCB(action="open", client_id=KS)], data=_ks_paused,
         title="карточка: на паузе"),
    Shot("adm.cl.blocked", role="admin", press=[ClientCB(action="open", client_id=KS)], data=_ks_blocked,
         title="карточка: тихий админ-блок"),
    Shot("adm.cl.many", role="admin", press=[ClientCB(action="open", client_id=KS)], data=_ks_many,
         title="карточка: устройства не влезли"),
    Shot("adm.cl.devices", role="admin", press=[ClientCB(action="devices", client_id=KS)], data=_ks_many,
         title="устройства профиля"),
    Shot("adm.cl.missing", role="admin", press=[ClientCB(action="open", client_id=99)], data=_people,
         title="профиля нет"),
    Shot("adm.cl.edit", role="admin", press=[ClientCB(action="edit", client_id=KS)], data=_people,
         title="«✏️ Изменить»"),
    Shot("adm.cl.name", role="admin", press=[ClientCB(action="edit_name", client_id=KS)], data=_people,
         title="имя профиля: приглашение"),
    Shot("adm.cl.name.done", role="admin", press=[ClientCB(action="edit_name", client_id=KS)], text="Ксения",
         data=_people, title="имя профиля: итог"),
    Shot("adm.cl.limit", role="admin", press=[ClientCB(action="edit_limit", client_id=KS)], data=_people,
         title="лимит устройств: пресеты"),
    Shot("adm.cl.limit.preset", role="admin", press=[PresetCB(kind="cli_devs", ref=KS, val=5)], data=_people,
         title="лимит устройств пресетом"),
    Shot("adm.cl.limit.other", role="admin", press=[PresetCB(kind="cli_devs", ref=KS, val=-1)], data=_people,
         title="лимит устройств: «✏️ Другое»"),
    Shot("adm.cl.limit.other.done", role="admin", press=[PresetCB(kind="cli_devs", ref=KS, val=-1)], text="7",
         data=_people, title="лимит устройств своим числом"),
    Shot("adm.cl.traffic", role="admin", press=[ClientCB(action="edit_traffic", client_id=KS)], data=_people,
         title="лимит трафика: пресеты"),
    Shot("adm.cl.traffic.preset", role="admin", press=[PresetCB(kind="cli_traffic", ref=KS, val=100)],
         data=_people, title="лимит трафика пресетом"),
    Shot("adm.cl.period", role="admin", press=[ClientCB(action="edit_period", client_id=KS)], data=_people,
         title="период: дата начала"),
    Shot("adm.cl.period.start", role="admin", press=[ClientCB(action="edit_period", client_id=KS)], text="-",
         data=_people, title="период: дата окончания"),
    Shot("adm.cl.extend", role="admin", press=[ClientCB(action="extend", client_id=KS)], data=_people,
         title="продление с остатком"),
    Shot("adm.cl.extend.keep_off", role="admin",
         press=[ClientCB(action="extend", client_id=KS), PeriodCB(kind="keep_tgl", ctx="extend", ref=KS, keep=0)],
         data=_people, title="продление: остаток не сохранять"),
    Shot("adm.cl.extend.expired", role="admin", press=[ClientCB(action="extend", client_id=BR)],
         data=_br_expired, title="продление истёкшей — остатка нет"),
    Shot("adm.cl.extend.grace", role="admin", press=[ClientCB(action="extend", client_id=BR)], data=_br_grace,
         title="продление после отсрочки"),
    Shot("adm.cl.extend.done", role="admin", press=[PeriodCB(kind="month", ctx="extend", ref=KS)],
         data=_people, title="продлено — след и карточка"),
    Shot("adm.cl.extend_exp", role="admin", press=[ClientCB(action="extend_exp", client_id=BR)], data=_people,
         title="продление из «Истекают»"),
    Shot("adm.cl.extend_exp.done", role="admin",
         press=[ClientCB(action="extend_exp", client_id=BR), PeriodCB(kind="month", ctx="extend", ref=BR)],
         data=_people, title="продлено из «Истекают» — список опустел, карточка"),
    Shot("adm.cl.resume", role="admin", press=[ClientCB(action="resume_pause", client_id=KS)],
         data=_ks_paused, title="снять паузу"),
    Shot("adm.cl.regen", role="admin", press=[ClientCB(action="regen_invite", client_id=PT)], data=_people,
         title="новое приглашение"),
    Shot("adm.cl.delete", role="admin", press=[ClientCB(action="delete", client_id=KS)], data=_people,
         title="удалить профиль?"),
    Shot("adm.cl.delete.yes", role="admin", press=[ClientCB(action="delete_yes", client_id=KS)], data=_people,
         title="профиль удалён — список"),
    Shot("adm.cl.block", role="admin", press=[BlockCB(target="cli", action="menu_block", ref=KS)], data=_people,
         title="блок профиля: пауза подписки?"),
    Shot("adm.cl.block.pause_yes", role="admin", press=[BlockCB(target="cli", action="pause_yes", ref=KS)],
         data=_people, title="блок профиля: уведомить? (с паузой)"),
    Shot("adm.cl.block.do", role="admin",
         press=[BlockCB(target="cli", action="block", ref=KS, kind="notified", days=0)], data=_people,
         title="профиль заблокирован с паузой"),
    Shot("adm.cl.unblock.reasons", role="admin", press=[BlockCB(target="cli", action="menu_unblock", ref=KS)],
         data=_ks_blocked_twice, title="разблок профиля: какую причину"),
    Shot("adm.cl.add_device", role="admin", press=[ClientCB(action="add_device", client_id=KS)], data=_people,
         title="устройство профилю: имя"),
    Shot("adm.cl.add_device.done", role="admin", press=[ClientCB(action="add_device", client_id=KS)],
         text="Планшет", data=_people, title="устройство профилю: итог в карточке"),
    Shot("adm.cl.add_device.full", role="admin", press=[ClientCB(action="add_device", client_id=BR)],
         data=_people, title="устройство профилю: лимит исчерпан"),
    Shot("adm.cl.add_device.slot", role="admin", press=[ClientCB(action="add_device_slot", client_id=BR)],
         data=_people, title="«➕ Слот и добавить»"),
]

_NEW = [
    # ── 1.5 новый профиль и объявление ──────────────────────────────────────
    Shot("adm.new", role="admin", press=[Menu(action="add_client")], data=_people, title="новый профиль: имя"),
    Shot("adm.new.devs", role="admin", press=[Menu(action="add_client")], text="Маша", data=_people,
         title="новый профиль: устройств"),
    Shot("adm.new.cancel", role="admin", press=[Menu(action="add_client"), CancelCB(kind="main")],
         data=_people, title="новый профиль: «✖️ Отмена» — главная"),
    Shot("adm.bc", role="admin", press=[BroadcastCB(action="pick")], data=_bc_ready, title="объявление: кому"),
    Shot("adm.bc.empty", role="admin", press=[BroadcastCB(action="pick")], data=_quiet,
         title="объявление: профилей нет"),
    Shot("adm.bc.all", role="admin", press=[BroadcastCB(action="pick"), BroadcastCB(action="all")],
         data=_bc_ready, title="отмечены все"),
    Shot("adm.bc.ext", role="admin", press=[BroadcastCB(action="pick"), BroadcastCB(action="ext")],
         data=_bc_ready, title="с продлением подписки"),
    Shot("adm.bc.next", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="all"), BroadcastCB(action="next")],
         data=_bc_ready, title="текст объявления: приглашение"),
    Shot("adm.bc.next.ext", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="ext"), BroadcastCB(action="all"),
                BroadcastCB(action="next")], data=_bc_ready, title="с продлением: дни"),
    Shot("adm.bc.days", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="ext"), BroadcastCB(action="all"),
                BroadcastCB(action="next"), PresetCB(kind="bc_days", val=7)], data=_bc_ready,
         title="с продлением: текст"),
    Shot("adm.bc.days.other", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="ext"), BroadcastCB(action="all"),
                BroadcastCB(action="next"), PresetCB(kind="bc_days", val=-1)], data=_bc_ready,
         title="с продлением: свои дни"),
    Shot("adm.bc.preview", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="all"), BroadcastCB(action="next")],
         text="В субботу с 02:00 до 03:00 — <b>работы на сервере</b>", data=_bc_ready, title="превью"),
    Shot("adm.bc.preview.ext", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="ext"), BroadcastCB(action="all"),
                BroadcastCB(action="next"), PresetCB(kind="bc_days", val=7)],
         text="Неделя в подарок — спасибо, что с нами", data=_bc_ready, title="превью с продлением"),
    Shot("adm.bc.cancel", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="all"), BroadcastCB(action="cancel")],
         data=_bc_ready, title="отмена — главная"),
]

_SETTINGS = [
    # ── 1.6 настройки ───────────────────────────────────────────────────────
    Shot("adm.set.root", role="admin", press=[SetCB(sec="root")], data=_people, title="корень настроек"),
    Shot("adm.set.notify", role="admin", press=[SetCB(sec="notify")], data=_people, title="уведомления"),
    Shot("adm.set.notify.off", role="admin", press=[SetCB(sec="notify")], data=_people,
         conf={"quiet_hours.quiet_hours_enabled": False, "resource_alerts.enabled": False},
         title="уведомления: тихие часы и алерты выключены"),
    Shot("adm.set.notify.mail_off", role="admin",
         press=[SetCB(sec="notify", act="toggle", key="notifications.email_fallback")], data=_people,
         title="аварии на e-mail без ящика"),
    Shot("adm.set.notify.edit", role="admin",
         press=[SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start")], data=_people,
         title="начало тихих часов: приглашение"),
    Shot("adm.set.notify.edit.done", role="admin",
         press=[SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start")], text="22",
         data=_people, title="начало тихих часов: итог"),
    Shot("adm.set.notify.edit.cancel", role="admin",
         press=[SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start"),
                CancelCB(kind="set_notify")], data=_people, title="ввод: «✖️ Отмена» — раздел"),
    Shot("adm.set.ncl", role="admin", press=[SetCB(sec="ncl")], data=_people, title="события профилей"),
    Shot("adm.set.srv", role="admin", press=[SetCB(sec="srv")], data=_srv(), title="сервер AWG"),
    Shot("adm.set.srv.ip", role="admin", press=[SetCB(sec="srv")],
         data=_srv(host="203.0.113.10", dns="1.1.1.1, 8.8.8.8", kernel=""),
         title="сервер без домена, DNS публичный"),
    Shot("adm.set.srv.blocked", role="admin", press=[SetCB(sec="srv")],
         data=_srv(migration_blocked="идёт переезд профилей", port=45871, port_conf=43125),
         title="сервер: порт расходится, переезд недоступен"),
    Shot("adm.set.srv.host", role="admin",
         press=[SetCB(sec="srv", act="edit", key="app.network.server_host")], data=_srv(),
         title="домен: приглашение"),
    Shot("adm.set.srv.host.done", role="admin",
         press=[SetCB(sec="srv", act="edit", key="app.network.server_host")], text="vpn2.example.org",
         data=_srv(), title="домен: итог"),
    Shot("adm.set.srv.dns", role="admin",
         press=[SetCB(sec="srv", act="edit", key="app.client_config.dns1")], data=_srv(),
         title="DNS: приглашение"),
    Shot("adm.set.srv.dns.done", role="admin",
         press=[SetCB(sec="srv", act="edit", key="app.client_config.dns1")], text="1.1.1.1, 8.8.8.8",
         data=_srv(), title="DNS: итог"),
    Shot("adm.set.dns", role="admin", press=[SetCB(sec="dns")], data=_srv(dns="1.1.1.1, 8.8.8.8"),
         title="свой DNS-резолвер"),
    Shot("adm.set.dns.later", role="admin", press=[SetCB(sec="dns", act="do", key="later")], data=_people,
         title="резолвер — при переезде"),
    Shot("adm.set.dns.never", role="admin", press=[SetCB(sec="dns", act="do", key="never")], data=_people,
         title="резолвер не нужен"),
    Shot("adm.set.dns.now", role="admin", press=[SetCB(sec="dns", act="do", key="now")], data=_people,
         title="резолвер — переехать сейчас"),
    Shot("adm.set.mig_prep", role="admin", press=[SetCB(sec="mig_prep")], data=_people,
         title="смена порта или подсети"),
    Shot("adm.set.mig_prep.port", role="admin", press=[SetCB(sec="mig_prep", act="edit", key="port")],
         data=_people, title="свой порт: приглашение"),
    Shot("adm.set.mig_prep.port.done", role="admin", press=[SetCB(sec="mig_prep", act="edit", key="port")],
         text="51820", data=_people, title="свой порт: экран с портом"),
    Shot("adm.set.fw", role="admin", press=[SetCB(sec="fw")], data=_fw(), title="SSH-доступ: адресов нет"),
    Shot("adm.set.fw.allow", role="admin", press=[SetCB(sec="fw")], data=_fw(**_FW_ALLOW),
         title="SSH-доступ: адреса есть, фильтр выключен"),
    Shot("adm.set.fw.on", role="admin", press=[SetCB(sec="fw")],
         data=_fw(enabled=True, present=True, **_FW_ALLOW), title="SSH-доступ: фильтр включён"),
    Shot("adm.set.fw.add", role="admin", press=[SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")],
         data=_fw(), title="адрес: приглашение"),
    Shot("adm.set.fw.add.done", role="admin",
         press=[SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")], text="203.0.113.5",
         data=_fw(), title="адрес добавлен"),
    Shot("adm.set.fw.del", role="admin",
         press=[SetCB(sec="fw", act="do", key="del", val=f"0.{_tag('203.0.113.5')}")],
         data=_fw(**_FW_ALLOW), title="адрес убран"),
    Shot("adm.set.fw.on.do", role="admin", press=[SetCB(sec="fw", act="do", key="on")],
         data=_fw(**_FW_ALLOW), title="фильтр включён"),
    Shot("adm.set.fw.port", role="admin", press=[SetCB(sec="fw", act="edit", key="port")], data=_fw(),
         title="порт SSH: приглашение"),
    Shot("adm.set.fw.port.owner", role="admin", press=[SetCB(sec="fw", act="edit", key="port")],
         data=_fw(owner="cloud-init", owner_where="/etc/ssh/sshd_config.d/50-cloud-init.conf",
                  owner_detail="# generated by cloud-init, do not edit",
                  owner_files=["/etc/ssh/sshd_config.d/50-cloud-init.conf"]),
         title="порт SSH: конфигом владеет другая программа"),
    Shot("adm.set.fw.port.same", role="admin", press=[SetCB(sec="fw", act="edit", key="port")], text="22",
         data=_fw(), title="порт SSH: тот же"),
    Shot("adm.set.fw.port.busy", role="admin", press=[SetCB(sec="fw", act="edit", key="port")], text="8080",
         data=_fw(), title="порт SSH: занят"),
    Shot("adm.set.fw.port.done", role="admin", press=[SetCB(sec="fw", act="edit", key="port")], text="2222",
         data=_fw(), title="порт SSH: изменён"),
    Shot("adm.set.fw.port.retry", role="admin", press=[SetCB(sec="fw", act="do", key="port_retry")],
         data=_fw(), title="финишер: другой порт"),
    Shot("adm.set.email", role="admin", press=[SetCB(sec="email")], data=_people, title="e-mail: не подключён"),
    Shot("adm.set.email.on", role="admin", press=[SetCB(sec="email")], data=_email_on,
         title="e-mail: подключён"),
    Shot("adm.set.email.setup", role="admin", press=[SetCB(sec="email", act="do", key="setup")],
         data=_people, title="мастер почты: адрес"),
    Shot("adm.set.email.setup.known", role="admin", press=[SetCB(sec="email", act="do", key="setup")],
         text="admin@gmail.com", data=_people, title="мастер почты: известный провайдер — пароль"),
    Shot("adm.set.email.setup.unknown", role="admin", press=[SetCB(sec="email", act="do", key="setup")],
         text="admin@example.org", data=_people, title="мастер почты: свой сервер — IMAP"),
    Shot("adm.set.email.change", role="admin", press=[SetCB(sec="email", act="do", key="setup")],
         data=_email_on, title="сменить ящик: адрес"),
    Shot("adm.set.email.check", role="admin", press=[SetCB(sec="email", act="do", key="check")],
         data=_email_on, title="проверка соединения"),
    Shot("adm.set.email.test", role="admin", press=[SetCB(sec="email", act="do", key="test")],
         data=_email_on, title="тестовое письмо"),
    Shot("adm.set.email.forget", role="admin", press=[SetCB(sec="email", act="do", key="forget")],
         data=_email_on, title="отключить почту?"),
    Shot("adm.set.email.forget.yes", role="admin", press=[SetCB(sec="email", act="do", key="forget!")],
         data=_email_on, title="почта отключена"),
    Shot("adm.set.email.resume_addr", role="admin",
         press=[SetCB(sec="email", act="edit", key="email.resume_address")], data=_email_on,
         title="адрес для кода: приглашение"),
    Shot("adm.set.email.resume_addr.done", role="admin",
         press=[SetCB(sec="email", act="edit", key="email.resume_address")], text="codes@example.org",
         data=_email_on, title="адрес для кода: итог"),
    Shot("adm.set.subs", role="admin", press=[SetCB(sec="subs")], data=_people, title="подписки"),
    Shot("adm.set.subs.edit", role="admin", press=[SetCB(sec="subs", act="edit", key="grace.grace_days")],
         data=_people, title="отсрочка: приглашение"),
    Shot("adm.set.subs.edit.done", role="admin",
         press=[SetCB(sec="subs", act="edit", key="grace.grace_days")], text="7", data=_people,
         title="отсрочка: итог"),
    Shot("adm.set.mon", role="admin", press=[SetCB(sec="mon")], data=_people, title="мониторинг"),
    Shot("adm.set.mon.edit.done", role="admin",
         press=[SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes")], text="5", data=_people,
         title="частота опроса: итог"),
    Shot("adm.set.backup", role="admin", press=[SetCB(sec="backup")], data=_people, title="бэкапы"),
    Shot("adm.set.backup.off", role="admin", press=[SetCB(sec="backup")], data=_people,
         conf={"app.scheduler.backup_enabled": False}, title="бэкапы выключены"),
    Shot("adm.set.backup.email", role="admin", press=[SetCB(sec="backup")], data=_people,
         conf={"app.scheduler.backup_channel": "email"}, title="бэкапы на e-mail"),
    Shot("adm.set.backup.channel", role="admin",
         press=[SetCB(sec="backup", act="cycle", key="app.scheduler.backup_channel")], data=_people,
         title="канал на e-mail без ящика"),
    Shot("adm.set.backup.when", role="admin", press=[SetCB(sec="backup", act="edit", key="backup_when")],
         data=_people, title="день и час: приглашение"),
    Shot("adm.set.backup.when.done", role="admin",
         press=[SetCB(sec="backup", act="edit", key="backup_when")], text="5 3", data=_people,
         title="день и час: итог"),
    Shot("adm.set.backup.now", role="admin", press=[SetCB(sec="backup", act="do", key="now")],
         data=_backup_files, title="копия сейчас — файлом в чат"),
    Shot("adm.set.backup.enc", role="admin", press=[SetCB(sec="backup", act="do", key="enc")], data=_people,
         title="шифрование"),
    Shot("adm.set.backup.enc_set", role="admin", press=[SetCB(sec="backup", act="do", key="enc_set")],
         data=_people, title="фраза: первый ввод"),
    Shot("adm.set.backup.enc_set.first", role="admin", press=[SetCB(sec="backup", act="do", key="enc_set")],
         text="DUMMY-passphrase-1234", data=_people, title="фраза: повтор"),
    Shot("adm.set.svc", role="admin", press=[SetCB(sec="svc")], data=_people, title="сервис"),
    Shot("adm.set.svc.awg", role="admin", press=[SetCB(sec="svc", act="do", key="awg")], data=_restarts,
         title="перезапустить AWG?"),
    Shot("adm.set.svc.awg.yes", role="admin", press=[SetCB(sec="svc", act="do", key="awg!")],
         data=_restarts, title="AWG перезапущен"),
    Shot("adm.set.svc.bot", role="admin", press=[SetCB(sec="svc", act="do", key="bot")], data=_restarts,
         title="перезапустить бота?"),
    Shot("adm.set.svc.bot.yes", role="admin", press=[SetCB(sec="svc", act="do", key="bot!")],
         data=_restarts, title="бот перезапускается"),
    Shot("adm.set.upd", role="admin", press=[SetCB(sec="upd")], data=_people, title="обновления: актуальна"),
    Shot("adm.set.upd.found", role="admin", press=[SetCB(sec="upd")], data=_upd_found,
         title="обновления: доступна новая"),
    Shot("adm.set.upd.notify", role="admin", press=[SetCB(sec="upd", act="toggle", key="notify")],
         data=_upd_found, title="уведомления об обновлениях выключены"),
    Shot("adm.set.upd.cycle", role="admin", press=[SetCB(sec="upd", act="cycle", key="updates.poll_schedule")],
         data=_people, title="расписание проверки"),
    Shot("adm.upd.install", role="admin", press=[UpdateCB(action="install")], data=_upd_found,
         title="обновить: ожидание"),
    Shot("adm.upd.mute", role="admin", press=[UpdateCB(action="mute")], data=_people,
         title="«Не уведомлять» на уведомлении"),
    Shot("adm.upd.menu", role="admin", press=[UpdateCB(action="menu")], data=_people,
         title="«В меню» на итоге обновления"),
]

_GATEWAYS = [
    # ── 1.6 шлюзы и РФ-доступ в настройках (вход — «🛰 Шлюзы» с главной) ────
    Shot("adm.rt.provision", role="admin", press=[SetCB(sec="rt")], data=_people,
         title="РФ-доступ не развёрнут"),
    Shot("adm.rt.provision.do", role="admin", press=[SetCB(sec="rt", act="do", key="provision")],
         data=_rt_none, title="развернуть: итог"),
    Shot("adm.rt.asleep", role="admin", press=[SetCB(sec="rt")], data=_rt_asleep,
         title="обвязка есть, функция спит до перезапуска"),
    Shot("adm.rt.off", role="admin", press=[SetCB(sec="rt")], data=_rt_off, title="РФ-доступ выключен"),
    Shot("adm.rt.empty", role="admin", press=[SetCB(sec="rt")], data=_rt_none, title="шлюз не назначен"),
    Shot("adm.rt.one", role="admin", press=[SetCB(sec="rt")], data=_slots(1), title="один шлюз"),
    Shot("adm.rt.two", role="admin", press=[SetCB(sec="rt")], data=_slots(2), title="два шлюза, резерв жив"),
    Shot("adm.rt.two.down", role="admin", press=[SetCB(sec="rt")], data=_slots(2, standby="down"),
         title="два шлюза, резерв лежит"),
    Shot("adm.rt.gw", role="admin", press=[SetCB(sec="rt_gw")], data=_rt_none, title="назначить: выбор машины"),
    Shot("adm.rt.gw.standby", role="admin", press=[GwSlotCB(action="add")], data=_slots(1),
         title="резервный шлюз: выбор машины"),
    Shot("adm.rt.gw.full", role="admin", press=[SetCB(sec="rt_gw")], data=_slots(2),
         title="слоты заняты"),
    Shot("adm.rt.gw.replace", role="admin", press=[SetCB(sec="rt_gw", key="1")], data=_slots(1),
         title="заменить машину слота"),
    Shot("adm.rt.pick_list", role="admin", press=[GwMarkCB(action="pick_list")], data=_rt_none,
         title="из моих устройств"),
    Shot("adm.rt.pick_list.empty", role="admin", press=[GwMarkCB(action="pick_list")], data=_rt_none_bare,
         title="из моих устройств: пусто"),
    Shot("adm.rt.pick", role="admin", press=[GwMarkCB(action="pick", device_id=MAC)], data=_rt_none,
         title="назначить шлюзом?"),
    Shot("adm.rt.pick.replace", role="admin", press=[GwMarkCB(action="pick", device_id=PI4, slot=1)],
         data=_slots(1), title="заменить машину на эту?"),
    Shot("adm.rt.mark_yes", role="admin", press=[GwMarkCB(action="mark_yes", device_id=MAC)], data=_rt_none,
         title="токен бота шлюза: приглашение"),
    Shot("adm.rt.new_ask.replace", role="admin", press=[GwMarkCB(action="new_ask", slot=1)], data=_slots(1),
         title="новое устройство вместо прежнего?"),
    Shot("adm.rt.token.done", role="admin", press=[GwMarkCB(action="new_ask")], text=TOKEN, data=_rt_none,
         title="токен принят: инструкция и файл первого применения"),
    Shot("adm.rt.users", role="admin", press=[SetCB(sec="rt_users")], data=_slots(1), title="кому доступен"),
    Shot("adm.rt.users.allow", role="admin", press=[SetCB(sec="rt", act="do", key="allow", val=str(BR))],
         data=_slots(1), title="разрешить профилю"),
    Shot("adm.rt.params", role="admin", press=[SetCB(sec="rt_params")], data=_slots(1), title="параметры"),
    Shot("adm.rt.params.cycle", role="admin",
         press=[SetCB(sec="rt_params", act="cycle", key="app.routing.probe_seconds")], data=_slots(1),
         title="такт зонда циклом"),
    Shot("adm.rt.params.lists", role="admin", press=[SetCB(sec="rt", act="do", key="lists_refresh")],
         data=_slots(1), title="списки обновлены"),
    Shot("adm.rt.disable", role="admin",
         press=[SetCB(sec="rt", act="toggle", key="app.routing.enabled")], data=_slots(1),
         title="выключить РФ-доступ для всех?"),
    Shot("adm.rt.disable.yes", role="admin", press=[SetCB(sec="rt", act="do", key="off!")], data=_slots(1),
         title="РФ-доступ выключен"),
    Shot("adm.rt.enable", role="admin", press=[SetCB(sec="rt", act="toggle", key="app.routing.enabled")],
         data=_rt_off, title="включить РФ-доступ"),
    Shot("adm.gw.card", role="admin", press=[GwSlotCB(action="card", slot=1)], data=_slots(1),
         title="карточка слота"),
    Shot("adm.gw.card.standby", role="admin", press=[GwSlotCB(action="card", slot=2)], data=_slots(2),
         title="карточка резервного"),
    Shot("adm.gw.card.lan", role="admin", press=[GwSlotCB(action="card", slot=1)], data=_slots(1, lan=True),
         title="карточка: VPN-транзит включён"),
    Shot("adm.gw.card.missing", role="admin", press=[GwSlotCB(action="card", slot=9)], data=_slots(1),
         title="слота нет"),
    Shot("adm.gw.dev", role="admin", press=[DeviceCB(action="open", device_id=NASPI)], data=_slots(1),
         title="устройство-шлюз — карточка слота"),
    Shot("adm.gw.edit", role="admin", press=[GwSlotCB(action="edit", slot=1)], data=_slots(1),
         title="«✏️ Изменить» слота"),
    Shot("adm.gw.edit.two", role="admin", press=[GwSlotCB(action="edit", slot=2)], data=_slots(2),
         title="«✏️ Изменить» при двух слотах"),
    Shot("adm.gw.pref", role="admin", press=[GwSlotCB(action="pref", slot=2)], data=_slots(2),
         title="предпочтительный при старте"),
    Shot("adm.gw.ping", role="admin", press=[GwSlotCB(action="ping", slot=1)], data=_slots(1), title="пинг"),
    Shot("adm.gw.switch_ask", role="admin", press=[GwSlotCB(action="switch_ask", slot=2)], data=_slots(2),
         title="переключить трафик?"),
    Shot("adm.gw.switch_ask.down", role="admin", press=[GwSlotCB(action="switch_ask", slot=2)],
         data=_slots(2, standby="down"), title="переключить на лежащий?"),
    Shot("adm.gw.switch_yes", role="admin", press=[GwSlotCB(action="switch_yes", slot=2)], data=_slots(2),
         title="трафик переключён"),
    Shot("adm.gw.lan_ask.nonet", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)], data=_slots(1),
         title="VPN-транзит без локальной подсети"),
    Shot("adm.gw.lan_ask", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_slots(1, subnets=True), title="включить VPN-транзит?"),
    Shot("adm.gw.lan_ask.off", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_slots(1, lan=True), title="выключить VPN-транзит?"),
    Shot("adm.gw.lan_yes", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="1")],
         data=_slots(1, subnets=True), title="VPN-транзит включён"),
    Shot("adm.gw.router", role="admin", press=[GwSlotCB(action="router", slot=1)], data=_slots(1, lan=True),
         title="рецепт роутера: MikroTik"),
    Shot("adm.gw.router.ow", role="admin", press=[GwSlotCB(action="router", slot=1, val="ow")],
         data=_slots(1, lan=True), title="рецепт роутера: OpenWrt"),
    Shot("adm.gw.bundle", role="admin", press=[GwSlotCB(action="bundle", slot=1)], data=_slots(1),
         title="конфигурация шлюза файлом"),
    Shot("adm.gw.bundle.card", role="admin",
         press=[GwSlotCB(action="bundle", slot=1), SetCB(sec="rt", act="do", key="bundle_cancel", val="1")],
         data=_slots(1), title="«🛰 В карточку» под файлом"),
    Shot("adm.gw.bundle.home", role="admin",
         press=[GwSlotCB(action="bundle", slot=1), SetCB(sec="rt", act="do", key="bundle_home", val="1")],
         data=_slots(1), title="«⬅️ На главную» под файлом"),
    Shot("adm.gw.home", role="admin", press=[GwSlotCB(action="home", slot=1)], data=_slots(1),
         title="локальные подсети: приглашение"),
    Shot("adm.gw.home.done", role="admin", press=[GwSlotCB(action="home", slot=1)], text="192.168.1.0/24",
         data=_slots(1), title="локальные подсети: итог"),
    Shot("adm.gw.label", role="admin", press=[GwSlotCB(action="label", slot=1)], data=_slots(1),
         title="подпись: приглашение"),
    Shot("adm.gw.label.done", role="admin", press=[GwSlotCB(action="label", slot=1)], text="дача",
         data=_slots(1), title="подпись: итог"),
    Shot("adm.gw.name.done", role="admin", press=[GwSlotCB(action="name", slot=1)], text="NAS", data=_slots(1),
         title="имя устройства слота: итог"),
    Shot("adm.gw.remove_ask", role="admin", press=[GwSlotCB(action="remove_ask", slot=1)], data=_slots(1),
         title="снять единственный шлюз?"),
    Shot("adm.gw.remove_ask.active", role="admin", press=[GwSlotCB(action="remove_ask", slot=1)],
         data=_slots(2), title="снять активный при живом резерве?"),
    Shot("adm.gw.remove_ask.standby", role="admin", press=[GwSlotCB(action="remove_ask", slot=2)],
         data=_slots(2), title="снять резервный?"),
    Shot("adm.gw.remove_yes", role="admin", press=[GwSlotCB(action="remove_yes", slot=2)], data=_slots(2),
         title="резервный снят"),
    Shot("adm.gw.failover", role="admin", press=[GwSlotCB(action="failover")],
         data=_slots(2, failover=False), title="автопереключение снова включено"),
    Shot("adm.gw.peer_ask", role="admin", press=[GwSlotCB(action="peer_ask")], data=_slots(2),
         title="связать подсети?"),
    Shot("adm.gw.peer_yes", role="admin", press=[GwSlotCB(action="peer_yes", val="1")], data=_slots(2),
         title="подсети связаны"),
    Shot("adm.main.gw", role="admin", start="", data=_slots(2), title="главная со шлюзами"),
    Shot("adm.main.rt_off", role="admin", start="", data=_rt_unavailable,
         title="главная: РФ-доступ админу не виден"),
]

_MIGRATION = [
    # ── переезд профилей (⚙️ → 🔧 Сервис) ───────────────────────────────────
    Shot("adm.set.svc.mig", role="admin", press=[SetCB(sec="svc")], data=_mig_ready,
         title="сервис: переезд настроен"),
    Shot("adm.set.mig.start", role="admin", press=[SetCB(sec="mig", act="do", key="start")], data=_mig_ready,
         title="начать переезд?"),
    Shot("adm.set.mig.start.yes", role="admin", press=[SetCB(sec="mig", act="do", key="start!")],
         data=_mig_ready, title="переезд начат — след и раздел"),
    Shot("adm.set.svc.running", role="admin", press=[SetCB(sec="svc")], data=_mig_running,
         title="сервис: переезд идёт"),
    Shot("adm.set.mig.pending", role="admin", press=[SetCB(sec="mig", act="do", key="pending")],
         data=_mig_running, title="кто не переехал"),
    Shot("adm.set.mig.finish", role="admin", press=[SetCB(sec="mig", act="do", key="finish")],
         data=_mig_running, title="завершить переезд?"),
    Shot("adm.set.mig.cancel", role="admin", press=[SetCB(sec="mig", act="do", key="cancel")],
         data=_mig_running, title="отменить переезд?"),
    Shot("adm.set.mig.cancel.yes", role="admin", press=[SetCB(sec="mig", act="do", key="cancel!")],
         data=_mig_running, title="переезд отменён — след и раздел"),
    Shot("adm.set.mig.finish.yes", role="admin", press=[SetCB(sec="mig", act="do", key="finish!")],
         data=_mig_finishing, title="переезд завершён — след, раздел, перезапуск"),
    Shot("adm.set.mig.orphans", role="admin", press=[SetCB(sec="mig", act="do", key="orphans")],
         data=_mig_cancelled, title="переехавшие после отмены"),
    Shot("adm.migration", role="admin", press=[Menu(action="migration")], data=_mig_running,
         title="обзор переезда"),
    Shot("adm.main.migration", role="admin", start="", data=_mig_running, title="главная во время переезда"),
]

_LINKS = [
    # ── 8. ссылки /start <payload> с главной, карточек и уведомлений ────────
    Shot("adm.link.online", role="admin", start="online", data=_people, title="/start online"),
    Shot("adm.link.expiring", role="admin", start="expiring", data=_people, title="/start expiring"),
    Shot("adm.link.unassigned", role="admin", start="unassigned", data=_people, title="/start unassigned"),
    Shot("adm.link.traffic", role="admin", start="traffic", data=_people, title="/start traffic"),
    Shot("adm.link.traffic_dev", role="admin", start=f"traffic-{KS}", data=_people,
         title="трафик профиля по устройствам"),
    Shot("adm.link.traffic_local", role="admin", start="traffic_local", data=_people,
         title="прежняя ссылка «РФ-доступ» — трафик"),
    Shot("adm.link.traffic_local_dev", role="admin", start=f"traffic_local-{KS}-t", data=_people,
         title="прежняя ссылка «РФ-доступ профиля»"),
    Shot("adm.link.extend", role="admin", start=f"extend-{BR}", data=_people,
         title="продление по ссылке из «Истекают»"),
    Shot("adm.link.cl", role="admin", start=f"cl-{KS}", data=_people, title="карточка профиля по ссылке"),
    Shot("adm.link.dev", role="admin", start=f"dev-{PHONE}", data=_people, title="карточка устройства по ссылке"),
    Shot("adm.link.cl.missing", role="admin", start="cl-99", data=_people, title="профиля нет — главная"),
    Shot("adm.link.upd", role="admin", start="upd", data=_upd_found, title="/start upd — обновления"),
    Shot("adm.link.gw", role="admin", start="gw-1", data=_slots(2), title="карточка слота с главной"),
    Shot("adm.link.gw.missing", role="admin", start="gw-9", data=_slots(1), title="слота нет"),
    Shot("adm.link.gwcfg", role="admin", start="gwcfg-1", data=_slots(1),
         title="«Перевыпусти» из уведомления — файл сразу"),
    Shot("adm.link.migration", role="admin", start="migration", data=_mig_running, title="/start migration"),
    Shot("adm.link.migration_cl", role="admin", start=f"migration-{KS}", data=_mig_running,
         title="переезд профиля"),
    Shot("adm.link.unknown", role="admin", start="whatever", data=_people, title="чужая ссылка — главная"),
]

_OUTSIDE = [
    # ── 1.7 вне меню ────────────────────────────────────────────────────────
    Shot("adm.claim", role="admin", text=CLAIM, data=_claim, title="пересланное сообщение агента"),
    Shot("adm.claim.already", role="admin", text=CLAIM, data=_claim_already, title="этот шлюз уже назначен"),
    Shot("adm.claim.bad", role="admin", text=CLAIM, data=_claim_bad, title="подпись не сошлась"),
    Shot("adm.note.extend", role="admin", press=[NoteCB(kind="extend", ref=BR)], data=_people,
         title="«Продлить» на уведомлении об истечении"),
    Shot("adm.note.unassigned", role="admin", press=[NoteCB(kind="unassigned")], data=_people,
         title="кнопка уведомления о пире без профиля"),
    Shot("adm.note.gwcfg", role="admin", press=[NoteCB(kind="gwcfg", ref=1)], data=_slots(1),
         title="«Перевыпустить» на напоминании о конфигурации шлюза"),
]

SHOTS += _DEVICES + _RF + _PROFILES + _NEW + _SETTINGS + _GATEWAYS + _MIGRATION + _LINKS + _OUTSIDE


# ── шаг 3: диалоги до конца, файл копии, события без действия ───────────────

def _archive(role: str = "main", db: bytes | None = None) -> bytes:
    """Резервная копия как её собирает make_backup: метка роли и даты, база —
    по желанию (без базы проверять схему нечего)."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, raw):
            ti = tarfile.TarInfo(name)
            ti.size = len(raw)
            tar.addfile(ti, io.BytesIO(raw))
        add("state/backup-meta.json",
            json.dumps({"role": role, "created_at": "2026-09-01T03:00:00+03:00"}).encode())
        if db is not None:
            add("state/bot.db", db)
    return buf.getvalue()


def _old_db_bytes() -> bytes:
    """База со схемой ниже 3.2.0: в clients нет колонки kind."""
    import os
    import sqlite3
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE clients(id INTEGER PRIMARY KEY, tg_name TEXT);"
                          "CREATE TABLE devices(id INTEGER PRIMARY KEY, holder_client_id INTEGER);")
        con.commit()
        con.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


_BK_OK = _archive()
_BK_GW = _archive("gw")
_BK_OLD = _archive(db=_old_db_bytes())
_BK_NAME = "awg-bot-backup-2026-09-01.tgz"


def _restore_ready(services):
    """Восстановление из файла в чате: раскладка на диск и запуск awg-bot
    restore — заглушки (они останавливают сервис на хосте)."""
    who = _people(services)
    services.prepare_restore = lambda plain: "/nonexistent/DUMMY-restore.tgz"
    services.launch_restore = lambda path: None
    return who


def _mail_check(ok: bool):
    """Мастер почты: вход по IMAP и SMTP — без сети (подменены сами проверки
    протоколов, итог запоминает настоящий email_check), удачный или отказ."""
    def build(services, mp):
        from awgbot.infra import mail
        who = _people(services)

        def imap(acc):
            if not ok:
                raise mail.MailError("IMAP: [AUTHENTICATIONFAILED] Invalid credentials")
        mp.setattr(mail, "check_imap", imap)
        mp.setattr(mail, "check_smtp", lambda acc: None)
        return who
    return build


def _ks_expiring(services):
    """Годовая подписка Ксюши кончается через неделю без часа: порог «7 дней»
    только что пересечён, отсрочку она ещё не брала."""
    who = _people(services)
    services.db.update_client_fields(KS, period_start=_iso(NOW - _dt.timedelta(days=358, hours=1)),
                                     period_end=_iso(NOW + _dt.timedelta(days=7) - _dt.timedelta(hours=1)))
    return who


def _gw_file_in_chat(plain: bool):
    """Слот NASPi, в чате админа лежит его файл конфигурации (#40) под
    погасшей карточкой (#39); plain — файл первого применения."""
    def build(services, mp):
        who = _slots(1)(services, mp)
        services.gw_bundle_msg_set(1, config.ADMIN_ID, 40, 39, "DUMMYFP", plain)
        return who
    return build


async def _ev_applied(services, bot):
    from awgbot.bot.handlers.settings import bundle_applied
    await bundle_applied(bot, services, 1, True, "", "DUMMYFP")


async def _ev_applied_fail(services, bot):
    from awgbot.bot.handlers.settings import bundle_applied
    await bundle_applied(bot, services, 1, False, "routing-gw-setup.sh: nft: syntax error", "DUMMYFP")


async def _ev_installed(services, bot):
    from awgbot.bot.handlers.settings import bundle_installed
    await bundle_installed(bot, services, 1)


def _upd_pending(tag: str):
    """Перед рестартом запущено обновление до tag, в чате — «дождись» (#30);
    тело релиза — без сети."""
    def build(services, mp):
        from awgbot.infra import updates
        who = _people(services)
        services.db.set_state("update_pending", tag)
        services.set_update_wait(config.ADMIN_ID, 30)
        mp.setattr(updates, "release_body", lambda t: "- исправлено одно\n- добавлено другое")
        return who
    return build


async def _ev_update_result(services, bot):
    from awgbot.runtime.main import report_update_result
    await report_update_result(bot, services)


async def _ev_update_available(services, bot):
    import types
    from awgbot.runtime.scheduler import notify_update_available
    await notify_update_available(bot, services, types.SimpleNamespace(
        tag="v1.3.0", body="- исправлено одно\n- добавлено другое"))


def _restart_promised(services):
    """Бот перезапускали по кнопке: обещание «вернётся» — сообщение #25."""
    who = _people(services)
    services.set_restart_wait(config.ADMIN_ID, 25)
    return who


async def _ev_restarted(services, bot):
    from awgbot.bot.handlers.admin.panel import restore_panel_after_restart
    await restore_panel_after_restart(bot, services)


def _restore_done(services):
    """awg-bot restore отработал и оставил маркер."""
    who = _people(services)
    services.pop_restore_done = lambda: {"created_at": "2026-09-01T03:00:00+03:00"}
    return who


async def _ev_restore_done(services, bot):
    from awgbot.bot.handlers.restore import report_restore_result
    await report_restore_result(bot, services)


async def _ev_expiry(services, bot):
    """Задача сроков, как её собирает планировщик: уведомления об истечении
    с кнопкой отсрочки (kb.grace_offer)."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot.notifier import send_notifications
    from awgbot.core import settings
    notes = services.check_expiry()
    for n in notes:
        if getattr(n, "grace_offer_client_id", 0):
            n.reply_markup = kb.grace_offer(n.grace_offer_client_id, settings.get_int("grace.grace_days", 14))
    await send_notifications(bot, notes)


async def _ev_added_by_admin(services, bot):
    """Уведомление владельцу о новом устройстве от админа (kb.added_by_admin)."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot import texts
    from awgbot.bot.notifier import notify_one
    used, limit = services.device_quota(KS)
    await notify_one(bot, 2001, texts.reassign_recipient_notice("Ноутбук", used, limit),
                     reply_markup=kb.added_by_admin(LAPTOP))


_PERIOD = [("press", ClientCB(action="edit_period", client_id=KS)), ("text", "-")]
_NEWP = [("press", Menu(action="add_client")), ("text", "Маша")]
_NEWP_TRAFFIC = _NEWP + [("press", PresetCB(kind="new_devs", val=3))]
_NEWP_PERIOD = _NEWP_TRAFFIC + [("press", PresetCB(kind="new_traffic", val=100))]
_BC_TEXT = [("press", BroadcastCB(action="pick")), ("press", BroadcastCB(action="all")),
            ("press", BroadcastCB(action="next")), ("text", "В субботу с 02:00 до 03:00 — работы на сервере")]
_BC_EXT_TEXT = [("press", BroadcastCB(action="pick")), ("press", BroadcastCB(action="ext")),
                ("press", BroadcastCB(action="all")), ("press", BroadcastCB(action="next")),
                ("press", PresetCB(kind="bc_days", val=7)), ("text", "Неделя в подарок — спасибо, что с нами")]
_MAIL_KNOWN = [("press", SetCB(sec="email", act="do", key="setup")), ("text", "admin@gmail.com")]
_MAIL_OWN = [("press", SetCB(sec="email", act="do", key="setup")), ("text", "admin@example.org"),
             ("text", "imap.example.org:993")]
_PHRASE = [("press", SetCB(sec="backup", act="do", key="enc_set")), ("text", "DUMMY-passphrase-1234")]

_STEP3 = [
    # ── диалоги ввода до итога ──────────────────────────────────────────────
    Shot("adm.cl.period.done", role="admin", steps=_PERIOD + [("text", "15.10.2027")], data=_people,
         title="период: итог"),
    Shot("adm.cl.period.forever", role="admin", steps=_PERIOD + [("text", "0")], data=_people,
         title="период: бессрочно"),
    Shot("adm.new.traffic", role="admin", steps=_NEWP_TRAFFIC, data=_people, title="новый профиль: трафик"),
    Shot("adm.new.devs.ask", role="admin", steps=_NEWP + [("press", PresetCB(kind="new_devs", val=-1))],
         data=_people, title="новый профиль: «✏️ Другое» — приглашение к числу устройств"),
    Shot("adm.new.devs.other", role="admin",
         steps=_NEWP + [("press", PresetCB(kind="new_devs", val=-1)), ("text", "4")], data=_people,
         title="новый профиль: своё число устройств — трафик"),
    Shot("adm.new.traffic.other", role="admin",
         steps=_NEWP_TRAFFIC + [("press", PresetCB(kind="new_traffic", val=-1))], data=_people,
         title="новый профиль: «✏️ Другое» — приглашение к трафику"),
    Shot("adm.new.traffic.typed", role="admin",
         steps=_NEWP_TRAFFIC + [("press", PresetCB(kind="new_traffic", val=-1)), ("text", "70")], data=_people,
         title="новый профиль: свой трафик текстом — срок"),
    Shot("adm.new.period", role="admin", steps=_NEWP_PERIOD, data=_people, title="новый профиль: срок"),
    Shot("adm.new.invite", role="admin",
         steps=_NEWP_PERIOD + [("press", PeriodCB(kind="year", ctx="create"))], data=_people,
         title="новый профиль: приглашение"),
    Shot("adm.bc.preview.markup", role="admin",
         steps=_BC_TEXT[:3] + [("text", "В субботу — работы на сервере",
                                [{"type": "bold", "offset": 12, "length": 17}])],
         data=_bc_ready, title="превью с разметкой Telegram"),
    Shot("adm.bc.send", role="admin", steps=_BC_TEXT + [("press", BroadcastCB(action="send"))],
         data=_bc_ready, title="разослано: объявление и отчёт"),
    Shot("adm.bc.send.ext", role="admin", steps=_BC_EXT_TEXT + [("press", BroadcastCB(action="send"))],
         data=_bc_ready, title="разослано с продлением"),
    Shot("adm.set.email.saved", role="admin", steps=_MAIL_KNOWN + [("text", "DUMMY-app-password")],
         data=_mail_check(True), title="мастер почты: ящик подключён"),
    Shot("adm.set.email.refused", role="admin", steps=_MAIL_KNOWN + [("text", "DUMMY-app-password")],
         data=_mail_check(False), title="мастер почты: вход не прошёл"),
    Shot("adm.set.email.smtp", role="admin", steps=_MAIL_OWN, data=_mail_check(True),
         title="мастер почты: SMTP-сервер"),
    Shot("adm.set.email.password", role="admin", steps=_MAIL_OWN + [("text", "smtp.example.org:465")],
         data=_mail_check(True), title="мастер почты: пароль своего сервера"),
    Shot("adm.set.email.own.saved", role="admin",
         steps=_MAIL_OWN + [("text", "smtp.example.org:465"), ("text", "DUMMY-password")],
         data=_mail_check(True), title="мастер почты: свой сервер подключён"),
    Shot("adm.set.backup.enc_set.done", role="admin", steps=_PHRASE + [("text", "DUMMY-passphrase-1234")],
         data=_people, title="фраза совпала — шифрование включено"),
    Shot("adm.set.backup.enc_set.mismatch", role="admin", steps=_PHRASE + [("text", "DUMMY-passphrase-9999")],
         data=_people, title="фраза не совпала — заново"),
    # ── A190: восстановление из файла в чате ────────────────────────────────
    Shot("adm.restore", role="admin", steps=[("file", _BK_NAME, _BK_OK)], data=_restore_ready,
         title="копия в чате: восстановить?"),
    Shot("adm.restore.yes", role="admin",
         steps=[("file", _BK_NAME, _BK_OK), ("press", SetCB(sec="backup", act="do", key="restore!"))],
         data=_restore_ready, title="восстановление запущено"),
    Shot("adm.restore.drop", role="admin",
         steps=[("file", _BK_NAME, _BK_OK), ("press", SetCB(sec="backup", act="do", key="restore_drop"))],
         data=_restore_ready, title="файл отброшен"),
    Shot("adm.restore.junk", role="admin", steps=[("file", _BK_NAME, b"DUMMY")], data=_restore_ready,
         title="не резервная копия"),
    Shot("adm.restore.gw", role="admin", steps=[("file", _BK_NAME, _BK_GW)], data=_restore_ready,
         title="копия агента шлюза"),
    Shot("adm.restore.old", role="admin", steps=[("file", _BK_NAME, _BK_OLD)], data=_restore_ready,
         title="копия со схемой ниже 3.2.0"),
    # ── события без действия человека ───────────────────────────────────────
    Shot("adm.ev.gw_applied", role="admin", call=("bundle_applied", _ev_applied),
         data=_gw_file_in_chat(False), title="A134c: конфигурация шлюза применена"),
    Shot("adm.ev.gw_applied.fail", role="admin", call=("bundle_applied", _ev_applied_fail),
         data=_gw_file_in_chat(False), title="A134c: конфигурация шлюза не применилась"),
    Shot("adm.ev.gw_installed", role="admin", call=("bundle_installed", _ev_installed),
         data=_gw_file_in_chat(True), title="A146: шлюз настроен"),
    Shot("adm.ev.upd_available", role="admin", call=("notify_update_available", _ev_update_available),
         data=_people, title="доступна новая версия"),
    Shot("adm.ev.upd_done", role="admin", call=("report_update_result", _ev_update_result),
         data=_upd_pending("v1.2.3"), title="обновлён после рестарта"),
    Shot("adm.ev.upd_failed", role="admin", call=("report_update_result", _ev_update_result),
         data=_upd_pending("v1.3.0"), title="обновление не применилось"),
    Shot("adm.ev.restarted", role="admin", call=("restore_panel_after_restart", _ev_restarted),
         data=_restart_promised, title="бот вернулся после перезапуска"),
    Shot("adm.ev.restore_done", role="admin", call=("report_restore_result", _ev_restore_done),
         data=_restore_done, title="восстановление из копии завершено"),
    Shot("adm.ev.expired", role="admin", call=("check_expiry", _ev_expiry), data=_br_expired,
         title="подписка истекла — «⏱ Продлить» админу"),
    Shot("adm.ev.grace_offer", role="admin", call=("check_expiry", _ev_expiry), data=_ks_expiring,
         title="подписка истекает — владельцу кнопка отсрочки"),
    Shot("adm.ev.added_by_admin", role="admin", call=("notify_one", _ev_added_by_admin), data=_people,
         title="владельцу — новое устройство от админа"),
]

SHOTS += _STEP3


# ── шаг 5: построители клавиатур без снимка ─────────────────────────────────

def _delete_partial(services):
    """Сервер не снял пир «Ноутбука»: профиль не удаляется, удалось — частично."""
    from awgbot.domain.services import ServiceError
    who = _people(services)
    real = services.remove_device

    def remove(device_id):
        if device_id == LAPTOP:
            raise ServiceError("awg: пир не снят")
        return real(device_id)
    services.remove_device = remove
    return who


def _gw_dev_no_slot(services, mp):
    """Устройство-шлюз, для которого слот не находится (двойник шлюза в окне
    переезда: признак шлюза есть, строки слота по нему нет)."""
    who = _slots(1)(services, mp)
    services.db.gateway_by_device = lambda device_id: None
    # трафик несимметричный: у шлюза стрелки развёрнуты (отдача ↑ — это tx)
    services.db.add_traffic_bulk([(NASPI, int(0.83 * GB), int(10.95 * GB))])
    return who


def _new_generation(available: bool):
    """Поставка привезла поколение ядра новее применённого; available —
    второй интерфейс под переезд уже поднят."""
    def build(services, mp):
        from awgbot.infra import awglock
        who = _mig_ready(services, mp) if available else _people(services)
        mp.setattr(awglock, "generation", lambda: 2)
        mp.setattr(awglock, "applied_generation", lambda: 1)
        return who
    return build


async def _ev_migration_needed(services, bot):
    from awgbot.runtime.main import _notify_migration_needed
    await _notify_migration_needed(bot, services)


def _dns_public(services, mp):
    """Ядро на хосте, DNS клиентов публичный, решения о своём резолвере нет."""
    from awgbot.core import settings
    who = _people(services)
    mp.setattr(config, "AWG_RUNTIME", "host")
    settings.set_value("app.client_config.dns1", "1.1.1.1")
    settings.set_value("app.client_config.dns2", "8.8.8.8")
    return who


async def _ev_private_dns_offer(services, bot):
    from awgbot.runtime.main import _notify_private_dns_offer
    await _notify_private_dns_offer(bot, services)


SHOTS += [
    Shot("adm.cl.delete.partial", role="admin", press=[ClientCB(action="delete_yes", client_id=KS)],
         data=_delete_partial, title="удаление профиля: сервер не снял часть устройств"),
    Shot("adm.gw.dev.noslot", role="admin", press=[DeviceCB(action="open", device_id=NASPI)],
         data=_gw_dev_no_slot, title="карточка устройства-шлюза без слота"),
    Shot("adm.ev.generation_pending", role="admin", call=("notify_migration_needed", _ev_migration_needed),
         data=_new_generation(False), title="новое поколение ядра: интерфейс под переезд не поднят"),
    Shot("adm.ev.migration_needed", role="admin", call=("notify_migration_needed", _ev_migration_needed),
         data=_new_generation(True), title="новое поколение ядра: нужен переезд"),
    Shot("adm.ev.private_dns_offer", role="admin", call=("notify_private_dns_offer", _ev_private_dns_offer),
         data=_dns_public, title="предложение своего DNS-резолвера"),
]

# у каждого снимка — построитель: он же сбрасывает модульные подмены прежних
assert all(sh.data is not None for sh in SHOTS), [sh.id for sh in SHOTS if sh.data is None]

# Длинная подпись в ряду из 2+ разрешена макетом экрана — {id снимка: подписи};
# длина считается без селектора варианта («☑️ Аварии на e-mail» — 18).
LABEL_EXCEPTIONS: dict[str, set[str]] = {}


# ── шаг 5: ветки, которые держали e2e — переезд и главная ──────────────────

def _mig_spread(services, mp):
    """Переезд идёт, меняются порт (43125 → 51821), подсеть и ядро (gen1 →
    gen2). Когорта — пять устройств: у админа не переехало ни одно из двух
    (iPhone был на связи час назад, MacBook — ни разу), у Ксюши — одно из
    двух, Боря переехал целиком."""
    from awgbot.infra import awg, awglock
    who = _mig_ready(services, mp)
    hour_ago = int(NOW.timestamp()) - 3600
    for d in (IPHONE, MAC, PHONE, LAPTOP, TABLET):
        services.db.update_device_fields(d, last_handshake=hour_ago)
    services.migration_start()
    twins = services.db.twins_by_origin()
    _online(services, twins[PHONE], twins[TABLET])
    services.db.update_device_fields(MAC, last_handshake=0)
    mp.setattr(awg, "read_server_params", lambda force=False, iface=None: {"listen_port": 51821})
    mp.setattr(awglock, "target_generation", lambda: 2)
    return who


def _rt_disabled_quiet(services, mp):
    """Только админ, РФ-доступ развёрнут, но выключен, шлюзов нет."""
    who = _base(services)
    _routing(services, mp, enabled=False)
    return who


def _nobody_online(services):
    """Никто не на связи, но есть истекающий профиль и пир без профиля;
    сервер работает 12 дней 4 часа."""
    who = _people(services)
    for d in (PHONE, IPHONE):
        services.db.update_device_fields(d, last_handshake=0)
    services.db.set_state("container_started_at", "2026-09-03T05:00:00Z")
    return who


def _update_and_migration(services, mp):
    """Идёт переезд, и проверка нашла версию; тег — без «v»."""
    who = _mig_running(services, mp)
    services.update_available_tag = lambda: "1.3.0"
    return who


def _down(services, mp):
    """Сервер не отвечает, шлюз РФ-доступа — тоже."""
    who = _slots(1)(services, mp)
    services.db.set_state("server_ok_view", "0")
    services.routing_admin_status = lambda: {"ok": False, "active": "NASPi", "active_slot": 1, "standby": []}
    return who


def _gw_online(services, mp):
    """Шлюз NASPi и Ксюшин телефон на связи."""
    who = _slots(1)(services, mp)
    _online(services, NASPI)
    services.db.update_device_fields(IPHONE, last_handshake=0)
    return who


def _two_expiring(services):
    """Истекают двое: Ксюша — завтра, Боря — через два дня."""
    who = _people(services)
    services.db.update_client_fields(KS, period_start=_iso(NOW - _dt.timedelta(days=29)),
                                     period_end=_iso(NOW + _dt.timedelta(days=1)), period_kind="month")
    return who


SHOTS += [
    Shot("adm.migration.params", role="admin", press=[Menu(action="migration")], data=_mig_spread,
         title="обзор переезда: порт и ядро меняются, отстающие по убыванию остатка"),
    Shot("adm.migration.empty", role="admin", press=[Menu(action="migration")], data=_people,
         title="обзор переезда: переезд не настроен — переезжать некому"),
    Shot("adm.link.migration_cl.spread", role="admin", start=f"migration-{ADM}", data=_mig_spread,
         title="переезд профиля: коннект старого пира, не подключалось"),
    Shot("adm.link.migration_cl.missing", role="admin", start="migration-99", data=_mig_running,
         title="переезд профиля, которого нет — главная"),
    Shot("adm.main.rt_disabled", role="admin", start="", data=_rt_disabled_quiet,
         title="главная: РФ-доступ выключен, шлюзов нет"),
    Shot("adm.main.nobody_online", role="admin", start="", data=_nobody_online,
         title="главная: онлайн 0 без ссылки рядом с «Истекают» и «Без профиля»; аптайм в днях и часах"),
    Shot("adm.main.update", role="admin", start="", data=_update_and_migration,
         title="главная: доступна версия (тег без «v»), переезд — последней строкой"),
    Shot("adm.main.down", role="admin", start="", data=_down,
         title="главная: сервер не отвечает, шлюз РФ-доступа недоступен"),
    Shot("adm.link.dev.missing", role="admin", start="dev-99", data=_people,
         title="устройства нет — главная"),
    Shot("adm.online.gw", role="admin", press=[Menu(action="online")], data=_gw_online,
         title="онлайн: шлюз вверху с пометкой"),
    Shot("adm.expiring.two", role="admin", press=[Menu(action="expiring")], data=_two_expiring,
         title="истекают двое: ближайший сверху"),
]


# ── шаг 5: устройства и профили списком, настройки — ветки из e2e ──────────

def _clients_icons(services):
    """Значки списка профилей по приоритету: Боря истёк, но на связи (🟢);
    Ксюша на паузе и на связи (⏸️); Аня на паузе и под блоком (⛔)."""
    from awgbot.core.blocks import ClientBlock
    who = _br_expired(services)
    _online(services, TABLET)
    ok, *_ = services.enter_pause(KS, 7)
    assert ok
    ann = _client(services, "Аня", 2010, devices=("Телефон Ани",))
    ok, *_ = services.enter_pause(ann, 7)
    assert ok
    services.block_client_manual(ann, ClientBlock.ADMIN_SILENT, False)
    return who


SHOTS += [
    Shot("adm.devices.gw", role="admin", press=[Menu(action="devices")], data=_gw_online,
         title="мои устройства: шлюз со значком 🛰, без кружка"),
    Shot("adm.clients.icons", role="admin", press=[Menu(action="clients")], data=_clients_icons,
         title="значки: истёкший онлайн, пауза, блок поверх паузы"),
    Shot("adm.set.notify.quiet", role="admin", press=[SetCB(sec="notify")], data=_people,
         conf={"quiet_hours.quiet_hours_enabled": True, "quiet_hours.quiet_hours_start": 22,
               "quiet_hours.quiet_hours_end": 6, "resource_alerts.thresholds_percent.cpu": 90},
         title="уведомления: тихие часы включены — границы в тексте и «С»/«До», свой порог CPU"),
    Shot("adm.set.mig_prep.port.bad", role="admin", press=[SetCB(sec="mig_prep", act="edit", key="port")],
         text="abc", data=_people, title="свой порт: буквы — не порт"),
]


# ── шаг 5: трафик списком и строка РФ в карточках — ветки из e2e ────────────

def _traffic_world(*, enabled: bool = True, rf_total=None, outside: int = 0, gone: bool = False):
    """Профили для экранов трафика без шлюзов: «Молчун» без трафика, Ксюша
    (РФ разрешён) с 2 ГБ, у её телефона rf_total — РФ-часть; РФ-итог сервера
    — сумма устройств плюс outside байт; gone — у Ксюши был «Ноут» с 2 ГБ
    РФ, его удалили в середине месяца."""
    def build(services, mp):
        who = _base(services)
        _routing(services, mp, enabled=enabled)
        _client(services, "Молчун", 2011, devices=("Телефон",))
        ks = _client(services, "Ксюша", 2012, devices=("Телефон",), routing=True)
        phone = services.db.list_devices(ks)[0].id
        services.db.add_traffic_bulk([(phone, GB, GB)])
        rx, tx = rf_total or (0, 0)
        if rx or tx:
            services.db.rf_add_bulk([(phone, rx, tx)])
        if gone:
            dev = services.add_device(ks, "Ноут").device_id
            services.db.add_traffic_bulk([(dev, GB, GB)])
            services.db.rf_add_bulk([(dev, GB, GB)])
            services.remove_device(dev)
            rx, tx = rx + GB, tx + GB
        services.db.set_state("rf_month_rx", str(rx))
        services.db.set_state("rf_month_tx", str(tx + outside))
        return who
    return build


# профили матрицы строки РФ: (имя, РФ разрешён, РФ-часть за месяц)
_RF_MATRIX = (("Аня", True, 0), ("Боря", False, 0), ("Вера", False, 4), ("Гоша", True, 4))
RFM = {n: (3 + i, 1 + i) for i, (n, _a, _r) in enumerate(_RF_MATRIX)}      # имя → (профиль, устройство)


def _rf_matrix(*, enabled: bool, selfcheck: bool = True):
    """Строка «└ 🇷🇺 РФ-доступ» в карточках и списке по правилу «разрешён и
    функция включена, или за месяц было»: четыре профиля по одному
    устройству (_RF_MATRIX), у каждого 2 ГБ трафика; selfcheck — самопроверка
    обвязки проходит."""
    def build(services, mp):
        from awgbot.infra import routing as rt
        who = _base(services)
        _routing(services, mp, enabled=enabled)
        if not selfcheck:
            mp.setattr(rt.selfcheck, "available", lambda: False)
        total = 0
        for i, (name, allowed, rf_gb) in enumerate(_RF_MATRIX):
            cid = _client(services, name, 2020 + i, devices=("Телефон",), routing=allowed)
            dev = services.db.list_devices(cid)[0].id
            assert (cid, dev) == RFM[name]
            services.db.add_traffic_bulk([(dev, GB, GB)])
            if rf_gb:
                services.db.rf_add_bulk([(dev, GB, (rf_gb - 1) * GB)])
                total += rf_gb * GB
        services.db.set_state("rf_month_rx", str(total // 4))
        services.db.set_state("rf_month_tx", str(total - total // 4))
        return who
    return build


def _rf_gateway(services, mp):
    """Профиль админа: iPhone и устройство-шлюз NASPi, у обоих трафик и
    РФ-счётчики (у шлюза — как если бы байты на него всё-таки легли)."""
    who = _slots(1)(services, mp)
    services.db.add_traffic_bulk([(NASPI, 3 * GB, 3 * GB)])
    services.db.rf_add_bulk([(IPHONE, GB, GB), (NASPI, 5 * GB, 5 * GB)])
    services.db.set_state("rf_month_rx", str(GB // 2 + GB))
    services.db.set_state("rf_month_tx", str(GB // 10 + GB))
    return who


def _no_feature(services):
    """Сервер без функции РФ-доступа: у админа телефон с трафиком."""
    who = _base(services)
    dev = services.add_device(ADM, "phone").device_id
    services.db.add_traffic_bulk([(dev, GB, GB)])
    return who


_LIVE = [("start", "")]                 # главная — живое меню #2, ссылка правит его

SHOTS += [
    Shot("adm.traffic.no_rf", role="admin", press=[Menu(action="traffic")], data=_traffic_world(),
         title="трафик: РФ-итог 0 при живом трафике, профиль без трафика выпал"),
    Shot("adm.traffic.outside", role="admin", press=[Menu(action="traffic")],
         data=_traffic_world(rf_total=(GB, 0), outside=GB), title="трафик: «Вне профилей» под РФ-итогом"),
    Shot("adm.traffic.outside.noise", role="admin", press=[Menu(action="traffic")],
         data=_traffic_world(rf_total=(GB, GB), outside=GB // 100 - 1),
         title="трафик: «Вне профилей» меньше 0.01 ГБ — шум, строки нет"),
    Shot("adm.traffic.outside.min", role="admin", press=[Menu(action="traffic")],
         data=_traffic_world(rf_total=(GB, GB), outside=GB // 100),
         title="трафик: «Вне профилей» ровно 0.01 ГБ"),
    Shot("adm.traffic.outside.deleted", role="admin", press=[Menu(action="traffic")],
         data=_traffic_world(rf_total=(GB, GB), gone=True),
         title="трафик: РФ удалённого устройства — во «Вне профилей»"),
    Shot("adm.traffic.old_back", role="admin", press=[Menu(action="traffic_local")], data=_people,
         title="«Назад» прежнего экрана РФ — дерево трафика"),
    Shot("adm.traffic.no_feature", role="admin", press=[Menu(action="traffic")], data=_no_feature,
         title="трафик: сервер без функции РФ-доступа"),
    Shot("adm.link.traffic_dev.idle", role="admin", start=f"traffic-{KS}", data=_ks_many,
         title="трафик профиля: устройства без трафика выпали, итог — только профиля"),
    Shot("adm.link.traffic_dev.gw", role="admin", start=f"traffic-{ADM}", data=_rf_gateway,
         title="трафик профиля со шлюзом: у шлюза нет РФ-ветки"),
    Shot("adm.link.traffic_dev.no_feature", role="admin", start=f"traffic-{ADM}", data=_no_feature,
         title="трафик профиля: сервер без функции РФ-доступа"),
    Shot("adm.link.traffic_dev.missing", role="admin", start="traffic-99", data=_people,
         title="трафик профиля, которого нет — главная"),
    Shot("adm.link.traffic_local_dev.short", role="admin", start=f"traffic_local-{KS}", data=_people,
         title="прежняя ссылка «traffic_local-<id>» без хвоста"),
    Shot("adm.link.traffic_dev.t", role="admin", start=f"traffic-{KS}-t", data=_people,
         title="ссылка «traffic-<id>-t»"),
    Shot("adm.link.traffic.live", role="admin", steps=_LIVE + [("start", "traffic")], data=_people,
         title="/start traffic при живой главной — правка меню, команда убрана"),
    Shot("adm.link.traffic_local.live", role="admin", steps=_LIVE + [("start", "traffic_local")],
         data=_people, title="прежняя /start traffic_local при живой главной — то же дерево"),
    Shot("adm.cl.admin", role="admin", press=[ClientCB(action="open", client_id=ADM)], data=_people,
         title="карточка своего профиля — главная"),
    Shot("adm.traffic.rf_on", role="admin", press=[Menu(action="traffic")], data=_rf_matrix(enabled=True),
         title="строки РФ в списке: функция включена"),
    Shot("adm.traffic.rf_off", role="admin", press=[Menu(action="traffic")], data=_rf_matrix(enabled=False),
         title="строки РФ в списке: функция выключена"),
]

for _on, _names in ((True, ("Аня", "Боря", "Вера")), (False, ("Аня", "Гоша"))):
    for _n in _names:
        _cid, _dev = RFM[_n]
        _what = dict((n, (a, r)) for n, a, r in _RF_MATRIX)[_n]
        _desc = (f"функция {'включена' if _on else 'выключена'}, РФ {'разрешён' if _what[0] else 'не разрешён'}, "
                 f"за месяц {_what[1]} ГБ")
        SHOTS += [
            Shot(f"adm.cl.rf.{'on' if _on else 'off'}.{_cid}", role="admin",
                 press=[ClientCB(action="open", client_id=_cid)], data=_rf_matrix(enabled=_on),
                 title=f"строка РФ в карточке профиля: {_desc}"),
            Shot(f"adm.dev.rf.{'on' if _on else 'off'}.{_dev}", role="admin",
                 press=[DeviceCB(action="open", device_id=_dev)], data=_rf_matrix(enabled=_on),
                 title=f"строка РФ в карточке устройства: {_desc}"),
        ]

SHOTS += [
    Shot("adm.cl.rf.selfcheck", role="admin", press=[ClientCB(action="open", client_id=RFM["Аня"][0])],
         data=_rf_matrix(enabled=True, selfcheck=False),
         title="строка РФ в карточке профиля: самопроверка обвязки не прошла — «0 ГБ» остаётся"),
    Shot("adm.dev.rf.selfcheck", role="admin", press=[DeviceCB(action="open", device_id=RFM["Аня"][1])],
         data=_rf_matrix(enabled=True, selfcheck=False),
         title="строка РФ в карточке устройства: самопроверка обвязки не прошла — «0 ГБ» остаётся"),
]


# ── шаг 5: SSH-доступ, сервер, резолвер, подготовка переезда — ветки из e2e ──

def _raise(kind: str, text: str):
    """Подмена метода сервиса, которая отказывает: kind — «service»
    (ServiceError — отказ, который бот объясняет) или «runtime» (сбой)."""
    def boom(*a, **k):
        from awgbot.domain.services import ServiceError
        raise (ServiceError if kind == "service" else RuntimeError)(text)
    return boom


def _fw_with(extra: dict | None = None, **over):
    """SSH-доступ (_FwHost) с подменами сверх него: extra — {метод сервиса:
    функция} (ответы CLI-таймера, отказы nft и sshd)."""
    def build(services):
        who = _fw(**over)(services)
        for k, v in (extra or {}).items():
            setattr(services, k, v)
        return who
    return build


_DNS_PUBLIC = {"mode": "public", "dns1": "1.1.1.1", "dns2": "1.0.0.1", "target": "10.8.1.1"}


def _dns_blocked(services):
    """Свой резолвер не решён, переезд сейчас невозможен (идёт другой)."""
    who = _people(services)
    services.private_dns_info = lambda: {**_DNS_PUBLIC, "decision": ""}
    services.migration_blocked_reason = lambda: "идёт переезд"
    return who


def _mig_prepare(ok: bool):
    """Подготовка переезда: второй интерфейс поднят (awg1, порт 443) или
    отказ хоста; перезапуск бота — только по кнопке, здесь не зовётся."""
    def build(services):
        who = _people(services)
        services.migration_prepare = ((lambda port=None: {"iface": "awg1", "subnet": "10.9.1.0/24",
                                                          "port": str(port or 443)})
                                      if ok else _raise("runtime", "порт 443 занят"))
        services.restart_bot = _raise("runtime", "перезапуск без кнопки")
        return who
    return build


_PORT = [("press", SetCB(sec="fw", act="edit", key="port"))]

SHOTS += [
    Shot("adm.set.fw.on.empty", role="admin", press=[SetCB(sec="fw")], data=_fw(enabled=True, present=True),
         title="SSH-доступ: фильтр включён, адресов нет — выключить можно"),
    Shot("adm.set.fw.confirm", role="admin", press=[SetCB(sec="fw", act="do", key="confirm")],
         data=_fw_with({"firewall_confirm": lambda: True}, enabled=True, present=True, rollback=True,
                       **_FW_ALLOW),
         title="кнопка таймера CLI: «оставить» — таймер снят"),
    Shot("adm.set.fw.rollback", role="admin", press=[SetCB(sec="fw", act="do", key="rollback")],
         data=_fw_with({"firewall_confirm": lambda: True}, enabled=True, present=True, rollback=True,
                       **_FW_ALLOW),
         title="кнопка таймера CLI: «откатить сейчас» — SSH снова открыт"),
    Shot("adm.set.fw.add.bad", role="admin",
         press=[SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")], text="мусор",
         data=_fw_with({"firewall_allow_add": _raise("service", "«мусор» не адрес, не подсеть и не имя")}),
         title="адрес: не адрес — отказ, ввод открыт"),
    Shot("adm.set.fw.del.stale", role="admin", press=[SetCB(sec="fw", act="do", key="del", val="5")],
         data=_fw(**_FW_ALLOW), title="убрать адрес: список изменился — номер вне списка"),
    Shot("adm.set.fw.unknown", role="admin", press=[SetCB(sec="fw", act="do", key="чего-то-нет")],
         data=_fw(), title="неизвестное действие раздела — кнопка устарела"),
    Shot("adm.set.fw.on.fail", role="admin", press=[SetCB(sec="fw", act="do", key="on")],
         data=_fw_with({"firewall_enable": _raise("runtime", "nft: Operation not permitted")}, **_FW_ALLOW),
         title="включить фильтр: отказ nft — «Не вышло», раздел перерисован"),
    Shot("adm.set.fw.owner", role="admin", press=[SetCB(sec="fw")],
         data=_fw(owner="generator", owner_detail="managed by ansible", owner_files=["/etc/ssh/sshd_config"]),
         title="SSH-доступ: конфигом sshd владеет другой процесс"),
    Shot("adm.set.fw.drift", role="admin", press=[SetCB(sec="fw")], data=_fw(listening=2222, drift=True),
         title="SSH-доступ: sshd слушает не тот порт, что держит фильтр"),
    Shot("adm.set.fw.firewalld", role="admin", press=[SetCB(sec="fw")], data=_fw(firewalld=True),
         title="SSH-доступ: firewalld активен"),
    Shot("adm.set.fw.port.busy.unknown", role="admin", steps=_PORT + [("text", "8443")],
         data=_fw_with({"ssh_port_busy": lambda port: "?"}),
         title="порт SSH занят, имя процесса не видно"),
    Shot("adm.set.fw.port.range", role="admin", steps=_PORT + [("text", "70000")], data=_fw(),
         title="порт SSH вне 1–65535 — переспрос"),
    Shot("adm.set.fw.port.refused", role="admin", steps=_PORT + [("text", "2222")],
         data=_fw_with({"ssh_port_change": _raise("service", "sshd -t: Bad configuration option")}),
         title="порт SSH: sshd отказал — первой строкой раздела"),
    Shot("adm.set.fw.port.back", role="admin", press=[SetCB(sec="fw", act="do", key="port_back")],
         data=_fw(), title="финишер порта: «Назад» — раздел"),
    Shot("adm.set.srv.pending", role="admin", press=[SetCB(sec="srv")],
         data=_srv(dns="1.1.1.1, 1.0.0.1", private_dns={**_DNS_PUBLIC, "decision": "pending"}),
         title="сервер: DNS публичный, при переезде станет свой"),
    Shot("adm.set.dns.blocked", role="admin", press=[SetCB(sec="dns")], data=_dns_blocked,
         title="свой резолвер при невозможном переезде — без «Переехать сейчас»"),
    Shot("adm.set.dns.now.blocked", role="admin", press=[SetCB(sec="dns", act="do", key="now")],
         data=_dns_blocked, title="«Переехать сейчас», когда идёт переезд — объяснение"),
    Shot("adm.set.mig_prep.go", role="admin", press=[SetCB(sec="mig_prep", act="do", key="go", val="443")],
         data=_mig_prepare(True), title="подготовка переезда: интерфейс поднят, перезапуск по кнопке"),
    Shot("adm.set.mig_prep.go.fail", role="admin", press=[SetCB(sec="mig_prep", act="do", key="go")],
         data=_mig_prepare(False), title="подготовка переезда: отказ — ничего не тронуто"),
]


# ── шаг 5: разделы настроек — ввод, почта, бэкапы, обновления, сервис ───────

def _mailbox(services):
    """Ящик сохранён мастером, вход ещё не проверялся."""
    who = _people(services)
    services.email_save("box@icloud.com", "DUMMY", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    return who


def _mail_fails(services):
    """Ящик подключён, но вход не проходит (итог записан, как пишет настоящая
    проверка), а тест-письмо отвергает SMTP; причины — с угловыми скобками."""
    from awgbot.infra import mail
    from awgbot.util import timeutil
    who = _email_on(services)

    def check(acc=None):
        services.db.set_state(services._MAIL_CHECK_KEY, f"fail|{timeutil.now_iso()}|IMAP: <auth> отказ")
        return False, "IMAP: <auth> отказ"

    def send():
        raise mail.MailError("SMTP 535 <bad>")
    services.email_check = check
    services.email_send_test = send
    return who


def _upd_scan_failed(services):
    """Проверка обновлений не дошла до списка релизов (сеть)."""
    who = _people(services)
    del services.update_scan                    # настоящая: признак сбоя ставит update_next

    def nxt():
        services.update_scan_failed = True
    services.update_next = nxt
    return who


def _upd_blocked(services):
    """Следующая версия найдена, но ставить её сейчас нельзя."""
    who = _upd_found(services)
    services.update_block_reason = lambda f: "идёт переезд на поколение 2"
    return who


def _upd_huge(services):
    """Список изменений следующей версии не влезает в сообщение."""
    import types
    who = _upd_found(services)
    found = types.SimpleNamespace(tag="v1.3.0", name="v1.3.0", url="https://example.org/r/v1.3.0",
                                  body="\n".join(f"- пункт номер {i} с подробным текстом" for i in range(150)))
    services.update_scan = lambda: found
    services.update_next = lambda: found
    return who


def _awg_restart_fails(services):
    who = _restarts(services)
    services.restart_service = _raise("runtime", "docker: <no such container>")
    return who


_MAIL_CORP = [("press", SetCB(sec="email", act="do", key="setup")), ("text", "box@corp.example")]
_BACKUP_WHEN = [("press", SetCB(sec="backup", act="edit", key="backup_when"))]

SHOTS += [
    Shot("adm.set.mon.edit", role="admin",
         press=[SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes")], data=_people,
         conf={"app.scheduler.monitor_minutes": 3}, title="частота опроса: приглашение с текущим и границами"),
    Shot("adm.set.mon.edit.bad", role="admin",
         press=[SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes")], text="0", data=_people,
         title="частота опроса: 0 — переспрос"),
    Shot("adm.set.mon.quiet", role="admin", press=[SetCB(sec="mon")], data=_people,
         conf={"app.scheduler.monitor_minutes": 3, "app.monitoring.alert_streak": 5,
               "app.monitoring.service_failure_alert_minutes": 5,
               "app.monitoring.service_failure_alert_loud": False},
         title="мониторинг: простой AWG — по правилам тихих часов"),
    Shot("adm.set.fw.add.many", role="admin", press=[SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")],
         data=_fw(raw_allow=[f"203.0.113.{i}" for i in range(1, 8)], allow=[f"203.0.113.{i}" for i in range(1, 8)]),
         title="адрес: приглашение — пять текущих и «и ещё 2»"),
    Shot("adm.set.email.unchecked", role="admin", press=[SetCB(sec="email")], data=_mailbox,
         conf={"email.resume_enabled": False},
         title="e-mail: вход ещё не проверялся, аварийный выход выключен"),
    Shot("adm.set.email.bad_host", role="admin", steps=_MAIL_CORP + [("text", "imap.corp.example:0")],
         data=_people, title="мастер почты: негодный порт в «сервер:порт» — переспрос"),
    Shot("adm.set.email.bare_host", role="admin", steps=_MAIL_CORP + [("text", "imap.corp.example")],
         data=_people, title="мастер почты: голое имя сервера — шаг порта"),
    Shot("adm.set.email.check.fail", role="admin", press=[SetCB(sec="email", act="do", key="check")],
         data=_mail_fails, title="проверка соединения: отказ в шапке раздела"),
    Shot("adm.set.email.test.fail", role="admin", press=[SetCB(sec="email", act="do", key="test")],
         data=_mail_fails, title="тестовое письмо: отказ первой строкой раздела"),
    Shot("adm.set.email.cycle.poll", role="admin",
         press=[SetCB(sec="email", act="cycle", key="email.poll_interval_sec")], data=_email_on,
         title="опрос ящика циклом: 1 → 5 мин"),
    Shot("adm.set.email.cycle.code", role="admin",
         press=[SetCB(sec="email", act="cycle", key="email.resume_code_len")], data=_email_on,
         title="длина кода циклом: 8 → 12"),
    Shot("adm.set.backup.when.bad", role="admin", steps=_BACKUP_WHEN + [("text", "31 12")], data=_people,
         title="день и час: день за 28 — переспрос"),
    Shot("adm.set.upd.failed", role="admin", press=[SetCB(sec="upd")], data=_upd_scan_failed,
         title="обновления: проверка не удалась"),
    Shot("adm.set.upd.blocked", role="admin", press=[SetCB(sec="upd")], data=_upd_blocked,
         title="обновления: новая есть, но сейчас недоступна — строка, а не кнопка"),
    Shot("adm.set.upd.huge", role="admin", press=[SetCB(sec="upd")], data=_upd_huge,
         title="обновления: список изменений обрезан со ссылкой на релиз"),
    Shot("adm.set.svc.awg.fail", role="admin", press=[SetCB(sec="svc", act="do", key="awg!")],
         data=_awg_restart_fails, title="AWG не перезапущен — первой строкой раздела"),
]


# ── шаг 5: карточки профиля и устройства, правки профиля — ветки из e2e ────

def _ks(**fields):
    """_people, поля профиля Ксюши поверх (лимит трафика, статус, блок)."""
    def build(services):
        from awgbot.core.blocks import ClientBlock
        who = _people(services)
        block = fields.pop("_block", 0)
        if fields:
            services.db.update_client_fields(KS, **fields)
        if block:
            services._client_set_block(KS, ClientBlock(block))
        return who
    return build


def _ks_many_paused(services):
    """Девять устройств у Ксюши без лимита, она на паузе, РФ разрешён."""
    who = _ks_many(services)
    services.db.update_client_fields(KS, device_limit=0)
    ok, *_ = services.enter_pause(KS, 7)
    assert ok
    return who


FOREVER = 6                             # бессрочный профиль после _people


def _forever(services):
    """Бессрочный профиль без лимита устройств."""
    who = _people(services)
    assert _client(services, "Вечный", 2030, kind="never", limit=0) == FOREVER
    return who


def _br_expired_status(services):
    """Подписка Бори кончилась, сроки уже отметили её истёкшей."""
    who = _br_expired(services)
    services.db.update_client_fields(BR, status="expired")
    return who


def _zero_limited(services):
    """Активированный профиль с лимитом 100 ГБ и без трафика."""
    who = _people(services)
    assert _client(services, "Новенькая", 2031, traffic_gb=100) == FOREVER
    return who


WATCH = 7                               # «Часы» Ксюши без трафика (после _people)


def _dev_limits(dev_limit: int, profile_limit: int, used: bool):
    """Устройство Ксюши со своим лимитом и лимитом профиля в ГБ: used —
    «Ноутбук» с трафиком за месяц, иначе — новые «Часы» без трафика."""
    def build(services):
        who = _people(services)
        services.db.update_client_fields(KS, traffic_limit=profile_limit * GB)
        dev = LAPTOP if used else services.add_device(KS, "Часы").device_id
        assert dev == (LAPTOP if used else WATCH)
        services.db.update_device_fields(dev, traffic_limit=dev_limit * GB)
        return who
    return build


_NAME = [("press", ClientCB(action="edit_name", client_id=KS))]

SHOTS += [
    Shot("adm.cl.traffic_limit", role="admin", press=[ClientCB(action="open", client_id=KS)],
         data=_ks(traffic_limit=100 * GB), title="карточка: трафик против лимита профиля"),
    Shot("adm.cl.traffic_blocked", role="admin", press=[ClientCB(action="open", client_id=KS)],
         data=_ks(traffic_limit=100 * GB, _block=4 | 16),
         title="карточка: ручной блок и исчерпанный трафик — разными строками"),
    Shot("adm.cl.traffic_out", role="admin", press=[ClientCB(action="open", client_id=KS)],
         data=_ks(traffic_limit=100 * GB, _block=4),
         title="карточка: трафик исчерпан, ручного блока нет"),
    Shot("adm.cl.zero_limited", role="admin", press=[ClientCB(action="open", client_id=FOREVER)],
         data=_zero_limited, title="карточка: ноль трафика при лимите"),
    Shot("adm.cl.expired.status", role="admin", press=[ClientCB(action="open", client_id=BR)],
         data=_br_expired_status, title="карточка: истёкшая, доступ приостановлен"),
    Shot("adm.cl.many.paused", role="admin", press=[ClientCB(action="open", client_id=KS)],
         data=_ks_many_paused, title="карточка: пауза и РФ — устройства всё равно свёрнуты"),
    Shot("adm.cl.devices.unlimited", role="admin", press=[ClientCB(action="devices", client_id=KS)],
         data=_ks_many_paused, title="устройства профиля без лимита — просто число"),
    Shot("adm.cl.migration", role="admin", press=[ClientCB(action="open", client_id=KS)], data=_mig_spread,
         title="карточка во время переезда — строка последней"),
    Shot("adm.cl.edit.forever", role="admin", press=[ClientCB(action="edit", client_id=FOREVER)],
         data=_forever, title="«✏️ Изменить» бессрочного без лимита устройств"),
    Shot("adm.cl.name.empty", role="admin", steps=_NAME + [("text", "  ")], data=_people,
         title="имя профиля: пустое — переспрос"),
    Shot("adm.cl.name.cancel", role="admin", steps=_NAME + [("press", CancelCB(kind="edit", ref=KS))],
         data=_people, title="имя профиля: «✖️ Отмена» — «✏️ Изменить»"),
    Shot("adm.cl.period.bad", role="admin", steps=_PERIOD[:1] + [("text", "вчера")], data=_people,
         title="период: не дата — переспрос"),
    Shot("adm.new.devs.bad", role="admin",
         steps=_NEWP + [("press", PresetCB(kind="new_devs", val=-1)), ("text", "семь")], data=_people,
         title="новый профиль: число устройств словом — переспрос"),
    Shot("adm.new.stale", role="admin", press=[PeriodCB(kind="month", ctx="create")], data=_people,
         title="срок нового профиля без диалога — главная"),
    Shot("adm.link.cl.admin", role="admin", start=f"cl-{ADM}", data=_people,
         title="ссылка на карточку своего профиля — главная"),
    Shot("adm.dev.limit.profile", role="admin", press=[DeviceCB(action="open", device_id=LAPTOP)],
         data=_dev_limits(0, 100, True), title="карточка устройства: лимит профиля"),
    Shot("adm.dev.limit.own", role="admin", press=[DeviceCB(action="open", device_id=LAPTOP)],
         data=_dev_limits(50, 100, True), title="карточка устройства: свой лимит важнее профильного"),
    Shot("adm.dev.limit.zero", role="admin", press=[DeviceCB(action="open", device_id=WATCH)],
         data=_dev_limits(50, 0, False), title="карточка устройства: ноль при своём лимите"),
    Shot("adm.dev.reassign.owned", role="admin", press=[DeviceCB(action="reassign", device_id=LAPTOP)],
         data=_people, title="перенос устройства профиля: чьё оно"),
    Shot("adm.dev.reassign.owned.go", role="admin",
         press=[ReassignCB(device_id=LAPTOP, client_id=PT, stage="go")], data=_people,
         title="перенос между профилями: оба ссылками"),
    Shot("adm.dev.block.silent", role="admin",
         press=[BlockCB(target="dev", action="block", ref=PHONE, kind="silent")], data=_people,
         title="тихий блок устройства — всплывашка с «(тихо)»"),
]


# ── шаг 5: главная — «В меню» и строка РФ под трафиком ──────────────────────

def _rf_home(*, people: bool = False, enabled: bool = True, rx: int = 0, error: str = "",
             username: bool = True):
    """Главная со строкой РФ: функция развёрнута и включена/выключена; rx —
    РФ-итог месяца (иначе — как у построителя); error — учёт сломан (как
    его оставил опрос); username — имя бота известно."""
    def build(services, mp):
        who = _people(services) if people else _base(services)
        _routing(services, mp, enabled=enabled)
        if rx:
            services.db.set_state("rf_month_rx", str(rx))
            services.db.set_state("rf_month_tx", "0")
        if error:
            services.db.set_state("rf_acct_error", error)
        if not username:
            services.bot_username = ""
        return who
    return build


SHOTS += [
    Shot("adm.main.menu", role="admin", press=[Menu(action="main")], data=_people,
         title="«⬅️ В меню» — главная поверх экрана"),
    Shot("adm.cl.delete.one", role="admin", press=[ClientCB(action="delete", client_id=BR)], data=_people,
         title="удалить профиль с одним устройством?"),
    Shot("adm.main.rf_zero", role="admin", start="", data=_rf_home(),
         title="главная: функция включена — «0 ГБ» РФ под нулевым трафиком, без ссылок"),
    Shot("adm.main.rf_kept", role="admin", start="", data=_rf_home(people=True, enabled=False),
         title="главная: функция выключена, а РФ за месяц был — строка остаётся"),
    Shot("adm.main.rf_broken", role="admin", start="", data=_rf_home(
         people=True, error="nft не найден — поставь пакет nftables"),
         title="главная: учёт РФ сломан — пометка без текста ошибки ядра"),
    Shot("adm.main.rf_only", role="admin", start="", data=_rf_home(rx=GB),
         title="главная: трафика нет, а РФ есть — ссылок нет"),
    Shot("adm.main.no_username", role="admin", start="", data=_rf_home(people=True, username=False),
         title="главная: имя бота неизвестно — подписи без ссылок"),
]


# ── шаг 5: устройства у админа — лимиты, выдача, гонки ─────────────────────

def _self_limited(services):
    """У профиля админа (вопреки обыкновению) лимит 2, и он занят."""
    who = _people(services)
    services.db.update_client_fields(ADM, device_limit=2)
    return who


def _gw_only(services, mp):
    """У админа единственное устройство — шлюз: выдавать нечего."""
    who = _base(services)
    _routing(services, mp)
    dev = services.add_device(ADM, "NASPi").device_id
    services.db.gateway_add(dev, "awglink", 443, "10.99.99.0/30", slot_id=1)
    return who


def _ks_friend_pending(services):
    """Ксюша отдала «Ноутбук» другу, тот ещё не принял приглашение."""
    who = _people(services)
    services.make_device_friendly(LAPTOP)
    return who


def _bc_marks(services):
    """Адресаты с продлением: бессрочный профиль и истёкший (со статусом)."""
    who = _bc_ready(services)
    assert _client(services, "Вечный", 2030, kind="never", limit=0) == FOREVER
    services.db.update_client_fields(BR, period_start=_iso(NOW - _dt.timedelta(days=31)),
                                     period_end=_iso(NOW - _dt.timedelta(days=1)), status="expired")
    return who


async def _ev_slot_taken(services, bot):
    """Пока админ вводил имя, последний слот Ксюши занял кто-то ещё."""
    services.add_device(KS, "Часы")


SHOTS += [
    Shot("adm.cl.limit.preset.low", role="admin", press=[PresetCB(kind="cli_devs", ref=KS, val=1)],
         data=_people, title="лимит устройств пресетом ниже занятого — сразу, с ⚠️"),
    Shot("adm.cl.limit.other.bad", role="admin",
         steps=[("press", PresetCB(kind="cli_devs", ref=KS, val=-1)), ("text", "много")], data=_people,
         title="лимит устройств своим числом: не число — переспрос"),
    Shot("adm.self.gen_qr", role="admin", press=[AdminSelfCB(action="gen_qr")], data=_people_gen,
         title="кнопка старого образца «QR»: выбор своего устройства"),
    Shot("adm.self.gen_file", role="admin", press=[AdminSelfCB(action="gen_file")], data=_people_gen,
         title="кнопка старого образца «Файл»: выбор своего устройства"),
    Shot("adm.self.gen_link.gw_only", role="admin", press=[AdminSelfCB(action="gen_link")], data=_gw_only,
         title="кнопка старого образца «Ссылка»: только шлюз — выдавать нечего"),
    Shot("adm.self.gen_link.gw", role="admin", press=[AdminSelfCB(action="gen_link")], data=_slots(1),
         title="кнопка старого образца «Ссылка»: шлюза в выборе нет"),
    Shot("adm.cl.gen_for.admin", role="admin", press=[ClientCB(action="gen_for", client_id=ADM)],
         data=_people, title="старая «Выдать конфиг» у профиля админа — главная"),
    Shot("adm.self.add.full", role="admin", press=[AdminSelfCB(action="add")], data=_self_limited,
         title="своё устройство при занятом лимите — сколько удалить"),
    Shot("adm.cl.add_device.race", role="admin",
         steps=[("press", ClientCB(action="add_device", client_id=KS)), ("call", _ev_slot_taken),
                ("text", "Планшет")], data=_people,
         title="устройство профилю: слот заняли, пока вводилось имя"),
    Shot("adm.dev.friend_pending", role="admin", press=[DeviceCB(action="open", device_id=LAPTOP)],
         data=_ks_friend_pending, title="карточка устройства с приглашением другу — без перевыдачи"),
    Shot("adm.dev.connect_menu.unmanaged", role="admin",
         press=[DeviceCB(action="connect_menu", device_id=ALIEN)], data=_people,
         title="старая кнопка «Данные для подключения» у пира без ключа"),
    Shot("adm.bc.ext.marks", role="admin", press=[BroadcastCB(action="pick"), BroadcastCB(action="ext")],
         data=_bc_marks, title="с продлением: пометки ∞ и 🟡 у адресатов"),
]


# ── шаг 5: ввод нового профиля, трафик своим числом, кнопки прежнего образца ─

SHOTS += [
    Shot("adm.new.name.empty", role="admin", steps=[("press", Menu(action="add_client")), ("text", "   ")],
         data=_people, title="новый профиль: пустое имя — переспрос"),
    Shot("adm.cl.traffic.other", role="admin", press=[PresetCB(kind="cli_traffic", ref=KS, val=-1)],
         data=_ks(traffic_limit=50 * GB), title="лимит трафика: «✏️ Другое» — приглашение"),
    Shot("adm.cl.traffic.other.done", role="admin", press=[PresetCB(kind="cli_traffic", ref=KS, val=-1)],
         text="70", data=_ks(traffic_limit=50 * GB), title="лимит трафика своим числом — итог в «✏️ Изменить»"),
    Shot("adm.cl.gen_for", role="admin", press=[ClientCB(action="gen_for", client_id=KS)], data=_people,
         title="старая «Выдать конфиг» профиля — карточка с устройствами"),
    Shot("adm.gen.link.client", role="admin", press=[DeviceCB(action="gen_link", device_id=PHONE)],
         data=_people_gen, title="ссылка устройства профиля"),
    Shot("adm.dev.connect_menu", role="admin", press=[DeviceCB(action="connect_menu", device_id=PHONE)],
         data=_people, title="старая кнопка «Данные для подключения» — карточка с рядом выдачи"),
]


# ── шаг 5: продление — ветки из e2e ─────────────────────────────────────────

def _br_grace_week(services):
    """Боря брал отсрочку ровно на неделю."""
    who = _people(services)
    services.db.update_client_fields(BR, grace_pending_cut=7 * 86400)
    return who


SHOTS += [
    Shot("adm.cl.extend.keep_on", role="admin",
         press=[ClientCB(action="extend", client_id=KS),
                PeriodCB(kind="keep_tgl", ctx="extend", ref=KS, keep=0),
                PeriodCB(kind="keep_tgl", ctx="extend", ref=KS, keep=1)],
         data=_people, title="продление: тумблер остатка включён обратно"),
    Shot("adm.cl.extend.forever", role="admin", press=[ClientCB(action="extend", client_id=FOREVER)],
         data=_forever, title="продление бессрочной — без тумблера остатка"),
    Shot("adm.cl.extend.never", role="admin", press=[PeriodCB(kind="never", ctx="extend", ref=KS, keep=1)],
         data=_people, title="продление «∞» — подписка бессрочная"),
    Shot("adm.cl.extend.grace_week", role="admin", press=[ClientCB(action="extend", client_id=BR)],
         data=_br_grace_week, title="продление при долге отсрочки в неделю — «Недели» нет"),
    Shot("adm.cl.extend_exp.more", role="admin",
         press=[ClientCB(action="extend_exp", client_id=KS), PeriodCB(kind="month", ctx="extend", ref=KS)],
         data=_two_expiring, title="продлено из «Истекают» — в списке остался другой"),
]


# ── шаг 5: блок профиля без паузы и отмена ──────────────────────────────────

SHOTS += [
    Shot("adm.cl.block.pause_no", role="admin", press=[BlockCB(target="cli", action="pause_no", ref=KS)],
         data=_people, title="блок профиля: уведомить? (без паузы)"),
    Shot("adm.cl.block.silent", role="admin",
         press=[BlockCB(target="cli", action="block", ref=KS, kind="silent", days=-1)], data=_people,
         title="профиль заблокирован тихо, без паузы"),
    Shot("adm.cl.block.cancel", role="admin", press=[BlockCB(target="cli", action="cancel", ref=KS)],
         data=_people, title="блок профиля: «⬅️ Отмена» — карточка"),
]


# ── шаг 5: почта и бэкапы у основного — ветки из e2e ───────────────────────

def _backup_to_mail(services):
    """Бэкапы на e-mail: ящик подключён, шифрование включено; копия и письмо
    — без диска и сети."""
    who = _email_on(services)
    services.backup_set_passphrase("DUMMY-passphrase-1234")
    services.make_backup = lambda: ["/nonexistent/DUMMY-a.enc", "/nonexistent/DUMMY-b.enc"]
    services.email_send_backup = lambda paths: None
    return who


_SETUP = [("press", SetCB(sec="email", act="do", key="setup"))]

SHOTS += [
    Shot("adm.set.email.setup.icloud", role="admin", steps=_SETUP + [("text", "box@icloud.com")],
         data=_people, title="мастер почты: iCloud — пароль приложения"),
    Shot("adm.set.email.port.bad", role="admin",
         steps=_MAIL_CORP + [("text", "imap.corp.example"), ("text", "99999")], data=_people,
         title="мастер почты: порт IMAP вне диапазона — переспрос"),
    Shot("adm.set.backup.now.email", role="admin", press=[SetCB(sec="backup", act="do", key="now")],
         data=_backup_to_mail, conf={"app.scheduler.backup_channel": "email"},
         title="копия сейчас — письмом на ящик"),
    Shot("adm.set.backup.enc_set.short", role="admin", steps=_PHRASE[:1] + [("text", "abc")], data=_people,
         title="фраза короче нужного — переспрос"),
]


# ── шаг 5: объявление — адресаты, дни, превью и отчёт — ветки из e2e ───────

FRIEND_TG = 2050                         # гость, которому Ксюша отдала «Ноутбук»


def _bc_holders(services):
    """Ксюша делится «Ноутбуком» с гостем: объявление уходит и ему."""
    who = _bc_ready(services)
    res = services.activate_friend(services.make_device_friendly(LAPTOP), tg_id=FRIEND_TG)
    assert res.ok, res.reason
    return who


def _bc_undelivered(services, mp):
    """Как _bc_holders, но Боря заблокировал бота: доставка ему не проходит
    (остальным — настоящей рассылкой)."""
    from awgbot.bot.handlers.admin import broadcast as bc
    who = _bc_holders(services)
    real = bc.broadcast

    async def broadcast(bot, tg_ids, text, photos=(), by_tg=None):
        ok, failed = await real(bot, [t for t in tg_ids if t != 2002], text, photos, by_tg)
        return ok, failed + (2002 in tg_ids)
    mp.setattr(bc, "broadcast", broadcast)
    return who


_BC = [("press", BroadcastCB(action="pick"))]
_BC_EXT = _BC + [("press", BroadcastCB(action="ext"))]
_BC_KS = [("press", BroadcastCB(action="tgl", ref=KS))]
_BC_NEXT = [("press", BroadcastCB(action="next"))]
_BC_TYPED = [("press", PresetCB(kind="bc_days", val=-1)), ("text", "10")]
_BC_SEND = [("text", "В субботу с 02:00 до 03:00 — работы на сервере"), ("press", BroadcastCB(action="send"))]

SHOTS += [
    Shot("adm.bc.partial", role="admin", steps=_BC + _BC_KS, data=_bc_ready,
         title="отмечены не все — «Выбрать все» остаётся ☑️"),
    Shot("adm.bc.by_hand", role="admin",
         steps=_BC + [("press", BroadcastCB(action="tgl", ref=c)) for c in (BR, KS, PT)], data=_bc_ready,
         title="все отмечены по одному — «✅ Выбрать все»"),
    Shot("adm.bc.next.none", role="admin", steps=_BC + _BC_NEXT, data=_bc_ready,
         title="«Далее» без адресатов — всплывашка"),
    Shot("adm.bc.blank", role="admin", steps=_BC_TEXT[:3] + [("text", "   ")], data=_bc_ready,
         title="пустое сообщение вместо объявления — переспрос с кнопкой"),
    Shot("adm.bc.preview.one", role="admin",
         steps=_BC + _BC_KS + _BC_NEXT + [("text", "В субботу с 02:00 до 03:00 — работы на сервере")],
         data=_bc_ready, title="превью одному профилю — адресат по имени"),
    Shot("adm.bc.send.one", role="admin", steps=_BC + _BC_KS + _BC_NEXT + _BC_SEND, data=_bc_ready,
         title="разослано одному профилю — отчёт"),
    Shot("adm.bc.send.holders", role="admin", steps=_BC + _BC_KS + _BC_NEXT + _BC_SEND, data=_bc_holders,
         title="разослано профилю и тем, с кем он делится устройствами"),
    Shot("adm.bc.send.undelivered", role="admin",
         steps=_BC + [("press", BroadcastCB(action="all"))] + _BC_NEXT + _BC_SEND, data=_bc_undelivered,
         title="разослано всем, держателям тоже; одному не доставлено"),
    Shot("adm.bc.next.unlimited", role="admin",
         steps=_BC_EXT + [("press", BroadcastCB(action="tgl", ref=FOREVER))] + _BC_NEXT, data=_bc_marks,
         title="с продлением отмечены одни бессрочные — продлевать некого"),
    Shot("adm.bc.next.ext.mixed", role="admin",
         steps=_BC_EXT + _BC_KS + [("press", BroadcastCB(action="tgl", ref=FOREVER))] + _BC_NEXT,
         data=_bc_marks, title="с продлением: дни, бессрочный — «без продления»"),
    Shot("adm.bc.days.one", role="admin",
         steps=_BC_EXT + _BC_KS + _BC_NEXT + [("press", PresetCB(kind="bc_days", val=7))], data=_bc_ready,
         title="с продлением одному профилю: приглашение к тексту"),
    Shot("adm.bc.days.bad", role="admin",
         steps=_BC_EXT + _BC_KS + _BC_NEXT + [("press", PresetCB(kind="bc_days", val=-1)), ("text", "400")],
         data=_bc_ready, title="свои дни продления вне границ — переспрос"),
    Shot("adm.bc.days.typed", role="admin", steps=_BC_EXT + _BC_KS + _BC_NEXT + _BC_TYPED, data=_bc_ready,
         title="свои дни продления — приглашение к тексту с кнопкой"),
    Shot("adm.bc.preview.ext.one", role="admin",
         steps=_BC_EXT + _BC_KS + _BC_NEXT + _BC_TYPED + [("text", "Спасибо за терпение")], data=_bc_ready,
         title="превью с продлением одному профилю на свои дни"),
    Shot("adm.bc.send.ext.marks", role="admin",
         steps=_BC_EXT + [("press", BroadcastCB(action="all"))] + _BC_NEXT
         + [("press", PresetCB(kind="bc_days", val=7)), ("text", "Спасибо за терпение"),
            ("press", BroadcastCB(action="send"))],
         data=_bc_marks, title="разослано с продлением: истёкшему — с текущей даты, бессрочному — без"),
]


# ── шаг 5: РФ-доступ профиля — отказы старых кнопок и правка чужого списка ──

def _ks_sites_revoked(services):
    """У Ксюши три сайта, а разрешение на РФ-доступ у неё уже отозвали."""
    who = _ks_sites(services)
    services.set_routing_allowed(KS, False)
    return who


def _ks_laptop_off(services):
    """РФ-доступ Ксюши: «Ноутбук» выключен вручную, «Телефон» включён."""
    who = _people(services)
    services.set_routing_device(LAPTOP, False)
    return who


SHOTS += [
    Shot("adm.rf.all.denied", role="admin", press=[RoutingCB(action="all", ref=BR)], data=_people,
         title="«все» у профиля без разрешения — отказ"),
    Shot("adm.rf.del.stale_idx", role="admin",
         press=[RoutingCB(action="del", ref=KS, idx=9, tag=_tag("kinopoisk.ru"))], data=_ks_sites,
         title="убрать сайт: номер за концом списка — отказ"),
    Shot("adm.rf.del.stale_tag", role="admin", press=[RoutingCB(action="del", ref=KS, idx=1, tag="deadbeef")],
         data=_ks_sites, title="убрать сайт: метка не та — отказ, сосед цел"),
    Shot("adm.rf.del.revoked", role="admin",
         press=[RoutingCB(action="del", ref=KS, idx=1, tag=_tag("kinopoisk.ru"))], data=_ks_sites_revoked,
         title="убрать сайт после отзыва разрешения — отказ"),
    Shot("adm.rf.sites.add.done", role="admin", press=[RoutingCB(action="add", ref=KS)], text="z.ru",
         data=_ks_sites, title="добавить сайт из «Сайтов» профиля — снова «Сайты», «Назад» в его раздел"),
    Shot("adm.rf.devs", role="admin", press=[RoutingCB(action="devs", ref=KS)], data=_ks_laptop_off,
         title="старое действие «устройства» — раздел профиля, одно устройство выключено"),
    Shot("adm.rf.dev.last", role="admin", press=[RoutingCB(action="dev", ref=LAPTOP)], data=_ks_laptop_off,
         title="включили вручную последнее — «✅ Выбрать все»"),
]


# ── шаг 5: карточки слотов и «Шлюзы» — ветки, которые держали e2e ──────────
# Сцены — как в tests/e2e/test_gateway_*_ui.py: два слота с VPN-транзитом
# (у NASPi 192.168.1.0/24, у Pi4 192.168.68.0/24), события канала линка —
# через services.gwlink_* в построителе, как их принял бы канал.

LAN1, LAN2 = "192.168.1.0/24", "192.168.68.0/24"


def _gw(n: int = 2, *then, lan2: bool = False, peers: bool | None = None, **kw):
    """_slots(n, **kw) и поверх — lan2 (VPN-транзит и подсети у обоих
    слотов), peers (тумблер связи подсетей) и шаги then(services, mp) по
    порядку."""
    def build(services, mp):
        from awgbot.core import settings
        who = _slots(n, **kw)(services, mp)
        if lan2:
            services.db.gateway_update(1, home_subnets=[LAN1], lan_mode=1)
            services.db.gateway_update(2, home_subnets=[LAN2], lan_mode=1)
        if peers is not None:
            settings.set_value("app.routing.peer_nets.enabled", peers)
        for step in then:
            step(services, mp)
        return who
    return build


# свои списки: слот 1 прислал 5 доменов в туннель и один напрямую, слот 2
# на связи и умеет синхронизацию — канон уходит ему

def _own_sent(services, mp):
    services.gwlink_own_in(1, "rx", [[i + 1, f"d{i}.com", "vpn", False] for i in range(5)]
                           + [[6, "shop.ru", "ru", False]])
    services.gwlink_session_opened(2, "3.1.0", 2)
    services.gwlink_own_hello_in(2, True)


def _own_ack(ok: bool = True, error: str = ""):
    def step(services, mp):
        digest = services.gwlink_own_for(services.db.gateway(2))[0]
        body = {"ok": True, "hash": digest, "n": 6} if ok else {"ok": False, "hash": digest, "error": error}
        services.gwlink_own_ack_in(2, body)
    return step


def _rename_pi4(services, mp):
    services.rename_device(PI4, "Pi & <2>")


def _own_old_agent(services, mp):
    services.gwlink_session_opened(2, "3.0.2", 2)
    services.gwlink_own_hello_in(2, False)
    services.set_gw_bot_identity(2, "pi2_gw_bot", "Шлюз <2> & co")


def _overlap(services, mp):
    services.db.gateway_update(2, home_subnets=[LAN1])


def _own_off(services, mp):
    services.gwlink_own_card = lambda gw: {"vpn": 0, "ru": 0, "state": "off", "error": ""}


# SMB соседей: слот 1 назвал сервер и ушёл со связи; слот 2 прислал снимок
# с тем, что у него стоит (peers — подсети соседей, уже применённые)

_NAS = {"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.1.10"}


def _smb_sent(peers: str = LAN1):
    def step(services, mp):
        services.gwlink_services_in(1, [_NAS])
        services.gwlink_session_closed(1)
        services.gwlink_snapshot_in(2, {"bundle": {"lan_mode": "1", "home_subnets": LAN2, "resolver": "10.9.1.1",
                                                   "peer_home_nets": peers, "admin_ips": "10.8.1.2"},
                                        "agent_version": "3.1.0", "link_contract": "1", "rev": 1}, 1, True)
    return step


def _smb_ack(ok: bool = True, error: str = ""):
    def step(services, mp):
        from awgbot.domain import gwservices
        h = gwservices.feed_hash([_NAS])
        services.gwlink_peer_services_ack_in(2, {"ok": True, "hash": h, "n": 1} if ok
                                             else {"ok": False, "hash": h, "error": error})
    return step


def _card(slot: int, data, title: str, sid: str):
    return Shot(sid, role="admin", press=[GwSlotCB(action="card", slot=slot)], data=data, title=title)


SHOTS += [
    # ── свои списки в карточке слота ─────────────────────────────────────
    _card(2, _gw(2, _own_sent, lan2=True), "свои списки: канон уходит на шлюз (домены — только числом)",
          "adm.gw.card.own.pending"),
    _card(2, _gw(2, _own_sent, _own_ack(), lan2=True, peers=True),
          "свои списки применены — строки судьбы нет, под ними связь подсетей", "adm.gw.card.own.applied"),
    _card(2, _gw(2, _own_sent, _own_ack(False, "<b>dnsmasq</b> & rc=1"), lan2=True),
          "свои списки: отказ шлюза, ошибка экранирована", "adm.gw.card.own.refused"),
    _card(2, _gw(2, _own_sent, _own_ack(False, ""), lan2=True),
          "свои списки: отказ без объяснения — без двоеточия", "adm.gw.card.own.refused.bare"),
    _card(2, _gw(2, _own_sent, _own_ack(False, "ошибка записи файла: нет места на диске"), lan2=True),
          "свои списки: поломка на шлюзе — не «не смог принять»", "adm.gw.card.own.broken"),
    _card(2, _gw(2, _rename_pi4, _own_sent, _own_ack(False, "ошибка записи файла: <i>&"), lan2=True),
          "свои списки: имя шлюза и текст поломки экранированы ровно раз", "adm.gw.card.own.broken.escaped"),
    _card(2, _gw(2, _own_old_agent, lan2=True),
          "свои списки: агент без синхронизации — обновить, ссылка на бота", "adm.gw.card.own.old_agent"),
    _card(2, _gw(2, lan2=True, peers=True),
          "связь подсетей включена: под строкой судьбы списков — «↔️ Связь подсетей ✅», SMB не найдены",
          "adm.gw.card.peers"),
    _card(2, _gw(2, _overlap, lan2=True, peers=False),
          "подсети слотов пересекаются: предупреждение своей строкой под строкой судьбы", "adm.gw.card.overlap"),
    _card(2, _gw(2, _own_off, lan2=True, peers=True),
          "синхронизация у слота не действует — строки «📋» нет", "adm.gw.card.own.off"),
    # ── SMB соседей в карточке слота ─────────────────────────────────────
    _card(2, _gw(2, _smb_sent(), lan2=True, peers=True), "SMB извне: записи уходят на шлюз",
          "adm.gw.card.smb.pending"),
    _card(2, _gw(2, _smb_sent(), _smb_ack(), lan2=True, peers=True), "SMB извне: на шлюзе, доступны",
          "adm.gw.card.smb.ok"),
    _card(2, _gw(2, _smb_sent(), _smb_ack(False, "<b>dnsmasq</b> & rc=1"), lan2=True, peers=True),
          "SMB извне: отказ шлюза, ошибка экранирована", "adm.gw.card.smb.refused"),
    _card(1, _gw(2, _smb_sent(), _smb_ack(False, "<b>dnsmasq</b> & rc=1"), lan2=True, peers=True),
          "SMB у публикующего: только «свои», о судьбе — ничего", "adm.gw.card.smb.own"),
    _card(2, _gw(2, _smb_sent(), _smb_ack(False, ""), lan2=True, peers=True),
          "SMB извне: отказ без объяснения — без двоеточия", "adm.gw.card.smb.refused.bare"),
    _card(2, _gw(2, _smb_sent(""), lan2=True, peers=True),
          "SMB извне: подсети соседей на шлюзе не применены — нужен перевыпуск", "adm.gw.card.smb.reissue"),
    _card(2, _gw(2, _rename_pi4, _smb_sent(), _smb_ack(False, ""), lan2=True, peers=True),
          "SMB извне: имя шлюза в заголовке и в отказе экранировано ровно раз", "adm.gw.card.smb.escaped"),
    _card(2, _gw(2, _smb_sent(), _smb_ack(False, "ошибка записи файла: нет места на диске"), lan2=True,
                 peers=True), "SMB извне: поломка на шлюзе — не «не смог принять»", "adm.gw.card.smb.broken"),
    _card(2, _gw(2, _rename_pi4, _smb_sent(), _smb_ack(False, "ошибка записи файла: <b>&</b>"), lan2=True,
                 peers=True), "SMB извне: имя и текст поломки экранированы", "adm.gw.card.smb.broken.escaped"),
    _card(2, _gw(2, _smb_sent(), lan2=True, peers=False), "связь подсетей выключена — строки SMB нет",
          "adm.gw.card.smb.off"),
    # ── рецепт роутера при связанных подсетях ────────────────────────────
    Shot("adm.gw.router.peers", role="admin", press=[GwSlotCB(action="router", slot=1)],
         data=_gw(2, lan2=True, peers=True), title="рецепт роутера: маршрут к подсети другого шлюза"),
    Shot("adm.gw.router.peers.ow", role="admin", press=[GwSlotCB(action="router", slot=1, val="ow")],
         data=_gw(2, lan2=True, peers=True), title="рецепт роутера OpenWrt: маршрут к подсети другого шлюза"),
    Shot("adm.gw.router.peers.two", role="admin", press=[GwSlotCB(action="router", slot=2)],
         data=_gw(2, lan2=True, peers=True), title="рецепт роутера резерва: маршрут к подсети первого"),
    Shot("adm.gw.router.nopeers", role="admin", press=[GwSlotCB(action="router", slot=1)],
         data=_gw(2, lan2=True, peers=False), title="связь подсетей выключена — маршрута к соседу нет"),
    Shot("adm.gw.router.nopeers.ow", role="admin", press=[GwSlotCB(action="router", slot=1, val="ow")],
         data=_gw(2, lan2=True, peers=False), title="OpenWrt: связь подсетей выключена — маршрута к соседу нет"),
    # ── связь подсетей: строка «Шлюзов», выключение ──────────────────────
    Shot("adm.gw.peer_ask.off", role="admin", press=[GwSlotCB(action="peer_ask")],
         data=_gw(2, lan2=True, peers=True), title="выключить связь подсетей?"),
    Shot("adm.gw.peer_yes.off", role="admin", press=[GwSlotCB(action="peer_yes")],
         data=_gw(2, lan2=True, peers=True), title="связь подсетей выключена — всплывашка о перевыпуске"),
]


# ── шаг 5: «Шлюзы», слоты и назначение — ветки из e2e ───────────────────────

TOKEN2 = "2222222222:DUMMYDUMMYDUMMYDUMMYDUMMY"


def _label(slot: int, label: str):
    def step(services, mp):
        services.db.gateway_update(slot, label=label)
    return step


def _token2(services, mp):
    """Бот второго слота уже известен серверу — токен спрашивать не нужно."""
    services._shot_tokens[2] = TOKEN2


def _active1(services, mp):
    """Активный слот записан (холодный старт его записывает): «⭐ При старте»
    без записи двигало бы и «активного» на экране."""
    services.db.set_state(services._RT_ACTIVE_KEY, "1")


def _pref2(services, mp):
    services.db.gateway_set_preferred(2)


def _standby_fails(services, mp):
    """Окно замеров резерва — сплошные отказы."""
    for _ in range(10):
        services._rt_window_push(2, False)


def _link_down(services, mp):
    services.routing_link_ok = lambda: False


def _no_streak(services, mp):
    services.db.set_state("routing_gw_2_up_streak", "")


def _switched(services, mp):
    services.gateway_switch(2, manual=True)


def _long_dead(services, mp):
    _standby_fails(services, mp)
    services.db.set_state("routing_gw_2_down_since",
                          str(int(NOW.timestamp()) - 13 * 86400 - 4 * 3600))


def _lan_standby(services, mp):
    services.db.gateway_update(2, home_subnets=[LAN2], lan_mode=1)


def _peer_no_lan(services, mp):
    services.db.gateway_update(2, lan_mode=0)


def _peer_no_nets(services, mp):
    services.db.gateway_update(2, home_subnets=[])


def _peer_overlap(services, mp):
    services.db.gateway_update(2, home_subnets=["192.168.1.0/25"])


def _token_kept(services, mp):
    """Запись env не удалась: токен снятого слота остался."""
    services.forget_gw_bot_token = lambda slot=None: None


def _slot7(services, mp):
    services.gateway_next_slot = lambda: (7, "awglink7", 9443, "10.99.99.24/30")


def _no_free_slot(services, mp):
    services.gateway_next_slot = _raise("service", "свободных слотов нет")


def _fresh_gw(services, mp):
    """Шлюз только что назначен: линк ещё ни разу не ответил."""
    services.routing_link_ok = lambda: False
    services._probe_slot = lambda g, active=False: "down"


def _allow_all(services, mp):
    for c in services.routing_grantable_clients():
        services.set_routing_allowed(c.id, True)


def _ceiling(services, mp):
    mp.setattr(config, "ROUTING_GATEWAYS_MAX", 1)


def _lists_aged(services, mp):
    """Списки: две записи из трёх источников, обновлены 2 ч назад, период 12 ч."""
    from awgbot.core import settings
    services._routing_read_cache = lambda name: ["a.ru", "b.ru"]
    services.db.set_state(services._RT_LISTS_KEY, str(int(NOW.timestamp()) - 7200))
    settings.set_value("app.routing.lists_refresh_hours", 12)
    mp.setattr(config, "ROUTING_LISTS_HOME_URLS", ["https://a.example", "https://b.example", "https://c.example"])


def _rt_none_token(services, mp):
    """Шлюза нет, токен бота первого слота уже известен."""
    who = _rt_none(services, mp)
    services._shot_tokens[1] = TOKEN
    return who


def _probe_down(services, mp):
    from awgbot.infra import routing as rt
    services.routing_probe = lambda: rt.PROBE_DOWN


async def _drop_slot2(services, bot):
    services.db.gateway_delete(2)


_PIN = "RERERERERERERERERERERERERERERERERERERERERERE="   # ключ аплинка Ксюшиного телефона — заглушка
_CLAIM_PRIV = base64.b64encode(b"DUMMY-CLAIM-KEY-FOR-SCREENS-32B!").decode()   # ключ линка под claim — заглушка, 32 байта


def _iphone_key(services, mp):
    services.db.update_device_fields(IPHONE, public_key=_PIN)
    services._link_privkey = lambda gw=None: _CLAIM_PRIV


def _claim_key(services, mp):
    services._link_privkey = lambda gw=None: _CLAIM_PRIV


def _claim_text(pub: str) -> str:
    """Токен claim, как его подписал бы агент, — с постоянными ts и nonce
    (gwsign.sign берёт случайный nonce, а эталону нужен один и тот же текст)."""
    import hashlib
    import hmac
    from awgbot.util import gwsign
    payload = json.dumps({"act": "claim", "pub": pub, "ts": int(NOW.timestamp()), "nonce": "00" * 8, "host": ""},
                         separators=(",", ":"), ensure_ascii=False).encode()
    mac = hmac.new(gwsign._key(_CLAIM_PRIV), payload, hashlib.sha256).digest()[:20]
    return gwsign.PREFIX + gwsign.b64u(payload) + "." + gwsign.b64u(mac)


_FW_UNRESOLVED = [f"h{i}.example" for i in range(15)]


SHOTS += [
    # ── подпись слота, пинг резерва, «⭐ При старте» ───────────────────────
    Shot("adm.rt.two.label", role="admin", press=[SetCB(sec="rt")], data=_gw(2, _label(2, "дача")),
         title="«Шлюзы»: подпись слота в скобках"),
    _card(2, _gw(2, _label(2, "дача")), "карточка: подпись слота в заголовке", "adm.gw.card.label"),
    _card(2, _gw(2, _lan_standby), "карточка резерва с VPN-транзитом — самый полный набор кнопок",
          "adm.gw.card.standby.lan"),
    Shot("adm.gw.ping.standby", role="admin", press=[GwSlotCB(action="ping", slot=2)], data=_slots(2),
         title="пинг резерва — свой линк"),
    Shot("adm.gw.pref.clear", role="admin", press=[GwSlotCB(action="pref", slot=2)], data=_gw(2, _active1, _pref2),
         title="«⭐ При старте» на предпочтительном — снять"),
    Shot("adm.gw.name", role="admin", press=[GwSlotCB(action="name", slot=1)], data=_slots(1),
         title="имя устройства слота: приглашение"),
    # ── переключение со списка и активного ──────────────────────────────
    Shot("adm.gw.switch_ask.list", role="admin", press=[GwSlotCB(action="switch_ask", slot=2, val="l")],
         data=_slots(2), title="переключить со списка — «Отмена» обратно в «Шлюзы»"),
    Shot("adm.gw.switch_yes.list", role="admin", press=[GwSlotCB(action="switch_yes", slot=2, val="l")],
         data=_slots(2), title="переключено со списка — снова «Шлюзы»"),
    Shot("adm.gw.switch_ask.active", role="admin", press=[GwSlotCB(action="switch_ask", slot=1)],
         data=_slots(2), title="переключить на активный — «уже идёт», без вопроса"),
    # ── ввод подсетей и подписи ──────────────────────────────────────────
    Shot("adm.gw.home.partial", role="admin", press=[GwSlotCB(action="home", slot=1)],
         text="192.168.1.0/24 мусор", data=_slots(2), title="подсети: часть не принята — названа в итоге"),
    Shot("adm.gw.label.clear", role="admin", press=[GwSlotCB(action="label", slot=2)], text="—",
         data=_gw(2, _label(2, "дача")), title="подпись: «—» убирает"),
    Shot("adm.gw.label.gone", role="admin",
         steps=[("press", GwSlotCB(action="label", slot=2)), ("call", _drop_slot2, "слот снят"),
                ("text", "дом 2")], data=_slots(2), title="подпись: слот исчез, пока висело приглашение"),
    # ── второй слот: новая машина и из моих ──────────────────────────────
    Shot("adm.rt.new_ask", role="admin", press=[GwMarkCB(action="new_ask")], data=_rt_none,
         title="новая машина — сразу токен, без подтверждения"),
    Shot("adm.rt.new_ask.standby", role="admin", press=[GwMarkCB(action="new_ask", slot=0)], data=_slots(1),
         title="резерв новой машиной — токен бота шлюза 2"),
    Shot("adm.rt.new_ask.full", role="admin", press=[GwMarkCB(action="new_ask", slot=0)],
         data=_gw(1, _no_free_slot), title="свободных слотов нет — отказ всплывашкой"),
    Shot("adm.rt.new_yes.slot7", role="admin", press=[GwMarkCB(action="new_yes", slot=0)],
         data=_gw(1, _slot7), title="номер нового слота — от сервиса (7)"),
    Shot("adm.rt.new_yes.known", role="admin", press=[GwMarkCB(action="new_yes")],
         data=_rt_none_token, title="новая машина при известном токене — не спрашивает"),
    Shot("adm.rt.new_yes.standby", role="admin", press=[GwMarkCB(action="new_yes", slot=0)],
         data=_gw(1, _token2), title="резерв новой машиной при известном токене — файл «Шлюз 2»"),
    Shot("adm.rt.token.done.standby", role="admin", press=[GwMarkCB(action="new_ask", slot=0)], text=TOKEN2,
         data=_slots(1), title="токен второго слота принят — файл и скрипт слота 2"),
    Shot("adm.rt.pick_list.standby", role="admin", press=[GwMarkCB(action="pick_list", slot=0)],
         data=_slots(1), title="резерв из моих — без назначенного"),
    Shot("adm.rt.pick.standby", role="admin", press=[GwMarkCB(action="pick", device_id=PI4, slot=0)],
         data=_slots(1), title="резерв из моих: станет резервным?"),
    Shot("adm.rt.mark_yes.standby", role="admin", press=[GwMarkCB(action="mark_yes", device_id=PI4, slot=0)],
         data=_gw(1, _token2), title="резерв из моих при известном токене — инструкция и файл"),
    Shot("adm.rt.mark_yes.standby.menu", role="admin",
         press=[GwMarkCB(action="mark_yes", device_id=PI4, slot=0),
                SetCB(sec="rt", act="do", key="bundle_cancel", val="2")],
         data=_gw(1, _token2), title="«🛰 В карточку» под файлом первого применения — карточка слота 2"),
    # ── снятие ───────────────────────────────────────────────────────────
    Shot("adm.gw.remove_ask.old", role="admin", press=[GwMarkCB(action="remove_ask", device_id=NASPI)],
         data=_slots(1), title="снять последний шлюз старой кнопкой — РФ-доступ выключится"),
    Shot("adm.gw.remove_yes.last", role="admin", press=[GwSlotCB(action="remove_yes", slot=1)],
         data=_slots(1), title="последний шлюз снят — РФ-доступ выключен до назначения"),
    Shot("adm.gw.remove_yes.old", role="admin", press=[GwMarkCB(action="remove_yes")], data=_slots(1),
         title="снять единственный старой кнопкой устройства"),
    Shot("adm.gw.remove_yes.none", role="admin", press=[GwMarkCB(action="remove_yes")], data=_rt_none,
         title="снять, когда шлюза нет, — «и так не назначен»"),
    Shot("adm.gw.remove_yes.token_kept", role="admin", press=[GwSlotCB(action="remove_yes", slot=2)],
         data=_gw(2, _token_kept), title="резерв снят, токен его бота остался в env"),
    # ── конфигурация файлом ──────────────────────────────────────────────
    Shot("adm.gw.bundle.label", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _label(2, "дача"), token=False), title="файл слота 2: подпись слота, бот неизвестен"),
    # ── параметры циклами ────────────────────────────────────────────────
    Shot("adm.rt.params.cycle.window", role="admin",
         press=[SetCB(sec="rt_params", act="cycle", key="app.routing.failover.window_samples")],
         data=_slots(1), title="окно циклом — порог пересчитан от окна"),
    Shot("adm.rt.params.cycle.threshold", role="admin",
         press=[SetCB(sec="rt_params", act="cycle", key="app.routing.failover.min_availability")],
         data=_slots(1), title="порог циклом"),
    Shot("adm.rt.params.cycle.lists", role="admin",
         press=[SetCB(sec="rt_params", act="cycle", key="app.routing.lists_refresh_hours")],
         data=_slots(1), title="период списков циклом"),
    Shot("adm.rt.params.lists_age", role="admin", press=[SetCB(sec="rt_params")], data=_gw(1, _lists_aged),
         title="параметры: списки — записи, источники, возраст, период"),
    Shot("adm.rt.lists.old", role="admin", press=[SetCB(sec="rt_lists")], data=_slots(1),
         title="старый подраздел «Списки» — «⚙️ Параметры»"),
    Shot("adm.rt.mon.old", role="admin", press=[SetCB(sec="rt_mon")], data=_slots(1),
         title="старый подраздел «Мониторинг» — «⚙️ Параметры»"),
    Shot("adm.rt.lists.off", role="admin", press=[SetCB(sec="rt_lists")], data=_rt_off,
         title="старый подраздел при выключенном РФ-доступе — раздел пуст"),
    Shot("adm.rt.bundle.old", role="admin", press=[SetCB(sec="rt_bundle", key="1")], data=_slots(1),
         title="старый экран перед выпуском файла — его больше нет"),
    # ── строка РФ-доступа на главной ─────────────────────────────────────
    Shot("adm.main.gw.label", role="admin", start="", data=_gw(1, _label(1, "дом 1")),
         title="главная: один шлюз с подписью"),
    Shot("adm.main.gw.checking", role="admin", start="", data=_gw(2, _no_streak),
         title="главная: резерв ещё проверяется"),
    Shot("adm.main.gw.standby_dead", role="admin", start="", data=_gw(2, _standby_fails),
         title="главная: резерв не отвечает (🟠)"),
    Shot("adm.main.gw.both_down", role="admin", start="", data=_gw(2, _standby_fails, _link_down),
         title="главная: оба шлюза не отвечают"),
    Shot("adm.main.gw.down.standby_alive", role="admin", start="", data=_gw(2, _link_down),
         title="главная: активный не отвечает, резерв жив"),
    Shot("adm.main.gw.slot2", role="admin", start="", data=_gw(2, _switched),
         title="главная: активен слот 2 — ссылки в свои карточки"),
    # ── VPN-транзит ──────────────────────────────────────────────────────
    Shot("adm.gw.lan_yes.off", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="0")],
         data=_slots(1, lan=True), title="VPN-транзит выключен — «❓ Роутер» ушёл"),
    Shot("adm.gw.lan_yes.again", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="1")],
         data=_slots(1, lan=True), title="повтор «Включить» при включённом — «Уже включено»"),
    # ── связь подсетей в «Шлюзах» ────────────────────────────────────────
    Shot("adm.rt.peers.ok", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _label(2, "дача"), lan2=True, peers=True), title="связь подсетей работает — парами"),
    Shot("adm.rt.peers.no_lan", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _peer_no_lan, lan2=True, peers=True), title="связь подсетей: у слота нет VPN-транзита"),
    Shot("adm.rt.peers.no_nets", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _peer_no_nets, lan2=True, peers=True), title="связь подсетей: у слота нет подсетей"),
    Shot("adm.rt.peers.overlap", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _peer_overlap, lan2=True, peers=True), title="связь подсетей: подсети пересекаются"),
    # ── автопереключение ─────────────────────────────────────────────────
    Shot("adm.gw.failover.off", role="admin", press=[GwSlotCB(action="failover")], data=_slots(2),
         title="автопереключение выключено — тумблер ☑️ и последствия строкой"),
    Shot("adm.rt.mon.failover", role="admin",
         press=[SetCB(sec="rt_mon", act="toggle", key="app.routing.failover.enabled")], data=_slots(2),
         title="тумблер автопереключения из старого «Мониторинга» — «Шлюзы»"),
    Shot("adm.rt.two.down.long", role="admin", press=[SetCB(sec="rt")], data=_gw(2, _long_dead),
         title="резерв лежит вторую неделю — сколько"),
    Shot("adm.rt.one.ceiling", role="admin", press=[SetCB(sec="rt")], data=_gw(1, _ceiling),
         title="один слот у потолка — без «➕ Резерв»"),
    Shot("adm.rt.one.fresh", role="admin", press=[SetCB(sec="rt")], data=_gw(1, _fresh_gw),
         title="только что назначенный шлюз — не зелёный"),
    Shot("adm.rt.enable.down", role="admin", press=[SetCB(sec="rt", act="toggle", key="app.routing.enabled")],
         data=_gw(1, _probe_down, enabled=False), title="включить РФ-доступ при лежащем шлюзе"),
    # ── «👥 Кому доступен»: выбрать все ──────────────────────────────────
    Shot("adm.rt.users.all", role="admin", press=[SetCB(sec="rt", act="do", key="allow_all")], data=_slots(1),
         title="«Выбрать все» — разрешено всем"),
    Shot("adm.rt.users.all.off", role="admin", press=[SetCB(sec="rt", act="do", key="allow_all")],
         data=_gw(1, _allow_all), title="«Выбрать все» при всех разрешённых — снято со всех"),
    # ── старые кнопки файла ──────────────────────────────────────────────
    Shot("adm.gw.bundle.old", role="admin", press=[SetCB(sec="rt", act="do", key="bundle")], data=_slots(1),
         title="«📤 Выпустить файл» прежней версии — тот же выпуск"),
    Shot("adm.gw.bundle.old.two", role="admin", press=[SetCB(sec="rt", act="do", key="bundle", val="2")],
         data=_slots(2), title="«📤 Выпустить файл» прежней версии для слота 2"),
    Shot("adm.gw.bundle.menu.old", role="admin", press=[SetCB(sec="rt", act="do", key="bundle_menu", val="2")],
         data=_slots(2), title="«В меню» под файлом до 3.1.0 — убирает сообщение, главная"),
    # ── пересланное сообщение агента ─────────────────────────────────────
    Shot("adm.claim.foreign", role="admin", text=_claim_text(_PIN), data=_gw(1, _iphone_key),
         title="claim другого устройства при назначенном шлюзе — не принято"),
    Shot("adm.claim.garbage", role="admin", text="GW1:abc.def", data=_gw(1, _claim_key), title="мусор вместо токена"),
    # ── файервол ВПС ─────────────────────────────────────────────────────
    Shot("adm.set.fw.unresolved", role="admin", press=[SetCB(sec="fw")],
         data=_fw(enabled=True, present=True, allow=[], raw_allow=_FW_UNRESOLVED, unresolved=_FW_UNRESOLVED,
                  admin_ips=["x"]), title="15 имён не резолвятся — блок предупреждений"),
]


# ── шаг 5: канал до шлюза в карточке слота — ветки из e2e ───────────────────
# Слот NASPi с VPN-транзитом, хендшейк линка свежий (сессия канала — «на
# связи»), резолвер слота — 10.9.1.1. Снимок кладётся так, как его принял бы
# канал; возраст снимка и часы малины — от NOW.

def _ch_base(services, mp):
    mp.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    services.gateway_resolver_addr = lambda g: "10.9.1.1" if g.lan_mode else ""


def _installed(services) -> dict:
    """Что стоит на шлюзе, когда всё доехало: ровно то, из чего ВПС соберёт файл."""
    addrs = " ".join(sorted(set(services.db.admin_device_addresses(config.ADMIN_ID))))
    return {"lan_mode": "1", "home_subnets": LAN1, "resolver": "10.9.1.1", "peer_home_nets": "", "admin_ips": addrs}


def _ch(bundle: dict | None = None, *, online: bool = True, closed: bool = False, age_h: float = 0,
        skew_min: int = 0, **kw):
    """Снимок слота 1: bundle — поверх установленного (None — всё доехало);
    online — сессия канала открыта; closed — закрыта после снимка; age_h —
    снимок принят столько часов назад; skew_min — часы малины спешат."""
    def step(services, mp):
        at = NOW - _dt.timedelta(hours=age_h)
        snap = {"bundle": {**_installed(services), **(bundle or {})}, "link_contract": "1", "plumbing_gen": "new",
                "mark_status": "confirmed", "agent_version": "3.1.0", "awg_generation": 1, "egress_ok": True,
                "boot_id": "b" * 36, "ts": _iso(at + _dt.timedelta(minutes=skew_min)), "rev": 1}
        snap.update(kw)
        services.gwlink_snapshot_in(1, snap, 1, True)
        services.db.set_state(services._gwlink_key(services._GWLINK_SNAP_AT_KEY, 1), _iso(at))
        if online:
            services.gwlink_session_opened(1, "3.1.0", 1)
        if closed:
            services.gwlink_session_closed(1)
    return step


def _ch_ack(ok: bool, error: str = ""):
    def step(services, mp):
        services.gwlink_ack_in(1, {"ok": ok, "error": error})
    return step


def _ch_old_agent(delta: bool = False):
    """Снимок агента, который не сообщает установленную конфигурацию."""
    def step(services, mp):
        services.gwlink_snapshot_in(1, {"agent_version": "3.0.0", "egress_ok": True, "ts": _iso(NOW), "rev": 1},
                                    1, True)
        services.gwlink_session_opened(1, "3.0.0", 1)
        if delta:
            services.gwlink_snapshot_in(1, {"plumbing_gen": "old", "rev": 2}, 2, False)
    return step


def _neighbour(services, mp):
    """Слот 2 с VPN-транзитом и подсетью 192.168.70.0/24, связь подсетей
    включена: у слота 1 появляются подсети соседа, которых на нём ещё нет."""
    services.db.gateway_update(2, home_subnets=["192.168.70.0/24"], lan_mode=1)


def _chan(n: int = 1, *steps, peers: bool | None = None):
    return _gw(n, _ch_base, *steps, lan=True, peers=peers)


_PEER_MISSING = {"ok": False, "missing": ["192.168.70.0/24", "192.168.71.0/24"]}


# ── шаг 5: бот шлюза и ссылки /start gw-N — ветки из e2e ────────────────────

def _bot(slot: int, username: str, name: str, token: str = TOKEN2):
    """Бот слота известен: токен есть, ответ getMe в кэше."""
    def step(services, mp):
        services._shot_tokens[slot] = token
        services.set_gw_bot_identity(slot, username, name)
    return step


def _token_replaced(services, mp):
    """Токен слота 2 сменили — прежний бот уже не тот."""
    services._shot_tokens[2] = "2222222222:DUMMYANOTHERDUMMYANOTHER"


def _snap_bot(username: str, name: str, rev: int = 1, full: bool = True):
    """Агент слота 2 прислал себя снимком канала (токена на сервере нет)."""
    def step(services, mp):
        body = {"agent_bot": {"username": username, "name": name}, "rev": rev}
        if full:
            body.update(agent_version="3.1.0", mark_status="confirmed", egress_ok=True)
        services.gwlink_snapshot_in(2, body, rev, full)
    return step


def _nav_live(services, mp):
    """В чате уже живое меню (#1) — карточка встанет на его место."""
    services.db.set_nav_message_id(config.ADMIN_ID, 1)


# ── шаг 5: файл конфигурации слота 2 в чате и итоги с шлюза ─────────────────

def _file2(plain: bool):
    """В чате админа лежит файл слота 2 (#40) под карточкой или инструкцией (#39)."""
    def step(services, mp):
        services.gw_bundle_msg_set(2, config.ADMIN_ID, 40, 39, "DUMMYFP", plain)
    return step


def _applied2(ok: bool, reason: str = ""):
    async def ev(services, bot):
        from awgbot.bot.handlers.settings import bundle_applied
        await bundle_applied(bot, services, 2, ok, reason, "DUMMYFP")
    return ev


async def _installed2(services, bot):
    from awgbot.bot.handlers.settings import bundle_installed
    await bundle_installed(bot, services, 2)


def _drop2(services, mp):
    services.db.gateway_delete(2)


_LABEL_CANCEL = CancelCB(kind="gwedit", ref=2)
_CARD2 = GwSlotCB(action="card", slot=2)

SHOTS += [
    # ── канал: связь, возраст, сверка ────────────────────────────────────
    _card(1, _chan(1, _ch()), "канал жив, конфигурация актуальна — одной строкой", "adm.gw.card.ch.live"),
    _card(1, _chan(1, _ch(online=False, closed=True, age_h=13 * 24)),
          "канал лежит — последнее известное с возрастом снимка", "adm.gw.card.ch.dead"),
    _card(1, _chan(1, _ch({"home_subnets": LAN2, "resolver": ""}, online=False, closed=True, age_h=2)),
          "канал лежит, конфигурация разошлась — пункты и возраст", "adm.gw.card.ch.drift.dead"),
    _card(1, _chan(1, _ch({"home_subnets": "<b>x</b> & y"}, online=False, closed=True)),
          "пункт расхождения с малины экранирован", "adm.gw.card.ch.drift.escaped"),
    _card(1, _chan(1, _ch({"home_subnets": LAN2, "resolver": ""})),
          "канал жив, расхождение уходит каналом", "adm.gw.card.ch.drift.live"),
    _card(1, _chan(1, _ch({"home_subnets": LAN2}), _ch_ack(False, "<b>LAN-интерфейс</b> не найден")),
          "шлюз не применил настройки — причина текстом", "adm.gw.card.ch.refused"),
    _card(1, _chan(1, _ch({"home_subnets": LAN2}, online=False), _ch_ack(False, "exit 1"), _ch(
        {"home_subnets": LAN2}, online=False, closed=True)),
          "старый отказ при лежащем канале — снова «расходится»", "adm.gw.card.ch.refused.dead"),
    _card(1, _chan(1, _ch(), _ch_ack(False, "exit 1"), _ch_ack(True)),
          "следующее применение прошло — отказа нет", "adm.gw.card.ch.refused.cleared"),
    _card(1, _chan(1, _ch(plumbing_gen="old", link_contract="")),
          "обвязка старого образца — строкой под каналом", "adm.gw.card.ch.plumbing.old"),
    _card(1, _chan(1, _ch(plumbing_gen="none")), "обвязка не развёрнута", "adm.gw.card.ch.plumbing.none"),
    _card(1, _chan(1, _ch(plumbing_gen="")), "образец обвязки не сообщён — строки нет",
          "adm.gw.card.ch.plumbing.unknown"),
    _card(1, _chan(1, _ch_old_agent()), "снимок без установленной конфигурации — не галочка",
          "adm.gw.card.ch.old_agent"),
    _card(1, _chan(1, _ch_old_agent(delta=True)), "снимок без установленного — и об обвязке не судим",
          "adm.gw.card.ch.old_agent.plumbing"),
    _card(1, _chan(1, _ch(awg_generation=7)), "поколение AWG на шлюзе другое", "adm.gw.card.ch.generation"),
    _card(1, _chan(1, _ch(agent_version="<b>3.1.0</b>")), "версия агента с разметкой — текстом",
          "adm.gw.card.ch.html"),
    _card(1, _chan(1, _ch(agent_version="3.1.0-" + "x" * 200)), "длинная версия агента обрезана",
          "adm.gw.card.ch.long_version"),
    _card(1, _chan(1, _ch(skew_min=10)), "часы шлюза спешат на 10 мин", "adm.gw.card.ch.clock"),
    _card(1, _chan(1, _ch(peer_nets=_PEER_MISSING)), "на шлюзе нет подсетей соседа — названы",
          "adm.gw.card.ch.peer_missing"),
    _card(1, _chan(1, _ch(peer_nets={"ok": True, "missing": []})), "вердикт связи подсетей исправен — строки нет",
          "adm.gw.card.ch.peer_ok"),
    _card(1, _chan(2, _neighbour, _ch(), peers=True),
          "подсети соседа не применены, канал жив — только файлом", "adm.gw.card.ch.neighbour.live"),
    _card(1, _chan(2, _neighbour, _ch(online=False, closed=True), peers=True),
          "подсети соседа не применены, канал лежит — пункт", "adm.gw.card.ch.neighbour.dead"),
    _card(1, _chan(2, _neighbour, _ch({"home_subnets": LAN2}), peers=True),
          "смешанное расхождение: что везёт канал, что — файл", "adm.gw.card.ch.neighbour.mixed"),
    # ── тумблеры при живом и лежащем канале ──────────────────────────────
    Shot("adm.gw.lan_ask.ch.live", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_chan(1, _ch()), title="выключить VPN-транзит при живом канале — без перевыпуска"),
    Shot("adm.gw.lan_yes.ch.live", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="0")],
         data=_chan(1, _ch()), title="VPN-транзит выключен — доедет каналом"),
    Shot("adm.gw.lan_ask.ch.dead", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_chan(1, _ch(online=False)), title="выключить VPN-транзит без канала — перевыпуск"),
    Shot("adm.gw.lan_yes.ch.dead", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="0")],
         data=_chan(1, _ch(online=False)), title="VPN-транзит выключен без канала — перевыпусти"),
    Shot("adm.gw.lan_yes.ch.peers", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="0")],
         data=_chan(1, _ch(), peers=True), title="VPN-транзит выключен каналом, но подсети соседей — файлом"),
    Shot("adm.gw.peer_ask.ch.live", role="admin", press=[GwSlotCB(action="peer_ask")],
         data=_chan(1, _ch(), peers=False), title="связать подсети при живом канале — всё равно перевыпуск"),
    Shot("adm.gw.peer_yes.ch.live", role="admin", press=[GwSlotCB(action="peer_yes", val="1")],
         data=_chan(1, _ch(), peers=False), title="подсети связаны при живом канале — перевыпусти"),
    # ── бот шлюза в карточках и подписях файла ───────────────────────────
    _card(2, _gw(2, _bot(2, "pi2_gw_bot", "Шлюз <Pi2>"), token=False),
          "бот слота известен — ссылка последней строкой, имя экранировано", "adm.gw.card.bot"),
    _card(1, _gw(2, _bot(2, "pi2_gw_bot", "Шлюз <Pi2>"), token=False),
          "бот известен только у соседнего слота — здесь строки нет", "adm.gw.card.nobot"),
    _card(2, _gw(2, _bot(2, "pi2_gw_bot", "")), "у бота нет имени — ссылка подписана username",
          "adm.gw.card.bot.username"),
    _card(2, _gw(2, _token_replaced), "токен слота сменили — ссылки на прежнего бота нет",
          "adm.gw.card.token_replaced"),
    _card(2, _gw(2, _snap_bot("pi2_gw_bot", "Шлюз <Pi2>"), token=False),
          "бот из снимка канала (токена на сервере нет)", "adm.gw.card.snapbot"),
    _card(2, _gw(2, _snap_bot("pi2_gw_bot", "Шлюз <Pi2>"), _snap_bot("pi2_gw_bot", "Новое имя", 2, False),
                 token=False), "бот из снимка переименован — дельтой", "adm.gw.card.snapbot.renamed"),
    _card(2, _gw(2, _snap_bot("pi2_gw_bot", "Шлюз <Pi2>"), _snap_bot("", "", 2, False), token=False),
          "в снимке пустой username — строки нет", "adm.gw.card.snapbot.gone"),
    _card(2, _gw(2, _snap_bot("snap_bot", "Из снимка"), _bot(2, "pi2_gw_bot", "Шлюз <Pi2>"), token=False),
          "ответ getMe по токену важнее снимка", "adm.gw.card.snapbot.token"),
    Shot("adm.gw.dev.nobot", role="admin", press=[DeviceCB(action="open", device_id=NASPI)],
         data=_slots(1, token=False), title="устройство-шлюз, бот неизвестен — без строки бота"),
    Shot("adm.gw.bundle.escaped", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _label(2, "дом <2>"), _bot(2, "pi2_gw_bot", "Шлюз <Pi2>")),
         title="подпись файла: подпись слота и имя бота экранированы"),
    Shot("adm.gw.bundle.username", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _bot(2, "pi2_gw_bot", "")), title="подпись файла: у бота нет имени — username"),
    Shot("adm.gw.bundle.token_replaced", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _token_replaced), title="подпись файла после смены токена — без ссылки"),
    Shot("adm.gw.bundle.snapbot", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _snap_bot("pi2_gw_bot", "Шлюз <Pi2>"), token=False),
         title="подпись файла: бот из снимка канала"),
    # ── /start gw-N ──────────────────────────────────────────────────────
    Shot("adm.link.gw.nav", role="admin", steps=[("start", ""), ("start", "gw-2")], data=_slots(2),
         title="карточка по ссылке — на место живого меню"),
    Shot("adm.link.gw.standby", role="admin", start="gw-2", data=_slots(2),
         title="карточка резерва по ссылке — пинг не меряется, но и «не отвечает» нет"),
    Shot("adm.link.gw.zero", role="admin", start="gw-0", data=_slots(1), title="gw-0 — слот снят"),
    Shot("adm.link.gw.garbage", role="admin", start="gw-abc", data=_slots(1), title="gw-abc — обычный /start"),
    Shot("adm.link.gw.one", role="admin", start="gw-1", data=_slots(1),
         title="один шлюз по ссылке — «Назад» на главную"),
    Shot("adm.link.gw.ping", role="admin", steps=[("start", "gw-2"), ("press", GwSlotCB(action="ping", slot=2))],
         data=_slots(2), title="карточка с главной после пинга — «Назад» на главную"),
    Shot("adm.link.gw.pref", role="admin",
         steps=[("start", "gw-2"), ("press", GwSlotCB(action="edit", slot=2)),
                ("press", GwSlotCB(action="pref", slot=2)), ("press", _CARD2)],
         data=_gw(2, _active1), title="карточка с главной после галочки «При старте» — «Назад» на главную"),
    Shot("adm.link.gw.label", role="admin",
         steps=[("start", "gw-2"), ("press", GwSlotCB(action="label", slot=2)), ("text", "дача"),
                ("press", _CARD2)],
         data=_slots(2), title="карточка с главной после ввода подписи — «Назад» на главную"),
    Shot("adm.link.gw.label_cancel", role="admin",
         steps=[("start", "gw-2"), ("press", GwSlotCB(action="label", slot=2)), ("press", _LABEL_CANCEL),
                ("press", _CARD2)],
         data=_slots(2), title="карточка с главной после отмены подписи — «Назад» на главную"),
    # ── файл слота 2: «В меню» и итоги с шлюза ───────────────────────────
    Shot("adm.gw.bundle.card.gone", role="admin",
         steps=[("press", GwSlotCB(action="bundle", slot=2)), ("call", _drop_slot2, "слот снят"),
                ("press", SetCB(sec="rt", act="do", key="bundle_cancel", val="2"))],
         data=_slots(2), title="«🛰 В карточку» под файлом снятого слота — главная"),
    Shot("adm.ev.gw_applied.label", role="admin", call=("bundle_applied", _applied2(True)),
         data=_gw(2, _label(2, "дом 2"), _file2(False)), title="файл слота 2 применён — имя с подписью"),
    Shot("adm.ev.gw_applied.gone", role="admin", call=("bundle_applied", _applied2(True)),
         data=_gw(2, _file2(False), _drop2), title="итог по снятому слоту — уведомление и главная"),
    Shot("adm.ev.gw_applied.fail.escaped", role="admin", call=("bundle_applied", _applied2(False, "нет <места> & прав")),
         data=_gw(2, _file2(False)), title="отказ шлюза — причина экранирована"),
    Shot("adm.ev.gw_applied.fail.bare", role="admin", call=("bundle_applied", _applied2(False)),
         data=_gw(2, _file2(False)), title="отказ шлюза без причины — без двоеточия"),
    Shot("adm.ev.gw_installed.label", role="admin", call=("bundle_installed", _installed2),
         data=_gw(2, _label(2, "дом 2"), _bot(2, "pi2_gw_bot", "Шлюз <Pi2>"), _file2(True)),
         title="шлюз слота 2 настроен — подпись слота и экранированное имя бота"),
    Shot("adm.ev.gw_installed.nobot", role="admin", call=("bundle_installed", _installed2),
         data=_gw(2, _file2(True), token=False), title="шлюз настроен, бот неизвестен — без строки бота"),
    Shot("adm.ev.gw_installed.nofile", role="admin", call=("bundle_installed", _installed2),
         data=_gw(2, _nav_live, token=False), title="шлюз настроен, файла в чате нет — одно уведомление"),
    Shot("adm.ev.gw_installed.enc", role="admin", call=("bundle_installed", _installed2),
         data=_gw(2, _file2(False), token=False),
         title="шлюз настроен, а в чате уже шифрованный файл — файл остаётся"),
]
