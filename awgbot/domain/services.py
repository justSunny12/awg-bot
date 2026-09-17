"""
services.py — бизнес-логика: склейка db + awg + configgen.

Слой синхронный (db и awg блокирующие). Async-слой (хендлеры, планировщик)
вызывает эти методы через asyncio.to_thread, чтобы docker exec не морозил loop.
Поэтому services НЕ шлют сообщения сами, а возвращают список Notification —
их рассылает async-слой.

Здесь живут потоки, спроектированные ранее: создание клиента с инвайтом,
активация, добавление/удаление устройства с откатом, продление с остатком,
опрос трафика, проверка сроков, реконсиляция состава пиров и блокировок,
карантин чужих пиров.
"""

from __future__ import annotations

import re

import datetime
import hashlib
import logging
import os
import sys
import time
import secrets
import string
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.infra import awg
from awgbot.infra import email_resume
from awgbot.infra import routing
from awgbot.domain import configgen
from awgbot.domain import routing as domain_routing
from awgbot.domain.migration import MigrationMixin
from awgbot.domain.selfupdate import SelfUpdateMixin
from awgbot.domain.mailmix import MailMixin
from awgbot.domain.backupcrypto import BackupCryptoMixin
from awgbot.domain.privatedns import PrivateDnsMixin
from awgbot.core.blocks import DeviceBlock, ClientBlock, DEVICE_TRAFFIC_ANY
from awgbot.core import models
from awgbot.core.enums import SubStatus, ActivationStatus, PauseMode, PeriodKind, FriendStatus


# Байт в гигабайте — физическая константа (не настройка), потому в коде, не в
# config. Лимиты храним в байтах, вводим/показываем пользователю в ГБ.
# У texts.py есть свой приватный дубль (_BYTES_PER_GB) — намеренно, см. там.
log = logging.getLogger("awgbot.services")

BYTES_PER_GB = 1024 ** 3

# Секунд в сутках — физическая константа. Резервы/сроки храним в днях, но «долг»
# отсрочки и длительность паузы считаем в секундах (унифицировано с ISO-датами).
SECONDS_PER_DAY = 86400


# ─────────────────────────────────────────────────────────────────────────────
# Исключения и результаты
# ─────────────────────────────────────────────────────────────────────────────

class ServiceError(Exception):
    """Ошибка бизнес-операции (показывается пользователю)."""


class LimitReached(ServiceError):
    pass


@dataclass
class Notification:
    tg_id: int
    text: str
    reply_markup: object = None        # опциональная inline-клавиатура
    grace_offer_client_id: int = 0     # >0 → прикрепить кнопку отсрочки (делает scheduler)
    force_sound: bool = False          # True → слать со звуком даже в тихие часы
    critical: bool = False             # влияет на всех клиентов → при недоступном
                                       # Telegram уходит админу на почту (если включено)


@dataclass
class ClientCreated:
    client_id: int
    invite_code: str
    period_end: object            # datetime


@dataclass
class ActivationResult:
    ok: bool
    reason: str = ""              # invalid | already_has_access | ok
    client: Optional[object] = None
    upgrade: Optional["GuestUpgrade"] = None   # гость стал владельцем (docs/guest-role.md)


@dataclass
class RoutingAddResult:
    """Разбор пачки доменов: что взято, что отброшено и почему.

    Отклонённые несут причину строкой — пользователь вставляет списком, и он
    должен видеть, что именно не прошло, а не гадать, почему добавилось меньше.
    """
    added: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    over_limit: int = 0            # не влезло в потолок списка
    limit: int = 0                 # сам потолок (для текста)


@dataclass
class DeviceCreated:
    device_id: int
    address: str
    vpn: str
    conf: str


@dataclass
class FriendActivation:
    """Итог активации кода F… (docs/guest-role.md).
    reason: ok | invalid | own_device (код на устройство своего же профиля) |
    other_donor (у держателя уже есть устройства от другого владельца — в
    held/donor что и от кого)."""
    ok: bool
    reason: str = ""
    device_id: Optional[int] = None
    device_name: Optional[str] = None
    holder: Optional[object] = None      # профиль держателя (гость или клиент)
    donor: Optional[object] = None       # владелец устройства (при ok) / прежний даритель (other_donor)
    held: list = field(default_factory=list)   # устройства, которые держатель уже держит


@dataclass
class GuestUpgrade:
    """Гость стал владельцем: что перенесено и от кого."""
    donor: Optional[object] = None
    moved: list = field(default_factory=list)  # Device — перенесённые в новый профиль


@dataclass
class PauseCredit:
    """Что стало со счётом дней паузы при оплате периода kind.
    reason: credited — начислено (added < полного — упёрлись в порог) |
    cap — порог уже достигнут, не начислено | expired — ежемесячное после
    истечения | grace — в прошлом периоде была отсрочка | none — тип не копит."""
    kind: str
    before: int
    after: int
    cap: int
    reason: str

    @property
    def added(self) -> int:
        return self.after - self.before


@dataclass
class ExtendResult:
    new_end: object              # datetime
    notifications: list = field(default_factory=list)
    pause: Optional["PauseCredit"] = None


@dataclass
class DaysExtension:
    """Сдвиг конца подписки на N дней для одного адресата объявления с
    продлением. old_end/new_end — None у бессрочной: продлевать нечего,
    объявление уходит без шапки. from_now — истёкшая: отсчёт от сегодня, а не
    от старого конца."""
    client: object
    old_end: Optional[object] = None      # datetime
    new_end: Optional[object] = None      # datetime
    from_now: bool = False

    @property
    def unlimited(self) -> bool:
        return self.new_end is None


# Тексты уведомлений (сухие, без слов про оплату — ТЗ 6.5).
_MONTH_CUT_MINUTES = 30 * 24 * 60             # порог, который месяцу не показываем
_TXT_EXTENDED = "Подписка продлена до {end}"
_TXT_EXTENDED_FOREVER = "Подписка теперь бессрочная 🎉"
_TXT_EXPIRED_CLIENT = "Срок действия подписки истёк. Доступ приостановлен."
_TXT_EXPIRING_CLIENT = "Внимание: подписка истекает через {label}."
_TXT_EXPIRING_ADMIN = "Клиент «{name}»: подписка истекает через {label}."
_TXT_EXPIRED_ADMIN = "Клиент «{name}»: подписка истекла, доступ приостановлен."
# Пир, которого нет в БД. Создавать пиры больше некому, кроме бота, — значит это
# либо ручная правка конфига, либо чужое вмешательство. Тревога, а не находка.
_TXT_UNKNOWN_PEER = (
    "🚨 В конфиге сервера пир, которого нет в базе: {ip}.\n\n"
    "Пиры создаёт только бот — значит это ручная правка конфига или чужое "
    "вмешательство. Пир помещён в карантин («Устройства без профиля»): "
    "проверь и либо привяжи к профилю, либо удали.")
# Пир исчез из конфига, а запись в БД осталась: сам бот так не удаляет —
# он снимает пира и строку разом. Значит конфиг правили мимо бота.
_TXT_PEER_GONE = ("Устройство «{name}» клиента «{client}» пропало из конфига сервера — бот его не удалял. Запись убрана, чтобы база сошлась с сервером.")
_TXT_RT_INFRA_BAD = (
    "🚨 Условная маршрутизация не применяется:\n<code>{err}</code>\n\n"
    "Если речь о dnsmasq — у клиентов сейчас нет DNS вообще, и выглядит это как "
    "«интернет работает через раз»: уже отрезолвленное ходит, новое — нет. "
    "Проверь <code>systemctl status dnsmasq</code> и "
    "<code>/etc/dnsmasq.d/awgbot-routing.conf</code>.")
_TXT_RT_SRC_STALE = (
    "⚠️ Источник списков маршрутизации замолчал:\n<code>{url}</code>\n\n"
    "Раньше он отдавал {n} записей, сейчас отвечает пустым. Прежний список "
    "продолжает работать, но обновляться перестал: новые заблокированные ресурсы "
    "в него уже не попадут. Проверь, не переехал ли файл.")
_TXT_RT_SRC_DOWN = (
    "⚠️ Источник списков маршрутизации не отвечает:\n<code>{url}</code>\n\n"
    "<code>{err}</code>\n\n"
    "Так ответили все {tries} попытки подряд. Файл может быть цел — до него не "
    "дошли мы. Прежний список продолжает работать, но обновляться перестал: "
    "новые заблокированные ресурсы в него уже не попадут. Лимит запросов и "
    "таймаут обычно проходят сами; скажу, когда источник ответит снова.")
_TXT_RT_SRC_GONE = (
    "⚠️ Источника списков маршрутизации нет по адресу:\n<code>{url}</code>\n\n"
    "<code>{err}</code>\n\n"
    "Так ответили все {tries} попытки подряд — файл переехал или удалён, само "
    "это не пройдёт. Прежний список продолжает работать, но обновляться "
    "перестал: новые заблокированные ресурсы в него уже не попадут. Новый адрес "
    "прописывается в <code>app.routing.lists_home_urls</code>.")
_TXT_RT_SRC_OK = (
    "🟢 Источник списков маршрутизации снова отдаёт данные:\n<code>{url}</code>\n\n"
    "Записей в ответе: {n}. Списки опять обновляются.")
_TXT_FRIEND_DEVICE_GONE = ("Устройство, которым ты управлял, удалено владельцем — "
                           "доступ по нему больше не работает.")


def _e(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _friend_blocked_text(device_name: str) -> str:
    return (f"🔴 Доступ к устройству «{_e(device_name)}» приостановлен: "
            "у владельца доступа закончилась подписка.")


def _friend_unblocked_text(device_name: str) -> str:
    return f"🟢 Доступ к устройству «{_e(device_name)}» снова активен."


# ── Тексты ручных блокировок (админ/клиент) ──────────────────────────────────

def _manual_device_blocked_client(name: str) -> str:
    return f"🛑 Устройство «{_e(name)}» заблокировано."


def _manual_device_unblocked_client(name: str) -> str:
    return f"🟢 Устройство «{_e(name)}» разблокировано."


# Клиенту сообщаем о его же блокировке — имя не подставляем (адресат и есть
# субъект), поэтому текст без параметров.
def _manual_client_blocked() -> str:
    return "🛑 Твой доступ приостановлен администратором."


def _manual_client_unblocked() -> str:
    return "🟢 Твой доступ восстановлен."


# ── Тексты приостановки подписки ─────────────────────────────────────────────

def _pause_auto_ended_client(actual_days: int, new_end) -> str:
    return (f"▶️ Приостановка завершена автоматически (истёк максимальный срок). "
            f"Учтено {actual_days} дн. паузы, подписка активна до "
            f"{timeutil.fmt_dt(new_end)}.")


def _pause_friend_started(device_name: str) -> str:
    return (f"⏸ Доступ к устройству «{_e(device_name)}» приостановлен владельцем "
            "(подписка на паузе).")


def _pause_friend_ended(device_name: str) -> str:
    return f"▶️ Доступ к устройству «{_e(device_name)}» снова активен."


# ── Тексты месячного сброса лимитов ──────────────────────────────────────────

def _gb_limit(num_bytes: int) -> str:
    """Лимит в целых ГБ для уведомлений: «500 ГБ». 0 = безлимит (в перечислениях
    такие не показываем, но на всякий)."""
    return f"{int(round(num_bytes / BYTES_PER_GB))} ГБ"


def _reset_client_text(total_limit: int, device_lines: list[str]) -> str:
    """Профилю: сброс + доступный лимит профиля + список лимитных устройств.
    total_limit>0 — показываем строку профиля; device_lines — уже отфильтрованы
    (только лимитные)."""
    parts = ["Начался новый месяц — лимит расхода по твоему профилю сброшен 🙂"]
    if total_limit > 0:
        parts.append(f"Доступный лимит на текущий месяц: {_gb_limit(total_limit)}")
    if device_lines:
        parts.append("\nДоступные лимиты по устройствам:\n" + "\n".join(device_lines))
    return "\n".join(parts)


def _reset_friend_text(device_lines: list[str]) -> str:
    """Другу: сброс + список ЕГО лимитных устройств."""
    return ("Начался новый месяц — лимиты расхода по твоим устройствам сброшены 🙂\n"
            "Доступные лимиты на текущий месяц:\n" + "\n".join(device_lines))


# ── Тексты уведомлений о потреблении (ТЗ 7-8) ────────────────────────────────


def _dev_warn_text(name: str, pct: int) -> str:
    return (f"⚠️ Устройство «{_e(name)}»: израсходовано ~{pct}% месячного лимита "
            "потребления.")


def _dev_over_text(name: str, until: str) -> str:
    return (f"🔴 Устройство «{_e(name)}»: месячный лимит потребления исчерпан. "
            f"Доступ приостановлен до {until} или пока лимит не увеличат.")


def _friend_dev_over_host_text(name: str, until: str) -> str:
    return (f"🔴 Устройство «{_e(name)}» (передано другу): лимит потребления "
            f"исчерпан, доступ приостановлен до {until}.")


def _cli_warn_text(pct: int) -> str:
    return f"⚠️ Израсходовано ~{pct}% месячного лимита потребления по всем устройствам."


def _cli_bonus_text(bonus_gb: int, until: str) -> str:
    return (f"📈 Месячный лимит потребления исчерпан. Тебе добавлено "
            f"{bonus_gb} ГБ до конца месяца — это разово, больше в этом месяце "
            "квота не увеличится. Лимит обновится "
            f"{until}.")


def _cli_bonus_admin_text(name: str, bonus_gb: int) -> str:
    return (f"📈 Клиенту «{_e(name)}» исчерпан лимит — выдано {bonus_gb} ГБ "
            "до конца месяца (разово).")


def _cli_over_text(until: str) -> str:
    return (f"🔴 Дополнительная квота исчерпана. Доступ ко всем устройствам "
            f"приостановлен до {until}.")


def _cli_over_admin_text(name: str) -> str:
    return f"🔴 Клиент «{_e(name)}» исчерпал лимит и доп.квоту — доступ приостановлен."


def _admin_self_over_text() -> str:
    return "🔴 Твой месячный лимит потребления исчерпан (уведомление, доступ не тронут)."


# ─────────────────────────────────────────────────────────────────────────────
# Services
# ─────────────────────────────────────────────────────────────────────────────

class Services(SelfUpdateMixin, MailMixin, BackupCryptoMixin, MigrationMixin, PrivateDnsMixin):
    # username бота — для deep-link'ов в текстах (t.me/<bot>?start=…); main
    # кладёт его после getMe. Пусто — ссылки не рисуются, текст остаётся текстом.
    bot_username: str = ""

    # ── потребление за месяц: по профилям и по устройствам ───────────────────

    # ── снимки экранов: всё, что экрану нужно, одним вызовом из одного потока ─
    # Панель админа собиралась 12–15 отдельными хопами в поток и ~20 SQL, с
    # дублями (admin_client дважды, видимость маршрутизации дважды).

    def admin_panel_snapshot(self) -> dict:
        st = self.server_status_cached()
        tot = self.db.get_total_month_traffic()
        st = {**st, "traffic_rx": tot["rx"], "traffic_tx": tot["tx"]}
        ac = self.admin_client()
        rt_visible = bool(ac and self.routing_client_visible(ac))
        routing_ok = self.routing_health_for_client(ac) if (ac and rt_visible) else None
        mig = self.migration_progress() if self.migration_running() else None
        return {
            "st": st, "ac": ac, "routing_ok": routing_ok, "mig": mig,
            "expiring": len(self.expiring_subscriptions()),
            "unassigned": self.count_unassigned_devices(),
            "has_dev": bool(ac and self.db.count_devices(ac.id)),
            "rt_visible": rt_visible,
            "rt_on": bool(ac and rt_visible and self.routing_profile_on(ac.id)),
        }

    def online_ref(self) -> datetime.datetime:
        """Момент, на который считать «онлайн»: последний опрос пиров.

        Хендшейки в БД свежее опроса не бывают, а опрос идёт раз в тик. Сравнивать
        их с текущим временем значило бы гасить тех, кто был на связи в момент
        опроса, с каждой минутой после него: счётчик в панели считался на момент
        опроса, а список по ссылке через три минуты показывал вдвое меньше.
        Опрос старше порога (опросчик встал) — данные протухли, берём «сейчас»:
        все оффлайн, и это честно."""
        raw = self.db.get_state("online_polled_at") or ""
        now = timeutil.now()
        thr = settings.get_int("app.online_handshake_seconds", 300)
        if raw.isdigit() and 0 <= now.timestamp() - int(raw) <= thr:
            return datetime.datetime.fromtimestamp(int(raw), tz=timeutil.TZ)
        return now

    def _devices_online(self, devices) -> bool:
        thr = settings.get_int("app.online_handshake_seconds", 300)
        ref = self.online_ref()
        return any(timeutil.handshake_is_online(d.traffic.last_handshake, ref, threshold=thr)
                   for d in devices)

    def client_card_data(self, client_id: int) -> Optional[dict]:
        """Карточка профиля у админа: 8 хопов и тройной list_devices → одно."""
        client = self.db.get_client(client_id)
        if client is None:
            return None
        devices = self.db.list_devices(client_id)
        progress = self.migration_client_progress(client_id) if self.migration_running() else None
        rt_visible = self.routing_client_visible(client)
        return {
            "client": client, "devices": devices,
            "traffic": self.db.get_client_traffic(client_id),
            "online": self._devices_online(devices),
            "progress": progress, "rt_visible": rt_visible,
            "rt_on": self.routing_profile_on(client_id) if rt_visible else False,
        }

    def client_info_data(self, client_id: int) -> Optional[dict]:
        """«Управлять подпиской» у клиента — то же одним вызовом."""
        client = self.db.get_client(client_id)
        if client is None:
            return None
        devices = self.db.list_devices(client_id)
        return {"client": client, "devices": devices,
                "traffic": self.db.get_client_traffic(client_id),
                "online": self._devices_online(devices),
                "routing": self.routing_client_visible(client)}

    def svc_screen_data(self) -> dict:
        state = self.migration_state()
        return {"state": state, "available": self.migration_available(),
                "progress": self.migration_progress() if state else None,
                "orphans": 0 if state else len(self.migration_orphan_twins())}

    def migration_orphan_rows(self) -> list[tuple[str, str]]:
        """[(имя профиля, имя устройства)] — имена одним проходом, не по одному."""
        names = {c.id: c.name for c in self.db.list_clients(include_service=True)}
        return [(names.get(d.client_id, "?"), d.name) for d in self.migration_orphan_twins()]

    # ── онлайн: кто подключён прямо сейчас ───────────────────────────────────

    def online_devices(self) -> list[tuple]:
        """[(устройство, имя профиля)] с живым хендшейком — по ВСЕМ профилям,
        включая админа. Порядок: по имени профиля, внутри — по имени устройства."""
        ref = self.online_ref()
        devs = [d for d in self.db.list_all_devices()
                if timeutil.handshake_is_online(d.traffic.last_handshake, ref)]
        names = {c.id: c.name for c in self.db.list_clients(include_service=True)}
        devs.sort(key=lambda d: (0 if d.is_gateway else 1,
                                 names.get(d.client_id, "").lower(), d.name.lower()))
        return [(d, names.get(d.client_id, "")) for d in devs]

    def online_client_ids(self) -> set[int]:
        """Профили, у которых онлайн хотя бы одно устройство."""
        return {d.client_id for d, _ in self.online_devices()}

    def traffic_by_profile(self) -> list[tuple]:
        """[(client, rx, tx)] за календарный месяц. Админ первым, остальные по
        имени — тот же порядок, что в списке клиентов."""
        out = []
        for c in self.db.list_clients(admin_first_tg=config.ADMIN_ID):
            t = self.db.get_client_traffic(c.id)
            out.append((c, int(t["rx_month"]), int(t["tx_month"])))
        return out

    def traffic_by_device(self, client_id: int) -> list[tuple]:
        """[(device, rx, tx)] за месяц по устройствам профиля — по одной строке
        на устройство (list_devices сам решает, какую из пары показать)."""
        return [(d, int(d.traffic.rx_month), int(d.traffic.tx_month))
                for d in self.db.list_devices(client_id)]

    def __init__(self, db):
        self.db = db

    # ── Блокировки (битовые маски причин) ────────────────────────────────────
    # is_blocked как отдельного поля нет: заблокирован ⇔ block_reason != 0.
    # IP физически режем/снимаем по ИТОГОВОМУ состоянию маски: DROP ставим, когда
    # появляется хоть один бит; снимаем — только когда сброшены ВСЕ.

    def _device_pair(self, dev, twins: Optional[dict] = None) -> list:
        """Устройство и его двойник по переезду — в порядке [сам, второй].

        Вне окна переезда список из одного элемента, и все операции ведут себя
        ровно как прежде. Внутри окна у устройства ДВА пира на двух интерфейсах,
        и мутации обязаны быть парными: заблокировать один, сняв другой,
        значит подарить доступ; удалить один — оставить призрачный.
        """
        pair = [dev]
        if dev.twin_of:
            other = self.db.get_device(dev.twin_of)
            if other is not None:
                pair.append(other)
            return pair
        twin_id = (twins if twins is not None else self.db.twins_by_origin()).get(dev.id)
        if twin_id:
            other = self.db.get_device(twin_id)
            if other is not None:
                pair.append(other)
        return pair

    def _device_set_block(self, device_id: int, bit: DeviceBlock, twins: Optional[dict] = None) -> None:
        """Установить причину блокировки устройства (бит) и наложить DROP.
        twins — заранее снятая карта пар: в циклах избавляет от скана devices
        на каждое устройство."""
        dev = self.db.get_device(device_id)
        if dev is None or dev.is_gateway:
            return                                # шлюз не блокируется ни одной причиной
        new_mask = int(dev.block_reason) | int(bit)
        if new_mask == int(dev.block_reason):
            return
        for peer in self._device_pair(dev, twins):
            self.db.update_device_fields(peer.id, block_reason=new_mask)
            try:
                awg.block_ip(peer.address)      # идемпотентно
            except awg.AwgError:
                pass

    def _device_clear_block(self, device_id: int, bit: DeviceBlock, twins: Optional[dict] = None) -> None:
        """Снять причину (бит). Если не осталось причин — снять DROP."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        new_mask = int(dev.block_reason) & ~int(bit)
        if new_mask == int(dev.block_reason):
            return
        for peer in self._device_pair(dev, twins):
            self.db.update_device_fields(peer.id, block_reason=new_mask)
            if new_mask == 0:
                try:
                    awg.unblock_ip(peer.address)   # идемпотентно
                except awg.AwgError:
                    pass

    def _client_set_block(self, client_id: int, bit: ClientBlock) -> None:
        """Установить причину блокировки клиента (только маска клиента; физически
        трафик режется по устройствам — этим занимается вызывающий код)."""
        c = self.db.get_client(client_id)
        if c is None:
            return
        self.db.update_client_fields(
            client_id, block_reason=int(c.block_reason) | int(bit))

    def _client_clear_block(self, client_id: int, bit: ClientBlock) -> None:
        c = self.db.get_client(client_id)
        if c is None:
            return
        old_mask = int(c.block_reason)
        new_mask = old_mask & ~int(bit)
        self.db.update_client_fields(client_id, block_reason=new_mask)
        # эпизод бана завершён (клиент полностью разблокирован) → в аудит
        if old_mask and new_mask == 0:
            self.db.archive_block(client_id, old_mask, "unblocked")

    # ── Ручные блокировки (админ / клиент) ───────────────────────────────────

    _TXT_GATEWAY_LOCKED = ("Это устройство — шлюз условной маршрутизации: его нельзя "
                           "заблокировать, удалить, передать или выдать ему ссылку. "
                           "Сначала «🛑 Не шлюз?» в карточке устройства.")

    def _refuse_if_gateway(self, dev) -> None:
        if dev is not None and dev.is_gateway:
            raise ServiceError(self._TXT_GATEWAY_LOCKED)

    def block_device_manual(self, device_id: int, bit: DeviceBlock,
                            notify: bool) -> list["Notification"]:
        """Ручной блок устройства заданным битом (ADMIN_SILENT/NOTIFIED/USER).
        notify=True → уведомить пользователя (клиента-владельца и/или друга).
        Тихий блок (notify=False) уведомлений не шлёт."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return []
        self._refuse_if_gateway(dev)
        self._device_set_block(device_id, bit)
        notes: list[Notification] = []
        if notify:
            owner = self.db.get_client(dev.client_id)
            if owner and owner.tg_id:
                notes.append(Notification(owner.tg_id,
                             _manual_device_blocked_client(dev.name)))
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_blocked_text(dev.name)))
        return notes

    def unblock_device_manual(self, device_id: int, bit: DeviceBlock,
                              notify: bool) -> list["Notification"]:
        """Снять ручной бит с устройства. notify → уведомить, если после снятия
        устройство разблокировано полностью (не осталось других причин)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return []
        self._device_clear_block(device_id, bit)
        fresh = self.db.get_device(device_id)
        fully_free = int(fresh.block_reason) == 0
        notes: list[Notification] = []
        if notify and fully_free:
            owner = self.db.get_client(dev.client_id)
            if owner and owner.tg_id:
                notes.append(Notification(owner.tg_id,
                             _manual_device_unblocked_client(dev.name)))
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_unblocked_text(dev.name)))
        return notes

    def block_client_manual(self, client_id: int, bit: ClientBlock,
                           notify: bool, pause_days=None) -> list["Notification"]:
        """Ручной блок клиента админом. Каскадит ТЕМ ЖЕ типом (silent/notified) на
        все устройства (физический DROP). notify → уведомить клиента и друзей.
        pause_days: None — без приостановки подписки; 0 — бессрочная приостановка
        (admin_open); N>0 — срочная (admin_fixed). Приостановка тикания подписки
        реализуется через enter_admin_pause + бит PAUSED."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service:
            return []
        if client.tg_id == config.ADMIN_ID:
            return []                    # клиент админа не блокируется (defense-in-depth)
        self._client_set_block(client_id, bit)
        # приостановка подписки (если запрошена) — до каскада, чтобы PAUSED тоже лёг
        if pause_days is not None:
            self.enter_admin_pause(client_id, pause_days)
            self._client_set_block(client_id, ClientBlock.PAUSED)
        dev_bit = DeviceBlock(int(bit))
        notes: list[Notification] = []
        for dev in self.db.list_devices(client_id):
            self._device_set_block(dev.id, dev_bit)
            if pause_days is not None:
                self._device_set_block(dev.id, DeviceBlock.PAUSED)
            if notify and dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_blocked_text(dev.name)))
        if notify and client.tg_id:
            notes.append(Notification(client.tg_id,
                         _manual_client_blocked()))
        return notes

    def unblock_client_manual(self, client_id: int, bit: ClientBlock,
                             notify: bool) -> list["Notification"]:
        """Снять ручной бит с клиента и каскадно с устройств (тем же типом).
        Если активна админская приостановка — сначала закрываем её через exit_pause
        (пересчёт периода по факту), затем снимаем биты."""
        client = self.db.get_client(client_id)
        if client is None:
            return []
        notes: list[Notification] = []
        had_admin_pause = (client.pause_active_since
                           and client.pause_mode in (PauseMode.ADMIN_FIXED, PauseMode.ADMIN_OPEN))
        if had_admin_pause:
            _, _, _, pause_notes = self.exit_pause(client_id, auto=False)
            notes += pause_notes           # друзьям — о снятии паузы (из exit_pause)
        self._client_clear_block(client_id, bit)
        dev_bit = DeviceBlock(int(bit))
        for dev in self.db.list_devices(client_id):
            self._device_clear_block(dev.id, dev_bit)
            fresh = self.db.get_device(dev.id)
            if (notify and int(fresh.block_reason) == 0
                    and dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id):
                notes.append(Notification(dev.friend_tg_id,
                             _friend_unblocked_text(dev.name)))
        # клиенту — одно уведомление о снятии, если полностью разблокирован и notify
        fresh_c = self.db.get_client(client_id)
        if notify and int(fresh_c.block_reason) == 0 and client.tg_id:
            notes.append(Notification(client.tg_id,
                         _manual_client_unblocked()))
        return notes

    # ── Изменение лимитов потребления (с авто-разблокировкой) ────────────────

    def set_device_traffic_limit(self, device_id: int, limit_bytes: int) -> None:
        """Задать/изменить лимит устройства. Если новый лимит выше текущего
        расхода (или снят в безлимит) — снять причину TRAFFIC_USER (свой лимит
        устройства). Каскад клиента (TRAFFIC_CLIENT) НЕ трогаем."""
        prev = self.db.get_device(device_id)
        if prev is not None and int(prev.traffic_limit) != int(limit_bytes):
            # аудит: снимок старого лимита перед изменением
            self.db.archive_device_quota(device_id, "limit_changed")
        if prev is not None:
            # Парно: проверка лимита считает СУММУ по паре против лимита пары,
            # и разъехавшиеся лимиты строк сделали бы её бессмысленной.
            for peer in self._device_pair(prev):
                self.db.update_device_fields(peer.id, traffic_limit=limit_bytes)
        else:
            self.db.update_device_fields(device_id, traffic_limit=limit_bytes)
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
        if (limit_bytes == 0 or used < limit_bytes) and \
                (int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER)):
            self._device_clear_block(device_id, DeviceBlock.TRAFFIC_USER)
        # снятая метка «over» — чтобы уведомление могло прийти повторно при
        # новом исчерпании после поднятия лимита
        self._forget_traffic_marker(dev.client_id, f"dev_over:{device_id}")
        self._forget_traffic_marker(dev.client_id, f"dev80:{device_id}")

    def set_client_traffic_limit(self, client_id: int, limit_bytes: int) -> None:
        """Задать/изменить тотал-лимит клиента. Если новый лимит выше текущего
        расхода — снять КАСКАДНУЮ причину (TRAFFIC_CLIENT) с клиента и устройств.
        Собственный лимит устройства (DeviceBlock.TRAFFIC_USER) НЕ трогаем — его
        снимет только поднятие лимита самого устройства."""
        prev = self.db.get_client(client_id)
        if prev is not None and prev.tg_id == config.ADMIN_ID:
            return                       # клиент админа не ограничивается (defense-in-depth)
        if prev is not None and int(prev.traffic_limit) != int(limit_bytes):
            # аудит: снимок старой квоты перед изменением
            self.db.archive_quota(client_id, "limit_changed")
        self.db.update_client_fields(client_id, traffic_limit=limit_bytes)
        client = self.db.get_client(client_id)
        if client is None:
            return
        devices = self.db.list_devices(client_id)
        total = sum(int(d.traffic_rx_month) + int(d.traffic_tx_month)
                    for d in devices)
        effective = limit_bytes + int(client.bonus_bytes)
        if limit_bytes == 0 or total < effective:
            if int(client.block_reason) & int(ClientBlock.TRAFFIC_CLIENT):
                self._client_clear_block(client_id, ClientBlock.TRAFFIC_CLIENT)
            for dev in devices:
                if int(dev.block_reason) & int(DeviceBlock.TRAFFIC_CLIENT):
                    self._device_clear_block(dev.id, DeviceBlock.TRAFFIC_CLIENT)
            self._forget_traffic_marker(client_id, "cli_over")
            self._forget_traffic_marker(client_id, "cli80")

    def _forget_traffic_marker(self, client_id: int, marker: str) -> None:
        """Снять одну метку трафик-уведомления (чтобы уведомление могло прийти
        снова после поднятия лимита и повторного исчерпания)."""
        cur = self.db.get_traffic_notified(client_id)
        if marker in cur:
            cur.discard(marker)
            self.db.update_client_fields(
                client_id, traffic_notified=",".join(sorted(cur)))

    # ── Инвайты / клиенты ────────────────────────────────────────────────────

    # Формат инвайт-кода: префикс (C=клиент / F=друг) + 11 символов [A-Za-z0-9].
    # Всего 12. Префикс определяет тип при активации — без сверки по таблицам.
    _CODE_ALPHABET = string.ascii_letters + string.digits   # 62 символа
    _CODE_BODY_LEN = 11

    def _gen_code_body(self) -> str:
        return "".join(secrets.choice(self._CODE_ALPHABET) for _ in range(self._CODE_BODY_LEN))

    def _gen_invite(self) -> str:
        """Клиентский код (префикс C), уникальный среди всех неиспользованных."""
        while True:
            code = "C" + self._gen_code_body()
            if self.db.get_client_by_invite(code) is None \
               and self.db.get_device_by_friend_code(code) is None:
                return code

    def _gen_friend_code(self) -> str:
        """Код друга (префикс F), уникальный среди всех неиспользованных."""
        while True:
            code = "F" + self._gen_code_body()
            if self.db.get_client_by_invite(code) is None \
               and self.db.get_device_by_friend_code(code) is None:
                return code

    def create_client(self, name: str, device_limit: int, period_kind: str,
                      traffic_limit: int = 0) -> ClientCreated:
        if period_kind not in config.PERIOD_CHOICES:
            raise ServiceError(f"Неизвестный период: {period_kind}")
        now = timeutil.now()
        end = None if period_kind == PeriodKind.NEVER else timeutil.add_period(now, period_kind)
        invite = self._gen_invite()
        cid = self.db.create_client(
            name, device_limit, timeutil.to_iso(now),
            timeutil.to_iso(end) if end else None, invite,
            traffic_limit=traffic_limit, period_kind=period_kind,
        )
        credit = self._pause_credit(None, period_kind).after   # первый оплаченный период
        if credit:
            self.db.set_pause_balance(cid, credit)
        return ClientCreated(client_id=cid, invite_code=invite, period_end=end)

    def activate_client(self, invite_code: str, tg_id: int) -> ActivationResult:
        existing = self.db.get_client_by_tg(tg_id)
        if existing is not None and not existing.is_service and not existing.is_guest:
            return ActivationResult(ok=False, reason="already_has_access", client=existing)
        row = self.db.get_client_by_invite(invite_code)
        if row is None:
            return ActivationResult(ok=False, reason="invalid")
        upgrade = None
        # Переход гостя и активация — один коммит: гостевой профиль удаляется
        # раньше (tg_id занят), и упасть между этим и активацией значило бы
        # оставить человека без профиля, а устройства — у никого.
        with self.db.transaction():
            if existing is not None and existing.is_guest:
                upgrade = self._upgrade_guest(existing, row.id)
            self.db.activate_client(row.id, tg_id)
        if upgrade is not None and upgrade.moved:
            self.reconcile_routing()                  # наборы — после коммита
        return ActivationResult(ok=True, reason="ok", client=self.db.get_client(row.id),
                                upgrade=upgrade)

    def _upgrade_guest(self, guest, new_client_id: int) -> "GuestUpgrade":
        """Гость становится владельцем (docs/guest-role.md): ВСЕ переданные ему
        устройства переходят в новый профиль независимо от лимита («3 из 2»
        честно), слоты дарителю возвращаются, управлять ими он больше не может.
        Пиры не трогаются. Личный список адресов едет с гостем. Гостевой
        профиль закрывается — иначе tg_id занят и активация не пройдёт.
        Только БД: вызывается внутри транзакции activate_client."""
        held = self.db.list_held_devices(guest.id)
        donor = self.db.get_client(held[0].client_id) if held else None
        for dev in held:
            for peer in self._device_pair(dev):
                self.db.update_device_fields(peer.id, client_id=new_client_id,
                                             holder_client_id=None)
            self._recompute_device_blocks(dev.id, new_client_id)
        self.db.move_routing_domains(guest.id, new_client_id)
        self.db.delete_client(guest.id, archive_reason="upgraded")
        return GuestUpgrade(donor=donor, moved=held)

    def _recompute_device_blocks(self, device_id: int, client_id: int) -> None:
        """Каскадные биты прежнего владельца (истечение, пауза) с устройства
        снять и наложить по новому: устройство переехало вместе с записью, а
        бит — это состояние подписки, которая осталась у прежнего."""
        client = self.db.get_client(client_id)
        if client is None or self.db.get_device(device_id) is None:
            return
        for bit, want in ((DeviceBlock.EXPIRY, client.status == SubStatus.EXPIRED),
                          (DeviceBlock.PAUSED, client.is_paused)):
            if want:
                self._device_set_block(device_id, bit)
            else:
                self._device_clear_block(device_id, bit)

    def regenerate_invite(self, client_id: int) -> str:
        """Перевыпуск инвайта для pending-клиента (потерял ссылку до активации)."""
        client = self.db.get_client(client_id)
        if client is None:
            raise ServiceError("Клиент не найден")
        if client.activation_status != ActivationStatus.PENDING:
            raise ServiceError("Клиент уже активирован — инвайт не нужен")
        code = self._gen_invite()
        self.db.update_client_fields(client_id, invite_code=code)
        return code

    # ── Устройства ───────────────────────────────────────────────────────────

    def add_device(self, client_id: int, name: str, traffic_limit: int = 0) -> DeviceCreated:
        """Поток 2: генерация ключей → аллокация IP → БД → awg.add_peer →
        конфиг. При сбое применения — откат БД.

        """
        client = self.db.get_client(client_id)
        if client is None:
            raise ServiceError("Клиент не найден")
        if not client.is_service:
            limit = client.device_limit
            if limit != 0 and self.db.count_devices(client_id) >= limit:  # 0 = безлимит
                raise LimitReached("Достигнут лимит устройств")

        # Весь блок «аллокация IP → запись в БД → применение в контейнере» под
        # мьютексом мутаций: закрывает гонку двух одновременных добавлений
        # (одинаковый IP / потерянный peer при конкурентной правке конфига).
        with awg.mutation_lock:
            # аллокация: занятые из БД + из живого конфига (учёт чужих пиров)
            occupied_live = awg.read_occupied_ips()
            ip = self.db.allocate_ip(
                subnet_prefix=config.SUBNET_PREFIX,
                occupied_extra=occupied_live,
                start_host=config.IP_HOST_START,
                end_host=config.IP_HOST_END,
            )
            priv, pub = awg.gen_keypair()
            server_params = awg.read_server_params()
            psk = server_params["psk"]

            # БД сначала (дешёвый откат)
            try:
                device_id = self.db.create_device(
                    client_id, name, pub, psk, ip, private_key=priv,
                    traffic_limit=traffic_limit,
                )
            except sqlite3.IntegrityError as e:
                raise ServiceError(f"Конфликт при создании устройства, попробуй ещё раз: {e}")
            try:
                awg.add_peer(pub, psk, ip)
            except awg.AwgError as e:
                self.db.delete_device(device_id, archive_reason=None)  # откат, не архивируем
                raise ServiceError(f"Не удалось применить конфиг на сервере: {e}")

        # если клиент сейчас истёкший — новое устройство тоже блокируем (EXPIRY)
        if not client.is_service and client.status == SubStatus.EXPIRED:
            self._device_set_block(device_id, DeviceBlock.EXPIRY)

        # Профилю разрешён РФ-доступ — новое устройство сразу в режиме: человеку
        # обещано «включено для всех твоих устройств», и новое — не исключение
        # (решение 16.09.2026; до этого приходило выключенным, чтобы не ходить
        # через шлюз молча — но выключить одно устройство проще, чем каждый раз
        # включать). Двойник переезда наследует флаг при рождении.
        if not client.is_service and client.routing_allowed:
            self.db.update_device_fields(device_id, routing_on=1)
            self.reconcile_routing()

        # В окне переезда устройство заводится ПАРОЙ, как все остальные, и
        # человеку выдаётся новый конфиг. Пара нужна не для красоты: до
        # завершения переезд можно отменить, а отмена возвращает людей на старые
        # пиры. Роди мы только новый — отменять для этого человека было бы нечем,
        # и он остался бы единственным, кого откат выбрасывает.
        if self.migration_running():
            try:
                twin_id = self._birth_twin(self.db.get_device(device_id))
            except Exception as e:                        # noqa: BLE001
                log.warning("add_device: двойник для %s не создан: %s", name, e)
            else:
                if client.tg_id == config.ADMIN_ID:
                    self.reconcile_ssh_access()
                twin = self.db.get_device(twin_id)
                cfg = self.generate_config(twin_id)
                return DeviceCreated(device_id=twin_id, address=twin.address,
                                     vpn=cfg["vpn"], conf=cfg["conf"])

        cfg = configgen.generate(priv, pub, ip, server_params, iface=config.AWG_INTERFACE)
        # новое устройство админа → сразу открыть ему SSH-к-хосту (не ждать цикла)
        if client.tg_id == config.ADMIN_ID:
            self.reconcile_ssh_access()
        return DeviceCreated(device_id=device_id, address=ip, vpn=cfg["vpn"], conf=cfg["conf"])

    def remove_device(self, device_id: int) -> Optional[int]:
        """Удаление устройства: сервер → БД (в таком порядке, чтобы не осталось
        записи в БД без реального снятия пира).
        Возвращает friend_tg_id, если у устройства был активный друг (для
        уведомления, что доступ прекращён), иначе None."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return None
        self._refuse_if_gateway(dev)
        friend_tg = dev.friend_tg_id if dev.friend_status == FriendStatus.ACTIVE else None
        # Снятие ПАРНОЕ. В окне переезда у устройства два пира на двух
        # интерфейсах; снять только видимый значит оставить второй работать —
        # призрачный доступ у того, кого человек считает удалённым. Ровно тот
        # класс, ради которого сверка перестала усыновлять неизвестных.
        for peer in self._device_pair(dev):
            try:
                awg.remove_peer(peer.public_key, iface=awg.iface_of(peer.iface))
            except awg.AwgError as e:
                raise ServiceError(f"Не удалось снять устройство на сервере: {e}")
        # DROP снимаем ПОСЛЕ успешного снятия пира: если remove_peer упал,
        # устройство осталось в конфиге и должно остаться заблокированным.
        # Снять обязательно — иначе осиротевшее правило заблокирует будущего
        # владельца этого IP (аллокатор переиспользует освободившиеся адреса).
        if int(dev.block_reason) != 0:
            for peer in self._device_pair(dev):
                try:
                    awg.unblock_ip(peer.address)
                except awg.AwgError:
                    pass
        for peer in self._device_pair(dev):
            if peer.id != device_id:
                self.db.delete_device(peer.id, archive_reason=None)
        self.db.delete_device(device_id)
        return friend_tg

    def generate_config(self, device_id: int, *, for_bundle: bool = False) -> dict:
        """Перевыпуск конфига устройства. Только для устройств, созданных ботом:
        приватный ключ есть лишь у них. Шлюзу ссылку не выдаём: его конфиг едет
        только внутри конфигурации шлюза (for_bundle)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if not for_bundle:
            self._refuse_if_gateway(dev)
        if not dev.private_key:
            raise ServiceError(
                "Это устройство создавал не бот — приватного ключа у него нет, "
                "выдать ссылку не из чего. Удали его и добавь новое через бота."
            )
        # Параметры берём у ТОГО интерфейса, где живёт пир. Общие отдали бы
        # конфигу двойника старый порт, старый серверный ключ и старую
        # обфускацию: превью выглядит нормально, а не подключается никто.
        iface = awg.iface_of(dev.iface)
        server_params = awg.read_server_params(iface=iface)
        return configgen.generate(dev.private_key, dev.public_key, dev.address,
                                  server_params, iface=iface)

    def rename_device(self, device_id: int, new_name: str) -> None:
        """Переименование устройства. Имя живёт только в нашей БД: сервер про
        имена не знает, в конфиге пира их нет."""
        new_name = new_name.strip()
        if not new_name:
            raise ServiceError("Имя не может быть пустым")
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        # Парно: иначе отмена переезда вернула бы старую строку со старым
        # именем, молча откатив переименование.
        for peer in self._device_pair(dev):
            self.db.update_device_fields(peer.id, name=new_name)

    # ── Друзья (роль invited): приглашение на управление одним устройством ────

    def make_device_friendly(self, device_id: int) -> str:
        """Помечает СУЩЕСТВУЮЩЕЕ устройство гостевым: генерит код друга, ставит
        pending. Возвращает код для пересылки. Требует bot-устройство (у app нет
        ссылки — другу нечего было бы выдать)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        self._refuse_if_gateway(dev)
        if not dev.private_key:
            raise ServiceError("Это устройство создавал не бот — передать его нельзя: "
                               "у бота нет ссылки, которую можно было бы выдать другу")
        if dev.friend_status == FriendStatus.ACTIVE:
            raise ServiceError("Устройством уже управляет друг")
        code = self._gen_friend_code()
        self.db.set_device_friend(device_id, friend_code=code, friend_status=FriendStatus.PENDING)
        return code

    def reissue_friend_code(self, device_id: int) -> str:
        """Перевыдать код друга — только пока приглашение не активировано."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if dev.friend_status != FriendStatus.PENDING:
            raise ServiceError("Перевыдать код можно только для неактивированного приглашения")
        code = self._gen_friend_code()
        self.db.set_device_friend(device_id, friend_code=code, friend_status=FriendStatus.PENDING)
        return code

    def activate_friend(self, code: str, tg_id: int, tg_name: str = "") -> "FriendActivation":
        """Активация кода F… (docs/guest-role.md). Держателем становится профиль
        этого tg: гость (заводится при первом коде, имя — из Telegram) или
        обычный клиент. Правило одного дарителя: у держателя уже есть устройства
        от другого владельца → отказ other_donor, код не сгорает. Админ и своё
        устройство — отказ."""
        dev = self.db.get_device_by_friend_code(code)
        if dev is None or dev.friend_status != FriendStatus.PENDING:
            return FriendActivation(ok=False, reason="invalid")
        if tg_id == config.ADMIN_ID:
            return FriendActivation(ok=False, reason="already_user")
        holder = self.db.get_client_by_tg(tg_id)
        if holder is not None and holder.is_service:
            holder = None
        if holder is not None and holder.id == dev.client_id:
            return FriendActivation(ok=False, reason="own_device", device_id=dev.id,
                                    device_name=dev.name, holder=holder)
        held = self.db.list_held_devices(holder.id) if holder is not None else []
        if held and held[0].client_id != dev.client_id:
            return FriendActivation(ok=False, reason="other_donor", device_id=dev.id,
                                    device_name=dev.name, holder=holder,
                                    donor=self.db.get_client(held[0].client_id), held=held)
        if holder is None:
            # профильное имя — раз, при рождении; дальше человека зовут по
            # имени аккаунта (clients.tg_name), его ведёт middleware
            hid = self.db.create_guest_client(tg_id, (tg_name or "Друг").strip()[:64])
            holder = self.db.get_client(hid)
        for peer in self._device_pair(dev):
            self.db.set_device_holder(peer.id, holder.id)
        if dev.routing_on:
            self.reconcile_routing()                  # адрес переезжает в набор держателя
        return FriendActivation(ok=True, reason="ok", device_id=dev.id, device_name=dev.name,
                                holder=holder, donor=self.db.get_client(dev.client_id),
                                held=self.db.list_held_devices(holder.id))

    def rekey_device(self, device_id: int) -> None:
        """Перевыпустить ключи устройства: прежний конфиг у людей перестаёт
        работать, имя и адрес те же. Нужно, когда устройство уходит от
        держателя к другому владельцу (docs/guest-role.md): иначе прежний
        держатель сохранил бы доступ по чужому теперь устройству. Сервер —
        снять старый пир, поставить новый; не поднялся — вернуть старый."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        self._refuse_if_gateway(dev)
        if not dev.private_key:
            raise ServiceError("Это устройство создавал не бот — перевыпустить ключи нельзя")
        priv, pub = awg.gen_keypair()
        for peer in self._device_pair(dev):
            iface = awg.iface_of(peer.iface)
            try:
                awg.remove_peer(peer.public_key, iface=iface)
                awg.add_peer(pub, peer.preshared_key, peer.address, iface=iface)
            except awg.AwgError as e:
                try:
                    awg.add_peer(peer.public_key, peer.preshared_key, peer.address, iface=iface)
                except awg.AwgError:
                    pass
                raise ServiceError(f"Не удалось перевыпустить ключи на сервере: {e}")
            self.db.update_device_fields(peer.id, public_key=pub, private_key=priv)

    def reassign_device(self, device_id: int, new_client_id: int,
                        add_slot: bool = False) -> dict:
        """Перепривязка устройства к клиенту. Если add_slot — заодно поднимаем
        лимит на 1 (когда у получателя не было свободного слота).
        Возвращает данные для уведомления ОБОИХ сторон:
          name, donor{tg_id,count,limit}, recipient{tg_id,count,limit},
          added_slot."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        self._refuse_if_gateway(dev)
        client = self.db.get_client(new_client_id)
        if client is None:
            raise ServiceError("Клиент не найден")
        donor = self.db.get_client(dev.client_id)          # прежний владелец
        with self.db.transaction():
            new_limit = client.device_limit
            # add_slot поднимает лимит, НО безлимит (0) не трогаем: протухшая
            # кнопка «добавить слот» не должна превращать безлимит в лимит-1
            slot_bumped = add_slot and new_limit != 0
            if slot_bumped:
                new_limit += 1
                self.db.update_client_fields(new_client_id, device_limit=new_limit)
            else:
                # Повторная проверка лимита ВНУТРИ транзакции — закрывает TOCTOU
                # между has_free_slot в хендлере и фактической привязкой. Без слота
                # и без add_slot привязывать нельзя (иначе появляется «3 из 2»).
                if new_limit != 0 and self.db.count_devices(new_client_id) >= new_limit:
                    raise LimitReached(
                        "У клиента нет свободного слота — привязка отклонена")
            # Парно: перенеси одну строку — и пара разорвётся между профилями,
            # старый пир останется у донора, а завершение переезда сольёт
            # трафик и заархивирует устройство не тому человеку.
            if dev.holder_client_id is not None and dev.holder_client_id != new_client_id:
                # Прежний держатель теряет не только управление, но и ДОСТУП:
                # ключи перевыпускаются (имя и адрес те же), его конфиг мёртв.
                # Внутри транзакции: не поднялся новый пир — БД не тронута.
                self.rekey_device(dev.id)
            for peer in self._device_pair(dev):
                self.db.reassign_device(peer.id, new_client_id)
                # Держатель снимается всегда: устройство переехало к другому
                # владельцу, а держать чужое можно только от одного дарителя
                # (docs/guest-role.md) — оставить его значило бы нарушить это
                # правило руками админа. Стал владельцем сам — держать нечего.
                if dev.holder_client_id is not None:
                    self.db.set_device_holder(peer.id, None)
        # счётчики ПОСЛЕ перепривязки (живой COUNT — уже актуальны)
        donor_count = self.db.count_devices(donor.id) if donor else 0
        recip_count = self.db.count_devices(new_client_id)
        return {
            "name": dev.name,
            "added_slot": slot_bumped,
            "donor": None if (donor is None or donor.is_service) else {
                "tg_id": donor.tg_id, "count": donor_count, "limit": donor.device_limit,
            },
            "recipient": {
                "tg_id": client.tg_id, "count": recip_count, "limit": new_limit,
            },
            # прежний держатель (если был и не стал владельцем) — ему сказать
            "holder_tg": (dev.holder_tg_id
                          if dev.holder_client_id not in (None, new_client_id) else None),
        }

    def ensure_admin_client(self) -> int:
        """Гарантирует существование клиентской записи админа (он тоже юзер VPN).
        Бессрочная (period_end NULL), безлимит (device_limit 0), сразу active,
        привязана к ADMIN_ID. Опознаётся по tg_id == ADMIN_ID (без новой колонки),
        скрыта из списка клиентов. Идемпотентно — зовётся при старте."""
        existing = self.db.get_client_by_tg(config.ADMIN_ID)
        if existing is not None:
            return existing.id
        now = timeutil.now()
        cid = self.db.create_client(
            "Администратор", 0, timeutil.to_iso(now), None, self._gen_invite(),
        )
        self.db.update_client_fields(
            cid, tg_id=config.ADMIN_ID, activation_status=ActivationStatus.ACTIVE, status=SubStatus.ACTIVE,
        )
        return cid

    _ADMIN_BOOTSTRAP_KEY = "admin_bootstrap_device"

    def bootstrap_admin_device(self) -> Optional["DeviceCreated"]:
        """Первое устройство админа заводим САМИ, ровно один раз.

        Прямое следствие карантина: пир, созданный в обход бота, больше не
        усыновляется, а становится тревогой. Значит взять доступ «снаружи» —
        приложением или руками в конфиге — больше нельзя, а он нужен: админ
        ходит в Telegram через этот же VPN, и без устройства он не достучится
        до бота, чтобы завести себе устройство. Замкнутый круг размыкаем здесь.

        Ровно один раз, и по двум признакам сразу: метка в state И отсутствие
        устройств. Метка одна не годится для уже работающих установок (её там
        нет, а устройства есть — получили бы лишнее). Отсутствие устройств одно
        не годится для админа, который СОЗНАТЕЛЬНО удалил у себя всё: бот
        возвращал бы удалённое на каждом рестарте.

        Метку ставим только после успеха: не поднялся awg — повторим на
        следующем старте, а не потеряем бутстрап навсегда.
        """
        if self.db.get_state(self._ADMIN_BOOTSTRAP_KEY) == "1":
            return None
        cid = self.ensure_admin_client()
        if self.db.count_devices(cid):
            self.db.set_state(self._ADMIN_BOOTSTRAP_KEY, "1")   # уже есть — считаем сделанным
            return None
        created = self.add_device(cid, "Админ")
        self.db.set_state(self._ADMIN_BOOTSTRAP_KEY, "1")
        return created

    def admin_client(self):
        """Клиентская запись админа (или None, если ещё не создана)."""
        return self.db.get_client_by_tg(config.ADMIN_ID)

    def has_free_slot(self, client_id: int) -> bool:
        client = self.db.get_client(client_id)
        if client is None:
            return False
        if client.device_limit == 0:            # 0 = безлимит
            return True
        return self.db.count_devices(client_id) < client.device_limit

    # ── Продление ────────────────────────────────────────────────────────────

    def remaining_for(self, client_id: int) -> int:
        """Секунд до конца текущего периода (для диалога сохранения остатка)."""
        client = self.db.get_client(client_id)
        if client is None or not client.period_end:
            return 0
        end = timeutil.parse_iso(client.period_end)
        return max(0, timeutil.remaining_seconds(end))

    def extend_period(self, client_id: int, period_kind: str, keep_remainder: bool) -> ExtendResult:
        """Поток 3: закрыть текущий период, создать новый (+остаток если keep),
        обнулить ПЕРИОДНЫЙ трафик, снять блокировку если был истёкшим, уведомить."""
        if period_kind not in config.PERIOD_CHOICES:
            raise ServiceError(f"Неизвестный период: {period_kind}")
        client = self.db.get_client(client_id)
        if client is None:
            raise ServiceError("Клиент не найден")

        # Клиент на паузе (любой режим) → корректно закрыть паузу ДО продления:
        # exit_pause пересчитает period_end по факту и снимет PAUSED-каскад с
        # устройств. Без этого archive_pause ниже снёс бы строку паузы, а биты
        # PAUSED остались бы навечно (снять их через UI больше нечем).
        pause_exit_notes: list[Notification] = []
        if client.pause_active_since:
            _, _, _, pause_exit_notes = self.exit_pause(client_id, auto=False)
            client = self.db.get_client(client_id)

        extra = self.remaining_for(client_id) if keep_remainder else 0
        pause_credit = self._pause_credit(client, period_kind)   # по СТАРОМУ состоянию
        new_start = timeutil.now()
        never = period_kind == PeriodKind.NEVER
        # «долг» отсрочки вычитается из нового периода (никогда — из «never»:
        # безлимитному вычитать не из чего). Фильтр периодов в UI гарантирует, что
        # выбранный период длиннее долга, так что в минус не уходим.
        pending_cut = 0 if never else int(client.grace_pending_cut)
        new_end = None if never else timeutil.add_period(
            new_start, period_kind, extra_seconds=extra - pending_cut)

        # периодный трафик обнуляем; месячный НЕ трогаем (свой цикл)
        self.db.reset_period_traffic(client_id)

        # возврат из истёкшего → снять причину EXPIRY со всех устройств. Именно
        # бит, не «разблокировать всё»: устройство может быть заблокировано ещё и
        # по трафику — эту причину продление подписки снимать не должно.
        friend_unblock_notes = []
        if client.status == SubStatus.EXPIRED:
            self._client_clear_block(client_id, ClientBlock.EXPIRY)
            for dev in self.db.list_devices(client_id):
                had_traffic = int(dev.block_reason) & int(DEVICE_TRAFFIC_ANY)
                self._device_clear_block(dev.id, DeviceBlock.EXPIRY)
                # уведомляем друга только если доступ РЕАЛЬНО вернулся (не остался
                # заблокирован по трафику)
                if (not had_traffic and dev.friend_status == FriendStatus.ACTIVE
                        and dev.friend_tg_id):
                    friend_unblock_notes.append(Notification(
                        dev.friend_tg_id, _friend_unblocked_text(dev.name)))

        # аудит перед сменой периода: снимок закрываемой подписки + закрытие
        # эпизодов grace/pause со сбросом (новый период — права заново)
        self.db.archive_subscription(client_id, "renewed")
        self.db.archive_grace(client_id, "new_period")   # снесёт строку, если была
        self.db.archive_pause(client_id, "new_period")   # снимок эпизода + сброс used_days
        self.db.set_pause_balance(client_id, pause_credit.after)   # счёт — заново (0 у бессрочной)
        self.db.update_client_fields(
            client_id,
            period_start=timeutil.to_iso(new_start),
            period_end=timeutil.to_iso(new_end) if new_end else None,
            period_kind=period_kind,
            status=SubStatus.ACTIVE,
        )
        self.db.reset_notified(client_id)                # новый период — пороги заново

        notifications = pause_exit_notes + friend_unblock_notes
        if client.tg_id:
            msg = (_TXT_EXTENDED_FOREVER if new_end is None
                   else _TXT_EXTENDED.format(end=timeutil.fmt_dt(new_end)))
            from awgbot.bot import texts                   # ленивый, как в соседних миксинах
            line = texts.pause_credit_line(pause_credit)   # что стало со счётом паузы
            if line:
                msg += f".\n{line}" if not msg.endswith(".") else f"\n{line}"
            notifications.append(Notification(client.tg_id, msg))
        return ExtendResult(new_end=new_end, notifications=notifications, pause=pause_credit)

    def set_subscription_dates(self, client_id: int, new_start, new_end):
        """Прямая правка дат подписки админом (не продление): пишем ровно
        заданные даты. Статус пересчитываем по new_end относительно now:
        будущее → active, прошлое → expired. При СМЕНЕ статуса приводим в
        порядок блокировки устройств (как watchdog/extend), иначе получим
        рассинхрон «подписка активна, а устройства заблокированы по EXPIRY»
        (или наоборот). period_kind и pause НЕ трогаем. Возвращает
        (start, end, notifications)."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service:
            raise ServiceError("Профиль не найден")
        was_expired = client.status == SubStatus.EXPIRED
        # new_end=None → бессрочная (никогда не истекает) → всегда active
        now_expired = new_end is not None and new_end <= timeutil.now()
        status = SubStatus.EXPIRED if now_expired else SubStatus.ACTIVE
        self.db.update_client_fields(
            client_id,
            period_start=timeutil.to_iso(new_start),
            period_end=timeutil.to_iso(new_end) if new_end else None,
            status=status,
        )
        self.db.reset_notified(client_id)     # период сменился — пороги истечения заново

        notifications: list[Notification] = []
        if was_expired and not now_expired:
            # реактивация: снять причину EXPIRY (но не трогать блок по трафику)
            self._client_clear_block(client_id, ClientBlock.EXPIRY)
            for dev in self.db.list_devices(client_id):
                had_traffic = int(dev.block_reason) & int(DEVICE_TRAFFIC_ANY)
                self._device_clear_block(dev.id, DeviceBlock.EXPIRY)
                if (not had_traffic and dev.friend_status == FriendStatus.ACTIVE
                        and dev.friend_tg_id):
                    notifications.append(Notification(
                        dev.friend_tg_id, _friend_unblocked_text(dev.name)))
            if client.tg_id:
                msg = (_TXT_EXTENDED_FOREVER if new_end is None
                       else _TXT_EXTENDED.format(end=timeutil.fmt_dt(new_end)))
                notifications.append(Notification(client.tg_id, msg))
        elif not was_expired and now_expired:
            # админ поставил прошлую дату → истекло: заблокировать как watchdog
            fresh = self.db.get_client(client_id)
            notifications.extend(self._block_client(fresh))
            if client.tg_id:
                notifications.append(Notification(client.tg_id, _TXT_EXPIRED_CLIENT))
        return new_start, new_end, notifications




    # ── объявление с продлением: правая граница периода уезжает на N дней ────

    def extension_plan(self, client_ids, days: int) -> list["DaysExtension"]:
        """Что даст продление на days дней каждому профилю — без записи, для
        превью. Активная — конец + N; истёкшая — сегодня + N (плюшка за простой
        или «неделька на слюнки» — от старого конца она была бы пустой);
        открытая админ-пауза — сохранённый конец (period_end на ней пуст);
        бессрочная — ничего. Порядок — по имени профиля."""
        now = timeutil.now()
        delta = datetime.timedelta(days=int(days))
        out: list[DaysExtension] = []
        for cid in sorted({int(c) for c in (client_ids or ())}):
            c = self.db.get_client(cid)
            if c is None or c.is_service:
                continue
            end_iso = c.effective_period_end
            if not end_iso:
                out.append(DaysExtension(c))
                continue
            old = timeutil.parse_iso(end_iso)
            if c.status == SubStatus.EXPIRED or old <= now:
                out.append(DaysExtension(c, old, now + delta, from_now=True))
            else:
                out.append(DaysExtension(c, old, old + delta))
        out.sort(key=lambda e: e.client.name.lower())
        return out

    def extend_days(self, client_ids, days: int) -> tuple[list["DaysExtension"], list["Notification"]]:
        """Применить extension_plan — одной транзакцией. Начало и тип периода,
        периодный трафик, долг отсрочки не трогаются: только правая граница
        уезжает на N дней. Истёкшим снимается EXPIRY (как при правке дат),
        держателям их устройств — «доступ вернулся»; штатное «Подписка
        продлена до …» владельцу глушится — эту роль играет шапка объявления."""
        plan = self.extension_plan(client_ids, days)
        notes: list[Notification] = []
        with self.db.transaction():
            for e in plan:
                if e.unlimited:
                    continue
                c = e.client
                if c.pause_mode == PauseMode.ADMIN_OPEN and c.pause_saved_end:
                    self.db.update_client_fields(c.id, pause_saved_end=timeutil.to_iso(e.new_end))
                    continue
                start = timeutil.parse_iso(c.period_start) if c.period_start else timeutil.now()
                _, _, ns = self.set_subscription_dates(c.id, start, e.new_end)
                notes.extend(n for n in ns if n.tg_id != c.tg_id)
        return plan, notes

    def activate_grace(self, client_id: int, days: int):
        """Клиент сам продлевает годовую подписку на `days` дней (один раз за
        период). Возвращает (ok, end|None): ok=False если предложение протухло
        (истёк / уже использовано / не годовой). Долг фиксируем снимком в секундах
        — вычтется при следующем продлении."""
        client = self.db.get_client(client_id)
        if client is None:
            return (False, None)
        if (client.status == SubStatus.EXPIRED or client.grace_used
                or client.period_kind != PeriodKind.YEAR or not client.period_end):
            return (False, None)
        end = timeutil.parse_iso(client.period_end)
        new_end = end + datetime.timedelta(days=days)
        self.db.update_client_fields(
            client_id,
            period_end=timeutil.to_iso(new_end),
            grace_used=1,
            grace_pending_cut=days * SECONDS_PER_DAY,
        )
        return (True, new_end)

    # ── Приостановка подписки («в отпуск») ───────────────────────────────────

    # ── счёт дней паузы (docs: README «Приостановка подписки») ──────────────
    # Годовая: +28 при создании на год и при каждом продлении на год (даже
    # несвоевременном), копится до двух таких. Ежемесячная: +2 за каждый
    # своевременно оплаченный месяц (создание и продление, пока подписка не
    # истекла и без отсрочки), копится до 12 таких. Смена типа: остаток
    # переносится в пределах максимума нового типа. День/неделя: счёт не
    # пополняется, остаток живёт. Бессрочно: останавливать нечего — счёт ноль.

    @staticmethod
    def pause_year_days() -> int:
        return settings.get_int("pause.pause_max_total_days", 28)

    @staticmethod
    def pause_month_days() -> int:
        return settings.get_int("pause.monthly_pause_days", 2)

    @classmethod
    def pause_month_cap(cls) -> int:
        return 12 * cls.pause_month_days()

    @classmethod
    def pause_year_cap(cls) -> int:
        return 2 * cls.pause_year_days()

    def _pause_credit(self, client, new_kind: str) -> "PauseCredit":
        """Счёт после оплаты периода new_kind. client — состояние ДО (None при
        создании). Ежемесячно «своевременно» = подписка не истекла и без
        отсрочки в закрываемом периоде; годовое начисление — безусловное.
        Накопленное сверх порога нового типа не сгорает — просто выше порога
        не начисляется."""
        bal = int(client.pause_balance_days) if client is not None else 0
        if new_kind == PeriodKind.YEAR:
            add, cap = self.pause_year_days(), self.pause_year_cap()
        elif new_kind == PeriodKind.MONTH:
            add, cap = self.pause_month_days(), self.pause_month_cap()
            if client is not None and client.status == SubStatus.EXPIRED:
                return PauseCredit(new_kind, bal, bal, cap, "expired")
            if client is not None and client.grace_used:
                return PauseCredit(new_kind, bal, bal, cap, "grace")
        elif new_kind == PeriodKind.NEVER:
            return PauseCredit(new_kind, bal, 0, 0, "none")
        else:
            return PauseCredit(new_kind, bal, bal, 0, "none")
        if bal >= cap:
            return PauseCredit(new_kind, bal, bal, cap, "cap")
        return PauseCredit(new_kind, bal, min(bal + add, cap), cap, "credited")

    def pause_available_days(self, client_id: int) -> int:
        """Сколько дней приостановки клиент может взять ПРЯМО СЕЙЧАС — его счёт.
        Бессрочной паузе нечего останавливать — 0. Ни остатком подписки, ни
        «максимумом за один вход» не ограничиваем."""
        client = self.db.get_client(client_id)
        if client is None or not client.period_end:
            return 0
        return max(0, int(client.pause_balance_days))

    _PAUSE_BALANCE_MIGRATED = "pause_balance_migrated"

    def migrate_pause_balances(self) -> int:
        """Разово после обновления на счёт паузы: годовым — остаток старого
        лимита (28 − использовано − резерв текущей паузы), ежемесячным — по
        числу оплаченных месяцев (создание + продления с месячного, все
        считаются своевременными) в пределах максимума, минус то же. Возвращает
        число профилей, получивших счёт."""
        if self.db.get_state(self._PAUSE_BALANCE_MIGRATED) == "1":
            return 0
        n = 0
        with self.db.transaction():
            for c in self.db.list_clients(include_service=False):
                spent = int(c.pause_used_days)
                if c.is_paused and c.pause_mode == PauseMode.USER:
                    spent += int(c.pause_reserved_days)
                if c.period_kind == PeriodKind.YEAR:
                    bal = self.pause_year_days() - spent
                elif c.period_kind == PeriodKind.MONTH:
                    months = 1 + self.db.monthly_renewals(c.id)
                    bal = min(months * self.pause_month_days(), self.pause_month_cap()) - spent
                else:
                    continue
                if bal > 0:
                    self.db.set_pause_balance(c.id, bal)
                    n += 1
            self.db.set_state(self._PAUSE_BALANCE_MIGRATED, "1")
        if n:
            log.info("пауза: счёт дней выдан %d профилям", n)
        return n

    def enter_pause(self, client_id: int, days: int = None):
        """Клиентский самоблок (mode=user). Резервирует `days` дней вперёд
        (сдвигает period_end), ставит PAUSED клиенту и каскадом устройствам.
        days=None — берёт весь доступный максимум (обратная совместимость);
        иначе резервирует ровно min(days, доступное). Возвращает
        (ok, reserved_days, notifications)."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service:
            return (False, 0, [], None)
        if client.pause_active_since or int(client.block_reason) & int(ClientBlock.PAUSED):
            return (False, 0, [], None)
        avail = self.pause_available_days(client_id)
        reserved = avail if days is None else max(0, min(int(days), avail))
        if reserved <= 0:
            return (False, 0, [], None)
        now = timeutil.now()
        end = timeutil.parse_iso(client.period_end)
        new_end = end + datetime.timedelta(days=reserved)
        # одноразовый код email-выхода — генерим всегда при входе (даже если
        # email-выход выключен: код безвреден, а включат фичу позже — сработает).
        code = email_resume.generate_code()
        # процесс + сопутствующие поля — атомарно (вложенные _tx коммитятся разом)
        with self.db.transaction():
            self.db.save_pause(client_id, models.PauseState(
                active_since=timeutil.to_iso(now), reserved_days=reserved,
                mode=PauseMode.USER,
                used_days=int(client.pause_used_days),   # накопленное за период
                balance_days=int(client.pause_balance_days) - reserved,   # резерв списан
                resume_code=code))
            self.db.update_client_fields(
                client_id,
                period_end=timeutil.to_iso(new_end),
                block_reason=int(client.block_reason) | int(ClientBlock.PAUSED))
        notes: list[Notification] = []
        for dev in self.db.list_devices(client_id):
            self._device_set_block(dev.id, DeviceBlock.PAUSED)
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _pause_friend_started(dev.name)))
        return (True, reserved, notes, code)

    def enter_admin_pause(self, client_id: int, days: int):
        """Приостановка подписки при АДМИНСКОМ блоке клиента. days>0 — срочная
        (admin_fixed: +days вперёд, авто-выход по сроку); days==0 — бессрочная
        (admin_open: period_end→NULL temp, снимок в pause_saved_end, пересчёт при
        снятии). Без лимита 28 и без привязки к годовой. НЕ ставит сам блок-бит
        (это делает вызывающий block_client_manual) и НЕ шлёт уведомлений
        (уведомляет вызывающий по notify-флагу). Возвращает reserved (0 у open)."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service or client.pause_active_since:
            return 0
        now = timeutil.now()
        with self.db.transaction():
            if days > 0:
                # срочная: сдвигаем период вперёд, как самоблок
                self.db.save_pause(client_id, models.PauseState(
                    active_since=timeutil.to_iso(now), reserved_days=days,
                    mode=PauseMode.ADMIN_FIXED,
                    used_days=int(client.pause_used_days),
                    balance_days=int(client.pause_balance_days)))
                if client.period_end:
                    end = timeutil.parse_iso(client.period_end)
                    self.db.update_client_fields(client_id, period_end=timeutil.to_iso(
                        end + datetime.timedelta(days=days)))
            else:
                # бессрочная: подписка temp-бессрочная, конец сохраняем для пересчёта
                self.db.save_pause(client_id, models.PauseState(
                    active_since=timeutil.to_iso(now), reserved_days=0,
                    mode=PauseMode.ADMIN_OPEN, saved_end=client.period_end,
                    used_days=int(client.pause_used_days),
                    balance_days=int(client.pause_balance_days)))
                self.db.update_client_fields(client_id, period_end=None)
        return days

    def preview_exit_pause(self, client_id: int):
        """Read-only предпросчёт для диалога подтверждения возобновления:
        сколько дней пауза УЖЕ длилась (спишется при выходе) против
        зарезервированных. Ничего не меняет в БД. Возвращает (actual, reserved)
        или None, если клиент не на паузе."""
        client = self.db.get_client(client_id)
        if client is None or not client.pause_active_since:
            return None
        mode = client.pause_mode or PauseMode.USER
        since = timeutil.parse_iso(client.pause_active_since)
        now = timeutil.now()
        actual = timeutil.ceil_days((now - since).total_seconds())
        if mode == PauseMode.ADMIN_OPEN:
            return actual, 0          # бессрочная админ-пауза — резерва вперёд не было
        reserved = int(client.pause_reserved_days)
        return max(0, min(actual, reserved)), reserved

    def exit_pause(self, client_id: int, *, auto: bool):
        """Выход из приостановки (любой режим). auto=True — по истечении срока
        (только user/admin_fixed). Пересчитывает фактическую длительность, правит
        period_end по режиму, снимает PAUSED-каскад. Возвращает
        (ok, actual_days, new_end, notifications)."""
        client = self.db.get_client(client_id)
        if client is None or not client.pause_active_since:
            return (False, 0, None, [])
        mode = client.pause_mode or PauseMode.USER
        since = timeutil.parse_iso(client.pause_active_since)
        now = timeutil.now()
        actual = timeutil.ceil_days((now - since).total_seconds())
        if mode == PauseMode.ADMIN_OPEN:
            # temp-бессрочная: восстанавливаем сохранённый конец + фактические дни
            saved = client.pause_saved_end
            if saved:
                new_end = timeutil.parse_iso(saved) + datetime.timedelta(days=actual)
                new_end_iso = timeutil.to_iso(new_end)
            else:
                new_end, new_end_iso = None, None       # была бессрочной и осталась
            used_add = 0
        else:
            # user / admin_fixed: резерв был добавлен вперёд, откатываем неисп.
            reserved = int(client.pause_reserved_days)
            actual = max(0, min(actual, reserved))
            new_end = None
            new_end_iso = client.period_end
            if client.period_end:
                end = timeutil.parse_iso(client.period_end)
                new_end = end - datetime.timedelta(days=reserved - actual)
                new_end_iso = timeutil.to_iso(new_end)
            used_add = actual if mode == PauseMode.USER else 0  # счёт списывает только user
        # неиспользованный резерв — обратно на счёт (только свой самоблок)
        balance = int(client.pause_balance_days)
        if mode == PauseMode.USER:
            balance += int(client.pause_reserved_days) - actual
        # атомарно: снимок эпизода в аудит + гашение активности паузы (used_days
        # периода сохраняем «спящим») + правка периода/блока
        with self.db.transaction():
            self.db.snapshot_pause(client_id, "auto" if auto else "manual")
            self.db.save_pause(client_id, models.PauseState(
                active_since=None, reserved_days=0, mode=None, saved_end=None,
                used_days=int(client.pause_used_days) + used_add,
                balance_days=max(0, balance)))
            self.db.update_client_fields(
                client_id,
                period_end=new_end_iso,
                block_reason=int(client.block_reason) & ~int(ClientBlock.PAUSED))
        notes: list[Notification] = []
        for dev in self.db.list_devices(client_id):
            self._device_clear_block(dev.id, DeviceBlock.PAUSED)
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                # «доступ снова активен» — только если устройство РЕАЛЬНО
                # разблокировано: при выходе из паузы могут оставаться другие
                # биты (админ-блок при admin_fixed, лимит трафика) — тогда
                # доступ не вернулся и радовать друга рано. О снятии этих битов
                # уведомит их собственный поток (unblock_client_manual и т.п.).
                fresh = self.db.get_device(dev.id)
                if fresh is not None and int(fresh.block_reason) == 0:
                    notes.append(Notification(dev.friend_tg_id,
                                 _pause_friend_ended(dev.name)))
        # клиенту — только при АВТО-выходе клиентского самоблока (по макс. сроку)
        if auto and mode == PauseMode.USER and client.tg_id:
            notes.append(Notification(client.tg_id,
                         _pause_auto_ended_client(actual, new_end)))
        return (True, actual, new_end, notes)

    def resume_by_email_code(self, code: str):
        """Аварийный email-выход: найти клиента с активной паузой и данным
        одноразовым кодом, снять паузу (тот же exit_pause). Возвращает
        (ok, notifications): ok=False если код не найден/не на паузе (тогда
        вызывающий молчит — письмо помечается прочитанным без ответа).
        Код одноразовый: exit_pause обнуляет resume_code (новый PauseState без
        кода), повторное письмо с тем же кодом уже не сматчится."""
        cid = self.db.find_client_by_resume_code((code or "").strip())
        if cid is None:
            return (False, [])
        client = self.db.get_client(cid)
        if client is None or not client.is_paused or client.pause_mode != PauseMode.USER:
            return (False, [])
        ok, actual, new_end, notes = self.exit_pause(cid, auto=False)
        if not ok:
            return (False, [])
        if client.tg_id:
            notes.append(Notification(
                client.tg_id,
                f"▶️ Приостановка снята по коду из письма. Использовано {actual} дн."))
        return (True, notes)

    def check_pauses(self) -> list["Notification"]:
        """Scheduler: авто-выход из СРОЧНЫХ приостановок (user/admin_fixed), у
        которых истёк зарезервированный срок. admin_open (бессрочные) — не трогаем,
        их снимает только админ."""
        notes: list[Notification] = []
        now = timeutil.now()
        for client in self.db.list_clients(include_service=False, paused_only=True):
            if not client.pause_active_since:
                continue
            mode = client.pause_mode or PauseMode.USER
            if mode == PauseMode.ADMIN_OPEN:
                continue
            reserved = int(client.pause_reserved_days)
            since = timeutil.parse_iso(client.pause_active_since)
            if (now - since).total_seconds() >= reserved * SECONDS_PER_DAY:
                ok, _, _, n = self.exit_pause(client.id, auto=True)
                if ok:
                    notes += n
        return notes

    def purge_old_history(self) -> dict:
        """Scheduler: удалить историю старше ретеншна (relativedelta лет из конфига).
        Считаем cutoff по календарю (високосные корректно), удаляем батчами по всем
        _histories. Возвращает {таблица: удалено} для лога."""
        from dateutil.relativedelta import relativedelta
        cutoff = timeutil.now() - relativedelta(years=config.HISTORY_RETENTION_YEARS)
        return self.db.purge_histories(timeutil.to_iso(cutoff),
                                       config.HISTORY_PURGE_BATCH_SIZE)

    # ── Опрос трафика (поток 4) ──────────────────────────────────────────────

    def poll_traffic(self) -> list:
        """Каждые 5 мин: dump → дельты с обработкой отката счётчика → накопление.
        last_handshake обновляем только при наличии (не затираем пустым).

        ВСЯ обработка — одна транзакция: (а) целостность — упади бот между
        накоплением и записью базы дельт, при раздельных коммитах дельта
        посчиталась бы дважды; (б) один fsync вместо 3-4 на устройство.

        Опрашиваем ВСЕ задействованные интерфейсы. Счётчики и хендшейки живут в
        ядре по интерфейсам, и опрос одного не увидит пиров другого: во время
        переезда у непереехавших замерло бы потребление, лимиты перестали бы
        срабатывать, а недоучтённое потерялось бы вместе со старым интерфейсом.
        Нечитаемый интерфейс просто пропускаем — его пиры подождут следующего
        такта, а состав пиров всё равно забота сверки."""
        # Поздравления собираем и здесь: обычно первый хендшейк ловит частый тик
        # migration_watch, но опрос всё равно ходит по ВСЕМ интерфейсам, а тот —
        # только по интерфейсу переезда. Страховка на случай, когда двойник
        # почему-то оказался не там, куда мы смотрим часто.
        greetings: list["Notification"] = []
        migrating = self.migration_running()
        peers: dict[str, dict] = {}
        for raw in self._migration_ifaces():
            try:
                for p in awg.show_dump(awg.iface_of(raw)):
                    peers[p["public_key"]] = p
            except awg.AwgError as e:
                log.warning("poll_traffic: %s не опрошен: %s", awg.iface_of(raw), e)
        # бесплатный побочный продукт: онлайн-счётчик для статусного блока
        # (dump уже в руках — не тратим отдельный exec в мониторе)
        polled_at = timeutil.now()
        online = sum(1 for p in peers.values()
                     if timeutil.handshake_is_online(p["last_handshake"], polled_at))
        with self.db.transaction():
            if self.db.get_state("online_count") != str(online):
                self.db.set_state("online_count", str(online))   # только при изменении
            # момент опроса: «онлайн» в списках и карточках считается на него,
            # а не на «сейчас» (см. online_ref) — иначе цифра в панели и список
            # по ссылке расходятся на возраст последнего опроса
            self.db.set_state("online_polled_at", str(int(polled_at.timestamp())))
            # Все сэмплы одним запросом, дельты и новые базы — двумя executemany:
            # 3M+1 операторов на тик превращаются в четыре. Строку сэмпла
            # переписываем только при изменении счётчиков — оффлайн-устройство
            # не должно генерить UPDATE каждые пять минут.
            samples = self.db.get_samples_all()
            deltas: list[tuple[int, int, int]] = []
            bases: list[tuple[int, int, int]] = []
            for dev in self.db.list_all_devices():
                p = peers.get(dev.public_key)
                if p is None:
                    continue                          # состав пиров — забота reconcile
                rx_now, tx_now = p["rx"], p["tx"]
                sample = samples.get(dev.id)
                if sample is None:
                    bases.append((dev.id, rx_now, tx_now))       # первая база
                else:
                    drx = rx_now - sample[0]
                    dtx = tx_now - sample[1]
                    if drx < 0:                       # счётчик упал (рестарт awg)
                        drx = rx_now
                    if dtx < 0:
                        dtx = tx_now
                    if drx or dtx:
                        deltas.append((dev.id, drx, dtx))
                        bases.append((dev.id, rx_now, tx_now))
                if p["last_handshake"] and p["last_handshake"] != dev.last_handshake:
                    # пишем только при изменении: оффлайн-устройство не должно
                    # генерить UPDATE тем же значением каждые 5 минут
                    if not dev.last_handshake and migrating:
                        # первый хендшейк в окне переезда — за него тянет жребий
                        # и частый тик migration_watch, поздравить должен один
                        if self.db.claim_first_handshake(dev.id, p["last_handshake"]):
                            note = self.migration_greeting(dev)
                            if note is not None:
                                greetings.append(note)
                    else:
                        self.db.update_device_fields(dev.id, last_handshake=p["last_handshake"])
            self.db.add_traffic_bulk(deltas)
            self.db.set_samples(bases)
        return greetings

    # ── Лимиты потребления (ТЗ 7-8) ──────────────────────────────────────────

    def check_traffic_limits(self) -> list["Notification"]:
        """После накопления дельт: проверка лимитов устройств и тоталов клиентов.
        Возвращает уведомления (превышения, пред-уведомления 80%, доп.квота).

        Всё в рамках календарного месяца (счётчики _month сбрасываются 1-го).
        Меряем против СУММЫ up+down. Блокировки — битом TRAFFIC (не трогая EXPIRY).
        """
        notes: list[Notification] = []
        warn_pct = settings.get_int("limits.traffic_warn_percent", 80)
        until = timeutil.first_of_next_month_str()
        admin_id = config.ADMIN_ID
        twins = self.db.twins_by_origin()             # один раз на проход, не на устройство

        with self.db.transaction():                   # один коммит вместо десятков
          for client in self.db.list_clients(include_service=False):
              if client.activation_status != ActivationStatus.ACTIVE:
                  continue
              # ОБЕ строки пары: в окне переезда потребление размазано по ним, и
              # лимит по одной дал бы человеку двойную квоту.
              devices = self.db.list_devices(client.id, all_rows=True)
              sent = client.traffic_notified          # уже в объекте — без запроса
              by_id = {d.id: d for d in devices}
              # id старых строк, у которых есть двойник, — их расход учитывается
              # в проходе по двойнику, отдельно не судим
              paired_old = {d.twin_of for d in devices if d.twin_of is not None}

              # шлюз условной маршрутизации в лимитах не участвует: ни своим,
              # ни в сумме профиля (его трафик — весь РФ-трафик клиентов)
              devices = [d for d in devices if not d.is_gateway]
              by_id = {d.id: d for d in devices}
              paired_old = {d.twin_of for d in devices if d.twin_of is not None}

              # ── лимиты устройств (независимо от клиентского) ──
              for dev in devices:
                  if dev.id in paired_old:
                      continue                  # учтён суммой у своего двойника
                  dlim = dev.traffic_limit
                  if dlim == 0:
                      continue
                  used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
                  mate = by_id.get(dev.twin_of) if dev.twin_of else None
                  if mate is not None:
                      # СУММА по паре против лимита пары (лимиты строк равны —
                      # сеттер парный). Считай каждую строку отдельно — и человек
                      # получает двойную квоту, у которой ни одна половина не
                      # дотягивает до порога.
                      used += int(mate.traffic_rx_month) + int(mate.traffic_tx_month)
                  over_marker = f"dev_over:{dev.id}"
                  warn_marker = f"dev80:{dev.id}"
                  if used >= dlim:
                      if not (int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER)):
                          self._device_set_block(dev.id, DeviceBlock.TRAFFIC_USER, twins)
                      if over_marker not in sent:
                          is_friend_dev = (dev.friend_status == FriendStatus.ACTIVE
                                           and dev.friend_tg_id)
                          # хозяину: спец-текст с пометкой «друг», если устройство
                          # передано; другу — обычный текст про его устройство
                          host_text = (_friend_dev_over_host_text(dev.name, until)
                                       if is_friend_dev else _dev_over_text(dev.name, until))
                          notes.append(Notification(client.tg_id, host_text))
                          if is_friend_dev:
                              notes.append(Notification(
                                  dev.friend_tg_id, _dev_over_text(dev.name, until)))
                          self.db.add_traffic_notified(client.id, over_marker)
                  elif used >= dlim * warn_pct // 100:
                      if warn_marker not in sent:
                          notes.append(Notification(
                              client.tg_id, _dev_warn_text(dev.name, warn_pct)))
                          if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                              notes.append(Notification(
                                  dev.friend_tg_id, _dev_warn_text(dev.name, warn_pct)))
                          self.db.add_traffic_notified(client.id, warn_marker)

              # ── тотал клиента ──
              climit = client.traffic_limit
              if climit == 0:
                  continue
              total = sum(int(d.traffic_rx_month) + int(d.traffic_tx_month)
                          for d in devices)
              effective = climit + int(client.bonus_bytes)
              is_admin_client = (client.tg_id == admin_id)

              if total >= effective:
                  # исчерпан текущий потолок (базовый или уже с доп.квотой)
                  if is_admin_client:
                      if "cli_over" not in sent:
                          notes.append(Notification(admin_id, _admin_self_over_text()))
                          self.db.add_traffic_notified(client.id, "cli_over")
                      continue
                  if not client.bonus_granted_month:
                      # первая доп.квота этого месяца
                      bonus = settings.get_int("limits.traffic_bonus_gb", 100) * BYTES_PER_GB
                      # аудит: снимок квоты до выдачи разовой доп.квоты
                      self.db.archive_quota(client.id, "bonus_granted")
                      self.db.update_client_fields(
                          client.id,
                          bonus_bytes=int(client.bonus_bytes) + bonus,
                          bonus_granted_month=1)
                      notes.append(Notification(
                          client.tg_id,
                          _cli_bonus_text(settings.get_int("limits.traffic_bonus_gb", 100), until)))
                      if settings.get_bool("notifications.client_events.bonus", True):
                          notes.append(Notification(
                              admin_id, _cli_bonus_admin_text(client.name, settings.get_int("limits.traffic_bonus_gb", 100))))
                      self.db.add_traffic_notified(client.id, "bonus")
                  else:
                      # доп.квота уже выдавалась и тоже исчерпана → блок всех устройств
                      # КАСКАДНЫМ битом (TRAFFIC_CLIENT), не собственным TRAFFIC:
                      # так поднятие лимита устройства не снимет блок «по клиенту».
                      if "cli_over" not in sent:
                          self._client_set_block(client.id, ClientBlock.TRAFFIC_CLIENT)
                          for dev in devices:
                              self._device_set_block(dev.id, DeviceBlock.TRAFFIC_CLIENT, twins)
                              if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                                  notes.append(Notification(
                                      dev.friend_tg_id, _dev_over_text(dev.name, until)))
                          notes.append(Notification(client.tg_id, _cli_over_text(until)))
                          if settings.get_bool("notifications.client_events.over_limit", True):
                              notes.append(Notification(
                                  admin_id, _cli_over_admin_text(client.name)))
                          self.db.add_traffic_notified(client.id, "cli_over")
              elif total >= effective * warn_pct // 100:
                  if "cli80" not in sent and not is_admin_client:
                      notes.append(Notification(client.tg_id, _cli_warn_text(warn_pct)))
                      self.db.add_traffic_notified(client.id, "cli80")

        return notes

    # ── Проверка сроков + уведомления (поток из ТЗ 7) ────────────────────────

    def _block_client(self, client) -> list["Notification"]:
        """Блокирует все устройства клиента по причине EXPIRY (подписка истекла).
        Ставит бит и клиенту. Возвращает уведомления друзьям переданных (active)
        устройств — доступ приостановлен."""
        notes: list[Notification] = []
        self._client_set_block(client.id, ClientBlock.EXPIRY)
        twins = self.db.twins_by_origin()
        for dev in self.db.list_devices(client.id):
            self._device_set_block(dev.id, DeviceBlock.EXPIRY, twins)
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_blocked_text(dev.name)))
        return notes

    # ── истекающие подписки (панель админа) ──────────────────────────────────

    @staticmethod
    def expiring_limit_days(length_days: int) -> int | None:
        """Порог «истекает» по длине периода L (дней): L < 12 — всегда в списке
        (None); иначе остаток ≤ max(⌊L/12⌋, 7) дней. Год → 30, месяц → 7."""
        if length_days < 12:
            return None
        return max(length_days // 12, 7)

    def expiring_subscriptions(self) -> list[tuple]:
        """[(client, секунд до конца)] — активные с конечным периодом, попавшие
        под порог; ближайшие сверху. Это НЕ пороги уведомлений: у панели своё
        правило, у уведомлений своё."""
        now = timeutil.now()
        out = []
        for client in self.db.list_clients(include_service=False, active_finite_only=True):
            end = timeutil.parse_iso(client.period_end)
            start = timeutil.parse_iso(client.period_start) if client.period_start else None
            secs = timeutil.remaining_seconds(end, now)
            if secs <= 0:
                continue
            length_days = int((end - start).total_seconds() // 86400) if start else 0
            limit = self.expiring_limit_days(length_days)
            if limit is None or secs <= limit * 86400:
                out.append((client, secs))
        out.sort(key=lambda t: t[1])
        return out

    def check_expiry(self) -> list[Notification]:
        now = timeutil.now()
        notifications: list[Notification] = []
        with self.db.transaction():
          for client in self.db.list_clients(include_service=False, active_finite_only=True):
              end = timeutil.parse_iso(client.period_end)
              start = timeutil.parse_iso(client.period_start)
              secs = timeutil.remaining_seconds(end, now)
              period_len_min = timeutil.period_minutes(start, end)

              # истёк
              if secs <= 0:
                  if client.status != SubStatus.EXPIRED:
                      friend_notes = self._block_client(client)
                      self.db.update_client_fields(client.id, status=SubStatus.EXPIRED)
                      if client.tg_id:
                          notifications.append(Notification(client.tg_id, _TXT_EXPIRED_CLIENT))
                      notifications.append(Notification(
                          config.ADMIN_ID, _TXT_EXPIRED_ADMIN.format(name=client.name)))
                      notifications.extend(friend_notes)   # друзьям — доступ приостановлен
                  continue

              # пороги приближения (строго меньше длительности периода).
              # Если бот «проспал» несколько порогов, шлём ТОЛЬКО самый строгий
              # (ближайший к концу) из пересечённых, остальные молча помечаем —
              # иначе клиент получит простыню «30 дней»+«14»+«7»+«1» разом.
              already = client.notified_thresholds  # уже в объекте — без запроса
              mins_left = secs // 60
              # Месяцу порог «30 дней» не показываем никогда: 31-дневный месяц
              # получал «истекает через 30 дней» назавтра после активации.
              crossed = [
                  (th_min, label) for th_min, label in config.NOTIFY_THRESHOLDS_MINUTES
                  if th_min < period_len_min and mins_left <= th_min and th_min not in already
                  and not (client.period_kind == "month" and th_min >= _MONTH_CUT_MINUTES)
              ]
              if crossed:
                  # самый строгий = наименьший порог по времени (сам порог
                  # дальше не нужен — только его подпись)
                  _, tightest_label = min(crossed, key=lambda x: x[0])
                  if client.tg_id:
                      # кнопка отсрочки: только КЛИЕНТУ (не другу — друзья идут иным
                      # путём), только на ГОДОВОМ периоде и один раз за период.
                      grace_offer = (client.period_kind == PeriodKind.YEAR
                                     and not client.grace_used)
                      notifications.append(Notification(
                          client.tg_id, _TXT_EXPIRING_CLIENT.format(label=tightest_label),
                          grace_offer_client_id=client.id if grace_offer else 0))
                  notifications.append(Notification(
                      config.ADMIN_ID,
                      _TXT_EXPIRING_ADMIN.format(name=client.name, label=tightest_label)))
                  # помечаем ВСЕ пересечённые отправленными (включая пропущенные крупные)
                  for th_min, _ in crossed:
                      self.db.add_notified(client.id, th_min)
        return notifications

    # ── Сбросы ───────────────────────────────────────────────────────────────

    def reset_monthly_traffic(self) -> list["Notification"]:
        """1-е число: обнулить месячные счётчики + доп.квоту + трафик-метки, и
        снять причину TRAFFIC со всех клиентов и устройств (разблокировать, если
        не осталось других причин). Причину EXPIRY НЕ трогаем — подписка живёт
        своим циклом. Возвращает уведомления о сбросе (профилям и друзьям);
        безлимитные позиции не показываем, пустые уведомления не шлём."""
        # аудит-метрика: снимок потребления завершившегося месяца ПЕРЕД обнулением.
        # Метка месяца — предыдущий календарный (сброс идёт 1-го числа за прошлый).
        _now = timeutil.now()
        _prev_month = (_now.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
        notes: list[Notification] = []
        friend_devs: dict[int, list] = {}     # friend_tg → [device rows] для их уведомлений
        # Одной транзакцией: и один fsync вместо ~5N+4NM, и атомарность —
        # падение посередине не оставит половину клиентов сброшенной.
        with self.db.transaction():
          self.db.snapshot_monthly_traffic(_prev_month)
          self.db.reset_month_traffic_all()
          twins = self.db.twins_by_origin()
          for client in self.db.list_clients(include_service=False):
            self.db.update_client_fields(
                client.id, bonus_bytes=0, bonus_granted_month=0)
            self.db.reset_traffic_notified(client.id)
            if int(client.block_reason) & int(ClientBlock.TRAFFIC_CLIENT):
                self._client_clear_block(client.id, ClientBlock.TRAFFIC_CLIENT)
            own_lines = []                    # лимитные СВОИ (не переданные) устройства
            for dev in self.db.list_devices(client.id):
                # месячный сброс снимает ОБЕ трафик-причины (свою и каскад клиента)
                for _tbit in (DeviceBlock.TRAFFIC_USER, DeviceBlock.TRAFFIC_CLIENT):
                    if int(dev.block_reason) & int(_tbit):
                        self._device_clear_block(dev.id, _tbit, twins)
                lim = int(dev.traffic_limit)
                if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                    if lim > 0:               # друг увидит в своём уведомлении
                        friend_devs.setdefault(dev.friend_tg_id, []).append(dev)
                elif lim > 0:
                    own_lines.append(f"{dev.name} — {_gb_limit(lim)}")
            # профилю шлём, если есть что показать: лимит профиля ИЛИ лимитные устройства
            total_limit = int(client.traffic_limit)
            if client.tg_id and (total_limit > 0 or own_lines):
                notes.append(Notification(
                    client.tg_id, _reset_client_text(total_limit, own_lines)))
        # друзьям — по их лимитным устройствам
        for friend_tg, devs in friend_devs.items():
            lines = [f"{d.name} — {_gb_limit(int(d.traffic_limit))}" for d in devs]
            notes.append(Notification(friend_tg, _reset_friend_text(lines)))
        return notes

    # ── Реконсиляция состава пиров (вотчдог) ─────────────────────────────────

    def _migration_ifaces(self) -> list[str]:
        """Интерфейсы, которые бот обязан обходить: те, на которых реально живут
        устройства, плюс заданный конфигом интерфейс переезда.

        Второй нужен отдельно: между поднятием интерфейса и рождением первого
        двойника устройств на нём ещё нет, а карантин на нём уже должен
        работать — иначе чужой пир, заведённый руками в этом окне, останется
        незамеченным.

        Возвращаем СЫРЫЕ значения (пустая строка = дефолт), разрешает их
        awg.iface_of: дефолтный интерфейс может смениться, и держать в списке
        одновременно '' и 'awg0' значило бы обойти один конфиг дважды.
        """
        raw = set(self.db.distinct_ifaces())
        raw.add("")                                   # дефолтный обходим всегда
        if config.MIGRATION_INTERFACE:
            names = {awg.iface_of(x) for x in raw}
            if config.MIGRATION_INTERFACE not in names:
                raw.add(config.MIGRATION_INTERFACE)
        return sorted(raw)

    @staticmethod
    def _peers_with_ip(conf_text: str) -> dict[str, str]:
        """pubkey → ip из живого conf интерфейса."""
        header, peers = awg._split_conf(conf_text)
        result: dict[str, str] = {}
        for p in peers:
            if not p["pubkey"]:
                continue
            ip = None
            for line in p["lines"]:
                if line.strip().startswith("AllowedIPs"):
                    ip = line.split("=", 1)[1].strip().split("/")[0]
            if ip:
                result[p["pubkey"]] = ip
        return result

    def reconcile_peers(self) -> list[Notification]:
        """Сверка живого конфига с БД. Пропавшие пиры → грациозный детект
        удаления (MISSING_SWEEPS_THRESHOLD сверок подряд). Неизвестные →
        карантин на служебном профиле + ТРЕВОГА админу.

        Раньше неизвестный пир считался находкой: приложение Amnezia могло
        завести его в обход бота, и сверка молча его усыновляла. Приложение с
        серверной стороны снято, создавать пиры больше некому, кроме нас, —
        значит пир, которого нет в базе, это ручная правка конфига или чужое
        вмешательство. Принять его молча означало бы узаконить чужой доступ.

        ПОРЯДОК ПРОХОДОВ ЗНАЧИМ. Пропавшие разбираем ПЕРВЫМИ, неизвестных
        заводим после: devices.address UNIQUE, а «первый свободный» адрес любой
        сторонний инструмент выдаст по тому же правилу, что и мы. Сняли пир,
        следом завели новый — он получит освободившийся адрес, которым в нашей
        БД ещё владеет уходящая запись. В обратном порядке заведение падало бы
        на UNIQUE ДО прохода по пропавшим, то есть запись-держатель адреса не
        удалялась бы никогда: сверка встаёт колом насовсем.

        СВЕРКА ИДЁТ ПО СВОЕМУ ИНТЕРФЕЙСУ У КАЖДОГО УСТРОЙСТВА. Пока интерфейс
        был один, хватало одного конфига; с двумя одиночное чтение объявило бы
        «пропавшими» ВСЕХ, кто живёт на соседнем, и через MISSING_SWEEPS_THRESHOLD
        сверок бот удалил бы их сам — молча и необратимо.

        Нечитаемый конфиг (интерфейс задан, но лежит; файл ещё не создан)
        пропускаем ЦЕЛИКОМ, не трогая missing_count: отсутствие конфига — это
        «не знаю», а не «пиров нет», и накапливать по нему пропажи значит
        готовить то же самое удаление, только медленнее.
        """
        db_devices = {d.public_key: d for d in self.db.list_all_devices()}
        service_id = self.db.get_service_client_id()
        notifications: list[Notification] = []

        # conf каждого задействованного интерфейса + тех, что заданы конфигом.
        # Интерфейс запоминаем ВМЕСТЕ с пиром: карантинная запись обязана знать,
        # где её пир живёт, иначе снимать его пойдут не с того конфига.
        live: dict[str, tuple[str, str]] = {}             # pub → (ip, iface)
        readable: set[str] = set()                        # интерфейсы, чей conf прочли
        for raw in self._migration_ifaces():
            name = awg.iface_of(raw)
            try:
                conf = awg.read_file(awg.conf_path(name))
            except awg.AwgError as e:
                log.warning("reconcile_peers: конфиг %s не прочитан, пропускаю: %s", name, e)
                continue
            readable.add(name)
            for pub, ip in self._peers_with_ip(conf).items():
                live[pub] = (ip, name)
        try:
            psk = awg.read_server_params()["psk"]
        except awg.AwgError:
            psk = ""

        # пропавшие пиры
        for pub, dev in db_devices.items():
            if awg.iface_of(dev.iface) not in readable:
                continue                                  # конфиг не прочли — не судим
            if pub in live:
                if dev.missing_count:
                    self.db.update_device_fields(dev.id, missing_count=0)
                continue
            mc = dev.missing_count + 1
            if mc >= config.MISSING_SWEEPS_THRESHOLD:
                client = self.db.get_client(dev.client_id)
                # снять осиротевший DROP: iptables-правило без пира заблокирует
                # БУДУЩЕГО владельца этого IP (аллокатор переиспользует адреса)
                if int(dev.block_reason) != 0:
                    try:
                        awg.unblock_ip(dev.address)
                    except awg.AwgError:
                        pass
                friend_tg = (dev.friend_tg_id
                             if dev.friend_status == FriendStatus.ACTIVE else None)
                self.db.delete_device(dev.id)
                if client and not client.is_service:
                    notifications.append(Notification(
                        config.ADMIN_ID,
                        _TXT_PEER_GONE.format(name=dev.name, client=client.name)))
                if friend_tg:
                    notifications.append(Notification(friend_tg, _TXT_FRIEND_DEVICE_GONE))
            else:
                self.db.update_device_fields(dev.id, missing_count=mc)

        # неизвестные пиры → карантин + тревога
        for pub, (ip, iface) in live.items():
            if pub in db_devices:
                continue
            name = f"Неизвестный пир {ip}"
            try:
                self.db.create_device(service_id, name, pub, psk, ip, private_key=None,
                                      iface=iface)
            except sqlite3.IntegrityError as e:
                # Адрес ещё за уходящей записью (порог MISSING_SWEEPS_THRESHOLD
                # не выбран). Пропускаем ЭТОТ пир, а не всю сверку: он попадёт в
                # карантин на сверке, где прежний владелец адреса удалится.
                log.warning("reconcile_peers: пир %s (%s) пока не в карантине: %s",
                            name, ip, e)
                continue
            # force_sound: это событие безопасности, а не информационная строка.
            # Тихие часы для него — не та цена, которую стоит платить за сон.
            notifications.append(Notification(
                config.ADMIN_ID, _TXT_UNKNOWN_PEER.format(ip=ip), force_sound=True))
        return notifications

    # ── Реконсиляция блокировок после рестарта контейнера ────────────────────

    def reconcile_blocks(self) -> None:
        """iptables-DROP'ы эфемерны — после рестарта переналагаем их на всех,
        у кого block_reason != 0 в БД (любая причина блокировки). Один
        `iptables -S` вместо -C на каждое устройство; block_ip сам идемпотентен."""
        try:
            present = awg.blocked_ips()
        except awg.AwgError:
            present = set()
        for address in self.db.blocked_addresses():
            if address in present:
                continue
            try:
                awg.block_ip(address)
            except awg.AwgError:
                pass

    def reconcile_ssh_access(self) -> None:
        """SSH-к-хосту из туннеля — только устройствам админа. Единственная
        точка: nft-таблица awg_bot_guard (infra/nftguard). Бот держит в ней set
        адресов админских устройств и сверяет его с желаемым в тех же точках,
        что и блокировки (старт, рестарт, тик монитора) плюс сразу при
        создании/удалении админского устройства: удаление устройства или
        переиспользование его IP другим профилем закрывается в пределах тика.

        Пока firewall.enabled=false (таблицу ещё не включали через
        `awg-bot firewall setup`), фильтр не ставим — включение файервола
        делается человеком с таймером отката, не ботом. Но NAT клиентов в
        host-режиме таблица держит всегда (nftguard: форма NAT-only)."""
        from awgbot.infra import nftguard
        try:
            admin_ips = self.db.admin_device_addresses(config.ADMIN_ID)
            res = nftguard.reconcile(admin_ips)
        except nftguard.GuardError as e:
            log.warning("firewall: %s", e)
            return
        if res != "ok":
            log.info("firewall: таблица awg_bot_guard — %s", res)

    def _live_listen_port(self) -> int:
        """ListenPort основного интерфейса, как его видит сервер. 0 — не
        прочитали (интерфейс лежит, конфиг недоступен)."""
        try:
            return int(awg.read_server_params(iface=config.AWG_INTERFACE)["listen_port"])
        except Exception as e:                            # noqa: BLE001
            log.debug("server_screen: порт интерфейса не прочитан: %s", e)
            return 0

    def server_screen(self) -> dict:
        """Значения раздела «Сервер». Живые (settings), а не константы старта:
        экран обязан показывать то, что уедет в следующую выданную ссылку."""
        from awgbot.infra import awglock
        g = settings.get
        import subprocess
        kernel = ""
        try:
            cp = subprocess.run(["modinfo", "-F", "version", "amneziawg"],
                                capture_output=True, timeout=10)
            kernel = cp.stdout.decode(errors="replace").strip() if cp.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            kernel = ""
        tag = awglock.built_module_tag() or awglock.module_tag()
        if tag:
            # Строка версии у разных тегов апстрима одинакова, поэтому в UI
            # показываем тег: только он отвечает на вопрос «что собрано».
            kernel = f"{tag} ({kernel})" if kernel else tag
        dns1 = g("app.client_config.dns1", config.DNS1)
        dns2 = g("app.client_config.dns2", config.DNS2)
        return {
            "host": g("app.network.server_host", config.SERVER_HOST),
            "name": g("app.client_config.server_name", config.SERVER_NAME),
            "dns": dns1 if dns1 == dns2 else f"{dns1}, {dns2}",
            "mtu": g("app.client_config.mtu", config.MTU),
            "keepalive": g("app.client_config.keepalive_seconds", config.KEEPALIVE_SECONDS),
            "iface": config.AWG_INTERFACE,
            # Порт берём У ЖИВОГО интерфейса — именно он уезжает в ссылки
            # (configgen читает listen_port оттуда же). Значение из конфига
            # показываем только при расхождении: экран, говорящий одно, пока
            # ссылки несут другое, хуже отсутствующего экрана.
            "port": self._live_listen_port() or g("app.network.server_port", config.SERVER_PORT),
            "port_conf": g("app.network.server_port", config.SERVER_PORT),
            "subnet": g("app.network.subnet_cidr", f"{config.SUBNET_PREFIX}.0/24"),
            "private_dns": self.private_dns_info(),
            "kernel": kernel,
            "generation": awglock.applied_generation(),
            # Почему смена порта/подсети сейчас невозможна — или пусто
            "migration_blocked": self.migration_blocked_reason(),
        }

    def migration_prepare_data(self, want_port: int = 0) -> dict:
        """Что показать на экране подготовки переезда: текущая топология и
        размер когорты. Когорта считается ровно так же, как её заморозит старт."""
        clients, devices, _to_birth = self.migration_start_preview()
        return {"iface": config.AWG_INTERFACE,
                "port": self._live_listen_port() or settings.get("app.network.server_port",
                                                                 config.SERVER_PORT),
                "subnet": settings.get("app.network.subnet_cidr",
                                       f"{config.SUBNET_PREFIX}.0/24"),
                "clients": clients, "devices": devices,
                "want_port": want_port,
                "private_dns": self.private_dns_for_migration(),
                "blocked": self.migration_blocked_reason()}

    # ── файервол из чата (README §6b) ────────────────────────────────────────
    # Раньше единственным интерфейсом был CLI, и «подтверди вход» требовало
    # второго SSH-сеанса. Из чата это честнее: Telegram доступен независимо от
    # того, заперли вы себе SSH или нет, а таймер отката страхует ровно от
    # этого случая.

    def firewall_screen(self) -> dict:
        from awgbot.infra import nftguard
        st = nftguard.status(self.db.admin_device_addresses(config.ADMIN_ID))
        spec = st["spec"]
        return {"enabled": st["enabled"], "present": st["present"],
                "rollback": st["rollback"], "ufw": st["ufw"],
                "ssh_port": spec.ssh_port, "allow": list(spec.ssh_allow4) + list(spec.ssh_allow6),
                "raw_allow": list(settings.get("app.firewall.ssh_allow", []) or []),
                "unresolved": list(spec.unresolved), "admin_ips": list(spec.tunnel_admin4),
                "nat": spec.nat}

    def firewall_allow_add(self, raw: str) -> list[str]:
        """Добавить адреса в вайтлист SSH. Добавление запереть не может —
        применяем без таймера отката."""
        from awgbot.infra import nftguard
        entries: list[str] = []
        for tok in str(raw).replace(",", " ").split():
            try:
                nftguard.classify(tok)
            except ValueError as e:
                raise ServiceError(f"«{tok}» не адрес, не подсеть и не имя: {e}")
            if tok not in entries:
                entries.append(tok)
        if not entries:
            raise ServiceError("пусто: жду адрес, подсеть или имя")
        cur = list(settings.get("app.firewall.ssh_allow", []) or [])
        cur += [e for e in entries if e not in cur]
        settings.set_value("app.firewall.ssh_allow", cur)
        if nftguard.enabled():
            self.reconcile_ssh_access()
        return cur

    def firewall_allow_remove(self, entry: str) -> list[str]:
        """Убрать адрес. Это МОЖЕТ запереть — применяем с таймером отката."""
        cur = [v for v in (settings.get("app.firewall.ssh_allow", []) or []) if v != entry]
        settings.set_value("app.firewall.ssh_allow", cur)
        from awgbot.infra import nftguard
        if nftguard.enabled():
            self._firewall_apply(rollback=True)
        return cur

    _FW_ROLLBACK_SECONDS = 300

    def _firewall_apply(self, rollback: bool) -> None:
        from awgbot.infra import nftguard
        spec = nftguard.build_spec(self.db.admin_device_addresses(config.ADMIN_ID))
        for line in nftguard.ensure_persistence():
            log.info("firewall: %s", line)
        nftguard.apply_text(nftguard.render(spec))
        if not rollback:
            return
        env = {k: str(v) for k, v in (("AWG_BOT_CONF_DIR", config.CONF_DIR),
                                      ("AWG_BOT_DATA_DIR", config.DATA_DIR),
                                      ("AWG_BOT_ENV", os.environ.get("AWG_BOT_ENV", "")))
               if v}
        cmd = ["-p", f"WorkingDirectory={config.BASE_DIR}", sys.executable,
               "-m", "tools.firewall", "rollback"]
        nftguard.arm_rollback(self._FW_ROLLBACK_SECONDS, cmd, env)

    def firewall_enable(self) -> int:
        """Включить фильтр с таймером отката. Возвращает секунды на проверку.

        Пустой вайтлист не запрещаем: SSH останется открыт всем адресам, и это
        осознанный выбор (ключи никто не отменял) — а вот молча включить фильтр,
        который никого не пускает, было бы ловушкой."""
        from awgbot.infra import nftguard
        settings.set_value("app.firewall.enabled", True)
        try:
            self._firewall_apply(rollback=True)
        except nftguard.GuardError as e:
            settings.set_value("app.firewall.enabled", False)
            raise ServiceError(str(e))
        return self._FW_ROLLBACK_SECONDS

    def firewall_confirm(self) -> bool:
        from awgbot.infra import nftguard
        return nftguard.disarm_rollback()

    def firewall_disable(self) -> list[str]:
        """Снять фильтр. NAT клиентов таблица держит дальше: «выключить
        файервол» не обещает «оставить клиентов без интернета»."""
        from awgbot.infra import nftguard
        nftguard.disarm_rollback()
        done = nftguard.remove()
        settings.set_value("app.firewall.enabled", False)
        return done

    def retire_legacy_ssh_gate(self) -> None:
        """Разово снять прежние ворота (цепочка AWGBOT_SSH + PostUp-страж) —
        но ТОЛЬКО когда новая таблица уже держит SSH. Иначе снятие открыло бы
        SSH из туннеля всем пирам до включения файервола."""
        from awgbot.infra import nftguard
        if not nftguard.enabled() or self.db.get_state("legacy_ssh_gate_removed"):
            return
        try:
            if awg.remove_legacy_ssh_gate():
                log.info("firewall: старые ворота SSH (AWGBOT_SSH, PostUp) сняты")
        except awg.AwgError as e:
            log.warning("firewall: старые ворота не сняты: %s", e)
            return
        self.db.set_state("legacy_ssh_gate_removed", "1")

    # ── Условная маршрутизация (docs/conditional-routing.md) ─────────────────
    # Российский IP для российских сервисов. Конфиги устройств не меняются:
    # режим — серверное состояние. Проекция состояния в инфраструктуру ровно
    # одна — членство адреса устройства в наборе ipset, поэтому переключение
    # стоит одну запись и обратимо без последствий для выданных ссылок.

    _RT_LINK_KEY = "routing_link_ok"
    _RT_STREAK_KEY = "routing_link_up_streak"
    _RT_UP_STREAK = 3                     # хороших замеров подряд до возврата
    # Плохих замеров подряд до ГАШЕНИЯ маркировки. Три — столько же, сколько на
    # возврат, и одинаково в обоих режимах: решение принято сознательно, ради
    # того чтобы мелкие сетевые флуктуации не дёргали режим туда-сюда. Каждое
    # переключение перекладывает трафик всех включённых, и на коротком провале
    # это дороже самого провала.
    #
    # Цена названа и принята: при умолчании «домой» помечено почти всё, поэтому
    # пока порог набирается, помеченный трафик уходит в тоннель, который никуда
    # не ведёт. Это не «не тот адрес», а отсутствие связи — до полутора минут в
    # худшем случае. Раньше там гасили по первому замеру именно из-за этого.
    #
    # Одиночный плохой замер и так не шум: внутри него зонд делает две попытки к
    # двум целям с таймаутом 4 с, то есть подтверждает отказ секундами. Три
    # замера — это уже около минуты подтверждённой недоступности.
    #
    # Совпадает с порогом объявления, и это удобно: админ узнаёт ровно тогда,
    # когда состояние действительно сменилось, а не до или после.
    _RT_DOWN_STREAK = 3
    _RT_DOWN_KEY = "routing_link_down_streak"
    _RT_ANNOUNCED_KEY = "routing_link_announced"
    _RT_ANNOUNCE_AFTER = 3                # плохих замеров подряд до письма админу
    # Подсказка про бандл — не украшение. Обфускация линка симметрична: не сойдись
    # H1..H4/S1..S4 у сторон, хендшейка не будет вовсе. Отказ громкий (вот эта
    # самая тревога), но причина со стороны ВПС не видна, и без строки ниже её
    # ищут в аплинке и NAT, где её нет.
    _TXT_RT_BUNDLE_HINT = (
        "\n\nЕсли началось сразу после обновления — пересобери бандл шлюза "
        "(<code>awg-bot gw-bundle</code>) и переустанови его на той стороне: "
        "набор обфускации линка обязан совпадать, иначе хендшейк не проходит.")

    _GW_BUNDLE_ISSUED_KEY = "gw_bundle_issued_at"

    def _link_script(self) -> str:
        return str(config.BASE_DIR / "install" / "routing-link-setup.sh")

    def _run_link_script(self, mode: str, env: dict | None = None) -> None:
        import subprocess
        proc = subprocess.run(["sh", self._link_script(), mode], capture_output=True,
                              timeout=120, env={**os.environ, **(env or {})})
        if proc.returncode != 0:
            raise ServiceError(f"скрипт линка ({mode}) не отработал: "
                               + proc.stderr.decode(errors="replace").strip()[-200:])

    def _gw_bundle_env(self) -> tuple[dict, list[str]]:
        """Окружение сборки бандла: устройства админа, ключ и конфиг аплинка
        назначенного шлюза (в окне переезда — двойника, старый ключ отдельно)."""
        admin_ips = self._gw_ssh_allow()
        env = {"ADMIN_IPS": " ".join(admin_ips)}
        gw = self.db.gateway_device()
        if gw is not None:
            import base64
            twin = self.db.twin_of_device(gw.id)
            target = twin or gw
            env["GATEWAY_PUBKEY"] = target.public_key
            env["GATEWAY_PREV_PUBKEY"] = gw.public_key if twin else ""
            try:
                env["UPLINK_B64"] = base64.b64encode(self.gateway_uplink_conf(target).encode()).decode()
            except ServiceError as e:
                log.warning("bundle: конфиг аплинка шлюза не собран: %s", e)
        return env, admin_ips

    def _gw_bundle_build(self) -> tuple[bytes, str]:
        """Собрать бандл скриптом линка (ключи не меняются) и дополнить почтой,
        фразой бэкапов. Возвращает (открытый текст, приватный ключ шлюза)."""
        from awgbot.util import bundlecrypt
        env, admin_ips = self._gw_bundle_env()
        self._run_link_script("--bundle", env)
        link_if = config.ROUTING_GW_INTERFACE or "awglink"
        with open(f"/root/gw-{link_if}.conf", encoding="utf-8") as f:
            priv = bundlecrypt.read_privkey(f.read())
        with open("/root/awg-gw-bundle.sh", "rb") as f:
            plain = f.read()
        plain = self._bundle_with_mail(plain)
        plain = self._bundle_with_agent(plain)
        self.db.set_state(self._GW_BUNDLE_SSH_KEY, " ".join(admin_ips))
        self.db.set_state(self._GW_BUNDLE_SSH_NOTIFIED_KEY, "")
        self.db.set_state(self._GW_BUNDLE_ISSUED_KEY, timeutil.to_iso(timeutil.now()))
        return plain, priv

    def gw_bundle_encrypted(self) -> tuple[bytes, str]:
        """Бандл для доставки чатом: шифрован ключом линка, который есть только
        у уже настроенного шлюза. Открытый бандл на диске ВПС остаётся под 600."""
        from awgbot.util import bundlecrypt
        plain, priv = self._gw_bundle_build()
        return bundlecrypt.encrypt(plain, priv), "awg-gw-bundle.enc"

    def gw_bundle_plain(self) -> tuple[bytes, str]:
        """Открытый бандл — для ПЕРВОГО применения на машине, у которой ключа
        линка ещё нет (новая машина или новые ключи). Внутри приватные ключи:
        тот же уровень доверия, что у ссылок vpn:// с ключами устройств."""
        plain, _ = self._gw_bundle_build()
        return plain, "awg-gw-bundle.sh"

    # ── шлюз условной маршрутизации: пометка устройства ──────────────────────
    _GW_NONCES_KEY = "gw_claim_nonces"

    def _link_privkey(self) -> str:
        from awgbot.util import bundlecrypt
        link_if = config.ROUTING_GW_INTERFACE or "awglink"
        try:
            with open(f"/root/gw-{link_if}.conf", encoding="utf-8") as f:
                return bundlecrypt.read_privkey(f.read())
        except (OSError, ValueError) as e:
            raise ServiceError(f"ключ линка не прочитан: {e}")

    def _gw_nonce_seen(self, nonce: str) -> bool:
        seen = (self.db.get_state(self._GW_NONCES_KEY) or "").split()
        if nonce in seen:
            return True
        self.db.set_state(self._GW_NONCES_KEY, " ".join((seen + [nonce])[-50:]))
        return False

    def gateway_claim(self, text: str) -> dict:
        """Пересланное от агента сообщение с токеном `claim`. Проверка подписи
        ключом линка, поиск устройства по ключу аплинка, единственность.
        Возвращает {'status': 'marked'|'already', 'device'}; занятый другим
        устройством шлюз — ServiceError, менять его — через настройки."""
        from awgbot.util import gwsign
        data = gwsign.verify(self._link_privkey(), text)
        if self._gw_nonce_seen(data["nonce"]):
            raise ServiceError("это сообщение уже принимали — пусть шлюз выдаст новое")
        dev = self.db.get_device_by_pubkey(data["pub"])
        if dev is None:
            raise ServiceError("устройства с таким ключом нет: аплинк шлюза должен быть "
                               "устройством админа, выпущенным этим ботом")
        admin = self.admin_client()
        if admin is None or dev.client_id != admin.id:
            raise ServiceError("шлюзом может быть только устройство профиля админа")
        if dev.is_gateway:
            return {"status": "already", "device": dev}
        prev = self.db.gateway_device()
        if prev is not None:
            raise ServiceError(f"шлюз уже назначен: «{prev.name}». Сменить его можно в "
                               "настройках условной маршрутизации («🔁 Сменить шлюз»)")
        self.db.set_gateway(dev.id)
        return {"status": "marked", "device": self.db.get_device(dev.id)}

    def gateway_candidates(self) -> list:
        """Устройства админа, выпущенные ботом, кроме текущего шлюза."""
        admin = self.admin_client()
        if admin is None:
            return []
        return [d for d in self.db.list_devices(admin.id) if d.private_key and not d.is_gateway]

    _GW_NEW_NAME = "Шлюз"

    def gateway_setup(self, device_id: Optional[int] = None, *, rekey: bool = False) -> dict:
        """Назначить шлюз. device_id — существующее устройство админа; None —
        создать новое устройство «Шлюз» в профиле админа (новая машина).
        rekey — новые ключи линка: прежняя машина теряет линк по построению,
        а первый бандл для новой едет открытым (ключа у неё ещё нет).
        Возвращает {'device', 'previous', 'created', 'rekeyed'}."""
        admin = self.admin_client()
        if admin is None:
            raise ServiceError("профиль админа ещё не создан")
        created = False
        if device_id is None:
            # Лимит шлюзу не делают исключением: профиль админа безлимитный по
            # построению, а счётчик, который врёт на одну строку, хуже лимита.
            dc = self.add_device(admin.id, self._GW_NEW_NAME)
            device_id = dc.device_id
            created = True
            rekey = True                       # новая машина без ключа линка
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if dev.client_id != admin.id:
            raise ServiceError("шлюзом может быть только устройство профиля админа")
        if not dev.private_key:
            raise ServiceError("это устройство создавал не бот — его конфиг в бандл не собрать")
        prev = self.db.gateway_device()
        if prev is not None and prev.id == dev.id:
            prev = None
        self.db.set_gateway(dev.id)
        if rekey:
            self._run_link_script("--rekey")
            routing.invalidate_self_check()
        return {"device": self.db.get_device(dev.id), "previous": prev,
                "created": created, "rekeyed": rekey}

    def gateway_remove(self) -> Optional[object]:
        """Убрать шлюз: флаг снять, ключи линка сменить (прежняя машина теряет
        линк), условную маршрутизацию выключить. Возвращает бывший шлюз."""
        prev = self.db.gateway_device()
        if prev is None:
            return None
        self.db.set_gateway(None)
        try:
            settings.set_value("app.routing.enabled", False)
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: маршрутизация не выключена: %s", e)
        try:
            self._run_link_script("--rekey")
            routing.invalidate_self_check()
        except ServiceError as e:
            log.warning("gateway_remove: ключи линка не сменены: %s", e)
        try:
            self.reconcile_routing()
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: реконсиляция: %s", e)
        return prev

    def gateway_state(self) -> dict:
        """Одно состояние для экрана: устройство, когда выпущен бандл, жив ли
        линк по последнему замеру и возраст хендшейка."""
        gw = self.db.gateway_device()
        issued = self.db.get_state(self._GW_BUNDLE_ISSUED_KEY) or ""
        age = None
        if gw is not None and config.ROUTING_GW_INTERFACE:
            try:
                age = routing.link_handshake_age()
            except Exception:                             # noqa: BLE001
                age = None
        return {"device": gw, "issued_at": issued, "link_ok": self.routing_link_ok(),
                "handshake_age": age}

    def gateway_uplink_conf(self, dev) -> str:
        """Конфиг аплинка шлюза для бандла: обычный клиентский .conf устройства
        в форме для машины-шлюза (без DNS, Table = off)."""
        cfg = self.generate_config(dev.id, for_bundle=True)
        return configgen.gateway_uplink_conf(cfg["conf"])

    _GW_BUNDLE_SSH_KEY = "gw_bundle_ssh_allow"
    _GW_BUNDLE_SSH_NOTIFIED_KEY = "gw_bundle_ssh_allow_notified"

    def _gw_ssh_allow(self) -> list[str]:
        return sorted(set(self.db.admin_device_addresses(config.ADMIN_ID)))

    def gw_bundle_drift_notes(self) -> list[Notification]:
        """Состав устройств админа разошёлся с тем, что уехало в бандл шлюза:
        напомнить один раз на каждое новое расхождение. Пока бандл не собирали
        — молчим: напоминать не о чем."""
        if not config.ROUTING_ENABLED:
            return []
        sent = self.db.get_state(self._GW_BUNDLE_SSH_KEY)
        if sent is None:
            return []
        cur = " ".join(self._gw_ssh_allow())
        if cur == sent or self.db.get_state(self._GW_BUNDLE_SSH_NOTIFIED_KEY) == cur:
            return []
        self.db.set_state(self._GW_BUNDLE_SSH_NOTIFIED_KEY, cur)
        return [Notification(config.ADMIN_ID,
                             "🛰 Состав устройств админа изменился, а на шлюз уехал прежний: "
                             "доступ к шлюзу и домашней сети через туннель — по старому списку. "
                             "Перевыпусти конфигурацию шлюза (🛰 Шлюз → Конфигурация шлюза).")]

    # Маркер контракта как ОТДЕЛЬНАЯ СТРОКА. Тот же текст встречается в бандле и
    # внутри sed-выражения, которым он вырезает скрипт обвязки; вставка туда
    # ломала sed, и на шлюз ложился пустой скрипт (наступили: 09.09.2026).
    _MAIL_MARK_LINE = re.compile(rb"^#__GW_SETUP_BELOW__$", re.M)

    # ── токен бота-агента: спрашиваем один раз, храним рядом со своим ────────
    _GW_TOKEN_ENV = "GW_BOT_TOKEN"

    @staticmethod
    def _env_path() -> str:
        return os.environ.get("AWG_BOT_ENV", "/etc/awg-bot/env")

    def gw_bot_token(self) -> str:
        """Токен бота шлюза из env. Пусто — ещё не спрашивали."""
        try:
            with open(self._env_path(), encoding="utf-8") as f:
                for line in f:
                    if line.startswith(self._GW_TOKEN_ENV + "="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def set_gw_bot_token(self, token: str) -> None:
        """Запомнить токен агента. Хранение осознанное: без него перевыпуск
        файла первого применения (переустановили машину-шлюз, сменили её)
        снова требовал бы идти в BotFather. Уровень доверия тот же, что у
        приватных ключей, которые в этом файле и так лежат."""
        token = str(token).strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ServiceError("это не похоже на токен бота — жду строку вида 123456789:AA…")
        path = self._env_path()
        try:
            lines = []
            try:
                with open(path, encoding="utf-8") as f:
                    lines = [ln for ln in f.read().splitlines()
                             if not ln.startswith(self._GW_TOKEN_ENV + "=")]
            except FileNotFoundError:
                pass
            lines.append(f"{self._GW_TOKEN_ENV}={token}")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(path, 0o600)
        except OSError as e:
            raise ServiceError(f"не записать {path}: {e}")

    def _bundle_with_agent(self, plain: bytes) -> bytes:
        """Токен агента и ADMIN_ID — в файл первого применения, чтобы установка
        на шлюзе не задавала ВООБЩЕ ни одного вопроса.

        Строки кладутся ПОСЛЕ `exec` в теле бандла (перед маркером скрипта
        обвязки), то есть при запуске бандла не исполняются — это данные для
        установщика, а не команды.
        """
        token = self.gw_bot_token()
        if not token:
            return plain
        m = self._MAIL_MARK_LINE.search(plain)
        if m is None:
            return plain
        lines = (f'AGENT_BOT_TOKEN="{token}"\n'
                 f'AGENT_ADMIN_ID="{config.ADMIN_ID}"\n').encode()
        return plain[:m.start()] + lines + plain[m.start():]

    def _bundle_with_mail(self, plain: bytes) -> bytes:
        """Настройки почты и парольная фраза бэкапов — в бандл, чтобы не вводить
        их дважды: строки MAIL_B64 / BACKUP_B64 (JSON в base64) перед строкой
        маркера контракта. Бандл шифрован ключом линка. Чего нет на ВПС — не
        добавляется, агент оставляет своё как есть."""
        m = self._MAIL_MARK_LINE.search(plain)
        if m is None:
            return plain
        import base64
        import json
        lines = b""
        acc = self.email_account()
        if acc is not None:
            payload = json.dumps({"login": acc.login, "password": acc.password,
                                  "imap_host": acc.imap_host, "imap_port": acc.imap_port,
                                  "smtp_host": acc.smtp_host, "smtp_port": acc.smtp_port},
                                 ensure_ascii=False).encode()
            lines += b'MAIL_B64="' + base64.b64encode(payload) + b'"\n'
        if self.backup_encryption_mode() == "passphrase":
            phrase = self.db.get_state(self._BK_PASSPHRASE_KEY) or ""
            lines += b'BACKUP_B64="' + base64.b64encode(json.dumps({"passphrase": phrase}).encode()) + b'"\n'
        if not lines:
            return plain
        return plain[:m.start()] + lines + plain[m.start():]

    @staticmethod
    def _rt_effect_line() -> str:
        """Что именно почувствуют пользователи, пока маркировка снята.

        Отказ мягкий: через шлюз идут только российские сервисы, всё прочее и так
        шло мимо. Раньше, в упразднённой обратной модели, тот же отвал означал
        «у людей пропал интернет» — и текст был другой.
        """
        return ("Маркировка снята: российские сервисы временно открываются "
                "с зарубежного адреса и могут ругаться. Всё остальное и так "
                "шло мимо шлюза — на него это не влияет.")

    def _txt_rt_gw_down(self) -> str:
        return ("🔴 Шлюз условной маршрутизации недоступен. "
                + self._rt_effect_line() + self._TXT_RT_BUNDLE_HINT)

    def _txt_rt_gw_no_path(self) -> str:
        return ("🔴 Шлюз условной маршрутизации отвечает, но интернета за ним нет "
                "— проверь аплинк и NAT на самом шлюзе. " + self._rt_effect_line())

    _TXT_RT_GW_UP = "🟢 Шлюз условной маршрутизации снова в строю."

    _RT_LINK_IF = "awglink"

    def routing_provisioned(self) -> bool:
        """Обвязка развёрнута? Признак — заданный интерфейс линка в конфиге."""
        return bool(settings.get("app.routing.gw_interface", config.ROUTING_GW_INTERFACE))

    def routing_provision(self) -> str:
        """Развернуть обвязку условной маршрутизации на ВПС и поднять линк.

        Раньше это были три команды в SSH из README: поставить dnsmasq,
        прогнать routing-host-setup.sh, прогнать routing-link-setup.sh, потом
        руками вписать интерфейс в app.yaml. Каждая — с ключами, которые легко
        перепутать, и ни одна не проверяла, что предыдущая отработала.

        Возвращает хвост вывода для показа админу. Бросает ServiceError, если
        какой-то шаг не отработал: полуразвёрнутая обвязка хуже отсутствующей —
        она выглядит рабочей.
        """
        import subprocess
        base = config.BASE_DIR / "install"
        out: list[str] = []

        def run(argv: list[str], what: str, timeout: int = 600) -> None:
            try:
                proc = subprocess.run(argv, capture_output=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as e:
                raise ServiceError(f"{what}: не запустилось ({e})")
            text = (proc.stdout + proc.stderr).decode(errors="replace").strip()
            out.append(text)
            if proc.returncode != 0:
                tail = "\n".join(text.splitlines()[-6:])
                raise ServiceError(f"{what} не отработал:\n{tail}")

        # dnsmasq: пакет dnsmasq-base даёт только бинарь (его тянут libvirt и
        # соседи), а обвязке нужен ЮНИТ — иначе routing-host-setup.sh честно
        # остановится на полпути.
        has_unit = subprocess.run(["systemctl", "list-unit-files", "dnsmasq.service"],
                                  capture_output=True)
        if has_unit.returncode != 0 or b"dnsmasq.service" not in has_unit.stdout:
            run(["apt-get", "install", "-y", "--no-install-recommends", "dnsmasq"],
                "установка dnsmasq")
        run(["sh", str(base / "routing-host-setup.sh"), "--apply"], "обвязка хоста")
        run(["sh", str(base / "routing-host-setup.sh"), "--install-unit"],
            "закрепление обвязки от ребута")
        run(["sh", str(base / "routing-link-setup.sh"), "--apply"], "линк до шлюза")
        settings.set_value("app.routing.gw_interface", self._RT_LINK_IF)
        # Включаем и саму функцию: разворачивать обвязку и оставить тумблер
        # выключенным значило бы спрятать «🛰 Назначить шлюз» — кнопку, на
        # которую отправляет итоговое сообщение. До назначения шлюза включённая
        # функция ничего не меняет: маркировать трафик некуда.
        settings.set_value("app.routing.enabled", True)
        routing.invalidate_self_check()
        log.info("условная маршрутизация: обвязка развёрнута, линк %s", self._RT_LINK_IF)
        return "\n".join(out[-1:])[-1500:]

    def routing_status(self) -> tuple[bool, str]:
        """(работоспособна ли фича, причина) — для preflight и админ-UI."""
        return routing.self_check()

    def routing_available(self) -> bool:
        """Функция работоспособна И включена админом.

        Два слоя намеренно разные по природе: инфраструктурный (self_check —
        есть ли чем маршрутизировать) холодный, а выключатель в настройках
        горячий. Первый отвечает «можно ли», второй — «нужно ли»."""
        return bool(settings.get_bool("app.routing.enabled", False)
                    and routing.available())

    def routing_grantable_clients(self) -> list:
        """Профили, которым можно выдать РФ-доступ, — для экрана настроек.

        Служебный профиль исключён (у него нет владельца), админский тоже:
        разрешение у него по умолчанию, и строка в списке предлагала бы выдать
        то, что и так есть."""
        return self.db.list_clients(exclude_tg=config.ADMIN_ID)

    def routing_allowed_for(self, client) -> bool:
        """Разрешён ли клиенту РФ-доступ.

        Админу — всегда: разрешение выдаёт он сам, и заставлять его сначала
        отмечать галочку себе бессмысленно. Остальным — по флагу, который админ
        ставит в их профиле.
        """
        if client is None:
            return False
        if client.tg_id and client.tg_id == config.ADMIN_ID:
            return True
        if client.routing_allowed:
            return True
        # держатель чужих устройств: разрешение — у их владельца (docs/guest-role.md)
        for dev in self.db.list_held_devices(client.id):
            owner = self.db.get_client(dev.client_id)
            if owner is not None and (owner.routing_allowed or owner.tg_id == config.ADMIN_ID):
                return True
        return False

    def routing_client_visible(self, client) -> bool:
        """Показывать ли фичу клиенту вообще. Пока админ не выдал разрешение,
        она невидима: иначе каждый первый пойдёт спрашивать, что это за пункт."""
        return bool(self.routing_allowed_for(client) and self.routing_available())

    def routing_device_counts(self, client_id: int) -> tuple[int, int]:
        """(включено, всего) — и заголовок кнопки, и состояние профиля разом.
        По устройствам СУБЪЕКТА с разрешённым РФ-доступом: свои непереданные и
        удерживаемые."""
        return self.db.routing_device_counts(client_id, config.ADMIN_ID)

    def routing_devices(self, client_id: int) -> list:
        """Устройства субъекта для экрана переключателей: свои, затем
        удерживаемые чужие."""
        return self.db.list_routing_devices(client_id, config.ADMIN_ID)

    def routing_lent_out(self, client_id: int) -> list:
        """Свои устройства, переданные другим: в разделе видны без
        переключателя — управляет держатель."""
        return self.db.list_lent_out_devices(client_id)

    def routing_profile_on(self, client_id: int) -> bool:
        """Режим у профиля включён ⇔ включён хоть на одном устройстве.

        ВЫВОДИМ, а не храним. Отдельная колонка на профиле существовала и была
        убрана: она обязана была совпадать с флагами устройств, а свестись
        обратно при расхождении ей было негде.
        """
        return self.db.routing_device_counts(client_id, config.ADMIN_ID)[0] > 0

    def routing_health_for_client(self, client) -> Optional[bool]:
        """Показывать ли клиенту статусную строку и что в ней.

        None — не показывать вовсе: админ функцию этому профилю не разрешил, и
        рассказывать про механизм тому, кому он недоступен, — шум.

        Разрешил — строка есть ВСЕГДА, в том числе когда сам режим у человека
        выключен. Она отвечает на вопрос «а работает ли оно вообще», который
        иначе задаётся заходом в раздел; знать это полезно как раз перед
        включением. Значение берём из кэша монитора, чтобы открытие меню не
        порождало сетевых вызовов.
        """
        if not self.routing_client_visible(client):
            return None
        return self.db.get_state(self._RT_LINK_KEY) != "0"

    def set_routing_allowed(self, client_id: int, allowed: bool) -> list["Notification"]:
        """Разрешение админа — верхний слой флага. Возвращает уведомление
        владельцу (0 или 1).

        Выдача включает режим на ВСЕХ устройствах профиля сразу: человеку
        обещано «функция включена для всех твоих устройств», и включать их
        руками после выдачи приходилось админу. Отзыв флаги устройств НЕ
        трогает: он гасит эффект, а не разрушает настройку.
        """
        client = self.db.get_client(client_id)
        if client is None:
            return []
        changed = bool(client.routing_allowed) != bool(allowed)
        self.db.update_client_fields(client_id, routing_allowed=1 if allowed else 0)
        if allowed:
            self.db.set_owner_devices_routing(client_id, True)   # и переданные тоже
        self.reconcile_routing()
        if not changed:
            return []
        from awgbot.bot import texts                   # ленивый, как в соседних миксинах
        notes: list[Notification] = []
        if client.tg_id:
            notes.append(Notification(client.tg_id, texts.ROUTING_GRANTED_NOTICE if allowed
                                      else texts.ROUTING_REVOKED_NOTICE))
        # держателям переданных устройств — то же, с оговоркой, от кого
        seen: set[int] = set()
        for dev in self.db.list_lent_out_devices(client_id):
            if dev.holder_tg_id and dev.holder_tg_id not in seen:
                seen.add(dev.holder_tg_id)
                notes.append(Notification(
                    dev.holder_tg_id,
                    texts.routing_granted_holder_notice(client) if allowed
                    else texts.routing_revoked_holder_notice(client)))
        return notes

    def set_routing_all(self, client_id: int, on: bool) -> int:
        """Массовое включение/выключение по всему профилю. Возвращает, сколько
        устройств изменилось.

        Досева при включении здесь нет. Он существовал для обратной модели, где
        набор означал «за границу»: без него сервис с закэшированным у клиента
        адресом уезжал на шлюз и получал отказ. В нынешней модели набор означает
        «домой», и промах даёт мягкий эффект — российский сервис просто
        продолжит ходить как ходил, пока кэш не истечёт. Резолвить ради этого
        шестьсот доменов по нажатию тумблера значило бы подвесить обработчик.
        """
        n = self.db.set_devices_routing(client_id, on)
        if n:
            self.reconcile_routing()
        return n

    def set_routing_device(self, device_id: int, on: bool) -> None:
        """Переключатель одного устройства. ПАРНЫЙ: в окне переезда человек
        видит и щёлкает двойника, а его реальный трафик до переимпорта идёт со
        СТАРОГО адреса. Тронь одну строку — и тумблер перестаёт делать что-либо:
        выключение не выключает, включение не включает, оба молча."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        for peer in self._device_pair(dev):
            self.db.update_device_fields(peer.id, routing_on=1 if on else 0)
        self.reconcile_routing()

    def toggle_routing_device(self, device_id: int) -> Optional[bool]:
        """Инвертировать флаг устройства. Возвращает новое состояние, None —
        устройства нет (колбэк из старого сообщения в истории чата)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return None
        new_state = not bool(dev.routing_on)
        self.set_routing_device(device_id, new_state)
        return new_state

    # ── Личный список доменов ────────────────────────────────────────────────

    def routing_domains(self, client_id: int) -> list[str]:
        return self.db.list_routing_domains(client_id)

    def routing_add_domains(self, client_id: int, text: str) -> "RoutingAddResult":
        """Добавить домены пачкой. Возвращает разбор: что взято, что отброшено.

        Пользователь вставляет списком из мессенджера, и молча проглотить часть
        нельзя — он должен видеть причину по каждой строке, иначе решит, что
        кнопка сломана.
        """
        limit = int(settings.get("app.routing.user_domains_max", 100))
        accepted, rejected = domain_routing.parse_batch(
            text, denylist=config.routing_denylist())
        existing = set(self.db.list_routing_domains(client_id))
        free = max(0, limit - len(existing))
        added: list[str] = []
        over = 0
        for dom in accepted:
            if dom in existing:
                rejected.append((dom, "уже в списке"))
                continue
            if len(added) >= free:
                over += 1
                continue
            if self.db.add_routing_domain(client_id, dom):
                added.append(dom)
        if added:
            self.reconcile_routing()
            self._routing_preseed(client_id, added)
        return RoutingAddResult(added=added, rejected=rejected,
                                over_limit=over, limit=limit)

    _RT_PRESEED_MAX = 20                  # доменов за раз; дальше ждём резолва клиента

    def _routing_preseed(self, client_id: int, domains: list[str]) -> None:
        """Досеять набор адресами только что добавленных доменов.

        Без этого набор для домена пуст до первого DNS-запроса клиента, а его
        не будет, пока не истечёт кэш браузера, — со стороны выглядит как
        «добавил, но не работает; потом само заработало».

        Не критично: резолв может не удаться, набор всё равно наполнится по
        запросам клиента. Поэтому все ошибки глотаем и работу не срываем.
        """
        if not routing.available():
            return
        addrs: list[str] = []
        for dom in domains[:self._RT_PRESEED_MAX]:
            addrs += routing.resolve_a(dom)
        if not addrs:
            return
        try:
            routing.add_networks(routing.user_set(client_id), addrs)
        except routing.RoutingError as e:
            log.warning("routing_preseed: %s", e)

    def routing_remove_domain(self, client_id: int, domain: str) -> bool:
        removed = self.db.remove_routing_domain(client_id, domain)
        if removed:
            self.reconcile_routing()
        return removed

    def routing_clear_domains(self, client_id: int) -> int:
        n = self.db.clear_routing_domains(client_id)
        if n:
            self.reconcile_routing()
        return n

    # ── Реконсиляция и мониторинг ────────────────────────────────────────────

    def reconcile_routing(self) -> None:
        """Привести инфраструктуру к состоянию БД. Идемпотентно.

        Единственная точка, где состояние фичи проецируется наружу: и тумблеры,
        и правки списков зовут её же. Дублировать «точечные» обновления рядом с
        полной сверкой значило бы завести второй путь, который однажды разойдётся
        с первым.
        """
        if not routing.available():          # инфраструктуры нет — трогать нечего
            return
        try:
            with routing.mutation_lock:
                if not settings.get_bool("app.routing.enabled", False):
                    self._routing_stand_down()
                else:
                    self._routing_apply()
        except routing.RoutingError as e:
            # Не только в лог. Сюда прилетает и отказ рестарта dnsmasq, а это не
            # «маршрутизация не применилась», а «резолвер лежит» — то есть у
            # ВСЕХ клиентов нет DNS. Бот при этом продолжал бы работать, считая
            # фичу живой, и сказать об этом было некому: журнал на сервере
            # читают, когда уже пришли разбираться.
            log.warning("reconcile_routing: %s", e)
            self.db.set_state(self._RT_INFRA_BAD, str(e)[:300])
            return
        self.db.set_state(self._RT_INFRA_BAD, "")

    def _routing_stand_down(self) -> None:
        """Снять всё, что фича делает с трафиком.

        Не «просто выйти»: выключатель обязан выключать уже размеченный трафик,
        а не только запрещать новые включения. Состояние в БД при этом цело —
        вернули условия, и следующая же реконсиляция всё восстановит.

        Единственный повод сюда попасть — выключение функции админом: пустой
        набор профилей равносилен выключенной функции сам по себе, и гасить
        ради него нечего.
        """
        routing.sync_nat_exempt(())
        routing.rebuild_chain(())
        routing.set_marking_enabled(False)

    def _routing_apply(self) -> None:
        """Разложить состояние БД по наборам, цепочке и конфигу dnsmasq.

        ДВА СОСТАВА ПРОФИЛЕЙ, и это главное здесь.

        `active` — чей трафик метить прямо сейчас. Меняется от каждого нажатия
        тумблера пользователем, и всё, что от него зависит, обязано быть
        дешёвым: членство в ipset и правила в своей цепочке.

        `known` — у кого вообще есть свой набор: кому админ разрешил функцию,
        плюс те, у кого есть личный список. Меняется только решением админа. На
        нём держится конфиг dnsmasq — потому что его применение стоит РЕСТАРТА
        РЕЗОЛВЕРА, а рестарт роняет кэш и на секунды лишает DNS всех клиентов
        разом, включая тех, кто ничего не переключал.

        Пока конфиг зависел от `active`, каждое нажатие тумблера любым
        пользователем переписывало все ~600 строк и перезапускало dnsmasq всем.
        Снаружи это выглядит как «включил режим — на минуту всё отвалилось», а
        для того, кто в этот момент резолвил имя, — как отказ на ровном месте.

        Работает разделение потому, что правило маркировки требует ОБОИХ
        совпадений: источник в `rt_src_u<N>` И назначение в `vpn_u<N>`. У
        выключенного профиля src-набор пуст, поэтому его набор назначений может
        спокойно наполняться — метить всё равно нечего. Побочная выгода: к
        моменту включения набор уже прогрет, и режим работает с первой секунды,
        не дожидаясь, пока клиент переспросит DNS.
        """
        addrs = self.db.routing_active_addresses(config.ADMIN_ID)
        domains = self.db.routing_domains_by_client()
        active_ids = sorted(set(addrs) | set(domains))
        known_ids = sorted(set(self.db.routing_allowed_client_ids(config.ADMIN_ID))
                           | set(domains))
        # Набор означает ДОМОЙ и наполняется только доменами: все скачиваемые
        # списки подсетей были про заграницу (Cloudflare, Google, Telegram) и
        # ушли вместе с обратной моделью. Российских подсетей сопровождаемого
        # источника не существует, поэтому здесь их нет.
        base_domains = list(self._routing_read_cache("home_domains"))

        # плечо контейнера: выпустить трафик включённых устройств
        # немаскараженным, иначе на хосте их не отличить от остальных
        routing.sync_nat_exempt([a for lst in addrs.values() for a in lst])

        # ДИФФ ПЕРЕД ЗАПИСЬЮ. Реконсиляция идёт каждый тик монитора, и раньше
        # она каждый раз пересобирала все наборы (6 exec на профиль) и цепочку
        # (флаш + правило на профиль) — ~24 000 exec в сутки ради состояния,
        # которое меняется нажатием тумблера. Один `ipset save` даёт состав
        # всех наборов; пишем только те, что разошлись. Не удалось прочитать —
        # пересобираем всё, как прежде.
        live = routing.snapshot_sets()
        for cid in known_ids:
            # src-набор наш — перезаписываем целиком (у выключенного профиля он
            # станет пустым, и это ровно то, что нужно); набор назначений только
            # СОЗДАЁМ: наполняет его dnsmasq по мере резолва доменов, и любая
            # запись с нашей стороны стёрла бы накопленное
            src = routing.src_set(cid)
            routing.replace_members(src, "hash:ip", addrs.get(cid, ()),
                                    current=None if live is None else live.get(src))
            usr = routing.user_set(cid)
            routing.ensure_set(usr, "hash:net", exists=live is not None and usr in live)

        routing.rebuild_chain(active_ids)
        self._routing_drop_orphan_sets(known_ids, names=None if live is None else list(live))
        routing.write_dnsmasq_conf(domain_routing.build_dnsmasq_conf(
            base_domains=base_domains,
            domains_by_client=domains,
            client_ids=known_ids,
            set_user_prefix=config.ROUTING_SET_USER_PREFIX,
        ))

    def _routing_drop_orphan_sets(self, live_ids, names=None) -> None:
        """Снести наборы удалённых клиентов.

        Осиротевший набор сам по себе безвреден (правила на него уже нет), но
        накапливается и однажды совпадёт по имени с новым client_id — тогда
        чужие домены достанутся другому человеку. Ровно та же логика, по которой
        remove_device снимает осиротевший DROP.
        """
        live = {int(c) for c in live_ids}
        prefixes = (config.ROUTING_SET_USER_PREFIX, config.ROUTING_SET_SRC_PREFIX)
        for name in (routing.list_sets() if names is None else names):
            for pref in prefixes:
                if not name.startswith(pref) or name.endswith("_tmp"):
                    continue
                tail = name[len(pref):]
                if tail.isdigit() and int(tail) not in live:
                    routing.destroy_set(name)

    _RT_LISTS_KEY = "routing_lists_updated_at"

    # Скачанные списки лежат в КЭШЕ рядом с БД, а не сразу в наборах: наборы
    # пер-юзерные, их состав вычисляется при каждой реконсиляции, и держать
    # исходник отдельно от результата — единственный способ пересобрать состав
    # при появлении нового профиля, не выкачивая всё заново.
    def _routing_cache(self, kind: str):
        return config.DATA_DIR / f"routing-{kind}.lst"

    def _routing_write_cache(self, kind: str, items) -> None:
        path = self._routing_cache(kind)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text("\n".join(items) + "\n", encoding="utf-8")
            tmp.replace(path)                 # атомарно: без полуфайла
        except OSError as e:
            log.warning("routing: не записать кэш %s (%s)", kind, e)

    def _routing_read_cache(self, kind: str) -> list[str]:
        """Пусто — значит списки ещё не качали. Файл перечитывается только при
        смене mtime: раньше ~600 строк читались с диска каждый тик."""
        path = self._routing_cache(kind)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        mem = self.__dict__.setdefault("_routing_cache_mem", {})   # на экземпляр
        hit = mem.get(str(path))
        if hit and hit[0] == mtime:
            return list(hit[1])
        try:
            items = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            return []
        mem[str(path)] = (mtime, items)
        return list(items)

    # ── источники списков: замечать, когда источник перестал отдавать ────────
    # Кэш переживает недоступность источника намеренно — устаревшие списки лучше
    # пустых. Но у этого есть оборотная сторона: источник может замолчать
    # навсегда (переехал, переименовали файл, репозиторий забросили), а кэш
    # останется прежним и никто не узнает. routing_lists_ready смотрит на
    # непустоту, а не на свежесть, поэтому такой отказ не виден вообще ничем.
    _RT_SRC_N = "rt_src_n:"          # последнее НЕнулевое число записей
    _RT_SRC_BAD = "rt_src_bad2:"     # подтверждённая беда: "" | "down" | "gone" | "empty"
    _RT_SRC_ERR = "rt_src_err:"      # текст ошибки — половина диагноза
    _RT_SRC_FAILS = "rt_src_fails:"  # неудач подряд, для добора попыток
    _RT_SRC_SAID = "rt_src_said:"    # о какой беде уже доложили

    # Тревога не по первой неудаче: сеть моргает, а GitHub отдаёт 429 на минуты.
    # Доклад по одному промаху приучил бы не читать эти сообщения ровно к тому
    # разу, когда источник умер по-настоящему.
    _RT_SRC_TRIES = 4                # первая попытка и три добора
    _RT_SRC_RETRY_SECS = 300         # пауза между ними

    @staticmethod
    def _routing_src_key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:12]

    def _routing_note_source(self, url: str, count: int, err: str = "",
                             code: int = 200) -> bool:
        """Запомнить исход обращения к источнику. True — нужен скорый повтор.

        Исходов четыре, и чинятся они в разных местах: отдал записи; не ответил
        (наша связь, файл при этом может быть цел); ответил 404 (файл переехал
        или удалён); ответил 200, но разбирать нечего (сменился формат). Прежде
        все, кроме первого, приходили сюда одинаковым нулём, и доклад звал
        искать переехавший файл там, где до файла попросту не дошли.

        Пустой ответ добором попыток не проверяем: он не про связь, и следующая
        попытка вернёт ровно то же самое.
        """
        k = self._routing_src_key(url)
        if count:
            self.db.set_state(self._RT_SRC_N + k, str(count))
            self.db.set_state(self._RT_SRC_FAILS + k, "0")
            self.db.set_state(self._RT_SRC_BAD + k, "")
            return False
        if not self.db.get_state(self._RT_SRC_N + k):
            return False              # ни разу не отдавал — сравнивать не с чем
        if not err:
            self.db.set_state(self._RT_SRC_BAD + k, "empty")
            return False
        fails = int(self.db.get_state(self._RT_SRC_FAILS + k) or 0) + 1
        self.db.set_state(self._RT_SRC_FAILS + k, str(fails))
        self.db.set_state(self._RT_SRC_ERR + k, err)
        if fails < self._RT_SRC_TRIES:
            return True               # рано тревожить — добираем попытки
        # 404/410 добором не лечится, но и он его проходит: правило одно на все
        # не-двухсотые, а разделяем их только в докладе.
        self.db.set_state(self._RT_SRC_BAD + k, "gone" if code in (404, 410) else "down")
        return False

    def _routing_src_state(self, k: str) -> str:
        """Что с источником сейчас. Пока доборы не исчерпаны — прежнее значение:
        неподтверждённая неудача не считается ни бедой, ни выздоровлением."""
        return self.db.get_state(self._RT_SRC_BAD + k) or ""

    def _routing_src_said(self, k: str) -> str:
        """О чём по этому источнику уже доложено."""
        return self.db.get_state(self._RT_SRC_SAID + k) or ""

    # Реконсиляция упала. Отдельный ключ, а не флаг рядом с источниками: там
    # «списки застыли», здесь «примениться не удалось», и чинятся они в разных
    # местах. Хранится текст ошибки — он же и есть половина диагноза.
    _RT_INFRA_BAD = "rt_infra_bad"
    _RT_INFRA_ANNOUNCED = "rt_infra_announced"

    def routing_infra_alerts(self) -> list[Notification]:
        """Реконсиляция маршрутизации падает — сказать админу. Один доклад.

        Главный случай — не поднявшийся после правки конфига dnsmasq: у клиентов
        при этом умирает DNS целиком, а внешне это «интернет работает через раз»,
        потому что всё уже отрезолвленное продолжает ходить. Связать такое с
        маршрутизацией без подсказки почти невозможно.
        """
        err = self.db.get_state(self._RT_INFRA_BAD) or ""
        announced = self.db.get_state(self._RT_INFRA_ANNOUNCED) == "1"
        if not err:
            if not announced:
                return []
            self.db.set_state(self._RT_INFRA_ANNOUNCED, "0")
            return [Notification(config.ADMIN_ID,
                                 "🟢 Условная маршрутизация снова применяется.")]
        if announced:
            return []
        self.db.set_state(self._RT_INFRA_ANNOUNCED, "1")
        return [Notification(config.ADMIN_ID, _TXT_RT_INFRA_BAD.format(err=_e(err)), critical=True)]

    def routing_source_alerts(self) -> list[Notification]:
        """Смена состояния источника — доклад. Один на смену, не на тик.

        Докладываем и о восстановлении: молчащий источник не ломает
        маршрутизацию сегодня, поэтому увидеть своими глазами, что списки снова
        обновляются, неоткуда — как и понять, чинить ли ещё. Пока в боте были
        одни тревоги, разошедшийся сам собой лимит запросов оставался бы висеть
        нерешённым делом.

        Переход down→empty (или обратно) тоже доклад: диагноз сменился, а с ним
        и место, куда идти чинить.
        """
        notes: list[Notification] = []
        for url in config.ROUTING_LISTS_HOME_URLS:
            if not url:
                continue
            k = self._routing_src_key(url)
            state = self._routing_src_state(k)
            if state == self._routing_src_said(k):
                continue
            self.db.set_state(self._RT_SRC_SAID + k, state)
            n = self.db.get_state(self._RT_SRC_N + k) or "?"
            err = _e(self.db.get_state(self._RT_SRC_ERR + k) or "?")
            if not state:
                text = _TXT_RT_SRC_OK.format(url=_e(url), n=n)
            elif state == "down":
                text = _TXT_RT_SRC_DOWN.format(url=_e(url), err=err,
                                               tries=self._RT_SRC_TRIES)
            elif state == "gone":
                text = _TXT_RT_SRC_GONE.format(url=_e(url), err=err,
                                               tries=self._RT_SRC_TRIES)
            else:
                text = _TXT_RT_SRC_STALE.format(url=_e(url), n=n)
            notes.append(Notification(config.ADMIN_ID, text))
        return notes

    def routing_update_lists(self, force: bool = False) -> int:
        """Обновить базовый набор из внешних источников. Возвращает число записей.

        Зовётся ботом самостоятельно — при старте и по расписанию. Руками
        запускать ничего не нужно: требовать этого от админа значит гарантировать,
        что однажды забудут, а без списков режим не действует вовсе — человек
        включит тумблер и не получит ничего.

        Недоступность источника не считается ошибкой: прежний набор остаётся в
        силе. Застывший список всё ещё покрывает большинство сервисов, пустой не
        покрывает ни одного.
        """
        # Гейт НАМЕРЕННО не routing.available(): полная самопроверка требует
        # рабочего окружения, а наполнение списков — это как раз то, чем оно
        # становится рабочим. Проверять её здесь значило бы получить
        # взаимоблокировку: набор пуст → «недоступна» → наполнять не идём →
        # набор пуст. Достаточно того, что функция вообще включена.
        if not force:
            if not config.ROUTING_ENABLED:
                return 0
            if not settings.get_bool("app.routing.enabled", False):
                return 0
        now = int(time.time())
        every = int(settings.get("app.routing.lists_refresh_hours", 6)) * 3600
        if not force:
            last = self.db.get_state(self._RT_LISTS_KEY)
            cached = len(self._routing_read_cache("home_domains"))
            # по расписанию ИЛИ немедленно, если кэш пуст: без списков режим не
            # действует вовсе, и ждать следующего окна незачем — в том числе на
            # старте бота, где эта же ветка и срабатывает
            if last and now - int(last) < every and cached > 0:
                return cached
        try:
            # СКАЧИВАНИЕ — СНАРУЖИ ЗАМКА. Источники с таймаутом по 15 с, а под
            # замком ждёт тик живости: держать его на это время значило бы
            # менять один отказ на другой. Замок берём только на запись.
            # Российские сервисы, которым нужен российский адрес. Списки
            # заграницы (блокировки, геоблок, подсети CDN) ушли вместе с
            # обратной моделью: набор перечисляет то, что идёт на шлюз, а туда
            # заграница не ходит по определению.
            home: list[str] = []
            retry = False
            for url in config.ROUTING_LISTS_HOME_URLS:
                if not url:
                    continue
                body, err, code = routing.fetch(url)
                got = domain_routing.parse_domain_list(body) if body else []
                retry |= self._routing_note_source(url, len(got), err, code)
                home.extend(got)

            with routing.mutation_lock:
                if home:
                    self._routing_write_cache("home_domains", sorted(set(home)))
                size = len(self._routing_read_cache("home_domains"))
                # Неудача не должна съедать окно целиком: 429 живёт минуты, а
                # окно — часы, и следующая попытка пришлась бы на давно
                # разошедшийся лимит. Пока доборы не исчерпаны, метку сдвигаем
                # так, чтобы гейт открылся через паузу добора.
                self.db.set_state(self._RT_LISTS_KEY, str(
                    now - every + min(every, self._RT_SRC_RETRY_SECS) if retry else now))
                # окружение изменилось нашими руками — прежний вердикт
                # самопроверки протух
                routing.invalidate_self_check()
                log.info("routing: списки обновлены, в базовом наборе %d записей", size)
                return size
        except routing.RoutingError as e:
            log.warning("routing_update_lists: %s", e)
            return len(self._routing_read_cache("home_domains"))

    def routing_lists_info(self) -> dict:
        """Состояние списков для чата: сколько записей, когда обновлялись,
        период. Обновление — молчаливый процесс на тике монитора, и без этой
        сводки админ не отличит «списки свежие» от «источники умерли полгода
        назад, кэш застыл»."""
        raw = self.db.get_state(self._RT_LISTS_KEY)
        updated_at = int(raw) if raw and raw.isdigit() else None
        return {
            "count": len(self._routing_read_cache("home_domains")),
            "updated_at": updated_at,
            "age_seconds": (int(time.time()) - updated_at) if updated_at else None,
            "every_hours": int(settings.get("app.routing.lists_refresh_hours", 6)),
            "sources": len([u for u in config.ROUTING_LISTS_HOME_URLS if u]),
        }

    def routing_link_ok(self) -> bool:
        """Проходит ли трафик через шлюз — по последнему замеру зонда.

        Читаем сохранённый результат, а не зондируем на месте: метод дёргают
        экраны и preflight, а зонд — это пинги с таймаутами, им в отрисовке
        интерфейса не место. Замер делает routing_liveness_tick.
        """
        return self.db.get_state(self._RT_LINK_KEY) != "0"

    def routing_engaged(self) -> bool:
        """Должна ли маркировка быть включена в принципе — до вопроса о живости.

        Один предикат на всех, кто трогает рубильник. Реконсиляция и тик живости
        уже однажды разошлись во мнениях и спорили за него: один снимал политику,
        другой немедленно возвращал. Пока условие живёт в одном месте, разойтись
        им негде. routing.available() — это здоровье ОБВЯЗА, а не выключатель
        фичи; их легко перепутать, и тут они сведены явно.
        """
        return (routing.available()
                and settings.get_bool("app.routing.enabled", False))

    def routing_probe(self) -> str:
        """Замер прямо сейчас. Отдельно от routing_link_ok, чтобы было видно,
        где реальные пинги, а где чтение кэша."""
        # Две цели по умолчанию: один внешний хост — сам по себе точка отказа,
        # и его заминка выглядела бы как отвал шлюза.
        targets = settings.get("app.routing.probe_targets", None) or ["77.88.8.8", "8.8.8.8"]
        port = int(settings.get("app.routing.probe_port", 53))
        return routing.probe_gateway(list(targets), port)

    def routing_liveness_tick(self) -> list[Notification]:
        """Замер живости шлюза и деградация. Тикает часто (десятки секунд).

        Раньше это жило в трёхминутном мониторе и решало по возрасту хендшейка.
        Метрика была неверна и остаётся таковой: возраст хендшейка говорит,
        поднят ли туннель, а не ходит ли через него трафик.

        Частота досталась от обратной модели, где шлюз был ОСНОВНЫМ путём и его
        отказ означал «нет интернета». Сейчас он означает «российские сервисы
        ругаются на адрес», и такой срочности нет. Такт оставлен коротким
        сознательно: замер — это TCP-коннект, стоит копейки, а быстрый возврат
        из деградации полезен сам по себе.

        Пороги СИММЕТРИЧНЫ — три замера в обе стороны, см. _RT_DOWN_STREAK.
        Асимметрия имела смысл, пока включение было рискованнее выключения;
        теперь дороже всего дребезг, потому что каждое переключение
        перекладывает трафик всех включённых профилей.
        """
        if not routing.available():
            return []
        # Зондируем, только если маркировка вообще должна быть включена: при
        # выключенной фиче рубильник обязан стоять в «выкл» независимо от того,
        # что там со шлюзом.
        engaged = self.routing_engaged()
        # Зонд СНАРУЖИ замка: он длится секунды (сеть), и держать на это время
        # реконсиляцию значило бы менять один отказ на другой.
        verdict = self.routing_probe() if engaged else routing.PROBE_DOWN
        # Тик только МЕРИТ. Обвязку утверждаем по событию — смена вердикта,
        # первый тик после старта — и страховочно каждый 10-й тик (5 мин):
        # утверждать маршрут и правила каждые 30 с значило ~14 000 exec/сутки
        # ради состояния, которое меняется раз в неделю.
        self._rt_tick = getattr(self, "_rt_tick", -1) + 1
        last_verdict = getattr(self, "_rt_last_verdict", None)
        if engaged and (verdict != last_verdict or self._rt_tick % 10 == 0):
            routing.ensure_policy()
        self._rt_last_verdict = verdict

        # ГИСТЕРЕЗИС, а не пересчёт с нуля каждый тик. Пока порог гашения был
        # равен единице, пересчёт совпадал с гистерезисом и разницы не было. С
        # порогом больше единицы он ломается: плохой замер обнуляет счётчик
        # хороших, и следующий же хороший такт даёт good=1 < порога возврата —
        # то есть маркировка снимается ИМЕННО ТОГДА, когда шлюз ожил. Состояние
        # обязано меняться только на пересечении порогов, а не выводиться из
        # текущей серии.
        was_on = self.db.get_state(self._RT_LINK_KEY) == "1"
        # Стрики с потолком на пороге (сравнения только «>= порога» / «< порога»)
        # и одной транзакцией: в установившемся состоянии тик каждые 30 с не
        # пишет на диск вовсе — раньше это было 3 коммита × 2880 в сутки.
        down_cap = max(self._RT_DOWN_STREAK, self._RT_ANNOUNCE_AFTER)
        with self.db.transaction():
          if not engaged:
            # РЕШЕНИЕ, а не измерение. Гистерезис сглаживает дребезг сети, но
            # выключение фичи админом — не дребезг, и ждать три такта тут значит
            # не выполнить прямое указание. Раньше разницы не было: «выключено»
            # выражалось тем же плохим вердиктом, а порог гашения равнялся
            # единице, и оба пути совпадали.
            self.db.set_state(self._RT_STREAK_KEY, "0")
            down = 0
            ok = False
          elif verdict == routing.PROBE_OK:
            good = min(int(self.db.get_state(self._RT_STREAK_KEY) or 0) + 1, self._RT_UP_STREAK)
            self.db.set_state(self._RT_STREAK_KEY, str(good))
            down = 0
            ok = True if was_on else good >= self._RT_UP_STREAK
          else:
            self.db.set_state(self._RT_STREAK_KEY, "0")
            down = min(int(self.db.get_state(self._RT_DOWN_KEY) or 0) + 1, down_cap)
            # держим маркировку, пока порог гашения не набран
            ok = was_on and down < self._RT_DOWN_STREAK
          self.db.set_state(self._RT_DOWN_KEY, str(down))

        try:
            # ПОД ЗАМКОМ: реконсиляция под ним же пересобирает ту цепочку, чей
            # рубильник мы дёргаем. _routing_apply перекладывает и наборы, и
            # состав правил; включить маркировку посреди этого значит открыть
            # хук в цепочку, собранную наполовину, — метить не тем набором и не
            # для тех профилей.
            with routing.mutation_lock:
                routing.set_marking_enabled(ok)
        except routing.RoutingError as e:
            log.warning("routing_liveness_tick: %s", e)
            return []

        self.db.set_state(self._RT_LINK_KEY, "1" if ok else "0")

        # ДЕЙСТВИЕ и ОБЪЯВЛЕНИЕ — разные пороги, и это не педантизм. Написать
        # админу дорого: короткий провал на домашнем аплинке — обычное дело, и
        # пара «отвалился/поднялся» в одну минуту не несёт ему информации, только
        # приучает не читать. Об отказе сообщаем, лишь когда он подтвердился
        # несколькими замерами. Сейчас пороги СОВПАДАЮТ (три и три), то есть
        # админ узнаёт ровно в момент смены состояния; разными они остаются по
        # смыслу — это разные решения, и разводить их можно, не трогая второе.
        announced = self.db.get_state(self._RT_ANNOUNCED_KEY) == "1"

        if ok:
            # «Снова в строю» — только если об отвале действительно сообщали.
            # Иначе админ получал бы одинокое «всё хорошо» на ровном месте.
            if not announced:
                return []
            self.db.set_state(self._RT_ANNOUNCED_KEY, "0")
            return [Notification(config.ADMIN_ID, self._TXT_RT_GW_UP)]

        if announced or down < self._RT_ANNOUNCE_AFTER:
            return []
        self.db.set_state(self._RT_ANNOUNCED_KEY, "1")
        # Разные причины — разный ремонт, поэтому и текст разный: «шлюз молчит»
        # чинят на линке, «за шлюзом нет интернета» — на самом шлюзе.
        text = (self._txt_rt_gw_no_path() if verdict == routing.PROBE_NO_PATH
                else self._txt_rt_gw_down())
        return [Notification(config.ADMIN_ID, text, critical=True)]

    # ── Детект рестарта сервиса ──────────────────────────────────────────────

    def detect_and_handle_restart(self) -> bool:
        """Сверяет метку старта сервиса с сохранённой. Изменилась (был рестарт) —
        реконсиляция блокировок. Возвращает True, если был рестарт.

        Что считать меткой, решает awg.service_started_at: в контейнере это его
        StartedAt, на хосте — время загрузки системы. Ключ в state исторически
        зовётся container_started_at и переименованию не подлежит — иначе первая
        же сверка после обновления не найдёт сохранённого значения."""
        current = awg.service_started_at()
        if not current:
            return False
        stored = self.db.get_state("container_started_at")
        if current != stored:
            self.db.set_state("container_started_at", current)
            if stored is not None:                        # не первый запуск
                self.reconcile_blocks()
                self.reconcile_ssh_access()               # SSH-фильтр тоже слетел
                return True
        return False

    # ── Вьюхелперы для отображения ───────────────────────────────────────────

    def count_unassigned_devices(self) -> int:
        service_id = self.db.get_service_client_id()
        return self.db.count_devices(service_id)

    def profile_traffic_limit(self, client_id: int) -> int:
        """Лимит трафика профиля-владельца (байты, 0 = безлимит) — для подсказки
        при задании лимита устройства."""
        c = self.db.get_client(client_id)
        return int(c.traffic_limit) if c else 0

    def client_is_online(self, client_id: int) -> bool:
        """Онлайн ли хоть одно устройство профиля — одним индексным запросом."""
        return self.db.client_has_online_device(
            client_id, settings.get_int("app.online_handshake_seconds", 300),
            ref_ts=int(self.online_ref().timestamp()))

    def device_slots(self, client_id: int) -> tuple[int, int]:
        """(добавлено, лимит) — для подсветки «M из N»."""
        client = self.db.get_client(client_id)
        if client is None:
            return (0, 0)
        return (self.db.count_devices(client_id), client.device_limit)

    def is_only_device(self, device_id: int) -> bool:
        """True, если это единственное устройство своего клиента (удаление =
        потеря доступа к VPN и, возможно, к боту). Для устройств «без профиля»
        (служебный клиент) — всегда False: у них нет tg_id-владельца, который
        «потеряет доступ через этот VPN», предупреждение неприменимо."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return False
        if dev.client_id == self.db.get_service_client_id():
            return False
        return self.db.count_devices(dev.client_id) <= 1

    # ── Бэкап ────────────────────────────────────────────────────────────────

    def make_backup(self) -> list[str]:
        """Одна резервная копия — один архив: БД, все conf/*.yaml, env и
        серверный конфиг awg-интерфейса (единственная копия этого файла вне
        сервера). Задан секрет — файл шифруется целиком (*.tgz.enc), иначе
        уходит открытым (в разделе это видно красным). Разворачивается
        `awg-bot restore <файл>` на любом хосте."""
        extra: list[tuple[str, bytes]] = []
        try:
            conf = awg.read_file(config.CONF_PATH)
            extra.append((f"awg/{config.AWG_INTERFACE}.conf", conf.encode("utf-8")))
        except awg.AwgError:
            pass
        return self.write_backup_archive("main", extra)

    # ── Статус сервера (мониторинг) ──────────────────────────────────────────

    def check_resource_alerts(self, metrics: dict) -> list["Notification"]:
        """Гистерезис загрузки хоста по СТРИКАМ (замерам подряд). Вызывается на
        каждом тике монитора с локальным снимком {cpu, ram, disk} (% или None).

        На каждый ресурс держим два счётчика в state: подряд-превышений и
        подряд-нормы. Значение ≥ порога двигает превышения (+1) и обнуляет норму;
        < порога — наоборот. Алерт «высокая загрузка» — когда превышения достигают
        RESOURCE_ALERT_STREAK и алерт ещё не активен; «отбой» — когда норма
        достигает того же порога и алерт активен. Симметрично вверх/вниз.

        При streak=5 и тике монитора 3 мин реакция ≤ 15 мин. Обычные Notification
        (force_sound=False) → в тихие часы без звука. None-метрика не двигает
        счётчики (нет данных ≠ норма)."""
        if not settings.get_bool("resource_alerts.enabled", True):
            return []
        streak_n = settings.get_int("app.monitoring.alert_streak", 5)
        thresholds = {
            "cpu": (settings.get_int("resource_alerts.thresholds_percent.cpu", 80), "CPU", "🖥"),
            "ram": (settings.get_int("resource_alerts.thresholds_percent.ram", 80), "RAM", "🧠"),
            "disk": (settings.get_int("resource_alerts.thresholds_percent.disk", 80), "Диск", "💽"),
        }
        notes: list[Notification] = []
        # Одна транзакция на тик и счётчики с ПОТОЛКОМ: стрик выше порога
        # ничего не решает (сравнения только «>= порога»), а без потолка
        # счётчик нормы рос бы вечно и каждый тик был бы записью на диск.
        # В спокойном состоянии (норма, стрик набран) тик не пишет ничего.
        with self.db.transaction():
          for key, (threshold, label, icon) in thresholds.items():
            value = metrics.get(key)
            if value is None:
                continue                       # нет данных — счётчики не трогаем
            hi_key = f"res_hi_{key}"           # подряд-превышений
            lo_key = f"res_lo_{key}"           # подряд-нормы
            armed_key = f"res_alert_{key}"     # "1" ⇔ алерт активен
            hi = int(self.db.get_state(hi_key) or 0)
            lo = int(self.db.get_state(lo_key) or 0)
            armed = self.db.get_state(armed_key) == "1"
            if value >= threshold:
                hi, lo = min(hi + 1, streak_n), 0
                if hi >= streak_n and not armed:
                    self.db.set_state(armed_key, "1")
                    notes.append(Notification(
                        config.ADMIN_ID,
                        f"⚠️ {icon} Высокая загрузка: {label} {value:.0f}% "
                        f"(порог {threshold}%, держится ≥{streak_n} замеров).",
                        critical=True))
            else:
                lo, hi = min(lo + 1, streak_n), 0
                if lo >= streak_n and armed:
                    self.db.set_state(armed_key, "0")
                    notes.append(Notification(
                        config.ADMIN_ID,
                        f"✅ {icon} {label} вернулся в норму: {value:.0f}% "
                        f"(ниже порога {threshold}%)."))
            self.db.set_state(hi_key, str(hi))
            self.db.set_state(lo_key, str(lo))
        return notes

    def server_status_cached(self) -> dict:
        """Статусный блок из state — ноль docker exec. Живость awg пишет монитор,
        started_at — детект рестарта, online_count — опросчик трафика, а метрики
        железа (CPU/RAM/диск) монитор снимает ЛОКАЛЬНО (co-located) — hostmetrics. Возраст
        метрик показываем в инфобоксе (обновляет монитор каждый тик, локально)."""
        from awgbot.runtime import hostmetrics
        ok_raw = self.db.get_state("last_server_ok")
        ok = None if ok_raw is None else (ok_raw == "1")
        started = timeutil.parse_docker_time(self.db.get_state("container_started_at") or "")
        uptime = timeutil.fmt_uptime(started) if started else None
        online_raw = self.db.get_state("online_count")
        online = int(online_raw) if online_raw is not None else None
        cpu = ram = disk = age_seconds = None
        snap = hostmetrics.get_host_metrics(self.db)
        if snap:
            cpu, ram, disk = snap.get("cpu"), snap.get("ram"), snap.get("disk")
            age_seconds = snap.get("age_seconds")
        return {"ok": ok, "uptime": uptime, "online_count": online,
                "cpu": cpu, "ram": ram, "disk": disk, "age_seconds": age_seconds}

    def server_ok(self) -> bool:
        """Живая проверка: если awg внутри контейнера ответил — контейнер
        заведомо запущен, отдельный container_running не нужен (1 exec, не 2)."""
        return awg.awg_responding()

    def server_ok_cached(self) -> bool:
        """Статус из state (пишет monitor-задача каждые MONITOR_MINUTES и старт).
        Для приветствий/меню: 0 docker exec, свежесть ≤3 мин — для строки
        «сервер работает» более чем достаточно."""
        cached = self.db.get_state("last_server_ok")
        if cached is not None:
            return cached == "1"
        return self.server_ok()          # state ещё не прогрет (первый старт)

    def refresh_status_now(self) -> list:
        """Внеплановое обновление статусного блока по требованию (кнопка админа):
        живой снимок awg-статуса, опрос пиров (счётчик онлайн и хендшейки — без
        него цифра оставалась бы от прошлого тика) и локальные метрики железа
        прямо в state, минуя ожидание следующего тика монитора. Блокирующий
        (docker exec + /proc) — вызывать через asyncio.to_thread. Меню/инфобокс
        потом читают из state как обычно (0 docker exec на показ). Возвращает
        уведомления опроса (поздравления переезда) — отправить их обязан
        вызывающий."""
        from awgbot.runtime import hostmetrics
        ok = self.server_ok()
        self.db.set_state("last_server_ok", "1" if ok else "0")
        started = awg.service_started_at()
        if started:
            self.db.set_state("container_started_at", started)
        hostmetrics.collect_and_store(self.db)
        return self.poll_traffic() if ok else []

    def restart_service(self) -> None:
        """Перезапуск AmneziaWG по кнопке админа. На хосте это awg-quick
        down/up — метка старта не меняется, блокировки не слетают, и
        reconcile_blocks ниже лишь подтверждает картину. В docker-режиме
        меняется StartedAt контейнера, и detect_and_handle_restart переналагает
        и блокировки, и SSH-фильтр."""
        awg.restart_server()
        self.detect_and_handle_restart()
        self.reconcile_blocks()


__all__ = [
    "Services", "ServiceError", "LimitReached", "Notification",
    "ClientCreated", "ActivationResult", "DeviceCreated", "ExtendResult",
]
