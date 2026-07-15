"""states.py — FSM-состояния диалогов (aiogram)."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddDevice(StatesGroup):
    name = State()                # ждём имя нового устройства
    traffic = State()             # лимит потребления устройства (ГБ, 0=безлимит)


class AddDeviceGuide(StatesGroup):
    name = State()                # имя устройства при добавлении ВНУТРИ гайда
    traffic = State()             # лимит потребления устройства


class CreateClient(StatesGroup):
    name = State()                # имя клиента
    limit = State()               # лимит устройств
    traffic = State()             # тотал-лимит потребления (ГБ, 0=безлимит)
    # период выбирается кнопками (PeriodCB ctx=create) — данные берём из FSM


class EditName(StatesGroup):
    value = State()


class PauseDays(StatesGroup):
    value = State()               # ввод «своего» числа дней приостановки


class EditPeriod(StatesGroup):
    start = State()               # новая дата начала (или 0 — оставить)
    end = State()                 # новая дата окончания (или 0 — оставить)


class EditDeviceName(StatesGroup):
    value = State()


class EditLimit(StatesGroup):
    value = State()


class EditTrafficLimit(StatesGroup):
    value = State()               # новый лимит потребления (ГБ); ref в FSM: клиент/устройство


class BlockPauseDays(StatesGroup):
    days = State()                # админ вводит длительность приостановки (0=бессрочно)


class RestoreDevice(StatesGroup):
    link = State()                # ждём vpn:// строку


class AdminAddDevice(StatesGroup):
    name = State()                # админ вводит имя устройства для клиента
    traffic = State()             # лимит потребления устройства


class AdminSelfAddDevice(StatesGroup):
    name = State()                # админ добавляет устройство СЕБЕ
    traffic = State()             # лимит потребления устройства


__all__ = ["AddDevice", "AddDeviceGuide", "CreateClient", "EditName", "EditDeviceName", "EditPeriod", "PauseDays", "EditLimit",
           "EditTrafficLimit", "BlockPauseDays", "RestoreDevice", "AdminAddDevice", "AdminSelfAddDevice"]


class SettingsInput(StatesGroup):
    """Ввод числового значения настройки. В FSM-data кладём dotted-ключ (key),
    раздел для возврата (sec) и границы валидации (lo/hi)."""
    value = State()


class Broadcast(StatesGroup):
    """Броадкаст: ввод текста объявления. Готовый текст держим в FSM-data до
    подтверждения отправки."""
    text = State()
