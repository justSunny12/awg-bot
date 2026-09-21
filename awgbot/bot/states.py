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


class AdminAddDevice(StatesGroup):
    name = State()                # админ вводит имя устройства для клиента
    traffic = State()             # лимит потребления устройства


class AdminSelfAddDevice(StatesGroup):
    name = State()                # админ добавляет устройство СЕБЕ
    traffic = State()             # лимит потребления устройства


class SettingsInput(StatesGroup):
    """Ввод числового значения настройки. В FSM-data кладём dotted-ключ (key),
    раздел для возврата (sec) и границы валидации (lo/hi)."""
    value = State()


class GatewayToken(StatesGroup):
    """Токен бота-агента для новой машины-шлюза: спрашиваем один раз на слот,
    дальше он живёт в env и уезжает в файл первого применения. В данных —
    gw_slot (слот) и gw_device_id (замена машины)."""
    value = State()


class GatewayHome(StatesGroup):
    """Домашние подсети слота шлюза (концепт «резервный шлюз» 6.8). В данных — gw_slot."""
    value = State()


class GatewayLabel(StatesGroup):
    """Подпись места слота шлюза («дом 1»). В данных — gw_slot."""
    value = State()


class GatewayLanDomain(StatesGroup):
    """Агент шлюза: домены в личные списки локальной сети без VPN
    (концепт «локальная сеть» §3.5). В данных — kind: add | ru | del."""
    value = State()


class MigrationPort(StatesGroup):
    """Порт второго интерфейса перед переездом: единственный параметр, который
    иногда хотят выбрать сами (443 на хосте, где его никто не слушает)."""
    value = State()


class RoutingDomains(StatesGroup):
    """Ввод доменов в личный список условной маршрутизации. Принимаем пачкой —
    человек вставляет списком, а не по одному."""
    value = State()


class Broadcast(StatesGroup):
    """Броадкаст: сначала выбор адресатов, потом ввод текста.

    Выбор — отдельное состояние, а не «просто экран»: набор отмеченных профилей
    живёт в FSM-data между нажатиями (колбэки состояния не носят), и пока он
    набирается, случайное сообщение боту не должно уехать в текст объявления.
    Готовый текст держим там же до подтверждения отправки."""
    targets = State()
    days = State()           # с продлением: на сколько дней (между адресатами и текстом)
    text = State()


class EmailSetup(StatesGroup):
    """Мастер подключения ящика (⚙️ Настройки → ✉️ E-mail)."""
    address = State()             # адрес ящика
    imap_host = State()           # только для незнакомого провайдера
    imap_port = State()
    smtp_host = State()
    smtp_port = State()
    password = State()            # сообщение с паролем удаляется после приёма


class BackupPassphrase(StatesGroup):
    """Парольная фраза шифрования бэкапов — дважды, сообщения удаляются."""
    first = State()
    second = State()
