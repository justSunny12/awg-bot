"""roles.py — словарь роли установки: чем основной бот и агент шлюза различаются
в общих экранах.

Роль установки (`config.ROLE`: «client» — основной бот на ВПС, «gateway» —
агент на малине) — не роль человека в чате (admin / client / invited —
`data["role"]`), поэтому модуль и параметр зовутся иначе: `roles.py`,
`BotRole`, в сигнатурах — `br`.

Словарь — данные, не поведение: законченные слова и фразы роли (хост в
падежах, цена перезапуска, что уходит в копию…), какие ключи настроек роль
читает (`keys`) и какие блоки у неё есть (`has`). Общий текст пишется один раз
с подстановкой слов роли — вместо двух функций и `if gateway`. В общих
модулях нет ветвлений по `br.name`: есть `br.has.<блок>`.

Умолчаний у полей нет: забытое во втором экземпляре поле роняет импорт.
Поля, законно пустые у одной роли, перечислены в OPTIONAL.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Keys:
    """Какие ключи настроек читает роль."""
    monitor_minutes: str         # частота опроса монитора
    outage: str                  # порог простоя (у агента — молчание линка)
    outage_scale: int            # множитель хранения: агент хранит секунды, показывает минуты
    outage_loud: str             # «Звук 24/7» для аварии простоя/линка
    temp_alert: str              # порог температуры; "" — у роли его нет (OPTIONAL)


@dataclass(frozen=True)
class Has:
    """Какие блоки общих разделов есть у роли."""
    email_resume: bool           # аварийный выход из паузы в «E-mail»
    migration: bool              # переезд профилей в «Сервисе»


@dataclass(frozen=True)
class BotRole:
    name: str                    # "main" | "gateway" — для логов, не для ветвлений
    host: str                    # сервер / шлюз
    host_gen: str                # сервера / шлюза
    host_loc: str                # сервере / шлюзе
    awg_restart_cost: str        # цена перезапуска AWG, когда роль трафик не несёт
    awg_restart_cost_carrying: str   # то же, когда несёт (у основного — та же фраза)
    bot_restart_cost: str
    svc_about: str               # строка раздела «🔧 Сервис»
    mail_alarms: str             # что считается аварией для писем
    email_purpose: str           # зачем роли почта
    email_forget_tail: str       # что перестанет работать без почты
    backup_contents: str         # что в копии
    backup_keys: str             # почему копия только шифрованная
    mon_outage: str              # «простой AWG дольше» / «линк молчит дольше»
    mon_outage_button: str       # «⏳ Простой» / «⏳ Линк»
    ssh_port_tail: str           # хвост приглашения порта SSH (OPTIONAL)
    settings_root: tuple[str, ...]   # разделы корня настроек по порядку
    subsections: tuple[str, ...]     # вложенные разделы («ncl» — «👥 События»)
    keys: Keys
    has: Has


# Поля, которые у одной из ролей законно пусты — сторож заполненности их пропускает.
OPTIONAL = frozenset({"keys.temp_alert", "ssh_port_tail", "subsections"})


MAIN = BotRole(
    name="main",
    host="сервер", host_gen="сервера", host_loc="сервере",
    awg_restart_cost="Все соединения оборвутся на несколько секунд и поднимутся сами",
    awg_restart_cost_carrying="Все соединения оборвутся на несколько секунд и поднимутся сами",
    bot_restart_cost="Вернётся через несколько секунд; сервер и соединения не трогаются",
    svc_about="Перезапуск AWG рвёт соединения на несколько секунд, перезапуск бота не влияет на пользователей",
    mail_alarms="падение VPN-сервиса, шлюз, перегруз хоста",
    email_purpose="для аварийного выхода из паузы по коду в письме, бэкапов и критичных алертов",
    email_forget_tail="аварийный выход из паузы перестанет работать",
    backup_contents="профили, устройства, подписки, ключи шифрования",
    backup_keys="в базе приватные ключи устройств",
    mon_outage="простой AWG дольше",
    mon_outage_button="⏳ Простой",
    ssh_port_tail="",
    settings_root=("notify", "srv", "fw", "email", "subs", "backup", "mon", "svc", "upd"),
    subsections=("ncl",),
    keys=Keys(monitor_minutes="app.scheduler.monitor_minutes",
              outage="app.monitoring.service_failure_alert_minutes", outage_scale=1,
              outage_loud="app.monitoring.service_failure_alert_loud",
              temp_alert=""),
    has=Has(email_resume=True, migration=True),
)

GATEWAY = BotRole(
    name="gateway",
    host="шлюз", host_gen="шлюза", host_loc="шлюзе",
    awg_restart_cost="Линк опустится и поднимется",
    awg_restart_cost_carrying="Линк опустится и поднимется — РФ-доступ у всех прервётся на секунды",
    bot_restart_cost="Вернётся через несколько секунд; без влияния на пользователей",
    svc_about=("Перезапуск AWG переподнимает линк до сервера AWG — РФ-доступ у всех прервётся на секунды; "
               "перезапуск бота на трафик не влияет"),
    mail_alarms="линк, выход наружу, обвязка, питание, перегрев, перегруз",
    email_purpose=("для бэкапов и критичных алертов, когда Telegram недоступен; настройки почты "
                   "приезжают в конфигурации шлюза"),
    email_forget_tail="бэкапы и аварийные алерты по почте перестанут уходить",
    backup_contents="конфиги линка и туннеля, настройки, свои списки",
    backup_keys="в копии приватные ключи линка и туннеля",
    mon_outage="линк молчит дольше",
    mon_outage_button="⏳ Линк",
    ssh_port_tail=". Проброс порта на роутере (при наличии) поправь сам",
    settings_root=("notify", "email", "ssh", "mon", "backup", "svc", "upd"),
    subsections=(),
    keys=Keys(monitor_minutes="app.gateway.monitor_minutes",
              outage="app.gateway.handshake_max_age", outage_scale=60,
              outage_loud="app.gateway.link_alert_loud",
              temp_alert="app.gateway.temp_alert_c"),
    has=Has(email_resume=False, migration=False),
)


def current() -> BotRole:
    """Словарь роли этой установки — по config.ROLE."""
    from awgbot.core import config
    return GATEWAY if config.ROLE == "gateway" else MAIN


def pick(gateway: bool) -> BotRole:
    """Переходный мостик для кода, где роль ещё приходит флагом (Hooks.gateway,
    restore(gateway=)); уходит вместе с флагами."""
    return GATEWAY if gateway else MAIN
