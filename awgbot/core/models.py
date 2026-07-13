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
    subscription: Subscription = field(default_factory=Subscription)
    quota: TrafficQuota = field(default_factory=TrafficQuota)
    grace: Optional[GraceState] = None
    pause: Optional[PauseState] = None

    # ——— плоские property: удобный доступ к полям под-объектов ———
    # Инкапсулируют делегирование и None-логику ленивых процессов в ОДНОМ месте,
    # чтобы вызывающий код читал client.period_end / client.pause_mode напрямую
    # (атрибутами, не через dict-магию), а None-безопасность жила тут.
    @property
    def period_start(self): return self.subscription.period_start
    @property
    def period_end(self): return self.subscription.period_end
    @property
    def period_kind(self): return self.subscription.period_kind
    @property
    def status(self): return self.subscription.status
    @property
    def notified_thresholds(self): return self.subscription.notified_thresholds
    @property
    def traffic_limit(self): return self.quota.limit
    @property
    def bonus_bytes(self): return self.quota.bonus_bytes
    @property
    def bonus_granted_month(self): return self.quota.bonus_granted_month
    @property
    def traffic_notified(self): return self.quota.traffic_notified
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
    def pause_mode(self): return self.pause.mode if self.pause else None
    @property
    def pause_saved_end(self): return self.pause.saved_end if self.pause else None
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
    """Гостевой доступ (передача устройства другу). None ⇔ обычное устройство."""
    tg_id: Optional[int] = None
    code: Optional[str] = None
    status: Optional[str] = None          # pending | active


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
    full_access_link: Optional[str] = None    # vpn:// полного доступа (admin-устройство)
    traffic: DeviceTraffic = field(default_factory=DeviceTraffic)
    friend: Optional[Friend] = None

    @property
    def is_admin(self) -> bool:
        """Устройство несёт ссылку полного доступа к серверу (метка [Доступ к серверу])."""
        return bool(self.full_access_link)

    @property
    def is_managed(self) -> bool:
        """Бот управляет устройством напрямую — у него есть приватный ключ, значит
        может выдавать ссылку/QR/файл, передавать другу и т.д."""
        return bool(self.private_key)

    @property
    def is_app(self) -> bool:
        """«Чистое» устройство из приложения: у бота нет ни ключа, ни ФА-ссылки —
        только подхваченный с сервера пир. Ему предлагаем «прописать строку»
        (реставрацию) и метим суффиксом (APP)."""
        return not self.private_key and not self.full_access_link

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
    def friend_tg_id(self): return self.friend.tg_id if self.friend else None
    @property
    def friend_code(self): return self.friend.code if self.friend else None
    @property
    def friend_status(self): return self.friend.status if self.friend else None


__all__ = ["Subscription", "TrafficQuota", "GraceState", "PauseState", "Client",
           "DeviceTraffic", "Friend", "Device"]
