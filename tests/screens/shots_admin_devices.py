"""Снимки админа: устройства — свои, чужие, без профиля; выдача и лимиты."""
from __future__ import annotations

from awgbot.bot.callbacks import (AdminSelfCB, BlockCB, BroadcastCB, ClientCB, DelDeviceCB, DeviceCB, Menu,
                                  PageCB, PresetCB, ReassignCB)

from tests.screens.base import Shot
from tests.screens.shots_admin_common import (ADM, ALIEN, BR, GB, IPHONE, KS, LAPTOP, PHONE, TABLET, _base,
                                              _bc_marks, _fake_config, _people, _people_gen, _quiet)
from tests.screens.shots_admin_common_gw import NASPI, _gw_online, _routing, _slots


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


DEVICES = [
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


def _gw_dev_no_slot(services, mp):
    """Устройство-шлюз, для которого слот не находится (двойник шлюза в окне
    переезда: признак шлюза есть, строки слота по нему нет)."""
    who = _slots(1)(services, mp)
    services.db.gateway_by_device = lambda device_id: None
    # трафик несимметричный: у шлюза стрелки развёрнуты (отдача ↑ — это tx)
    services.db.add_traffic_bulk([(NASPI, int(0.83 * GB), int(10.95 * GB))])
    return who


# ── шаг 5: построители клавиатур без снимка ─────────────────────────────────
GW_NO_SLOT = [
    Shot("adm.gw.dev.noslot", role="admin", press=[DeviceCB(action="open", device_id=NASPI)],
         data=_gw_dev_no_slot, title="карточка устройства-шлюза без слота"),
]


# ── шаг 5: устройства списком — ветки из e2e ────────────────────────────────
DEVICES_GW = [
    Shot("adm.devices.gw", role="admin", press=[Menu(action="devices")], data=_gw_online,
         title="мои устройства: шлюз со значком 🛰, без кружка"),
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


async def _ev_slot_taken(services, bot):
    """Пока админ вводил имя, последний слот Ксюши занял кто-то ещё."""
    services.add_device(KS, "Часы")


ADMIN = [
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
