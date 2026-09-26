"""Общие элементы: reply-клавиатура, маркеры состояния, выбор периода, да/нет, блокировки."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
    ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import config
from awgbot.core import blocks as _blocks
from awgbot.bot.callbacks import (BlockCB, CancelCB, ClientCB, ConfirmCB, Menu, PeriodCB,
                                  PresetCB, HideCB, PageCB)
from awgbot.bot import texts as _texts


# ─────────────────────────────────────────────────────────────────────────────
# Reply-клавиатура (глобальные команды у поля ввода): «Меню» и «Отмена».
# Тексты кнонок — точные строки, по ним ловим в приоритетном роутере
# reply_commands. Эмодзи-префикс делает случайное совпадение с вводом
# (имя устройства и т.п.) практически невозможным.
# ─────────────────────────────────────────────────────────────────────────────

BTN_CANCEL = "\u2716\ufe0f Отмена"  # ✖️ Отмена


def reply_cancel() -> ReplyKeyboardMarkup:
    """Кнопка «Отмена» у поля ввода — на время текстового ввода. Новые диалоги
    зовут cancel_input (инлайн под приглашением); эта живёт одну версию для
    экранов, которые ещё не переведены."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True, is_persistent=True)


def reply_hide() -> ReplyKeyboardRemove:
    """Убрать reply-клавиатуру (когда открыто главное меню)."""
    return ReplyKeyboardRemove()


# ── Маркеры состояния и суффиксы для кнопок ────────────────────────────────

def _chk(on: bool) -> str:
    """Тумблер «включено/выключено» на кнопке: ✅ / ☑️ у обеих ролей. Кружки
    🟢/🔴 остаются состоянию объектов (онлайн, работает), не настройкам."""
    return "✅" if on else "☑️"


def cancel_input(kind: str, ref: int = 0) -> InlineKeyboardMarkup:
    """«✖️ Отмена» под приглашением к вводу — возврат на экран (kind, ref)
    реестра экранов без сообщения-следа."""
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_CANCEL, callback_data=CancelCB(kind=kind, ref=ref))
    return kb.as_markup()


def confirm(cancel_cb, do_text: str, do_cb, *, danger: bool = True) -> InlineKeyboardMarkup:
    """Подтверждение: «⬅️ Отмена» первой, действие второй; разрушительное —
    красным (style у кнопки, Bot API 9.x; старые клиенты рисуют обычную)."""
    cancel = InlineKeyboardButton(text="⬅️ Отмена", callback_data=_packed(cancel_cb))
    kw = {"style": "danger"} if danger else {}
    do = InlineKeyboardButton(text=do_text, callback_data=_packed(do_cb), **kw)
    return InlineKeyboardMarkup(inline_keyboard=[[cancel, do]])


def _packed(cb) -> str:
    return cb if isinstance(cb, str) else cb.pack()


def select_all_button(selected: int, total: int, cb) -> InlineKeyboardButton:
    """Массовый выбор: ☑️, пока выбраны не все; ✅, когда все (и когда их
    отметили по одному) — нажатие на ✅ снимает всё."""
    mark = "✅" if total and selected >= total else "☑️"
    return InlineKeyboardButton(text=f"{mark} Выбрать все", callback_data=_packed(cb))


# Пресеты лимита трафика устройства, ГБ: не выше лимита профиля; «∞» — только
# когда у профиля лимита нет (0).
DEVICE_LIMIT_PRESETS_GB = (10, 50, 100)


def device_limit_presets(profile_limit_bytes: int) -> list[int]:
    """Значения кнопок в ГБ (0 — без лимита). Профиль 100 ГБ → [10, 50, 100];
    безлимитный → [10, 50, 100, 0]; профиль 5 ГБ, ниже всех пресетов → [5]."""
    limit_gb = int(profile_limit_bytes or 0) // (1024 ** 3)
    if not profile_limit_bytes:
        return [*DEVICE_LIMIT_PRESETS_GB, 0]
    fit = [g for g in DEVICE_LIMIT_PRESETS_GB if g <= limit_gb]
    if limit_gb and limit_gb not in fit:
        fit.append(limit_gb)
    return fit or [max(1, limit_gb)]


def device_limit_kb(ref: int, profile_limit_bytes: int, cancel_cb) -> InlineKeyboardMarkup:
    """Пресеты лимита устройства по три в ряд, затем «✏️ Другое» и «⬅️ Отмена».
    ref — id устройства (карточка) или 0 (новое устройство другу)."""
    kb = InlineKeyboardBuilder()
    vals = device_limit_presets(profile_limit_bytes)
    for g in vals:
        kb.button(text="∞" if g == 0 else f"{g} ГБ",
                  callback_data=PresetCB(kind="devlimit", ref=ref, val=g))
    kb.button(text="✏️ Другое", callback_data=PresetCB(kind="devlimit", ref=ref, val=-1))
    kb.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data=_packed(cancel_cb)))
    n = len(vals)
    kb.adjust(*([3] * (n // 3) + ([n % 3] if n % 3 else [])), 1, 1)
    return kb.as_markup()


def _tick(on: bool) -> str:
    """Галочка для СПИСКОВ-перечислений: кому разрешено, о чём уведомлять.

    Кружок оставлен переключателям состояния сервиса («функция включена»), а в
    перечислениях он читался как «профиль жив / профиль лежит» — то есть как
    состояние того, что перечислено, а не как отметка выбора. Галочка та же,
    что в списке устройств маршрутизации и в выборе адресатов рассылки.
    """
    return "✅" if on else "☑️"


# ── Листание длинных списков ─────────────────────────────────────────────────
# Правило одно на весь интерфейс: не больше десяти кнопок на экране. Кнопки
# списка занимают то, что осталось от постоянных кнопок экрана; не влезло —
# страницы, и тогда две кнопки листания тоже входят в десятку. Подписи
# намеренно словами, а не «◀️ Назад»: та ведёт на другой экран.
MAX_BUTTONS = 10
PREV_LABEL = "◀️ Пред. страница"
NEXT_LABEL = "След. страница ▶️"


def page_slice(items, page: int, static: int) -> tuple[list, int, bool, bool]:
    """(кнопки этой страницы, страница после зажима, есть ли назад, есть ли
    вперёд). static — сколько постоянных кнопок на экране, кроме списка и
    листания. Помещается целиком — страницы нет. Элементы — (index, item):
    номер в ПОЛНОМ списке остаётся у колбэков удаления и переключения."""
    items = list(items)
    room = max(1, MAX_BUTTONS - static)
    if len(items) <= room:
        return list(enumerate(items)), 0, False, False
    per = max(1, room - 2)
    pages = (len(items) + per - 1) // per
    page = min(max(int(page or 0), 0), pages - 1)
    start = page * per
    chunk = [(start + i, x) for i, x in enumerate(items[start:start + per])]
    return chunk, page, page > 0, page < pages - 1


def page_nav(kb: InlineKeyboardBuilder, screen: str, ref: int, page: int,
             has_prev: bool, has_next: bool, back: str) -> int:
    """Кнопки листания рядом; возвращает, сколько их добавлено (для adjust)."""
    n = 0
    if has_prev:
        kb.button(text=PREV_LABEL, callback_data=PageCB(screen=screen, ref=ref, page=page - 1, back=back))
        n += 1
    if has_next:
        kb.button(text=NEXT_LABEL, callback_data=PageCB(screen=screen, ref=ref, page=page + 1, back=back))
        n += 1
    return n


def issuable(devices) -> list:
    """Устройства, которым можно выдать ссылку/QR/файл: все, кроме шлюза —
    его конфиг едет только внутри конфигурации шлюза, сервис такую выдачу
    отвергает, а кнопка обещала бы лишнее."""
    return [d for d in devices if not getattr(d, "is_gateway", False)]


def _btn_suffix(dev) -> str:
    """Суффикс имени устройства для КНОПОК (HTML не рендерится): звёздочка у
    устройств, которые бот не создавал. Единый источник для всех списков."""
    return "" if dev.is_managed else " *"


def _dev_emoji(d) -> str:
    """Иконка типа устройства — та же, что в текстовых списках (см.
    texts.device_emoji): своя копия здесь про шлюз и про непринятый инвайт не
    знала."""
    return _texts.device_emoji(d)


# ─────────────────────────────────────────────────────────────────────────────
# Выбор периода (создание/продление)
# ─────────────────────────────────────────────────────────────────────────────

def period_choices(ctx: str, ref: int = 0, min_days: int = 0,
                   cancel_to: str | None = None) -> InlineKeyboardMarkup:
    """ctx: create | extend. ref: id клиента при продлении.
    min_days: скрыть периоды короче/равные (после вычета отсрочки остался бы ноль
    или минус). «never» не отсекается никогда — вычитать из безлимита нечего.
    Минимальные длительности kind'ов берём консервативно (month=28, year=365),
    чтобы гарантированно не показать период, который может оказаться коротким."""
    _MIN_DAYS = {"day": 1, "week": 7, "month": 28, "year": 365}
    kb = InlineKeyboardBuilder()
    n = 0
    for kind in config.PERIOD_CHOICES:
        if kind != "never" and _MIN_DAYS.get(kind, 0) <= min_days:
            continue
        kb.button(text=config.PERIOD_LABELS[kind],
                  callback_data=PeriodCB(kind=kind, ctx=ctx, ref=ref))
        n += 1
    # Кнопка выхода: при продлении — назад к карточке клиента; при создании —
    # отмена в главное меню. Без неё диалог выбора срока — тупик (был баг).
    if cancel_to:                                   # пришли не из карточки
        kb.button(text="⬅️ Отмена", callback_data=cancel_to)
    elif ctx == "extend" and ref:
        kb.button(text="⬅️ Отмена", callback_data=ClientCB(action="open", client_id=ref))
    else:
        kb.button(text="⬅️ Отмена", callback_data=Menu(action="main"))
    # периоды по 2 в ряд, кнопка отмены — отдельной строкой снизу
    rows = [2] * (n // 2) + ([1] if n % 2 else []) + [1]
    kb.adjust(*rows)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Да/Нет
# ─────────────────────────────────────────────────────────────────────────────

def yes_no(action: str, ref: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да", callback_data=ConfirmCB(action=action, ref=ref, yes=True))
    kb.button(text="Нет", callback_data=ConfirmCB(action=action, ref=ref, yes=False))
    kb.adjust(2)
    return kb.as_markup()


def to_menu() -> InlineKeyboardMarkup:
    """Одна кнопка «В меню» — завершитель под контентом (admin/client)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    return kb.as_markup()


def append_hide_row(kb: InlineKeyboardBuilder) -> InlineKeyboardMarkup:
    """Добавляет «Скрыть» ПОСЛЕДНЕЙ строкой к уже собранной клавиатуре и
    возвращает готовую разметку. Используется везде, где у проактивного
    уведомления есть свои кнопки действия (сейчас — только grace_offer)."""
    kb.row(InlineKeyboardButton(text="Скрыть", callback_data=HideCB().pack()))
    return kb.as_markup()


def hide_only() -> InlineKeyboardMarkup:
    """Клавиатура из одной кнопки «Скрыть» — дефолт для проактивных уведомлений
    без собственных кнопок действия (notifier подставляет её автоматически)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Скрыть", callback_data=HideCB())
    return kb.as_markup()


# ── Ручные блокировки ────────────────────────────────────────────────────────


def _manual_block_button(target: str, ref: int, mask: int, *, for_admin: bool):
    """Кнопка «Заблокировать»/«Разблокировать» для карточки.
    Админ управляет всеми ручными битами → смотрит на всю ручную маску.
    Клиент управляет ТОЛЬКО своим USER-битом → кнопка отражает лишь его: если
    сам заблокировал → «Разблокировать», иначе «Заблокировать». Админские биты
    на его устройстве клиент кнопкой не снимет (и кнопка это не обещает)."""
    if for_admin:
        manual = _blocks.DEVICE_MANUAL if target == "dev" else _blocks.CLIENT_MANUAL
        has_manual = int(mask) & int(manual)
    else:
        user_bit = (_blocks.DeviceBlock.USER if target == "dev"
                    else _blocks.ClientBlock.USER)
        has_manual = int(mask) & int(user_bit)
    if has_manual:
        return ("✅ Разблок", BlockCB(target=target, action="menu_unblock", ref=ref))
    return ("🛑 Блок", BlockCB(target=target, action="menu_block", ref=ref))


def block_pause_choice(client_id: int) -> InlineKeyboardMarkup:
    """Блок клиента: приостановить ли подписку на время блокировки."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⏸ Да, приостановить подписку",
              callback_data=BlockCB(target="cli", action="pause_yes", ref=client_id))
    kb.button(text="▶️ Нет, подписка тикает",
              callback_data=BlockCB(target="cli", action="pause_no", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target="cli", action="cancel", ref=client_id))
    kb.adjust(1)
    return kb.as_markup()


def block_notify_choice(target: str, ref: int, pause_days: int = -1) -> InlineKeyboardMarkup:
    """Админ ставит блок: уведомить пользователя или тихо. pause_days — режим
    приостановки (в отдельном поле days): -1 без паузы, 0 бессрочно, N срочная."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 С уведомлением",
              callback_data=BlockCB(target=target, action="block", ref=ref, kind="notified", days=pause_days))
    kb.button(text="🔕 Тихо (не уведомлять)",
              callback_data=BlockCB(target=target, action="block", ref=ref, kind="silent", days=pause_days))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target=target, action="cancel", ref=ref))
    kb.adjust(1)
    return kb.as_markup()


def block_unblock_reasons(target: str, ref: int, mask: int) -> InlineKeyboardMarkup:
    """Админ снимает блок: перечислить активные РУЧНЫЕ причины + «Снять всё»
    (если больше одной). Если причина ровно одна — этот экран не показываем
    вовсе (см. admin_unblock_menu), снимаем сразу."""
    kb = InlineKeyboardBuilder()
    if target == "dev":
        items = [("silent", _blocks.DeviceBlock.ADMIN_SILENT, "Тихий админ-блок"),
                 ("notified", _blocks.DeviceBlock.ADMIN_NOTIFIED, "Админ-блок"),
                 ("user", _blocks.DeviceBlock.USER, "Блок владельца")]
    else:
        items = [("silent", _blocks.ClientBlock.ADMIN_SILENT, "Тихий админ-блок"),
                 ("notified", _blocks.ClientBlock.ADMIN_NOTIFIED, "Админ-блок"),
                 ("user", _blocks.ClientBlock.USER, "Блок владельца")]
    active = [(kind, lbl) for kind, bit, lbl in items if int(mask) & int(bit)]
    for kind, lbl in active:
        kb.button(text=lbl,
                  callback_data=BlockCB(target=target, action="unblock", ref=ref, kind=kind))
    if len(active) > 1:
        kb.button(text="Снять всё",
                  callback_data=BlockCB(target=target, action="unblock", ref=ref, kind="all"))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target=target, action="cancel", ref=ref))
    kb.adjust(1)
    return kb.as_markup()
