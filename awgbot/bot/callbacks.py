"""
callbacks.py — типизированные callback-data (aiogram CallbackData).

Общие для keyboards.py и handlers/*. Один источник схемы колбэков — меньше
шансов рассинхронить строки между клавиатурой и обработчиком.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Menu(CallbackData, prefix="m"):
    """Навигация по меню. action: main|info|devices|gen_link|gen_qr|gen_file|
    backup|clients|add_client|add_device_choice|add_device_pick|unassigned|
    restart|noop"""
    action: str


class ClientCB(CallbackData, prefix="c"):
    """Действия над клиентом (админ). action: open|edit_name|edit_limit|
    extend|delete|regen_invite|gen_for"""
    action: str
    client_id: int = 0


class DeviceCB(CallbackData, prefix="d"):
    """Действия над устройством. action: open|connect_menu|gen_link|gen_qr|
    gen_file|gen_guide|restore|reassign|transfer|transfer_yes|reinvite|
    edit_name|edit_traffic|fa_link|fa_assign|clear_fa|add|add_self|add_friend"""
    action: str
    device_id: int = 0


class PeriodCB(CallbackData, prefix="p"):
    """Выбор длительности периода. kind: day|week|month|year.
    ctx — контекст (create|extend), ref — id клиента при extend."""
    kind: str
    ctx: str = ""
    ref: int = 0


class GraceCB(CallbackData, prefix="gc"):
    """Кнопка отсрочки «Продли чуток?» в уведомлении об истечении.
    action: take (активировать). ref — id клиента. Закрытие уведомления теперь
    через универсальный HideCB (последняя кнопка на любом уведомлении)."""
    action: str
    ref: int = 0


class HideCB(CallbackData, prefix="hd"):
    """Универсальная кнопка «Скрыть» — последней строкой на ЛЮБОМ проактивном
    уведомлении (Notification из services/scheduler). Нажатие удаляет само
    сообщение целиком (не просто прячет клавиатуру). Без полей — cb.message
    уже знает, какое сообщение удалять; отдельный ref не нужен."""


class PauseCB(CallbackData, prefix="pz"):
    """Приостановка подписки клиентом. action: ask (показать инфо+выбор дней) |
    pick (выбран пресет дней) | other (ввод своего числа) | warn (показать
    предупреждение перед подтверждением) | confirm (войти в паузу) |
    resume (выйти досрочно) | cancel (закрыть диалог). ref — id клиента,
    days — выбранное число дней (для pick/warn/confirm)."""
    action: str
    ref: int = 0
    days: int = 0


class BlockCB(CallbackData, prefix="bl"):
    """Ручные блокировки (админ/клиент).
    target: dev | cli — что блокируем (устройство/клиента).
    action: menu_block | menu_unblock | block | unblock | pause_yes | pause_no |
            cancel.
    kind: silent | notified | user | all — тип бита (или «все» при снятии).
    days: длительность приостановки при админ-блоке клиента (-1 = без паузы,
          0 = бессрочно, N = срочная). ref — id устройства или клиента."""
    target: str
    action: str
    ref: int = 0
    kind: str = ""
    days: int = -1


class FaHintCB(CallbackData, prefix="fah"):
    """Подсветка «назначь устройство полного доступа»: action=choose|ignore."""
    action: str


class AdminLinkGate(CallbackData, prefix="alg"):
    """Гейт выдачи ссылки полного доступа (admin-устройство). method:
    link|qr|file — что отдать после подтверждения; confirm — нажата ли «отдай»."""
    device_id: int
    method: str
    confirm: bool = False


class ConfirmCB(CallbackData, prefix="y"):
    """Да/Нет диалог. action — что подтверждаем, ref — id объекта,
    yes — ответ."""
    action: str
    ref: int = 0
    yes: bool = False


class ReassignCB(CallbackData, prefix="ra"):
    """Привязка app-устройства к клиенту. device_id → client_id.
    stage: go — привязать (проверив слот); slot_yes/slot_no — ответ на вопрос
    «добавить слот, раз лимит исчерпан?»."""
    device_id: int
    client_id: int
    stage: str = "go"


class HelpCB(CallbackData, prefix="h"):
    """Меню помощи с настройкой. platform: apple|android|windows|mac|skip|menu"""
    platform: str


class DelDeviceCB(CallbackData, prefix="dd"):
    """Подтверждение удаления устройства (усиленное для единственного).
    stage: ask|confirm — вторая ступень для единственного устройства."""
    device_id: int
    stage: str = "ask"


class GuideCB(CallbackData, prefix="g"):
    """Навигация по визарду-гайду. guide: apple|android|windows|mac|connect|toggle.
    step — номер шага (с 0). Состояние в callback, не в FSM — переживает рестарт.
    Для шага подключения (connect): dev — id выбранного устройства, kind — способ
    выдачи (link|qr|file); пусто вне этого шага."""
    guide: str
    step: int = 0
    dev: int = 0
    kind: str = ""


class AdminSelfCB(CallbackData, prefix="as"):
    """Личные VPN-действия админа над своей клиентской записью.
    action: add | devices | gen_link | gen_file."""
    action: str


class FriendCB(CallbackData, prefix="fr"):
    """Действия в гостевом меню друга (invited).
    action: gen_link | gen_file | help | refresh | open | list.
    device_id — целевое устройство (мультидружба: у друга их может быть >1)."""
    action: str
    device_id: int = 0


__all__ = ["Menu", "ClientCB", "DeviceCB", "PeriodCB", "ConfirmCB", "AdminLinkGate", "FaHintCB", "ReassignCB",
           "HelpCB", "DelDeviceCB", "GuideCB", "AdminSelfCB", "FriendCB", "GraceCB",
           "BlockCB", "PauseCB"]
