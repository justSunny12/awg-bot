"""
callbacks.py — типизированные callback-data (aiogram CallbackData).

Общие для keyboards/ и handlers/*. Один источник схемы колбэков — меньше
шансов рассинхронить строки между клавиатурой и обработчиком.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Menu(CallbackData, prefix="m"):
    """Навигация по меню. action: main|info|refresh|devices|gen_link|gen_qr|
    gen_file|clients|add_client|add_device_choice|add_device_pick|unassigned|
    expiring|traffic"""
    action: str


class ClientCB(CallbackData, prefix="c"):
    """Действия над клиентом (админ). action: open|devices|add_device|
    edit_name|edit_limit|edit_traffic|edit_period|extend|resume_pause|delete|
    regen_invite|gen_for"""
    action: str
    client_id: int = 0


class DeviceCB(CallbackData, prefix="d"):
    """Действия над устройством. action: open|connect_menu|gen_link|gen_qr|
    gen_file|gen_guide|reassign|transfer|transfer_yes|reinvite|
    edit_name|edit_traffic|add|add_self|add_friend"""
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


class UpdateCB(CallbackData, prefix="upd"):
    """Обновления бота. action:
      install — скачать следующую версию, сверить sha256 и применить;
      mute    — выключить автоуведомления/стартовую проверку об обновлениях;
      menu    — «В меню» с финишного сообщения об обновлении.
    Ручная проверка — не здесь: SetCB(sec="upd", act="do", key="check").
    Тег в data не носим: «следующая ступень» детерминирована от установленной
    версии, обработчик пересчитывает next_release() сам (нет протухания)."""
    action: str


class PauseCB(CallbackData, prefix="pz"):
    """Приостановка подписки клиентом. action: ask (показать инфо+выбор дней) |
    pick (выбран пресет дней) | other (ввод своего числа) | confirm (войти в
    паузу) | resume_ask (спросить про досрочный выход) | resume (выйти
    досрочно) | cancel (закрыть диалог). ref — id клиента, days — выбранное
    число дней (для pick/confirm)."""
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


class ConfirmCB(CallbackData, prefix="y"):
    """Да/Нет диалог. action — что подтверждаем, ref — id объекта,
    yes — ответ."""
    action: str
    ref: int = 0
    yes: bool = False


class ReassignCB(CallbackData, prefix="ra"):
    """Привязка устройства без профиля к клиенту. device_id → client_id.
    stage: go — привязать (проверив слот); slot_yes/slot_no — ответ на вопрос
    «добавить слот, раз лимит исчерпан?»."""
    device_id: int
    client_id: int
    stage: str = "go"


class HelpCB(CallbackData, prefix="h"):
    """Меню помощи с настройкой. platform: apple|android|windows|mac|skip|root"""
    platform: str


class DelDeviceCB(CallbackData, prefix="dd"):
    """Подтверждение удаления устройства (усиленное для единственного).
    stage: ask|confirm — вторая ступень для единственного устройства."""
    device_id: int
    stage: str = "ask"


class GuideCB(CallbackData, prefix="g"):
    """Навигация по визарду-гайду. guide: apple|android|windows|mac|connect|
    connect_apple|toggle.
    step — номер шага (с 0). Состояние в callback, не в FSM — переживает рестарт.
    Для шага подключения (connect): dev — id выбранного устройства, kind — способ
    выдачи (link|qr|file); пусто вне этого шага."""
    guide: str
    step: int = 0
    dev: int = 0
    kind: str = ""


class AdminSelfCB(CallbackData, prefix="as"):
    """Личные VPN-действия админа над своей клиентской записью.
    action: add | devices | gen_link | gen_qr | gen_file."""
    action: str


class FriendCB(CallbackData, prefix="fr"):
    """Действия в гостевом меню друга (invited).
    action: gen_link | gen_qr | gen_file | connect_menu | help | refresh | open | list.
    device_id — целевое устройство (мультидружба: у друга их может быть >1)."""
    action: str
    device_id: int = 0


class SetCB(CallbackData, prefix="set"):
    """Экран настроек. sec — раздел: root/notify/srv/fw/rt (+ rt_gw/rt_lists/
    rt_users/rt_bundle)/email/subs/svc/mon/backup/upd/mig/mig_prep/ncl;
    act — действие (open/toggle/edit/pick/do); key — dotted-ключ настройки или
    id действия; val — необязательное значение (для pick-выбора enum)."""
    sec: str
    act: str = "open"
    key: str = ""
    val: str = ""


class RoutingCB(CallbackData, prefix="rt"):
    """Условная маршрутизация (docs/conditional-routing.md). action:
      panel   — открыть раздел клиента (список доменов + вход в устройства);
      devs    — экран устройств профиля с переключателями (ref = client_id);
      dev     — переключить режим ОДНОГО устройства (ref = device_id);
      all     — включить/выключить все устройства профиля (ref = client_id);
      add     — начать ввод доменов;
      del     — удалить домен (idx — позиция в списке, ref = client_id);
      clear   — спросить подтверждение очистки; clear_yes — очистить.
    Админское разрешение профилю — не здесь: SetCB(sec="rt", act="do",
    key="allow") в настройках.

    Домен в callback_data не носим: лимит Telegram — 64 байта на всю строку, а
    имена бывают длиннее. Позиция берётся из того же порядка, что показан
    пользователю (added_at, domain), и на применении перепроверяется по границам.
    """
    action: str
    ref: int = 0
    idx: int = -1


class BroadcastCB(CallbackData, prefix="bc"):
    """Броадкаст объявления. action:
      pick   — открыть выбор режима (единственный вход, с главной админа);
      mode   — режим выбран: ref 0 — простое, 1 — с продлением подписки;
      tgl    — отметить/снять один профиль (ref = client_id);
      all    — отметить/снять всех;
      next   — перейти к вводу текста;
      send   — подтвердить отправку подготовленного;
      cancel — выйти, сбросив состояние.

    Набор отмеченных профилей в callback_data НЕ носим: лимит Telegram — 64
    байта на строку, а профилей может быть сколько угодно. Он живёт в FSM-data,
    сюда приезжает только id того, что переключают.
    """
    action: str
    ref: int = 0


class GwMarkCB(CallbackData, prefix="gwm"):
    """Назначение машины в слот шлюза у основного бота: pick_list|pick|mark_yes
    (из моих устройств), new_ask|new_yes (новая машина), remove_ask|remove_yes
    («🛑 Не шлюз?» из карточки устройства). slot — номер слота; 0 — новый слот
    (docs/gateway-failover.md)."""
    action: str
    device_id: int = 0
    slot: int = 0


class GwSlotCB(CallbackData, prefix="gws"):
    """Слоты шлюзов (docs/gateway-failover.md §6). action:
      list — список слотов; card — карточка слота; add — новый слот;
      switch_ask|switch_yes — переложить трафик на слот; ping — замер;
      pref — тумблер «предпочтительный при холодном старте»;
      home|label — ввод домашних подсетей / подписи (FSM);
      remove_ask|remove_yes — убрать слот; bundle — конфигурация слота;
      failover — тумблер автопереключения;
      lan_ask|lan_yes — «за шлюзом — без VPN» с подтверждением (docs/gateway-lan.md);
      router — экран настройки роутера;
      peer_ask|peer_yes — доступ между подсетями за шлюзами с подтверждением."""
    action: str
    slot: int = 0


class GwCB(CallbackData, prefix="gw"):
    """Кнопки агента шлюза (роль gateway). action:
      panel|refresh|health — панель и её обновление, проверки живьём;
      settings и разделы notify|email|mon|backup|maint|updates;
      tgl|edit|enc|enc_set|bk_ch (val — ключ/вариант) — правки настроек;
      restart|reassert|botrestart — показ подтверждения, с «!» — исполнение;
      backup!, restore!|restore_drop, em_setup|em_check|em_test|em_forget(!);
      apply!|apply_ow!|apply_keep!|drop — принять/отклонить бандл;
      upd_toggle|upd_check|upd_sched (val — вариант расписания)."""
    action: str
    val: str = ""
