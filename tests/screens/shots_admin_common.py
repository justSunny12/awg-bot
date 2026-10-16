"""Общие построители снимков админа: константы номеров и состояние БД.

Состояние БД каждого снимка строят построители (_people, _base, …): порядок
создания в них фиксирован, поэтому номера профилей, устройств и слотов в
колбэках снимков — константы (сверяются assert'ом в построителе). Хост
(sshd, файервол, ядро AWG) в снимках не трогается: его ответы —
фиксированные словари и подмены методов сервисов. Здесь — то, чем
пользуются снимки нескольких разделов; построитель одного раздела живёт в
модуле раздела.
"""
from __future__ import annotations

import datetime as _dt
import json

from awgbot.core import config

from tests.screens.base import NOW


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


def _br_expired(services):
    """Подписка Бори кончилась вчера."""
    who = _people(services)
    services.db.update_client_fields(BR, period_start=_iso(NOW - _dt.timedelta(days=31)),
                                     period_end=_iso(NOW - _dt.timedelta(days=1)))
    return who


def _ks_many(services):
    """У Ксюши девять устройств — в карточку не влезают."""
    who = _people(services)
    services.db.update_client_fields(KS, device_limit=10)
    for n in ("Планшет", "Часы", "ТВ", "Приставка", "Рабочий", "Ноут 2", "Колонка"):
        services.add_device(KS, n)
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


def _upd_peers(*vers, fresh: bool = True, seen: str = "") -> list:
    """Шлюзы для сверки версий: Pi5, Pi4, NASPi, … с данными версиями агента."""
    from awgbot.domain.selfupdate import UpdatePeer
    names = ("Pi5", "Pi4", "NASPi", "Дача", "Офис")
    return [UpdatePeer(slot=i + 1, name=names[i], version=v, fresh=fresh, seen=seen) for i, v in enumerate(vers)]


def _upd_waits(*vers, tag: str = "v1.3.0", need: str = "1.3.0"):
    """Допустимой цели нет: tag ждёт шлюзы с агентами vers."""
    def data(services):
        who = _people(services)
        services.update_scan = lambda: None
        services.update_next = lambda: None
        services.update_available_tag = lambda: ""
        services.update_waiting = lambda: {"tag": tag, "need": need, "role": "client",
                                           "peers": _upd_peers(*vers)}
        return who
    return data


def _upd_silent(services):
    """Цель v1.3.0 есть; выше v1.4.0 ждёт Pi5; Pi4 молчит с 10.09 на старом
    агенте, шлюз 3 — ни разу не выходил на связь."""
    from awgbot.domain.selfupdate import UpdatePeer
    who = _upd_found(services)
    services.update_waiting = lambda: {"tag": "v1.4.0", "need": "1.4.0", "role": "client",
                                       "peers": _upd_peers("1.3.0")}
    services.update_silent_peers = lambda f: {"tag": "v1.3.0", "need": "1.3.0", "peers": [
        UpdatePeer(slot=2, name="Pi4", version="1.2.0", fresh=False, seen="2026-09-10T12:00:00+03:00"),
        UpdatePeer(slot=3, name="шлюз 3", version="", fresh=False, seen="")]}
    return who


def _upd_skipped(services):
    """Проверка нашла цель, а v1.4.0 пропущен: метаданные релиза не прочитаны."""
    who = _upd_found(services)
    services.update_skipped = lambda: [("v1.4.0", "sha256 метаданных релиза не совпал")]
    return who


def _upd_hold(services):
    """Окно ожидания после старта: ждём Pi5 и Pi4 — до NOW + 15 минут."""
    who = _people(services)
    hold = {"until": NOW + _dt.timedelta(minutes=15), "waiting": ["Pi5", "Pi4"], "role": "client"}
    services.update_hold = lambda: hold
    services.update_next = lambda: None
    return who


def _upd_offline(services):
    """Сеть не ответила на нажатии «⬆️ Обновить»."""
    who = _upd_found(services)

    def nxt():
        services.update_scan_failed = True
    services.update_next = nxt
    return who


def _bc_ready(services):
    """Для рассылки: кулдаун прошлых отправок того же процесса не мешает."""
    from awgbot.bot.handlers.admin import broadcast as bc
    bc._last_broadcast_at.clear()
    return _people(services)


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


def _two_expiring(services):
    """Истекают двое: Ксюша — завтра, Боря — через два дня."""
    who = _people(services)
    services.db.update_client_fields(KS, period_start=_iso(NOW - _dt.timedelta(days=29)),
                                     period_end=_iso(NOW + _dt.timedelta(days=1)), period_kind="month")
    return who


# ── шаг 5: SSH-доступ, сервер, резолвер, подготовка переезда — ветки из e2e ──

def _raise(kind: str, text: str):
    """Подмена метода сервиса, которая отказывает: kind — «service»
    (ServiceError — отказ, который бот объясняет) или «runtime» (сбой)."""
    def boom(*a, **k):
        from awgbot.domain.services import ServiceError
        raise (ServiceError if kind == "service" else RuntimeError)(text)
    return boom


FOREVER = 6                             # бессрочный профиль после _people


def _bc_marks(services):
    """Адресаты с продлением: бессрочный профиль и истёкший (со статусом)."""
    who = _bc_ready(services)
    assert _client(services, "Вечный", 2030, kind="never", limit=0) == FOREVER
    services.db.update_client_fields(BR, period_start=_iso(NOW - _dt.timedelta(days=31)),
                                     period_end=_iso(NOW - _dt.timedelta(days=1)), status="expired")
    return who
