"""traffic.py — часть пакета notices (см. __init__): уведомления о трафике,
сроках и месячных сбросах."""
from __future__ import annotations

from awgbot.util.textfmt import plural_ru


# Тексты уведомлений (сухие, без слов про оплату — ТЗ 6.5).
_MONTH_CUT_MINUTES = 30 * 24 * 60             # порог, который месяцу не показываем

_TXT_EXPIRED_CLIENT = "🔴 Подписка истекла — доступ приостановлен"
def _left_label(mins_left: int) -> str:
    """«3 дня» / «5 часов» / «меньше часа» — остаток словами для уведомления
    о сроке, когда ближайший порог уже позади."""
    days, hours = -(-mins_left // 1440), -(-mins_left // 60)      # вверх, как remaining_brief
    if mins_left >= 1440:
        return f"{days} {plural_ru(days, 'день', 'дня', 'дней')}"
    if hours >= 1:
        return f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')}"
    return "меньше часа"


_TXT_EXPIRING_CLIENT = "⏳ Подписка истекает через {label}"
# с кнопкой отсрочки: строка объясняет, что делает кнопка под ней
_TXT_EXPIRING_CLIENT_GRACE = "⏳ Подписка истекает через {label} — можно взять отсрочку на {days}"
_TXT_EXPIRING_ADMIN = "⏳ Профиль {name}: подписка истекает через {label}"     # name — ссылка
_TXT_EXPIRED_ADMIN = "🔴 Профиль {name}: подписка истекла — доступ приостановлен"

# ── Тексты месячного сброса лимитов ──────────────────────────────────────────

def _gb_limit(num_bytes: int) -> str:
    """Лимит в целых ГБ для уведомлений: «500 ГБ». 0 = безлимит (в перечислениях
    такие не показываем, но на всякий)."""
    # лениво: сервисы берут эти тексты при своей загрузке — встречный импорт
    # на уровне модуля замкнул бы цикл, если notices.traffic грузится первым
    from awgbot.domain.services.types import BYTES_PER_GB
    return f"{int(round(num_bytes / BYTES_PER_GB))} ГБ"


def _reset_client_text(total_limit: int, device_lines: list[str]) -> str:
    """Профилю: сброс и доступный объём профиля; устройства не перечисляются
    (device_lines решает только, слать ли уведомление). total_limit>0 — строка
    объёма."""
    parts = ["Начался новый месяц — лимиты трафика обнулены 🙂"]
    if total_limit > 0:
        parts.append(f"Доступно на текущий месяц: {_gb_limit(total_limit)}")
    return "\n".join(parts)


def _reset_friend_text(device_lines: list[str]) -> str:
    """Гостю: сброс и ЕГО лимитные устройства списком « · имя — объём» (с вылетом)."""
    return ("Начался новый месяц — лимиты трафика обнулены 🙂\n"
            "Доступно на текущий месяц:\n" + "\n".join(f" · {ln}" for ln in device_lines))


# ── Тексты уведомлений о потреблении (ТЗ 7-8) ────────────────────────────────


# link — имя устройства ссылкой на его карточку (self.dev_link), name — имя
# профиля ссылкой (self.cl_link): строит помощник в base.py

def _dev_warn_text(link: str, pct: int) -> str:
    return f"⚠️ Устройство {link}: израсходовано ~{pct}% месячного лимита"


def _dev_over_text(link: str, until: str) -> str:
    return (f"🔴 Устройство {link}: месячный лимит исчерпан. "
            f"Доступ приостановлен до {until} или пока лимит не увеличат")


def _friend_dev_over_host_text(link: str, until: str) -> str:
    return (f"🔴 Устройство {link} (передано другу): лимит исчерпан, "
            f"доступ приостановлен до {until}")


def _cli_warn_text(pct: int) -> str:
    return f"⚠️ Израсходовано ~{pct}% месячного лимита профиля (суммарный на все устройства)"


def _cli_bonus_text(amount: str, until: str) -> str:
    """amount — «10 ГБ» / «0,5 ГБ» (bonus.gb_text)."""
    return (f"📈 Месячный лимит исчерпан. Добавлено {amount} до конца месяца — "
            f"разово, больше в этом месяце квота не вырастет. Лимит сбросится {until}")


def _cli_bonus_admin_text(name: str, amount: str, note: str = "") -> str:
    """note — откуда число у процентного бонуса («10% лимита»), пусто — без скобок."""
    tail = f" ({note})" if note else ""
    return f"📈 Профиль {name}: лимит исчерпан — выдано {amount}{tail} до конца месяца"


def _cli_limit_off_text(until: str) -> str:
    """Бонус выключен (0): лимит исчерпан — доступ сразу приостановлен."""
    return f"🔴 Месячный лимит исчерпан. Доступ всех устройств приостановлен до {until}"


def _cli_limit_off_admin_text(name: str) -> str:
    return f"🔴 Профиль {name}: лимит исчерпан — доступ приостановлен"


def _cli_over_text(until: str) -> str:
    return f"🔴 Дополнительная квота исчерпана. Доступ всех устройств приостановлен до {until}"


def _cli_over_admin_text(name: str) -> str:
    return f"🔴 Профиль {name}: исчерпан лимит и доп. квота — доступ приостановлен"


def _admin_self_over_text() -> str:
    return "🔴 Твой месячный лимит исчерпан (уведомление, доступ не тронут)"


def _admin_dev_over_text(link: str) -> str:
    return f"🔴 Устройство {link}: месячный лимит исчерпан (уведомление, доступ не тронут)"


