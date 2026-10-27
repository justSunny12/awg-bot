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
    expiring|traffic|traffic_local"""
    action: str


class CancelCB(CallbackData, prefix="x"):
    """«✖️ Отмена» под приглашением к вводу: сбросить диалог и вернуть экран,
    с которого пришли (kind, ref — экран реестра bot/screens.py)."""
    kind: str
    ref: int = 0


class PresetCB(CallbackData, prefix="ps"):
    """Кнопка-пресет числа вместо ввода: kind — что выбираем (devlimit —
    лимит трафика устройства; cli_devs / cli_traffic — лимиты профиля;
    new_devs / new_traffic — шаги нового профиля; bc_days — дни продления в
    объявлении), ref — объект (устройство, профиль), val — значение; 0 у
    лимита — без лимита, -1 — «✏️ Другое»."""
    kind: str
    ref: int = 0
    val: int = 0


class NoteCB(CallbackData, prefix="nt"):
    """Кнопка действия на уведомлении (Notification.action): kind — extend
    (продлить профиль ref), unassigned (устройства без профиля), sub
    (подписка пользователя); gwcfg — «📤 Конфигурация» уведомлений до 3.3.1, кнопка
    устаревшая. Нажатие снимает кнопки с уведомления и открывает экран новым
    живым меню."""
    kind: str
    ref: int = 0


class ClientCB(CallbackData, prefix="c"):
    """Действия над пользователем (админ). action: open|edit (подменю «✏️
    Изменить»)|devices|add_device|edit_name|edit_limit|edit_traffic|
    edit_period|extend|extend_exp (из списка истекающих)|resume_pause|delete|delete_yes|
    regen_invite|gen_for|traffic (трафик профиля по устройствам) и mig (переезд профиля) — для
    листания страниц"""
    action: str
    client_id: int = 0


class DeviceCB(CallbackData, prefix="d"):
    """Действия над устройством. action: open|connect_menu|gen_link|gen_qr|
    gen_file|gen_guide|reassign|transfer|transfer_yes|reinvite|
    edit_name|edit_traffic|add|add_self|add_friend (add_self/add_friend —
    переключатель «для кого» на приглашении ввода имени)"""
    action: str
    device_id: int = 0


class PeriodCB(CallbackData, prefix="p"):
    """Выбор длительности периода. kind: day|week|month|year|never; keep_tgl —
    тумблер «сохранить остаток» на том же экране.
    ctx — контекст (create|extend), ref — id пользователя при extend, keep —
    сохранить неистраченный остаток (1 по умолчанию); в колбэке, а не в FSM:
    переживает рестарт."""
    kind: str
    ctx: str = ""
    ref: int = 0
    keep: int = 1


class GraceCB(CallbackData, prefix="gc"):
    """Кнопка отсрочки «Продли чуток?» в уведомлении об истечении.
    action: take (активировать). ref — id пользователя. Закрытие уведомления теперь
    через универсальный HideCB (последняя кнопка на любом уведомлении)."""
    action: str
    ref: int = 0


class HideCB(CallbackData, prefix="hd"):
    """Универсальная кнопка «Скрыть» — последней строкой на ЛЮБОМ проактивном
    уведомлении (Notification из services/scheduler). Нажатие удаляет само
    сообщение целиком (не просто прячет клавиатуру). Без полей — cb.message
    уже знает, какое сообщение удалять; отдельный ref не нужен."""


class UpdateCB(CallbackData, prefix="upd"):
    """Обновления (набор: сервер AWG и шлюзы). action:
      install — диалог «⬆️ Обновить до vX?» (план и порядок);
      go      — начать ход набора к tag;
      cancel  — закрыть диалог;
      retry   — «🔁 Повторить»: набор откатился или догон не прошёл трижды;
      mute    — выключить автоуведомления/стартовую проверку об обновлениях;
      menu    — «В меню» с итога обновления.
    Ручная проверка — не здесь: SetCB(sec="upd", act="do", key="check").
    tag — цель, которую показали человеку (уведомление, раздел): ставится
    только она. Обработчик пересчитывает цель; другая (условие на другую роль
    закрылось, вышел новый релиз) — всплывашка, установки нет. Кнопки прежних
    сообщений без тега — так же: что за ними обещано, уже не узнать.
    Без тега данные кнопки — прежние («upd:menu», «upd:install»): кнопки
    «В меню» и «Не уведомлять» не меняются, а старые разбираются."""
    action: str
    tag: str = ""

    def pack(self) -> str:
        packed = super().pack()
        return packed[:-1] if not self.tag and packed.endswith(self.__separator__) else packed

    @classmethod
    def unpack(cls, value: str):
        if value.count(cls.__separator__) == 1:          # прежний образец: без тега
            value += cls.__separator__
        return super().unpack(value)


class PauseCB(CallbackData, prefix="pz"):
    """Приостановка подписки пользователем. action: ask (экран паузы с пресетами
    дней) | pick (выбран пресет — пауза ставится сразу) | other (ввод своего
    числа) | resume (снять паузу, без подтверждения) | cancel (назад к
    подписке). ref — id пользователя, days — выбранное число дней (pick)."""
    action: str
    ref: int = 0
    days: int = 0


class BlockCB(CallbackData, prefix="bl"):
    """Ручные блокировки (админ/пользователь).
    target: dev | cli — что блокируем (устройство/пользователя).
    action: menu_block | menu_unblock | block | unblock | pause_yes | pause_no |
            cancel.
    kind: silent | notified | user | all — тип бита (или «все» при снятии).
    days: длительность приостановки при админ-блоке пользователя (-1 = без паузы,
          0 = бессрочно, N = срочная). ref — id устройства или пользователя."""
    target: str
    action: str
    ref: int = 0
    kind: str = ""
    days: int = -1


class ReassignCB(CallbackData, prefix="ra"):
    """Привязка устройства без профиля к пользователю. device_id → client_id.
    stage: go — привязать (проверив слот); slot_yes/slot_no — ответ на вопрос
    «добавить слот, раз лимит исчерпан?»."""
    device_id: int
    client_id: int
    stage: str = "go"


class HelpCB(CallbackData, prefix="h"):
    """Меню помощи с настройкой. platform: apple|android|windows|mac|skip|root.
    У гостя те же кнопки: роутер гайдов принимает обе роли."""
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
    """Личные VPN-действия админа над своей записью пользователя.
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
    rt_users)/email/subs/svc/mon/backup/upd/mig/mig_prep/ncl/lk («🔑 Ключи линка»
    в «🔧 Сервис»: act=do, key pick|ask|yes, val — слот)/gtk («🤖 Токен бота»
    в «🔧 Сервис»: act=do, key pick|ask, val — слот);
    act — действие (open/toggle/edit/pick/do); key — dotted-ключ настройки или
    id действия; val — необязательное значение (для pick-выбора enum)."""
    sec: str
    act: str = "open"
    key: str = ""
    val: str = ""


class RoutingCB(CallbackData, prefix="rt"):
    """Условная маршрутизация. action:
      panel   — раздел профиля: переключатели устройств и вход в сайты;
      sites   — экран «📋 Сайты» (ref = client_id);
      devs    — прежний экран устройств (кнопки из старых сообщений → panel);
      dev     — переключить режим ОДНОГО устройства (ref = device_id);
      all     — включить/выключить все устройства профиля (ref = client_id);
      add     — начать ввод доменов (tag="panel" — кнопка раздела: вернуться в него);
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
    tag: str = ""          # метка записи (entry_tag) для del: номер один соседа не спасёт


class BroadcastCB(CallbackData, prefix="bc"):
    """Броадкаст объявления. action:
      pick   — экран адресатов (единственный вход, с главной админа);
      ext    — тумблер «☑️/✅ С продлением подписки» на нём;
      mode   — прежний выбор режима (кнопка старого образца → pick);
      tgl    — отметить/снять один профиль (ref = client_id);
      all    — отметить/снять всех (правило массового выбора);
      next   — дальше: дни продления (пресетами) или ввод текста;
      send   — подтвердить отправку подготовленного;
      cancel — выйти, сбросив состояние.

    Набор отмеченных профилей в callback_data НЕ носим: лимит Telegram — 64
    байта на строку, а профилей может быть сколько угодно. Он живёт в FSM-data,
    сюда приезжает только id того, что переключают.
    """
    action: str
    ref: int = 0


class GwJoinCB(CallbackData, prefix="gwj"):
    """Сообщение подключения шлюза командой: code («✏️ Ввести код»), cancel
    («⬅️ Отменить» / «↩️ Отменить замену»), cancel_yes, cancel_no (диалог
    отмены замены). jid — id сопряжения: кнопка относится только к своей
    заявке."""
    action: str
    slot: int = 0
    jid: str = ""


class GwMarkCB(CallbackData, prefix="gwm"):
    """Назначение машины в слот шлюза у основного бота: pick_list|pick|mark_yes
    (из моих устройств), new_ask|new_yes (новая машина), remove_ask|remove_yes
    («🛑 Не шлюз?» из карточки устройства). slot — номер слота; 0 — новый слот."""
    action: str
    device_id: int = 0
    slot: int = 0


class GwSlotCB(CallbackData, prefix="gws"):
    """Слоты шлюзов. action:
      list — список слотов; card — карточка слота; add — новый слот;
      switch_ask|switch_yes — переложить трафик на слот; ping — замер;
      pref — тумблер «предпочтительный при холодном старте»;
      lan_nets|label — ввод локальных подсетей / подписи (FSM);
      remove_ask|remove_yes — убрать слот;
      failover — тумблер автопереключения;
      lan_ask|lan_yes — «🔀 VPN-транзит» с подтверждением;
      router — рецепт роутера (val — вкладка: mt|ow);
      edit — подэкран «✏️ Изменить»;
      smb_ask|smb_yes (val 1|0) — «SMB в туннеле» с подтверждением."""
    action: str
    slot: int = 0
    val: str = ""


class PageCB(CallbackData, prefix="pg", sep="|"):
    """Листание длинного списка кнопок (правило: не больше десяти кнопок на
    экране). screen — имя списка, ref — его параметр (id профиля, слот),
    page — страница, back — упакованный колбэк, который рисует этот экран:
    хендлер листания запоминает страницу и отдаёт тот же колбэк заново.
    Разделитель «|», потому что в back — свои двоеточия."""
    screen: str
    ref: int = 0
    page: int = 0
    back: str = ""


class GwCB(CallbackData, prefix="gw"):
    """Кнопки агента шлюза (роль gateway). action:
      panel|refresh|health — панель и её обновление, проверки живьём;
      settings и общие разделы notify|email|mon|updates с правками
      tgl|edit|cyc (val — ключ), em_setup|em_check|em_test|em_forget(!),
      upd_toggle — разосланные имена,
      которые переводчик роли (bot/roles.py) отображает на действия общих
      разделов (bot/sections); новые кнопки разделов — общим правилом
      action="раздел/действие", val="ключ|значение";
      restart|reassert|botrestart — показ подтверждения, с «!» — исполнение;
      cfg_ask — первый вопрос очереди («⏳ Решить»), cfg_no|cfg_ok! (val — раздел
      link|uplink|mail|keep|keep_dev|keep_same) — у приглашений «⬅️ Отмена» / «🔑|🔐 Применить»,
      у вопросов «Оставить своё» / «Принять с сервера AWG»;
      lan — экран «🔀 VPN-транзит» (lan_list — прежнее имя); lan_add|lan_ru —
      ввод доменов в личные списки, lan_list — свои списки (домены кнопками,
      с листанием), lan_rm|lan_rm! (val — номер) — убрать домен с подтверждения.
      ssh — раздел «🛡 SSH-доступ», ssh_port|ssh_add|ssh_del (val — номер)|
      ssh_on|ssh_on!|ssh_off|ssh_port_retry|ssh_port_back — его действия."""
    action: str
    val: str = ""


class RsvCB(CallbackData, prefix="rsv"):
    """Раздел «🛟 Резерв»: invite («📤 Пригласить»), accept («📥 Принять
    приглашение»), code («✏️ Ввести код» у пригласившего), cancel («⬅️
    Отменить» приглашение), rf («РФ-доступ через мои шлюзы» в диалоге
    принятия), connect («🛟 Подключиться»), ready (тумблер готовности),
    rfmine (тумблер в карточке), ret (рычаг возврата), emerg («🆘 На крайний
    случай»), show_code, new_code, unpair, unpair_yes. pid — id приглашения:
    кнопка относится только к своему приглашению."""
    action: str
    pid: str = ""


class PeerCB(CallbackData, prefix="pa"):
    """Экран админа второго сервера: refresh («🔄 Обновить»), code («🔑 Ввести
    аварийный код», только в Н4)."""
    action: str


class ListCB(CallbackData, prefix="ls"):
    """Экран «весь список» (bot/lists): kind — вид, ref — слот (0 — без него),
    key — ключ снимка события, new — открыт из уведомления или следа (шапка
    живого вида — «· сейчас»). Листание — PageCB со screen="ls_<вид>" и back
    этим колбэком."""
    kind: str
    ref: int = 0
    key: str = ""
    new: int = 0
