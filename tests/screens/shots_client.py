"""Снимки экранов клиента (client.txt) и гостя (guest.txt): жмёт владелец профиля
или гость, диспетчер основного бота.

Состояние — построителями ниже, на свежей БД каждого снимка, поэтому номера
известны заранее: профиль 1 — служебный «Устройства без клиента», Вася — 2,
следующие — по порядку создания; устройства — с 1 по порядку создания.
Коды приглашений (C…/F…) детерминированы: генератор тела кода подменён на
экземпляре сервисов этого снимка (TEST0000001, TEST0000002, …) — иначе
приглашение другу и активация по коду меняли бы эталон на каждом прогоне.
"""
from __future__ import annotations

import itertools

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import (BlockCB, CancelCB, DelDeviceCB, DeviceCB, FriendCB, GraceCB, GuideCB,
                                  HelpCB, HideCB, Menu, NoteCB, PageCB, PauseCB, PresetCB, RoutingCB)
from awgbot.bot.keyboards.common import entry_tag
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.core import settings
from awgbot.infra import email_resume
from tests.screens.base import CLIENT_TG, Shot, owner

G = 1024 ** 3
GUEST_TG = 2000          # гость «Артём»
OTHER_TG = 3000          # второй владелец «Петя» — дарит Васе своё устройство
THIRD_TG = 4000          # третий владелец «Маша» — чужой даритель для отказа
STRANGER_TG = 5000       # незнакомец без профиля


# ── построители состояния ────────────────────────────────────────────────────

def _codes(services) -> None:
    """Тело кода приглашения — по счётчику, а не случайное."""
    n = itertools.count(1)
    services._gen_code_body = lambda: f"TEST{next(n):07d}"


def _make(services, name, tg, *, limit=3, period="year", traffic=0) -> int:
    c = services.create_client(name, limit, period, traffic)
    res = services.activate_client(c.invite_code, tg)
    assert res.ok, res.reason
    return c.client_id


def _lend(services, dev_id: int, tg: int, tg_name: str):
    res = services.activate_friend(services.make_device_friendly(dev_id), tg_id=tg, tg_name=tg_name)
    assert res.ok, res.reason
    return res.holder


def vasya(devices=(), *, limit=3, period="year", traffic=0, rf=False, rf_on=True, rf_down=False,
          domains="", lend=(), pending=(), holds=False, unmanaged=False, blocked=(), usage=(),
          after=None):
    """Владелец «Вася» (профиль 2) с устройствами devices (номера 1, 2, … по
    порядку). lend — номера устройств, переданных гостю «Артём»; pending —
    приглашение другу выписано, но не принято; holds — Вася держит устройство
    «Телефон Пети» от владельца «Петя»; unmanaged — следом пир без ключа
    «Пир»; blocked — номера, заблокированные своей кнопкой; usage — трафик
    за месяц ((номер, ↓, ↑), …); rf — РФ-доступ выдан (rf_on — включён на
    устройствах, rf_down — шлюз не пропускает); domains — свои адреса
    строкой; after(services) — последний штрих."""
    def build(services):
        _codes(services)
        cid = _make(services, "Вася", CLIENT_TG, limit=limit, period=period, traffic=traffic)
        ids = [services.add_device(cid, n).device_id for n in devices]
        if unmanaged:
            services.db.create_device(cid, "Пир", "PUBDUMMY", "PSKDUMMY", "10.8.1.90", private_key=None)
        if rf:
            services.set_routing_allowed(cid, True)
            if not rf_on:
                services.set_routing_all(cid, False)
            if rf_down:
                services.db.set_state("routing_link_ok", "0")
            if domains:
                services.routing_add_domains(cid, domains)
        for i in lend:
            _lend(services, ids[i - 1], GUEST_TG, "Артём")
        for i in pending:
            services.make_device_friendly(ids[i - 1])
        for i in blocked:
            services.block_device_manual(ids[i - 1], DeviceBlock.USER, True)
        if usage:
            services.db.add_traffic_bulk([(ids[i - 1], rx, tx) for i, rx, tx in usage])
        if holds:
            pid = _make(services, "Петя", OTHER_TG)
            did = services.add_device(pid, "Телефон Пети").device_id
            _lend(services, did, CLIENT_TG, "Вася")
        if after:
            after(services, cid)
        return CLIENT_TG, "Вася"
    return build


def _expiring(services, cid):
    services.db.update_client_fields(cid, period_end="2026-09-18T12:00:00+03:00",
                                     notified_thresholds="4320")


def _expired(services, cid):
    services.db.update_client_fields(cid, period_end="2026-09-10T12:00:00+03:00", status="expired")


def _paused(services, cid):
    ok, *_ = services.enter_pause(cid, 10)
    assert ok


def _admin_paused(services, cid):
    services.block_client_manual(cid, ClientBlock.ADMIN_NOTIFIED, True, pause_days=0)


def _no_pause_days(services, cid):
    services.db.set_pause_balance(cid, 0)


MAIL_CONF = {"email.imap_host": "imap.example.com", "email.smtp_host": "smtp.example.com"}


def with_mail(build):
    """Почта настроена (аварийный выход из паузы включён), код выхода —
    постоянный: настоящий берётся из secrets и менял бы эталон."""
    def wrapped(services, mp):
        services.db.set_state("email_login", "box@example.com")
        services.db.set_state("email_password", "DUMMY")
        mp.setattr(email_resume, "generate_code", lambda length=None: "DUMMYCODE")
        return build(services)
    return wrapped


async def _expiry_tick(services, bot):
    """Такт проверки сроков, как в планировщике (runtime/scheduler.py,
    job_expiry): уведомления сервиса и кнопка отсрочки к клиентскому."""
    notes = services.check_expiry()
    for n in notes:
        if getattr(n, "grace_offer_client_id", 0):
            n.reply_markup = kb.grace_offer(n.grace_offer_client_id, settings.get_int("grace.grace_days", 14))
    await send_notifications(bot, notes)


def _soon(services, cid):
    """До конца годовой подписки неделя, уведомления о сроке ещё не было."""
    services.db.update_client_fields(cid, period_start="2025-09-22T12:00:00+03:00",
                                     period_end="2026-09-22T12:00:00+03:00")


async def _added_by_admin(services, bot):
    """Админ добавил Васе устройство 2: уведомление с рядом выдачи
    (handlers/admin/devices.py — тем же вызовом)."""
    used, limit = services.device_quota(2)
    await notify_one(bot, CLIENT_TG, texts.reassign_recipient_notice("Ноутбук", used, limit),
                     reply_markup=kb.added_by_admin(2))


def stranger(after=None):
    """Незнакомец без профиля; after — что лежит в базе (приглашения)."""
    def build(services):
        _codes(services)
        if after:
            after(services)
        return STRANGER_TG, "Гена"
    return build


def _invite_waiting(services):
    """Профиль «Гена» создан админом, приглашение CTEST0000001 не активировано."""
    services.create_client("Гена", 3, "year", 0)


def guest(n=1, *, rf=False, drop=False, owner_after=None, more=False, other=False, upgrade=False):
    """Гость «Артём» держит n устройств Васи («Телефон Артёма», «Ноутбук
    Артёма», …; номера 1…n). rf — Васе выдан РФ-доступ; drop — все
    переданные удалены, гость остался без устройств; owner_after(services,
    id Васи) — состояние владельца; more — Вася выписал ещё одно приглашение
    (следующий код F…); other — приглашение от «Маши» (чужой даритель);
    upgrade — лежит неактивированный профиль-приглашение CTEST… для гостя."""
    names = ["Телефон Артёма", "Ноутбук Артёма", "Планшет Артёма"]

    def build(services):
        _codes(services)
        cid = _make(services, "Вася", CLIENT_TG, limit=5)
        if rf:
            services.set_routing_allowed(cid, True)
        ids = [services.add_device(cid, names[i]).device_id for i in range(n)]
        for d in ids:
            _lend(services, d, GUEST_TG, "Артём")
        if more:
            extra = services.add_device(cid, "Приставка").device_id
            services.make_device_friendly(extra)
        if other:
            mid = _make(services, "Маша", THIRD_TG)
            services.make_device_friendly(services.add_device(mid, "Телефон Маши").device_id)
        if upgrade:
            services.create_client("Артём", 3, "year", 0)
        if drop:
            for d in ids:
                services.remove_device(d)
        if owner_after:
            owner_after(services, cid)
        return GUEST_TG, "Артём"
    return build


def _guest_code_wait(services):
    """Вася с одним устройством и выписанным приглашением FTEST0000002."""
    cid = _make(services, "Вася", CLIENT_TG)
    services.make_device_friendly(services.add_device(cid, "Телефон Артёма").device_id)


# ── сокращения колбэков ──────────────────────────────────────────────────────

MAIN = Menu(action="main")
DEVICES = Menu(action="devices")
INFO = Menu(action="info")


def dev(action, i=1):
    return DeviceCB(action=action, device_id=i)


def fr(action, i=0):
    return FriendCB(action=action, device_id=i)


def rt(action, ref=2, **kw):
    return RoutingCB(action=action, ref=ref, **kw)


ONE = vasya(["Телефон"])
TWO = vasya(["Телефон", "Ноутбук"], limit=5)
FULL = vasya(["Телефон", "Ноутбук"], limit=2)
MANY = vasya([f"Устройство {i}" for i in range(1, 13)], limit=0)
MIXED = vasya(["Телефон", "Ноутбук", "Планшет", "Часы"], limit=5, lend=(3,), pending=(4,), holds=True)
CAPPED = vasya(["Телефон"], traffic=50 * G)
RF = vasya(["Телефон", "Ноутбук"], limit=5, rf=True)
RF_SITES = vasya(["Телефон"], rf=True, domains="sber.ru\nya.ru\ngosuslugi.ru")

DOMAINS = sorted(["sber.ru", "ya.ru", "gosuslugi.ru"])


# ── клиент ───────────────────────────────────────────────────────────────────

CLIENT_SHOTS = [
    # главная: /start в разных состояниях профиля
    Shot("cl.main", role="client", start="", data=owner, title="главная по /start"),
    Shot("cl.main.one", role="client", start="", data=ONE, title="главная: одно устройство"),
    Shot("cl.main.many", role="client", start="", data=TWO, title="главная: несколько устройств, есть место"),
    Shot("cl.main.full", role="client", start="", data=FULL, title="главная: лимит устройств исчерпан"),
    Shot("cl.main.unlimited", role="client", start="", data=vasya(["Телефон"], limit=0),
         title="главная: устройств без лимита"),
    Shot("cl.main.traffic", role="client", start="",
         data=vasya(["Телефон", "Ноутбук"], traffic=50 * G, usage=((1, 30 * G, 5 * G), (2, 4 * G, G))),
         title="главная: лимит трафика и потребление"),
    Shot("cl.main.lent_held", role="client", start="", data=MIXED,
         title="главная: переданное, приглашение и чужое в держании"),
    Shot("cl.main.rf", role="client", start="", data=RF, title="главная: РФ-доступ выдан и включён"),
    Shot("cl.main.rf_off", role="client", start="", data=vasya(["Телефон"], rf=True, rf_on=False),
         title="главная: РФ-доступ выдан, выключен"),
    Shot("cl.main.rf_down", role="client", start="", data=vasya(["Телефон"], rf=True, rf_down=True),
         title="главная: РФ-доступ не работает"),
    Shot("cl.main.expiring", role="client", start="", data=vasya(["Телефон"], after=_expiring),
         title="главная: подписка истекает"),
    Shot("cl.main.expired", role="client", start="", data=vasya(["Телефон"], after=_expired),
         title="главная: подписка истекла"),
    Shot("cl.main.paused", role="client", start="", data=vasya(["Телефон"], after=_paused),
         title="главная: своя пауза"),
    Shot("cl.main.admin_paused", role="client", start="", data=vasya(["Телефон"], after=_admin_paused),
         title="главная: приостановлена администратором"),
    Shot("cl.main.never", role="client", start="", data=vasya(["Телефон"], period="never"),
         title="главная: бессрочная подписка"),
    Shot("cl.main.back", role="client", press=[DEVICES, MAIN], data=ONE,
         title="«Назад» из устройств — главная на месте"),

    # активация и коды: незнакомец
    Shot("cl.start.cold", role="client", start="", data=stranger(), title="незнакомец: /start без кода"),
    Shot("cl.start.bad_code", role="client", start="CNOSUCHCODE1", data=stranger(),
         title="незнакомец: неверный код"),
    Shot("cl.start.activate", role="client", start="CTEST0000001", data=stranger(_invite_waiting),
         title="незнакомец: активация приглашения"),
    Shot("cl.code.no_arg", role="client", text="/code", data=stranger(), title="незнакомец: /code без кода"),
    Shot("cl.code.activate", role="client", text="/code CTEST0000001", data=stranger(_invite_waiting),
         title="незнакомец: активация командой /code"),
    Shot("cl.help.skip", role="client", start="CTEST0000001", press=[HelpCB(platform="skip")],
         data=stranger(_invite_waiting), title="после активации: «✅ Настрою сам»"),

    # коды у действующего клиента
    Shot("cl.code.already", role="client", start="CTEST0000002",
         data=vasya(after=lambda s, c: s.create_client("Гена", 3, "year", 0)),
         title="код клиента у того, у кого уже есть доступ"),
    Shot("cl.code.own_device", role="client", start="FTEST0000002", data=vasya(["Телефон"], pending=(1,)),
         title="код на своё же устройство"),
    Shot("cl.code.friend", role="client", start="FTEST0000003",
         data=vasya(["Телефон"], after=lambda s, c: s.make_device_friendly(
             s.add_device(_make(s, "Петя", OTHER_TG), "Телефон Пети").device_id)),
         title="код друга: чужое устройство в держание"),
    Shot("cl.code.other_donor", role="client", start="FTEST0000005",
         data=vasya(holds=True, after=lambda s, c: s.make_device_friendly(
             s.add_device(_make(s, "Маша", THIRD_TG), "Телефон Маши").device_id)),
         title="код от второго дарителя — отказ"),
    Shot("cl.code.invalid", role="client", text="/code FNOSUCHCODE1", data=ONE,
         title="/code с неверным кодом друга"),
    Shot("cl.code.client_no_arg", role="client", text="/code", data=ONE, title="/code без кода у клиента"),

    # ссылки /start <payload>
    Shot("cl.link.sub", role="client", start="sub", data=ONE, title="ссылка на подписку"),
    Shot("cl.link.rf", role="client", start="rf", data=RF, title="ссылка на РФ-доступ"),
    Shot("cl.link.rf_hidden", role="client", start="rf", data=ONE, title="ссылка на РФ-доступ без разрешения"),
    Shot("cl.link.dev", role="client", start="dev-1", data=ONE, title="ссылка на устройство"),
    Shot("cl.link.dev_foreign", role="client", start="dev-99", data=ONE, title="ссылка на чужое устройство"),

    # подписка
    Shot("cl.sub", role="client", press=[INFO], data=ONE, title="💳 Подписка: годовая активна"),
    Shot("cl.sub.month", role="client", press=[INFO], data=vasya(["Телефон"], period="month"),
         title="💳 Подписка: ежемесячная"),
    Shot("cl.sub.never", role="client", press=[INFO], data=vasya(["Телефон"], period="never"),
         title="💳 Подписка: бессрочная"),
    Shot("cl.sub.rf", role="client", press=[INFO], data=RF, title="💳 Подписка: с РФ-доступом"),
    Shot("cl.sub.capped", role="client", press=[INFO], data=CAPPED, title="💳 Подписка: лимит трафика"),
    Shot("cl.sub.expiring", role="client", press=[INFO], data=vasya(["Телефон"], after=_expiring),
         title="💳 Подписка: истекает"),
    Shot("cl.sub.expired", role="client", press=[INFO], data=vasya(["Телефон"], after=_expired),
         title="💳 Подписка: истекла"),
    Shot("cl.sub.paused", role="client", press=[INFO], data=vasya(["Телефон"], after=_paused),
         title="💳 Подписка: своя пауза"),
    Shot("cl.sub.admin_paused", role="client", press=[INFO], data=vasya(["Телефон"], after=_admin_paused),
         title="💳 Подписка: приостановлена администратором"),
    Shot("cl.sub.no_days", role="client", press=[INFO], data=vasya(["Телефон"], after=_no_pause_days),
         title="💳 Подписка: дней паузы нет"),

    # пауза
    Shot("cl.pause.ask", role="client", press=[INFO, PauseCB(action="ask", ref=2)], data=ONE,
         title="⏸️ Пауза: пресеты дней"),
    Shot("cl.pause.ask.few", role="client", press=[PauseCB(action="ask", ref=2)],
         data=vasya(["Телефон"], after=lambda s, c: s.db.set_pause_balance(c, 5)),
         title="⏸️ Пауза: дней меньше пресета"),
    Shot("cl.pause.ask.none", role="client", press=[PauseCB(action="ask", ref=2)],
         data=vasya(["Телефон"], after=_no_pause_days), title="⏸️ Пауза: дней нет — отказ"),
    Shot("cl.pause.ask.mail", role="client", press=[PauseCB(action="ask", ref=2)],
         data=with_mail(ONE), conf=MAIL_CONF, title="⏸️ Пауза: почтовый выход есть — без предупреждения"),
    Shot("cl.pause.pick.mail", role="client", press=[PauseCB(action="ask", ref=2),
                                                      PauseCB(action="pick", ref=2, days=7)],
         data=with_mail(ONE), conf=MAIL_CONF, title="пауза на 7 дней — итог, аварийный код, подписка"),
    Shot("cl.pause.pick", role="client", press=[INFO, PauseCB(action="ask", ref=2),
                                                 PauseCB(action="pick", ref=2, days=7)],
         data=ONE, title="пауза на 7 дней — итог и подписка"),
    Shot("cl.pause.other", role="client", press=[INFO, PauseCB(action="ask", ref=2),
                                                  PauseCB(action="other", ref=2)],
         data=ONE, title="пауза: своё число — приглашение"),
    Shot("cl.pause.other.bad", role="client", press=[INFO, PauseCB(action="ask", ref=2),
                                                      PauseCB(action="other", ref=2)],
         text="100", data=ONE, title="пауза: своё число вне диапазона"),
    Shot("cl.pause.other.done", role="client", press=[INFO, PauseCB(action="ask", ref=2),
                                                       PauseCB(action="other", ref=2)],
         text="10", data=ONE, title="пауза: своё число — итог"),
    Shot("cl.pause.other.cancel", role="client", press=[INFO, PauseCB(action="ask", ref=2),
                                                         PauseCB(action="other", ref=2),
                                                         CancelCB(kind="sub")],
         data=ONE, title="пауза: «✖️ Отмена» ввода — подписка"),
    Shot("cl.pause.cancel", role="client", press=[INFO, PauseCB(action="ask", ref=2),
                                                   PauseCB(action="cancel", ref=2)],
         data=ONE, title="пауза: «⬅️ Отмена» — подписка"),
    Shot("cl.pause.resume", role="client", press=[INFO, PauseCB(action="resume", ref=2)],
         data=vasya(["Телефон"], after=_paused), title="▶️ Снять паузу"),
    Shot("cl.pause.resume.admin", role="client", press=[PauseCB(action="resume", ref=2)],
         data=vasya(["Телефон"], after=_admin_paused), title="снять паузу администратора — отказ"),
    Shot("cl.pause.resume.none", role="client", press=[PauseCB(action="resume", ref=2)], data=ONE,
         title="снять паузу, которой нет — отказ"),

    # отсрочка из уведомления
    Shot("cl.note.expiring", role="client", call=("check_expiry", _expiry_tick),
         data=vasya(["Телефон"], after=_soon), title="уведомление: подписка истекает, кнопка отсрочки"),
    Shot("cl.grace.take", role="client",
         steps=[("call", _expiry_tick, "check_expiry"), ("press", GraceCB(action="take", ref=2))],
         data=vasya(["Телефон"], after=_soon), title="«Продли чуток?» на уведомлении — отсрочка"),
    Shot("cl.note.added", role="client", call=("notify_one", _added_by_admin),
         data=vasya(["Телефон", "Ноутбук"]), title="уведомление: админ добавил устройство"),
    Shot("cl.note.added.link", role="client",
         steps=[("call", _added_by_admin, "notify_one"), ("press", dev("gen_link", 2))],
         data=vasya(["Телефон", "Ноутбук"]), title="🔗 с уведомления о добавленном"),
    Shot("cl.grace.stale", role="client", press=[GraceCB(action="take", ref=2)],
         data=vasya(["Телефон"], period="month"), title="отсрочка неактуальна"),

    # устройства
    Shot("cl.devices.one", role="client", press=[DEVICES], data=ONE, title="📱 Устройства: одно"),
    Shot("cl.devices.full", role="client", press=[DEVICES], data=FULL, title="📱 Устройства: лимит исчерпан"),
    Shot("cl.devices.mixed", role="client", press=[DEVICES], data=MIXED,
         title="📱 Устройства: переданное, приглашение, чужое"),
    Shot("cl.devices.many", role="client", press=[DEVICES], data=MANY, title="📱 Устройства: первая страница"),
    Shot("cl.devices.page2", role="client",
         press=[DEVICES, PageCB(screen="devices", page=1, back=DEVICES.pack())], data=MANY,
         title="📱 Устройства: вторая страница"),

    # карточка устройства
    Shot("cl.dev.own", role="client", press=[DEVICES, dev("open")], data=ONE, title="карточка своего"),
    Shot("cl.dev.own.usage", role="client", press=[dev("open")],
         data=vasya(["Телефон"], traffic=50 * G, usage=((1, 30 * G, 5 * G),)),
         title="карточка своего: потребление и лимит профиля"),
    Shot("cl.dev.own.blocked", role="client", press=[dev("open")], data=vasya(["Телефон"], blocked=(1,)),
         title="карточка своего: заблокировано мной"),
    Shot("cl.dev.pending", role="client", press=[dev("open", 4)], data=MIXED,
         title="карточка своего: приглашение не принято"),
    Shot("cl.dev.lent", role="client", press=[dev("open", 3)], data=MIXED, title="карточка переданного"),
    Shot("cl.dev.held", role="client", press=[dev("open", 5)], data=MIXED, title="карточка чужого в держании"),
    Shot("cl.dev.unmanaged", role="client", press=[dev("open", 2)], data=vasya(["Телефон"], unmanaged=True),
         title="карточка пира без ключа"),
    Shot("cl.dev.connect_menu", role="client", press=[dev("connect_menu")], data=ONE,
         title="«Данные для подключения» прежнего образца — карточка"),
    Shot("cl.dev.foreign", role="client", press=[dev("open", 99)], data=ONE, title="чужое устройство — отказ"),

    # переименование
    Shot("cl.dev.rename", role="client", press=[dev("open"), dev("edit_name")], data=ONE,
         title="✏️ Имя — приглашение"),
    Shot("cl.dev.rename.empty", role="client", press=[dev("open"), dev("edit_name")], text="   ", data=ONE,
         title="✏️ Имя — пустое"),
    Shot("cl.dev.rename.done", role="client", press=[dev("open"), dev("edit_name")], text="Айфон", data=ONE,
         title="✏️ Имя — итог на карточке"),
    Shot("cl.dev.rename.cancel", role="client", press=[dev("open"), dev("edit_name"),
                                                        CancelCB(kind="dev", ref=1)],
         data=ONE, title="✏️ Имя — «✖️ Отмена»"),

    # лимит трафика устройства
    Shot("cl.dev.limit", role="client", press=[dev("open"), dev("edit_traffic")], data=ONE,
         title="✏️ Лимит — пресеты, профиль без лимита"),
    Shot("cl.dev.limit.capped", role="client", press=[dev("open"), dev("edit_traffic")], data=CAPPED,
         title="✏️ Лимит — пресеты не выше лимита профиля"),
    Shot("cl.dev.limit.preset", role="client",
         press=[dev("open"), dev("edit_traffic"), PresetCB(kind="devlimit", ref=1, val=10)], data=ONE,
         title="✏️ Лимит — пресет, итог на карточке"),
    Shot("cl.dev.limit.preset_over", role="client",
         press=[dev("open"), dev("edit_traffic"), PresetCB(kind="devlimit", ref=1, val=100)], data=CAPPED,
         title="✏️ Лимит — пресет выше лимита профиля (кнопка старого экрана)"),
    Shot("cl.dev.limit.other", role="client",
         press=[dev("open"), dev("edit_traffic"), PresetCB(kind="devlimit", ref=1, val=-1)], data=CAPPED,
         title="✏️ Лимит — своё число: приглашение"),
    Shot("cl.dev.limit.other.bad", role="client",
         press=[dev("open"), dev("edit_traffic"), PresetCB(kind="devlimit", ref=1, val=-1)], text="много",
         data=CAPPED, title="✏️ Лимит — не число"),
    Shot("cl.dev.limit.other.over", role="client",
         press=[dev("open"), dev("edit_traffic"), PresetCB(kind="devlimit", ref=1, val=-1)], text="100",
         data=CAPPED, title="✏️ Лимит — выше лимита профиля"),
    Shot("cl.dev.limit.other.done", role="client",
         press=[dev("open"), dev("edit_traffic"), PresetCB(kind="devlimit", ref=1, val=-1)], text="15",
         data=CAPPED, title="✏️ Лимит — своё число, итог на карточке"),

    # передача другу
    Shot("cl.dev.transfer", role="client", press=[dev("open"), dev("transfer")], data=ONE,
         title="👤 Другу — подтверждение"),
    Shot("cl.dev.transfer.yes", role="client", press=[dev("open"), dev("transfer"), dev("transfer_yes")],
         data=ONE, title="👤 Другу — приглашение и завершитель"),
    Shot("cl.dev.reinvite", role="client", press=[dev("open", 4), dev("reinvite", 4)], data=MIXED,
         title="🔁 Приглашение — новый код"),

    # выдача
    Shot("cl.dev.link", role="client", press=[dev("open"), dev("gen_link")], data=ONE, title="выдача: ссылка"),
    Shot("cl.dev.qr", role="client", press=[dev("open"), dev("gen_qr")], data=ONE, title="выдача: QR"),
    Shot("cl.dev.file", role="client", press=[dev("open"), dev("gen_file")], data=ONE, title="выдача: файл"),
    Shot("cl.dev.link.held", role="client", press=[dev("open", 5), dev("gen_link", 5)], data=MIXED,
         title="выдача: чужое в держании"),
    Shot("cl.finisher.menu", role="client", press=[dev("gen_link"), MAIN], data=ONE,
         title="«⬅️ В меню» под выдачей — главная"),
    Shot("cl.gen.one", role="client", press=[Menu(action="gen_link")], data=ONE,
         title="🔗 с главной: одно устройство — сразу"),
    Shot("cl.gen.pick", role="client", press=[Menu(action="gen_link")], data=TWO,
         title="🔗 с главной: выбор устройства"),
    Shot("cl.gen.pick.qr", role="client", press=[Menu(action="gen_qr")], data=TWO, title="🔳 с главной: выбор"),
    Shot("cl.gen.pick.file", role="client", press=[Menu(action="gen_file")], data=TWO,
         title="📄 с главной: выбор"),
    Shot("cl.gen.pick.done", role="client", press=[Menu(action="gen_file"), dev("gen_file", 2)], data=TWO,
         title="📄 выбор → файл"),
    Shot("cl.gen.none", role="client", press=[Menu(action="gen_link")], data=owner,
         title="выдача без устройств — отказ"),
    Shot("cl.gen.unmanaged", role="client", press=[Menu(action="gen_link"), dev("gen_link", 2)],
         data=vasya(["Телефон"], unmanaged=True), title="выдача пира без ключа — «удали»"),
    Shot("cl.gen.unmanaged.delete", role="client",
         press=[Menu(action="gen_link"), dev("gen_link", 2), DelDeviceCB(device_id=2)],
         data=vasya(["Телефон"], unmanaged=True), title="пир без ключа — 🗑 Удалить"),

    # блокировка своей кнопкой
    Shot("cl.dev.block.ask", role="client",
         press=[dev("open"), BlockCB(target="dev", action="menu_block", ref=1, kind="user")], data=ONE,
         title="🛑 Заблокировать — подтверждение"),
    Shot("cl.dev.block.do", role="client",
         press=[dev("open"), BlockCB(target="dev", action="menu_block", ref=1, kind="user"),
                BlockCB(target="dev", action="block", ref=1, kind="user")], data=ONE,
         title="🛑 Заблокировать — итог на карточке"),
    Shot("cl.dev.unblock", role="client",
         press=[dev("open"), BlockCB(target="dev", action="menu_unblock", ref=1, kind="user")],
         data=vasya(["Телефон"], blocked=(1,)), title="разблокировать своё"),
    Shot("cl.dev.block.held", role="client",
         press=[dev("open", 5), BlockCB(target="dev", action="menu_block", ref=5, kind="user")], data=MIXED,
         title="🛑 чужое в держании — подтверждение"),
    Shot("cl.dev.block.lent", role="client",
         press=[BlockCB(target="dev", action="menu_block", ref=3, kind="user")], data=MIXED,
         title="🛑 переданное — не моё управление, отказ"),

    # удаление
    Shot("cl.dev.delete.ask", role="client", press=[dev("open"), DelDeviceCB(device_id=1)], data=TWO,
         title="🗑 Удалить — своё"),
    Shot("cl.dev.delete.ask.only", role="client", press=[dev("open"), DelDeviceCB(device_id=1)], data=ONE,
         title="🗑 Удалить — единственное"),
    Shot("cl.dev.delete.ask.lent", role="client", press=[dev("open", 3), DelDeviceCB(device_id=3)], data=MIXED,
         title="🗑 Удалить — переданное"),
    Shot("cl.dev.delete.ask.held", role="client", press=[dev("open", 5), DelDeviceCB(device_id=5)], data=MIXED,
         title="🗑 Удалить — чужое в держании"),
    Shot("cl.dev.delete.yes", role="client",
         press=[dev("open"), DelDeviceCB(device_id=1), DelDeviceCB(device_id=1, stage="confirm")], data=TWO,
         title="🗑 удалено — итог на списке"),
    Shot("cl.dev.delete.yes.last", role="client",
         press=[dev("open"), DelDeviceCB(device_id=1), DelDeviceCB(device_id=1, stage="confirm")], data=ONE,
         title="🗑 удалено последнее — итог на главной"),
    Shot("cl.dev.delete.yes.lent", role="client",
         press=[dev("open", 3), DelDeviceCB(device_id=3), DelDeviceCB(device_id=3, stage="confirm")],
         data=MIXED, title="🗑 удалено переданное — держателю уведомление"),
    Shot("cl.dev.delete.yes.held", role="client",
         press=[dev("open", 5), DelDeviceCB(device_id=5), DelDeviceCB(device_id=5, stage="confirm")],
         data=MIXED, title="🗑 удалено чужое — владельцу уведомление"),

    # добавление устройства
    Shot("cl.add", role="client", press=[dev("add", 0)], data=ONE, title="➕ Устройство — имя, себе"),
    Shot("cl.add.first", role="client", press=[dev("add", 0)], data=owner, title="➕ Устройство — первое"),
    Shot("cl.add.unlimited", role="client", press=[dev("add", 0)], data=vasya(limit=0),
         title="➕ Устройство — без лимита"),
    Shot("cl.add.from_devices", role="client", press=[DEVICES, dev("add", 1)], data=ONE,
         title="➕ Устройство из списка"),
    Shot("cl.add.friend", role="client", press=[dev("add", 0), dev("add_friend", 0)], data=ONE,
         title="➕ переключатель «для друга»"),
    Shot("cl.add.self", role="client", press=[dev("add", 0), dev("add_friend", 0), dev("add_self", 0)],
         data=ONE, title="➕ переключатель обратно «для меня»"),
    Shot("cl.add.toggle_stale", role="client", press=[dev("add_friend", 0)], data=ONE,
         title="переключатель без диалога — приглашение заново"),
    Shot("cl.add.full", role="client", press=[dev("add", 0)], data=FULL, title="➕ при исчерпанном лимите — отказ"),
    Shot("cl.add.cancel", role="client", press=[dev("add", 0), CancelCB(kind="main")], data=ONE,
         title="➕ «✖️ Отмена» — главная"),
    Shot("cl.add.cancel.devices", role="client", press=[DEVICES, dev("add", 1), CancelCB(kind="devices")],
         data=ONE, title="➕ из списка, «✖️ Отмена» — список"),
    Shot("cl.add.name.empty", role="client", press=[dev("add", 0)], text="   ", data=ONE,
         title="➕ имя пустое"),
    Shot("cl.add.name.done", role="client", press=[dev("add", 0)], text="Ноутбук", data=ONE,
         title="➕ себе — создано, ряд выдачи"),
    Shot("cl.add.friend.name", role="client", press=[dev("add", 0), dev("add_friend", 0)], text="Телефон мамы",
         data=ONE, title="➕ другу — лимит пресетами"),
    Shot("cl.add.friend.name.capped", role="client", press=[dev("add", 0), dev("add_friend", 0)],
         text="Телефон мамы", data=CAPPED, title="➕ другу — пресеты не выше лимита профиля"),
    Shot("cl.add.friend.preset", role="client",
         steps=[("press", dev("add", 0)), ("press", dev("add_friend", 0)), ("text", "Телефон мамы"), ("press", PresetCB(kind="devlimit", val=10))],
         data=ONE, title="➕ другу — пресет: приглашение и завершитель"),
    Shot("cl.add.friend.other", role="client",
         steps=[("press", dev("add", 0)), ("press", dev("add_friend", 0)), ("text", "Телефон мамы"), ("press", PresetCB(kind="devlimit", val=-1))],
         data=CAPPED, title="➕ другу — своё число: приглашение"),
    Shot("cl.add.friend.other.over", role="client",
         steps=[("press", dev("add", 0)), ("press", dev("add_friend", 0)), ("text", "Телефон мамы"), ("press", PresetCB(kind="devlimit", val=-1)), ("text", "100")],
         data=CAPPED, title="➕ другу — своё число выше лимита профиля"),
    Shot("cl.add.friend.other.done", role="client",
         steps=[("press", dev("add", 0)), ("press", dev("add_friend", 0)), ("text", "Телефон мамы"), ("press", PresetCB(kind="devlimit", val=-1)), ("text", "20")],
         data=CAPPED, title="➕ другу — своё число: приглашение и завершитель"),
    # помощь и гайды
    Shot("cl.help", role="client", press=[HelpCB(platform="root")], data=ONE, title="❓ Как подключить"),
    *[Shot(f"cl.guide.{g}.{s}", role="client",
           press=[HelpCB(platform="root"), HelpCB(platform=g)] if s == 0
           else [GuideCB(guide=g, step=s)],
           data=ONE, photo=(g == "apple" and s in (2, 3, 4)), title=f"гайд {g}, шаг {s + 1}")
      for g, n in (("apple", 6), ("android", 2), ("windows", 2), ("mac", 2)) for s in range(n)],
    Shot("cl.guide.apple.back", role="client", press=[GuideCB(guide="apple", step=0)], data=ONE, photo=True,
         title="гайд apple: «Назад» со скриншота на текст"),
    Shot("cl.guide.connect.0", role="client", press=[GuideCB(guide="connect", step=0)], data=TWO,
         title="подключение, шаг 1: устройства"),
    Shot("cl.guide.connect.0.none", role="client", press=[GuideCB(guide="connect", step=0)], data=owner,
         title="подключение, шаг 1: устройств нет"),
    Shot("cl.guide.connect.0.full", role="client", press=[GuideCB(guide="connect", step=0)], data=FULL,
         title="подключение, шаг 1: лимит — без ➕"),
    Shot("cl.guide.connect_apple.0", role="client", press=[GuideCB(guide="connect_apple", step=0)], data=ONE,
         title="подключение Apple, шаг 1"),
    Shot("cl.guide.connect.pick", role="client", press=[GuideCB(guide="connect", step=0), dev("gen_guide")],
         data=ONE, title="подключение: выбрано устройство — способ"),
    Shot("cl.guide.connect.link", role="client",
         press=[GuideCB(guide="connect", step=1, dev=1, kind="link")], data=ONE,
         title="подключение: ссылка и шаг «Подключаемся»"),
    Shot("cl.guide.connect_apple.qr", role="client",
         press=[GuideCB(guide="connect_apple", step=1, dev=1, kind="qr")], data=ONE,
         title="подключение Apple: QR и шаг со шторкой"),
    Shot("cl.guide.connect.back", role="client",
         press=[GuideCB(guide="connect", step=1, dev=1, kind="link"), GuideCB(guide="connect", step=1, dev=1)],
         data=ONE, title="подключение: «Назад» к способу"),
    Shot("cl.guide.add", role="client", press=[GuideCB(guide="connect", step=0), GuideCB(guide="connect", step=-1)],
         data=ONE, title="подключение: ➕ имя"),
    Shot("cl.guide.add.full", role="client", press=[GuideCB(guide="connect", step=-1)], data=FULL,
         title="подключение: ➕ при лимите — отказ"),
    Shot("cl.guide.add.done", role="client",
         press=[GuideCB(guide="connect", step=0), GuideCB(guide="connect", step=-1)], text="Айпад", data=ONE,
         title="подключение: ➕ создано — способ"),
    Shot("cl.guide.add.cancel", role="client",
         press=[GuideCB(guide="connect_apple", step=0), GuideCB(guide="connect_apple", step=-1),
                CancelCB(kind="guide", ref=1)], data=ONE, title="подключение: ➕ «✖️ Отмена» — тот же шаг"),
    *[Shot(f"cl.guide.toggle.{s}", role="client", press=[GuideCB(guide="toggle", step=s)], data=ONE,
           title=f"шторка, шаг {s + 1}") for s in range(2)],

    # РФ-доступ клиента
    Shot("cl.rf.panel", role="client", press=[rt("panel")], data=RF, title="🇷🇺 РФ-доступ: включён"),
    Shot("cl.rf.panel.off", role="client", press=[rt("panel")], data=vasya(["Телефон"], rf=True, rf_on=False),
         title="🇷🇺 РФ-доступ: выключен"),
    Shot("cl.rf.panel.down", role="client", press=[rt("panel")], data=vasya(["Телефон"], rf=True, rf_down=True),
         title="🇷🇺 РФ-доступ: шлюз не пропускает"),
    Shot("cl.rf.panel.mixed", role="client", press=[rt("panel")],
         data=vasya(["Телефон", "Ноутбук", "Планшет"], limit=5, rf=True, lend=(3,), holds=True,
                    domains="sber.ru"),
         title="🇷🇺 РФ-доступ: переданное и чужое, свой адрес"),
    Shot("cl.rf.panel.empty", role="client", press=[rt("panel")], data=vasya(rf=True),
         title="🇷🇺 РФ-доступ: устройств нет"),
    Shot("cl.rf.panel.hidden", role="client", press=[rt("panel")], data=ONE, title="РФ-доступ не выдан — отказ"),
    Shot("cl.rf.dev", role="client", press=[rt("panel"), rt("dev", 2)], data=RF, title="переключить устройство"),
    Shot("cl.rf.all.off", role="client", press=[rt("panel"), rt("all")], data=RF, title="выключить все"),
    Shot("cl.rf.all.on", role="client", press=[rt("panel"), rt("all")],
         data=vasya(["Телефон"], rf=True, rf_on=False), title="включить все"),
    Shot("cl.rf.all.none", role="client", press=[rt("all")], data=vasya(rf=True), title="включить все без устройств"),
    Shot("cl.rf.lent", role="client", press=[rt("lent", 3)],
         data=vasya(["Телефон", "Ноутбук", "Планшет"], limit=5, rf=True, lend=(3,)),
         title="переданное в разделе — всплывашка"),
    Shot("cl.rf.sites.empty", role="client", press=[rt("panel"), rt("sites")], data=RF,
         title="📋 Сайты: пусто"),
    Shot("cl.rf.sites", role="client", press=[rt("panel"), rt("sites")], data=RF_SITES, title="📋 Сайты: список"),
    Shot("cl.rf.sites.many", role="client", press=[rt("sites")],
         data=vasya(["Телефон"], rf=True, domains="\n".join(f"site{i:02d}.ru" for i in range(1, 15))),
         title="📋 Сайты: первая страница"),
    Shot("cl.rf.add", role="client", press=[rt("panel"), rt("add", tag="panel")], data=RF,
         title="➕ Сайт из раздела — приглашение"),
    Shot("cl.rf.add.done", role="client", press=[rt("panel"), rt("add", tag="panel")],
         text="sber.ru\nhttps://www.ya.ru/path\nне домен", data=RF, title="➕ Сайт — итог на разделе"),
    Shot("cl.rf.add.sites", role="client", press=[rt("sites"), rt("add")], text="vk.com", data=RF_SITES,
         title="➕ Сайт из «Сайтов» — итог на «Сайтах»"),
    Shot("cl.rf.add.cancel", role="client", press=[rt("sites"), rt("add"), CancelCB(kind="sites", ref=2)],
         data=RF_SITES, title="➕ Сайт — «✖️ Отмена»"),
    Shot("cl.rf.add.start", role="client",
         steps=[("press", rt("sites")), ("press", rt("add")), ("start", "")], data=RF_SITES,
         title="/start во время ввода сайтов — главная, не адрес"),
    Shot("cl.rf.add.start_sub", role="client",
         steps=[("start", ""), ("press", rt("sites")), ("press", rt("add")), ("start", "sub")], data=RF_SITES,
         title="/start sub во время ввода сайтов — подписка на месте приглашения"),
    Shot("cl.rf.add.start_sub.back", role="client",
         steps=[("start", ""), ("press", rt("sites")), ("press", rt("add")), ("start", "sub"),
                ("press", MAIN)], data=RF_SITES,
         title="«Назад» с подписки, открытой ссылкой во время ввода"),
    Shot("cl.rf.del", role="client",
         press=[rt("sites"), rt("del", idx=0, tag=entry_tag(DOMAINS[0]))], data=RF_SITES,
         title="удалить адрес"),
    Shot("cl.rf.del.stale", role="client", press=[rt("sites"), rt("del", idx=0, tag="zz")], data=RF_SITES,
         title="удалить адрес из изменившегося списка — отказ"),
    Shot("cl.rf.clear", role="client", press=[rt("sites"), rt("clear")], data=RF_SITES,
         title="🗑 Очистить — подтверждение"),
    Shot("cl.rf.clear.yes", role="client", press=[rt("sites"), rt("clear"), rt("clear_yes")], data=RF_SITES,
         title="🗑 Очистить — итог"),

    # уведомления и общие кнопки
    Shot("cl.note.sub", role="client", press=[NoteCB(kind="sub", ref=2)], data=ONE,
         title="кнопка уведомления — подписка новым меню"),
    Shot("cl.hide", role="client", press=[HideCB()], data=ONE, title="«Скрыть» на уведомлении"),
    Shot("cl.reply_cancel", role="client", press=[dev("open"), dev("edit_name")], text="✖️ Отмена", data=ONE,
         title="reply-«✖️ Отмена» прежнего образца"),
]


# ── гость ────────────────────────────────────────────────────────────────────

G1 = guest(1)
G2 = guest(2)

GUEST_SHOTS = [
    Shot("gst.start.activate", role="guest", start="FTEST0000002", data=stranger(_guest_code_wait),
         title="незнакомец: код друга — гость"),
    Shot("gst.main.one", role="guest", start="", data=G1, title="главная гостя: одно устройство"),
    Shot("gst.main.many", role="guest", start="", data=G2, title="главная гостя: несколько"),
    Shot("gst.main.rf", role="guest", start="", data=guest(1, rf=True), title="главная гостя: РФ-доступ"),
    Shot("gst.main.none", role="guest", start="", data=guest(1, drop=True), title="главная гостя: устройств нет"),
    Shot("gst.main.owner_expired", role="guest", start="", data=guest(1, owner_after=_expired),
         title="главная гостя: у владельца подписка истекла"),
    Shot("gst.main.owner_paused", role="guest", start="", data=guest(1, owner_after=_paused),
         title="главная гостя: владелец на паузе"),
    Shot("gst.main.usage", role="guest", start="",
         data=guest(1, owner_after=lambda s, c: s.db.add_traffic_bulk([(1, 3 * G, G)])),
         title="главная гостя: потребление"),
    Shot("gst.refresh", role="guest", press=[fr("list"), fr("refresh")], data=G1,
         title="«Назад» из устройств — главная на месте"),

    # коды и ссылки у гостя
    Shot("gst.code.more", role="guest", start="FTEST0000003", data=guest(1, more=True),
         title="ещё одно устройство от того же владельца"),
    Shot("gst.code.other_donor", role="guest", start="FTEST0000004", data=guest(1, other=True),
         title="код от другого владельца — отказ"),
    Shot("gst.code.upgrade", role="guest", start="CTEST0000003", data=guest(1, upgrade=True),
         title="код клиента — гость становится владельцем"),
    Shot("gst.code.no_arg", role="guest", text="/code", data=G1, title="/code без кода"),
    Shot("gst.code.invalid", role="guest", text="/code FNOSUCHCODE1", data=G1, title="/code с неверным кодом"),
    Shot("gst.link.rf", role="guest", start="rf", data=guest(1, rf=True), title="ссылка на РФ-доступ"),
    Shot("gst.link.dev", role="guest", start="dev-1", data=G1, title="ссылка на устройство"),
    Shot("gst.link.sub", role="guest", start="sub", data=G1, title="ссылка на подписку — у гостя главная"),

    # устройства
    Shot("gst.devices", role="guest", press=[fr("list")], data=G1, title="📱 Устройства гостя"),
    Shot("gst.devices.many", role="guest", press=[fr("list")], data=G2, title="📱 Устройства гостя: несколько"),
    Shot("gst.dev", role="guest", press=[fr("list"), fr("open", 1)], data=G1, title="карточка гостя"),
    Shot("gst.dev.blocked", role="guest", press=[fr("open", 1)],
         data=guest(1, owner_after=lambda s, c: s.block_device_manual(1, DeviceBlock.USER, True)),
         title="карточка гостя: заблокировано"),
    Shot("gst.dev.foreign", role="guest", press=[fr("open", 99)], data=G1, title="чужое устройство — отказ"),
    Shot("gst.dev.connect_menu", role="guest", press=[fr("connect_menu", 1)], data=G1,
         title="кнопка прежнего образца — карточка"),

    # выдача
    Shot("gst.gen.one", role="guest", press=[fr("gen_link")], data=G1, title="🔗 с главной: сразу"),
    Shot("gst.gen.pick", role="guest", press=[fr("gen_link")], data=G2, title="🔗 с главной: выбор"),
    Shot("gst.gen.pick.qr", role="guest", press=[fr("gen_qr")], data=G2, title="🔳 с главной: выбор"),
    Shot("gst.gen.pick.done", role="guest", press=[fr("gen_qr"), fr("gen_qr", 2)], data=G2,
         title="🔳 выбор → QR"),
    Shot("gst.gen.none", role="guest", press=[fr("gen_link")], data=guest(1, drop=True),
         title="выдача без устройств — отказ"),
    Shot("gst.dev.link", role="guest", press=[fr("open", 1), fr("gen_link", 1)], data=G1, title="выдача: ссылка"),
    Shot("gst.dev.file", role="guest", press=[fr("open", 1), fr("gen_file", 1)], data=G1, title="выдача: файл"),
    Shot("gst.finisher.menu", role="guest", press=[fr("gen_link", 1), fr("refresh")], data=G1,
         title="«⬅️ В меню» под выдачей — главная"),

    # блокировка и удаление
    Shot("gst.block.ask", role="guest",
         press=[fr("open", 1), BlockCB(target="dev", action="menu_block", ref=1, kind="user")], data=G1,
         title="🛑 Заблокировать — подтверждение"),
    Shot("gst.block.do", role="guest",
         press=[fr("open", 1), BlockCB(target="dev", action="menu_block", ref=1, kind="user"),
                BlockCB(target="dev", action="block", ref=1, kind="user")], data=G1,
         title="🛑 Заблокировать — итог"),
    Shot("gst.block.cancel", role="guest",
         press=[fr("open", 1), BlockCB(target="dev", action="menu_block", ref=1, kind="user"),
                fr("open", 1)], data=G1, title="🛑 «⬅️ Отмена» — карточка"),
    Shot("gst.unblock", role="guest",
         press=[fr("open", 1), BlockCB(target="dev", action="menu_unblock", ref=1, kind="user")],
         data=guest(1, owner_after=lambda s, c: s.block_device_manual(1, DeviceBlock.USER, True)),
         title="разблокировать"),
    Shot("gst.delete.ask", role="guest", press=[fr("open", 1), DelDeviceCB(device_id=1)], data=G1,
         title="🗑 Удалить — подтверждение"),
    Shot("gst.delete.yes", role="guest",
         press=[fr("open", 1), DelDeviceCB(device_id=1), DelDeviceCB(device_id=1, stage="confirm")], data=G1,
         title="🗑 удалено последнее — главная без устройств"),
    Shot("gst.delete.yes.many", role="guest",
         press=[fr("open", 2), DelDeviceCB(device_id=2), DelDeviceCB(device_id=2, stage="confirm")], data=G2,
         title="🗑 удалено одно из двух"),

    # помощь
    Shot("gst.help", role="guest", press=[fr("help")], data=G1, title="❓ Как подключить"),
    Shot("gst.guide.android.0", role="guest", press=[fr("help"), HelpCB(platform="android")], data=G1,
         title="гайд Android у гостя, шаг 1"),
    Shot("gst.guide.android.1", role="guest", press=[GuideCB(guide="android", step=1)], data=G1,
         title="гайд Android у гостя, последний шаг"),
    Shot("gst.guide.apple.0", role="guest", press=[fr("help"), HelpCB(platform="apple")], data=G1,
         title="гайд Apple у гостя, шаг 1 (скриншот)"),
    Shot("gst.guide.connect.0", role="guest", press=[GuideCB(guide="connect", step=0)], data=G2,
         title="подключение у гостя: устройства без ➕"),
    Shot("gst.guide.connect.pick", role="guest", press=[GuideCB(guide="connect", step=0), dev("gen_guide", 2)],
         data=G2, title="подключение у гостя: способ"),
    Shot("gst.guide.connect.link", role="guest", press=[GuideCB(guide="connect", step=1, dev=1, kind="link")],
         data=G1, title="подключение у гостя: ссылка и «Подключаемся»"),
    # РФ-доступ гостя
    Shot("gst.rf.panel", role="guest", press=[rt("panel", 3)], data=guest(2, rf=True),
         title="🇷🇺 РФ-доступ гостя"),
    Shot("gst.rf.hidden", role="guest", press=[rt("panel", 3)], data=G1, title="РФ-доступ не выдан — отказ"),
    Shot("gst.rf.dev", role="guest", press=[rt("panel", 3), rt("dev", 1)], data=guest(2, rf=True),
         title="переключить устройство"),
    Shot("gst.rf.sites", role="guest", press=[rt("sites", 3)], data=guest(1, rf=True), title="📋 Сайты гостя"),
    Shot("gst.rf.add.done", role="guest", press=[rt("sites", 3), rt("add", 3)], text="sber.ru",
         data=guest(1, rf=True), title="➕ Сайт гостя — итог"),
    Shot("gst.rf.back", role="guest", press=[rt("panel", 3), fr("refresh")], data=guest(1, rf=True),
         title="«Назад» из раздела — главная гостя"),
    Shot("gst.hide", role="guest", press=[HideCB()], data=G1, title="«Скрыть» на уведомлении"),
]

SHOTS = CLIENT_SHOTS + GUEST_SHOTS

LABEL_EXCEPTIONS: dict[str, set[str]] = {}
