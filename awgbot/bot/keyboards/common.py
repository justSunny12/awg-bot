"""Общие элементы: reply-клавиатура, маркеры состояния, подтверждение, пресеты лимита устройства, листание, да/нет, «Скрыть», блокировки."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from awgbot.core import blocks as _blocks
from awgbot.bot import ui
from awgbot.bot.callbacks import (BlockCB, CancelCB, Menu, PresetCB, HideCB, PageCB)


# ─────────────────────────────────────────────────────────────────────────────
# Reply-клавиатура (глобальные команды у поля ввода): «Меню» и «Отмена».
# Тексты кнонок — точные строки, по ним ловим в приоритетном роутере
# reply_commands. Эмодзи-префикс делает случайное совпадение с вводом
# (имя устройства и т.п.) практически невозможным.
# ─────────────────────────────────────────────────────────────────────────────

BTN_CANCEL = "✖️ Отмена"  # ✖️ Отмена


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
    return ui.rows((BTN_CANCEL, CancelCB(kind=kind, ref=ref)))


def confirm(cancel_cb, do_text: str, do_cb, *, danger: bool = True) -> InlineKeyboardMarkup:
    """Подтверждение: «⬅️ Отмена» первой, действие второй; разрушительное —
    красным (style у кнопки, Bot API 9.x; старые клиенты рисуют обычную)."""
    return ui.rows([("⬅️ Отмена", cancel_cb), (do_text, do_cb, "danger") if danger else (do_text, do_cb)])


def _packed(cb) -> str:
    return cb if isinstance(cb, str) else cb.pack()


def select_all_button(selected: int, total: int, cb) -> InlineKeyboardButton:
    """Массовый выбор: ☑️, пока выбраны не все; ✅, когда все (и когда их
    отметили по одному) — нажатие на ✅ снимает всё."""
    mark = "✅" if total and selected >= total else "☑️"
    return ui.btn(f"{mark} Выбрать все", cb)


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
    presets = [("∞" if g == 0 else f"{g} ГБ", PresetCB(kind="devlimit", ref=ref, val=g))
               for g in device_limit_presets(profile_limit_bytes)]
    return ui.rows(*ui.grid(presets, 3),
                   ("✏️ Другое", PresetCB(kind="devlimit", ref=ref, val=-1)),
                   ("⬅️ Отмена", cancel_cb))


def _tick(on: bool) -> str:
    """Галочка для СПИСКОВ-перечислений: кому разрешено, о чём уведомлять.

    Кружок оставлен переключателям состояния сервиса («функция включена»), а в
    перечислениях он читался как «профиль жив / профиль лежит» — то есть как
    состояние того, что перечислено, а не как отметка выбора. Галочка та же,
    что в списке устройств маршрутизации и в выборе адресатов рассылки.
    """
    return "✅" if on else "☑️"


# ── Листание длинных списков ─────────────────────────────────────────────────
# Правило одно на весь интерфейс: не больше десяти РЯДОВ кнопок на экране.
# Кнопки списка (по одной в ряду) занимают то, что осталось от постоянных
# рядов экрана; не влезло — страницы, и тогда ряд листания (две кнопки) тоже
# входит в десятку. Подписи намеренно словами, а не «◀️ Назад»: та ведёт на
# другой экран. Экраны собирают список через ui.paged.
MAX_ROWS = 10
PREV_LABEL = "◀️ Пред. страница"
NEXT_LABEL = "След. страница ▶️"


def entry_tag(text: str) -> str:
    """Короткая метка записи списка для колбэка: сам текст в 64 байта не
    влезает, а номер один сменит хозяина, стоит списку измениться (двойное
    нажатие, правка из консоли между показом и нажатием)."""
    import hashlib
    return hashlib.sha1(str(text).encode()).hexdigest()[:8]


def page_slice(items, page: int, static: int) -> tuple[list, int, bool, bool]:
    """(кнопки этой страницы, страница после зажима, есть ли назад, есть ли
    вперёд). static — сколько постоянных РЯДОВ на экране, кроме списка и
    листания. Помещается целиком — страницы нет. Элементы — (index, item):
    номер в ПОЛНОМ списке остаётся у колбэков удаления и переключения."""
    items = list(items)
    room = max(1, MAX_ROWS - static)
    if len(items) <= room:
        return list(enumerate(items)), 0, False, False
    per = max(1, room - 1)                      # ряд листания
    pages = (len(items) + per - 1) // per
    page = min(max(int(page or 0), 0), pages - 1)
    start = page * per
    chunk = [(start + i, x) for i, x in enumerate(items[start:start + per])]
    return chunk, page, page > 0, page < pages - 1


def page_nav(screen: str, ref: int, page: int, has_prev: bool, has_next: bool,
             back: str) -> list[InlineKeyboardButton]:
    """Ряд листания: кнопки к соседним страницам (пустой — страниц нет)."""
    out = []
    if has_prev:
        out.append(ui.btn(PREV_LABEL, PageCB(screen=screen, ref=ref, page=page - 1, back=back)))
    if has_next:
        out.append(ui.btn(NEXT_LABEL, PageCB(screen=screen, ref=ref, page=page + 1, back=back)))
    return out


def issuable(devices) -> list:
    """Устройства, которым можно выдать ссылку/QR/файл: все, кроме шлюза —
    его конфиг едет только внутри конфигурации шлюза, сервис такую выдачу
    отвергает, а кнопка обещала бы лишнее."""
    return [d for d in devices if not getattr(d, "is_gateway", False)]


def _btn_suffix(dev) -> str:
    """Суффикс имени устройства для КНОПОК (HTML не рендерится): звёздочка у
    устройств, которые бот не создавал. Единый источник для всех списков."""
    return "" if dev.is_managed else " *"


# ─────────────────────────────────────────────────────────────────────────────
# Да/Нет
# ─────────────────────────────────────────────────────────────────────────────

def to_menu() -> InlineKeyboardMarkup:
    """Одна кнопка «В меню» — завершитель под контентом (admin/client)."""
    return ui.rows(ui.to_menu(Menu(action="main")))


def append_hide_row(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Добавляет «Скрыть» ПОСЛЕДНЕЙ строкой к готовой разметке. Используется
    везде, где у проактивного уведомления есть свои кнопки действия."""
    return InlineKeyboardMarkup(inline_keyboard=[*markup.inline_keyboard, [ui.btn("Скрыть", HideCB())]])


def hide_only() -> InlineKeyboardMarkup:
    """Клавиатура из одной кнопки «Скрыть» — дефолт для проактивных уведомлений
    без собственных кнопок действия (notifier подставляет её автоматически)."""
    return ui.rows(("Скрыть", HideCB()))


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


def block_unblock_reasons(target: str, ref: int, mask: int) -> InlineKeyboardMarkup:
    """Админ снимает блок: перечислить активные РУЧНЫЕ причины + «Снять всё»
    (если больше одной). Если причина ровно одна — этот экран не показываем
    вовсе (см. admin_unblock_menu), снимаем сразу."""
    if target == "dev":
        items = [("silent", _blocks.DeviceBlock.ADMIN_SILENT, "Тихий админ-блок"),
                 ("notified", _blocks.DeviceBlock.ADMIN_NOTIFIED, "Админ-блок"),
                 ("user", _blocks.DeviceBlock.USER, "Блок владельца")]
    else:
        items = [("silent", _blocks.ClientBlock.ADMIN_SILENT, "Тихий админ-блок"),
                 ("notified", _blocks.ClientBlock.ADMIN_NOTIFIED, "Админ-блок"),
                 ("user", _blocks.ClientBlock.USER, "Блок владельца")]
    active = [(kind, lbl) for kind, bit, lbl in items if int(mask) & int(bit)]
    return ui.rows(
        *[(lbl, BlockCB(target=target, action="unblock", ref=ref, kind=kind)) for kind, lbl in active],
        ("Снять всё", BlockCB(target=target, action="unblock", ref=ref, kind="all")) if len(active) > 1 else None,
        ("⬅️ Отмена", BlockCB(target=target, action="cancel", ref=ref)))
