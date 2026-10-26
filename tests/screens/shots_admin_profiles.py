"""Снимки админа: профили — список, карточка, правки, продление, блок; новый профиль."""
from __future__ import annotations

from awgbot.bot.callbacks import (BlockCB, CancelCB, ClientCB, DeviceCB, Menu, PageCB, PeriodCB, PresetCB,
                                  ReassignCB)

from tests.screens.base import Shot
from tests.screens.shots_admin_common import (ADM, BR, FOREVER, GB, KS, LAPTOP, PHONE, PT, TABLET,
                                              _br_expired, _client, _ks_many, _mig_spread, _online, _people,
                                              _people_gen, _quiet, _two_expiring)


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
    """Две ручные причины админа: невидимая и с уведомлением."""
    from awgbot.core.blocks import ClientBlock
    who = _people(services)
    services.block_client_manual(KS, ClientBlock.ADMIN_SILENT, False)
    services.block_client_manual(KS, ClientBlock.ADMIN_NOTIFIED, False)
    return who


def _br_grace(services):
    """Боря брал отсрочку: её дни вычтутся из следующего периода."""
    who = _people(services)
    services.db.update_client_fields(BR, grace_pending_cut=3 * 86400)
    return who


def _many_clients(services):
    """Двенадцать профилей — список листается."""
    who = _people(services)
    for i, n in enumerate(("Аня", "Вера", "Гоша", "Даша", "Егор", "Женя", "Зоя", "Иван", "Катя"), 1):
        _client(services, n, 2100 + i)
    return who


PROFILES = [
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
    Shot("adm.cl.limit.notify_off", role="admin",
         press=[ClientCB(action="edit_limit", client_id=KS), ClientCB(action="ntf_cli_devs", client_id=KS),
                PresetCB(kind="cli_devs", ref=KS, val=5)],
         data=_people, title="лимит устройств с «☑️ Не уведомлять» — пользователю ничего"),
    Shot("adm.cl.limit.other.done", role="admin", press=[PresetCB(kind="cli_devs", ref=KS, val=-1)], text="7",
         data=_people, title="лимит устройств своим числом"),
    Shot("adm.cl.traffic", role="admin", press=[ClientCB(action="edit_traffic", client_id=KS)], data=_people,
         title="лимит трафика: пресеты"),
    Shot("adm.cl.traffic.preset", role="admin", press=[PresetCB(kind="cli_traffic", ref=KS, val=100)],
         data=_people, title="лимит трафика пресетом — пользователю уведомление с «💳 Подписка»"),
    Shot("adm.cl.traffic.notify_tgl", role="admin",
         press=[ClientCB(action="edit_traffic", client_id=KS), ClientCB(action="ntf_cli_traffic", client_id=KS)],
         data=_people, title="лимит трафика: тумблер уведомления переключён"),
    Shot("adm.cl.period", role="admin", press=[ClientCB(action="edit_period", client_id=KS)], data=_people,
         title="период: дата начала — с «➡️ Не менять» и тумблером уведомления"),
    Shot("adm.cl.period.start", role="admin", press=[ClientCB(action="edit_period", client_id=KS)], text="-",
         data=_people, title="период: дата окончания (начало — текстом «-»)"),
    Shot("adm.cl.period.keep", role="admin",
         press=[ClientCB(action="edit_period", client_id=KS), ClientCB(action="period_keep", client_id=KS)],
         data=_people, title="период: «➡️ Не менять» начало — окончание на месте, с «∞ Бессрочно»"),
    Shot("adm.cl.period.notify_off", role="admin",
         press=[ClientCB(action="edit_period", client_id=KS), ClientCB(action="ntf_period", client_id=KS)],
         data=_people, title="период: тумблер «☑️ Не уведомлять» на приглашении"),
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
         data=_ks_paused, title="снять паузу: подтверждение с тумблером уведомления"),
    Shot("adm.cl.resume.yes", role="admin",
         press=[ClientCB(action="resume_pause", client_id=KS), ClientCB(action="resume_pause_yes", client_id=KS)],
         data=_ks_paused, title="пауза снята — след, карточка, пользователю уведомление"),
    Shot("adm.cl.regen", role="admin", press=[ClientCB(action="regen_invite", client_id=PT)], data=_people,
         title="новое приглашение"),
    Shot("adm.cl.delete", role="admin", press=[ClientCB(action="delete", client_id=KS)], data=_people,
         title="удалить профиль?"),
    Shot("adm.cl.delete.yes", role="admin", press=[ClientCB(action="delete_yes", client_id=KS)], data=_people,
         title="профиль удалён — список"),
    Shot("adm.cl.block", role="admin", press=[BlockCB(target="cli", action="menu_block", ref=KS)], data=_people,
         title="блок профиля: один экран, тумблеры паузы и уведомления"),
    Shot("adm.cl.block.pause_off", role="admin",
         press=[BlockCB(target="cli", action="menu_block", ref=KS, kind="notified", days=-1)],
         data=_people, title="блок профиля: «⏸️ Пауза: выкл» — строка про паузу пропадает"),
    Shot("adm.cl.block.do", role="admin",
         press=[BlockCB(target="cli", action="block", ref=KS, kind="notified", days=0)], data=_people,
         title="профиль заблокирован с паузой"),
    Shot("adm.cl.unblock.reasons", role="admin", press=[BlockCB(target="cli", action="menu_unblock", ref=KS)],
         data=_ks_blocked_twice, title="разблок профиля: какую причину"),
    Shot("adm.cl.unblock.one", role="admin", press=[BlockCB(target="cli", action="menu_unblock", ref=KS)],
         data=_ks_blocked, title="разблок профиля с одной причиной — сразу, всплывашка парой к блоку"),
    Shot("adm.cl.add_device", role="admin", press=[ClientCB(action="add_device", client_id=KS)], data=_people,
         title="устройство профилю: имя"),
    Shot("adm.cl.add_device.done", role="admin", press=[ClientCB(action="add_device", client_id=KS)],
         text="Планшет", data=_people, title="устройство профилю: итог в карточке"),
    Shot("adm.cl.add_device.full", role="admin", press=[ClientCB(action="add_device", client_id=BR)],
         data=_people, title="устройство профилю: лимит исчерпан"),
    Shot("adm.cl.add_device.slot", role="admin", press=[ClientCB(action="add_device_slot", client_id=BR)],
         data=_people, title="«➕ Слот и добавить»"),
]


NEW = [
    # ── 1.5 новый профиль и объявление ──────────────────────────────────────
    Shot("adm.new", role="admin", press=[Menu(action="add_client")], data=_people, title="новый профиль: имя"),
    Shot("adm.new.devs", role="admin", press=[Menu(action="add_client")], text="Маша", data=_people,
         title="новый профиль: устройств"),
    Shot("adm.new.cancel", role="admin", press=[Menu(action="add_client"), CancelCB(kind="main")],
         data=_people, title="новый профиль: «✖️ Отмена» — главная"),
]


_PERIOD = [("press", ClientCB(action="edit_period", client_id=KS)), ("text", "-")]
_NEWP = [("press", Menu(action="add_client")), ("text", "Маша")]
_NEWP_TRAFFIC = _NEWP + [("press", PresetCB(kind="new_devs", val=3))]
_NEWP_PERIOD = _NEWP_TRAFFIC + [("press", PresetCB(kind="new_traffic", val=100))]


DIALOGS = [
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
]


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


DELETE_PARTIAL = [
    Shot("adm.cl.delete.partial", role="admin", press=[ClientCB(action="delete_yes", client_id=KS)],
         data=_delete_partial, title="удаление профиля: AWG не снял часть устройств — «🔁 Повторить»"),
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


ICONS = [
    Shot("adm.clients.icons", role="admin", press=[Menu(action="clients")], data=_clients_icons,
         title="значки: истёкший онлайн, пауза, блок поверх паузы"),
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


CARDS = [
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


# ── шаг 5: ввод нового профиля, трафик своим числом, кнопки прежнего образца ─
NEW_INPUT = [
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


EXTEND = [
    Shot("adm.cl.extend.keep_on", role="admin",
         press=[ClientCB(action="extend", client_id=KS),
                PeriodCB(kind="keep_tgl", ctx="extend", ref=KS, keep=0),
                PeriodCB(kind="keep_tgl", ctx="extend", ref=KS, keep=1)],
         data=_people, title="продление: тумблер остатка включён обратно"),
    Shot("adm.cl.extend.forever", role="admin", press=[ClientCB(action="extend", client_id=FOREVER)],
         data=_forever, title="продление бессрочной — кнопок срока нет, срок задаётся в «✏️ Изменить»"),
    Shot("adm.cl.extend.never", role="admin", press=[PeriodCB(kind="never", ctx="extend", ref=KS, keep=1)],
         data=_people, title="«∞» из прежнего сообщения — кнопка устарела, подписка не тронута"),
    Shot("adm.cl.extend.notify_off", role="admin",
         press=[ClientCB(action="extend", client_id=KS), ClientCB(action="ntf_extend", client_id=KS)],
         data=_people, title="продление: «☑️ Не уведомлять» под тумблером остатка"),
    Shot("adm.cl.extend.grace_week", role="admin", press=[ClientCB(action="extend", client_id=BR)],
         data=_br_grace_week, title="продление при долге отсрочки в неделю — «Недели» нет"),
    Shot("adm.cl.extend_exp.more", role="admin",
         press=[ClientCB(action="extend_exp", client_id=KS), PeriodCB(kind="month", ctx="extend", ref=KS)],
         data=_two_expiring, title="продлено из «Истекают» — в списке остался другой"),
]


# ── шаг 5: блок профиля без паузы и отмена ──────────────────────────────────
BLOCK = [
    Shot("adm.cl.block.notify_off", role="admin",
         press=[BlockCB(target="cli", action="menu_block", ref=KS, kind="silent", days=0)],
         data=_people, title="блок профиля: «☑️ Не уведомлять»"),
    Shot("adm.cl.block.silent", role="admin",
         press=[BlockCB(target="cli", action="block", ref=KS, kind="silent", days=-1)], data=_people,
         title="профиль заблокирован тихо, без паузы"),
    Shot("adm.cl.block.cancel", role="admin", press=[BlockCB(target="cli", action="cancel", ref=KS)],
         data=_people, title="блок профиля: «✖️ Отмена» — карточка"),
]
