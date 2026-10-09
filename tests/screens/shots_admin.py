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
         press=[SetCB(sec="rt_params", act="toggle", key="app.routing.enabled")], data=_slots(1),
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
    Shot("adm.new.devs.other", role="admin",
         steps=_NEWP + [("press", PresetCB(kind="new_devs", val=-1)), ("text", "4")], data=_people,
         title="новый профиль: своё число устройств — трафик"),
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
