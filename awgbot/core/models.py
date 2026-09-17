"""
models.py — доменные объекты (нормализованное представление БД).

Разные бизнес-сущности разведены в отдельные датаклассы, а не свалены в плоский
Row: identity клиента, его подписка, квота потребления, процессы grace и pause —
каждый своим типом. Ленивые процессы (grace/pause у клиента, friend у устройства)
представлены как Optional: None ⇔ процесс не активен (в БД нет строки).

Доступ к полям под-объектов — через плоские @property (client.period_end,
dev.friend_status), которые делегируют в нужный под-объект и инкапсулируют
None-логику ленивых процессов в одном месте. Вложенная структура доступна
напрямую (client.subscription.period_end, client.pause.mode).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Под-объекты клиента
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Subscription:
    """Биллинг-цикл клиента."""
    period_start: Optional[str] = None    # ISO UTC+3; None у служебного/бессрочного
    period_end: Optional[str] = None
    period_kind: Optional[str] = None     # day|week|month|year|never
    status: str = "active"                # active | expired
    notified_thresholds: str = ""         # CSV порогов истечения, отправленных клиенту


@dataclass
class TrafficQuota:
    """Лимиты потребления клиента (тотал за календарный месяц)."""
    limit: int = 0                        # байты; 0 = безлимит
    bonus_bytes: int = 0                  # разовая доп.квота текущего месяца
    bonus_granted_month: int = 0          # 0/1: выдавали ли в этом месяце
    traffic_notified: str = ""            # CSV меток трафик-уведомлений


@dataclass
class GraceState:
    """Состояние отсрочки «продли на пару недель». None ⇔ нет ни взятой отсрочки,
    ни остаточного «долга»: db материализует объект при used==1 ИЛИ
    pending_cut!=0 (сам факт «отсрочку брали» — по used)."""
    used: int = 0                         # 0/1
    pending_cut: int = 0                  # сек «долга», вычесть из след. периода


@dataclass
class PauseState:
    """Состояние приостановки подписки. None ⇔ нет ни активной паузы, ни
    накопленного счётчика дней: db материализует объект и когда active_since
    пуст, но used_days>0. Факт «на паузе сейчас» — по active_since (Client.is_paused)."""
    active_since: Optional[str] = None    # ISO входа в паузу
    reserved_days: int = 0
    used_days: int = 0
    balance_days: int = 0                 # счёт дней паузы (годовая 28/период, месячная +2 за продление)
    mode: Optional[str] = None            # user | admin_fixed | admin_open
    saved_end: Optional[str] = None       # снимок period_end для admin_open
    resume_code: Optional[str] = None     # одноразовый код email-выхода (NULL вне паузы)


# ─────────────────────────────────────────────────────────────────────────────
# Клиент
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Client:
    id: int
    tg_id: Optional[int]
    name: str
    device_limit: int
    block_reason: int
    is_service: int
    activation_status: str
    invite_code: Optional[str]
    created_at: str
    # Тип профиля (docs/guest-role.md): owner — обычный, со своей подпиской;
    # guest — гость: профиль без подписки, держит устройства, переданные ему
    # одним владельцем; без устройств не закрывается — ждёт новый код.
    kind: str = "owner"
    # Имя Telegram-аккаунта — для ссылок на человека в текстах (профильное
    # name задаёт админ и оно про подписку, не про человека).
    tg_name: str = ""
    tg_name_at: Optional[str] = None       # когда имя обновлялось; None — никогда
    tg_username: str = ""                  # публичный @username — ссылка t.me/ видна всем
    # Условная маршрутизация: РАЗРЕШЕНИЕ админа. Собственного «включено» у
    # профиля нет — оно выводится из устройств (включено хоть на одном), см.
    # db.routing_device_counts. Хранить его ещё и здесь значило бы завести
    # второй источник истины, обязанный совпадать с первым.
    routing_allowed: int = 0              # 0/1: админ разрешил фичу клиенту
    subscription: Subscription = field(default_factory=Subscription)
    quota: TrafficQuota = field(default_factory=TrafficQuota)
    grace: Optional[GraceState] = None
    pause: Optional[PauseState] = None

    # ——— плоские property: удобный доступ к полям под-объектов ———
    # Инкапсулируют делегирование и None-логику ленивых процессов в ОДНОМ месте,
    # чтобы вызывающий код читал client.period_end / client.pause_mode напрямую
    # (атрибутами, не через dict-магию), а None-безопасность жила тут.
    @property
    def is_guest(self) -> bool:
        return self.kind == "guest"

    @property
    def period_start(self): return self.subscription.period_start
    @property
    def period_end(self): return self.subscription.period_end
    @property
    def period_kind(self): return self.subscription.period_kind
    @property
    def status(self): return self.subscription.status
    @property
    def notified_thresholds(self) -> set[int]:
        """Пороги истечения, о которых клиенту уже сказано (минуты до конца).
        В БД — CSV; наружу множество, как из db.get_notified, только без
        второго запроса: колонку уже привёз list_clients."""
        return {int(x) for x in self.subscription.notified_thresholds.split(",") if x.strip()}
    @property
    def traffic_limit(self): return self.quota.limit
    @property
    def bonus_bytes(self): return self.quota.bonus_bytes
    @property
    def bonus_granted_month(self): return self.quota.bonus_granted_month
    @property
    def traffic_notified(self) -> set[str]:
        """Метки отправленных трафик-уведомлений ('cli80', 'dev_over:{id}'…);
        сбрасываются 1-го числа. То же, что db.get_traffic_notified, из объекта."""
        return {x for x in self.quota.traffic_notified.split(",") if x.strip()}
    @property
    def grace_used(self): return self.grace.used if self.grace else 0
    @property
    def grace_pending_cut(self): return self.grace.pending_cut if self.grace else 0
    @property
    def pause_active_since(self): return self.pause.active_since if self.pause else None
    @property
    def pause_reserved_days(self): return self.pause.reserved_days if self.pause else 0
    @property
    def pause_used_days(self): return self.pause.used_days if self.pause else 0
    @property
    def pause_balance_days(self): return self.pause.balance_days if self.pause else 0
    @property
    def pause_mode(self): return self.pause.mode if self.pause else None
    @property
    def pause_saved_end(self): return self.pause.saved_end if self.pause else None
    @property
    def effective_period_end(self) -> Optional[str]:
        """Конец периода «по-настоящему»: на открытой админ-паузе period_end
        пуст (временно бессрочная), а настоящий конец — сохранённый."""
        if self.pause_mode == "admin_open" and self.pause_saved_end:
            return self.pause_saved_end
        return self.period_end

    @property
    def is_paused(self) -> bool:
        return self.pause is not None and self.pause.active_since is not None


# ─────────────────────────────────────────────────────────────────────────────
# Под-объекты устройства
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class DeviceTraffic:
    """Счётчики потребления устройства."""
    limit: int = 0
    rx_month: int = 0
    tx_month: int = 0
    rx_period: int = 0
    tx_period: int = 0
    last_handshake: Optional[int] = None
    missing_count: int = 0


@dataclass
class Friend:
    """Ожидающее приглашение на устройство: код выдан, друг его ещё не
    активировал. None ⇔ приглашения нет. После активации устройство получает
    ДЕРЖАТЕЛЯ (Device.holder_*), а строка приглашения исчезает."""
    code: Optional[str] = None
    status: Optional[str] = None          # pending


@dataclass
class Gateway:
    """Слот шлюза условной маршрутизации (docs/gateway-failover.md): устройство
    админа на машине-шлюзе и её линк на ВПС. Слотов может быть несколько —
    один несёт трафик, остальные в резерве; кто несёт, решает состояние
    (`routing_active_gateway`), а не строка слота."""
    id: int
    device_id: int
    link_if: str                    # awglink, awglink2
    link_port: int                  # 443, 8443
    link_cidr: str                  # 10.99.99.0/30
    preferred: int = 0              # берёт трафик при холодном старте (не более одного)
    home_subnets: list[str] = field(default_factory=list)
    label: str = ""                 # «дом 1» — подпись места, необязательна
    created_at: str = ""

    @property
    def mark(self) -> int:
        """Бит метки для зонда этого слота: не пересекается с меткой фичи (0x1)."""
        return 1 << self.id

    def table(self, base: int) -> int:
        """Таблица маршрутизации слота — только для зонда резерва."""
        return base + self.id


@dataclass
class Device:
    id: int
    client_id: int
    name: str
    private_key: Optional[str]
    public_key: str
    preshared_key: str
    address: str
    block_reason: int
    created_at: str
    # Условная маршрутизация НА ЭТОМ устройстве. Действует только поверх
    # разрешения профиля (clients.routing_allowed) — отзыв разрешения гасит
    # эффект, флаг устройства при этом сохраняется.
    routing_on: int = 0
    # Интерфейс, на котором живёт пир. ПУСТАЯ строка = интерфейс по умолчанию, а
    # не «неизвестно»: так база не требует бэкфилла, а после переезда значение
    # нормализуется обратно в пустое. Разрешать через iface_of().
    iface: str = ""
    # id старой строки у двойника, рождённого переездом; None — обычное
    # устройство. Пара нужна прогрессу, слиянию истории и парным операциям.
    twin_of: Optional[int] = None
    # Устройство стоит в слоте шлюза условной маршрутизации (docs/gateway-failover.md):
    # не считается в лимитах, не блокируется, не передаётся, не удаляется,
    # ссылку не выдаёт. Производное от таблицы gateways (и по оригиналу пары в
    # окне переезда), в самой строке устройства флага больше нет.
    is_gateway: int = 0
    traffic: DeviceTraffic = field(default_factory=DeviceTraffic)
    friend: Optional[Friend] = None
    # Держатель — кто управляет устройством через бота, если это не владелец
    # (docs/guest-role.md): гость или обычный клиент. Слот, квота, подписка и
    # разрешение РФ-доступа — у владельца (client_id). None ⇔ своё устройство.
    holder_client_id: Optional[int] = None
    holder_tg_id: Optional[int] = None
    holder_name: str = ""
    holder_tg_name: str = ""
    holder_tg_username: str = ""
    # Владелец — для карточки у держателя («получено от …»).
    owner_tg_id: Optional[int] = None
    owner_name: str = ""
    owner_tg_name: str = ""
    owner_tg_username: str = ""

    @property
    def is_managed(self) -> bool:
        """Бот управляет устройством напрямую — у него есть приватный ключ, значит
        может выдавать ссылку/QR/файл, передавать другу и т.д.

        Обратное (ключа нет) означает ровно одно: пир появился в конфиге сервера
        мимо бота и был подхвачен карантином. Восстановить ключ неоткуда — такое
        устройство можно только переименовать, привязать к профилю или удалить."""
        return bool(self.private_key)

    # ——— плоские property (аналогично Client) ———
    @property
    def traffic_limit(self): return self.traffic.limit
    @property
    def traffic_rx_month(self): return self.traffic.rx_month
    @property
    def traffic_tx_month(self): return self.traffic.tx_month
    @property
    def traffic_rx_period(self): return self.traffic.rx_period
    @property
    def traffic_tx_period(self): return self.traffic.tx_period
    @property
    def last_handshake(self): return self.traffic.last_handshake
    @property
    def missing_count(self): return self.traffic.missing_count
    @property
    def is_lent(self) -> bool:
        """Передано: управляет держатель, а не владелец."""
        return self.holder_client_id is not None

    # ——— совместимость: «активный друг» = держатель, «pending» = приглашение ———
    @property
    def friend_tg_id(self): return self.holder_tg_id
    @property
    def friend_code(self): return self.friend.code if self.friend else None
    @property
    def friend_status(self):
        if self.is_lent:
            return "active"
        return self.friend.status if self.friend else None


__all__ = ["Subscription", "TrafficQuota", "GraceState", "PauseState", "Client",
           "DeviceTraffic", "Friend", "Device"]
