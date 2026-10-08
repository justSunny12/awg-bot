"""Интерфейс агента v2.4.3: раскладка кнопок, снимок vs живьём, перезапуск бота."""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.roles import GATEWAY
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.bot.handlers import hide as hide_h
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from tests.conftest import FakeCallback, FakeMessage, FakeState


class _Svc(GatewayServices):
    def __init__(self, db):
        super().__init__(db)
        self.probes = 0
        self.bot_restarts = 0

    def status(self):
        self.probes += 1
        return GwStatus(link_up=True, handshake_age=5.0, hostname="pi")

    def restart_bot(self):
        self.bot_restarts += 1


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return _Svc(d)


def _labels(markup):
    return [[b.text for b in row] for row in markup.inline_keyboard]


def test_main_menu_layout():
    """Панель — пять кнопок в три ряда (VPN-транзит — только когда включён),
    корень настроек в два столбца без «Обслуживания»: мониторинг, бэкапы и
    перезапуски — прямо в корне. Съехала раскладка — человек ищет раздел,
    которого на старом месте нет."""
    assert _labels(kb.gateway_panel_kb()) == [["🩺 Здоровье", "🔧 Восстановить"],
                                              ["🔄 Обновить", "⚙️ Настройки"]]
    assert _labels(kb.gateway_panel_kb(True)) == [["🩺 Здоровье", "🔧 Восстановить"],
                                                  ["🔀 VPN-транзит"],
                                                  ["🔄 Обновить", "⚙️ Настройки"]]
    assert _labels(kb.gateway_settings_kb()) == [["🔔 Уведомления", "✉️ E-mail"],
                                                 ["🛡 SSH-доступ", "🩺 Мониторинг"],
                                                 ["💾 Бэкапы", "🔧 Сервис"],
                                                 ["⬆️ Обновления", "⬅️ В меню"]]
    assert _labels(kb.gateway_svc_kb()) == [["🔁 Перезапуск AWG", "🔁 Перезапуск бота"], ["⬅️ Назад"]]
    assert _labels(kb.gateway_health_kb()) == [["🔧 Восстановить", "⬅️ В меню"]]
    # из разделов, бывших в «Обслуживании», назад — в корень настроек
    for markup in (kb.gateway_mon_kb(), kb.gateway_backup_kb()):
        assert markup.inline_keyboard[-1][0].callback_data == GwCB(action="settings").pack()


def test_updates_back_leads_to_settings(monkeypatch):
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get", lambda key, default=None: "day")
    rows = kb.gateway_updates_kb(False).inline_keyboard
    assert rows[-1][0].callback_data == GwCB(action="settings").pack()


async def test_start_uses_the_tick_snapshot_and_refresh_probes_live(svc, fake_bot):
    """/start и «В меню» рисуются из снимка тика (ноль проб), «Обновить» —
    живьём и обновляет снимок."""
    svc.snapshot()                                   # тик уже был
    assert svc.probes == 1
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    assert svc.probes == 1, "/start сходил по пробам вместо снимка"
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_refresh(cb, svc, FakeState())
    assert svc.probes == 2
    assert any(t.startswith("🛰 <b>pi</b> ") for kind, t, _ in msg.sent if kind == "edit_text")


async def test_stale_snapshot_falls_back_to_live(svc, fake_bot):
    svc.db.set_state(GatewayServices._SNAPSHOT_KEY,
                     GwStatus(ts="2000-01-01T00:00:00+03:00").to_json())
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    assert svc.probes == 1


async def test_bot_restart_is_confirmed_then_promised_and_kept(svc, fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_confirm(cb, GwCB(action="botrestart"), svc)
    assert svc.bot_restarts == 0
    assert any("Перезапустить бота?" in t for kind, t, _ in msg.sent if kind == "edit_text")
    await gh.gw_bot_restart(cb, svc)
    assert svc.bot_restarts == 1
    assert svc.db.get_state("restart_wait") == f"{cfg.ADMIN_ID}:{msg.message_id}"
    # новый процесс: обещание → отчёт, панель следом
    await gh.restore_panel_after_restart(fake_bot, svc)
    assert svc.db.get_state("restart_wait") == ""
    edited = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edited and texts.BOT_RESTARTED in str(edited[-1])
    assert any(r[0] == "send_message" and str(r[2]).startswith("🛰 <b>pi</b> ") for r in fake_bot.records), \
        "панель после перезапуска не пришла"


async def test_health_screen_is_live(svc, fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_health(cb, svc)
    assert svc.probes == 1
    assert any(t.startswith("🩺 <b>Здоровье pi</b> ") for kind, t, _ in msg.sent if kind == "edit_text")


async def test_hide_button_deletes_the_notification(svc, fake_bot):
    """«Скрыть» на уведомлениях агента: раньше обработчика не было, кнопка
    молчала («not handled»)."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await hide_h.on_hide(cb)
    assert any(r[0] == "delete" for r in fake_bot.records), "уведомление не удалено"


def test_notify_section_layout_cpu_ram_then_disk_temp(monkeypatch):
    """Тихие часы с границами, алерты с четырьмя порогами парами, аварии на
    e-mail — в одном ряду с «Назад»; событий клиентов у шлюза нет."""
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get_bool", lambda key, default=True: True)
    monkeypatch.setattr(settings, "get_int", lambda key, default=0: default)
    rows = _labels(kb.gateway_notify_kb())
    assert rows == [["✅ Тихие часы"], ["С 20:00", "До 07:00"], ["✅ Алерты хоста"],
                    ["CPU 80%", "RAM 80%"], ["Диск 80%", "75 °C"],
                    ["✅ Аварии на e-mail", "⬅️ Назад"]], rows
    assert not any("клиент" in b.lower() for row in rows for b in row), "события клиентов у шлюза лишние"
    # выключенные тихие часы и алерты — без границ и порогов
    monkeypatch.setattr(settings, "get_bool", lambda key, default=True: False)
    assert _labels(kb.gateway_notify_kb()) == [["☑️ Тихие часы"], ["☑️ Алерты хоста"],
                                               ["☑️ Аварии на e-mail", "⬅️ Назад"]]


def test_mon_section_mirrors_main(monkeypatch):
    """Порог молчания линка — в минутах (хранится в секундах): «300 сек» в
    подписи человек переводил в уме, а ввод просил секунды."""
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get_bool", lambda key, default=True: True)
    monkeypatch.setattr(settings, "get_int", lambda key, default=0: default)
    rows = _labels(kb.gateway_mon_kb())
    assert rows == [["⏱ Опрос: 3 мин", "🔢 Замеров: 5"], ["⏳ Линк: 5 мин", "✅ Звук 24/7"],
                    ["⬅️ Назад"]], rows
    assert texts.settings_mon_text(GATEWAY) == (
        "🩺 <b>Мониторинг</b> · опрос раз в 3 мин · алерт после 5 плохих замеров · "
        "линк молчит дольше 5 мин — со звуком круглые сутки")


async def test_edit_flow_writes_value_and_returns_to_section(svc, fake_bot, monkeypatch):
    from awgbot.core import settings
    written = {}
    monkeypatch.setattr(settings, "set_value", lambda key, val: written.__setitem__(key, val))
    monkeypatch.setattr(settings, "get_int", lambda key, default=0: written.get(key, default))
    monkeypatch.setattr(settings, "get_bool", lambda key, default=True: True)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    state = FakeState()
    await gh.gw_edit(cb, GwCB(action="edit", val="app.gateway.monitor_minutes"), svc, state)
    assert any("Частота опроса" in t for kind, t, _ in msg.sent if kind == "edit_text")
    bad = FakeMessage(text="0", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_receive_value(bad, state, svc)
    assert written == {} and any("⚠️ Нужно целое число 1–1440 мин" in t for kind, t, _ in bad.sent)
    good = FakeMessage(text="5", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_receive_value(good, state, svc)
    assert written == {"app.gateway.monitor_minutes": 5}
    # итог ввода — первой строкой раздела, отдельного сообщения нет
    sections = [t for kind, t, _ in good.sent if kind == "answer" and "Мониторинг" in t]
    assert len(sections) == 1 and sections[0].startswith("✅ Частота опроса: ") \
        and sections[0].split("\n", 1)[0].endswith(" → 5 мин"), good.sent


async def test_backup_without_key_explains_instead_of_leaking(svc, fake_bot, monkeypatch):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_backup_now(cb, GatewayServices(svc.db))
    assert any("парольную фразу" in t for kind, t, _ in msg.sent if kind == "answer")
    assert not any(kind == "answer_document" for kind, *_ in msg.sent)


def test_backup_switch_hides_the_rest_in_both_bots(monkeypatch):
    """Автобэкапы выключены — остаются тумблер и «🔐 Шифрование» (фраза нужна
    и для восстановления шифрованных копий): канал, расписание и «сделать
    сейчас» без включённых бэкапов ничего не значат. Раскладка агента — та же,
    что у основного бота."""
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get_int", lambda key, default=0: default)
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(settings, "get_bool", lambda key, default=True: False)
    assert _labels(kb.gateway_backup_kb(False)) == [["☑️ Автобэкапы", "🔐 Шифрование"], ["⬅️ Назад"]]
    assert _labels(kb.settings_backup()) == _labels(kb.gateway_backup_kb(False))
    monkeypatch.setattr(settings, "get_bool", lambda key, default=True: True)
    rows = _labels(kb.gateway_backup_kb(True))
    assert rows == [["✅ Автобэкапы", "🔐 Шифрование"], ["📨 Куда: Telegram", "✏️ 1-е, 12:00"],
                    ["💾 Сделать сейчас"], ["⬅️ Назад"]], rows
    assert rows == _labels(kb.settings_backup(True)), "раскладка агента разошлась с основным ботом"


async def test_gateway_passphrase_flow(svc, fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    state = FakeState()
    await gh.gw_encryption(cb, svc, state)
    assert any(t.startswith("🔐 <b>Шифрование бэкапов") for kind, t, _ in msg.sent if kind == "edit_text")
    await gh.gw_encryption_set(cb, svc, state)
    m = lambda t: FakeMessage(text=t, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_passphrase_first(m("correct horse battery"), state, svc)
    await gh.gw_passphrase_second(m("correct horse battery"), state, svc)
    assert svc.backup_enc_kwargs() == {"passphrase": "correct horse battery"}


async def test_gateway_email_section_and_channel_offer(svc, fake_bot, monkeypatch):
    from awgbot.core import settings
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: bool(store.get(k, d)))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_section(cb, GwCB(action="email"), svc, FakeState())
    txt = [t for kind, t, _ in msg.sent if kind == "edit_text"][-1]
    assert txt.startswith("✉️ <b>E-mail</b> ящик не подключён") and "конфигурации шлюза" in txt, txt
    assert "паузы" not in txt and "Аварийный выход" not in txt, "аварийного выхода у агента нет"
    await gh.gw_backup_channel(cb, GwCB(action="bk_ch", val="email"), svc)
    assert any("Почта не настроена" in t for kind, t, _ in msg.sent if kind == "edit_text")
    await gh.gw_toggle(cb, GwCB(action="tgl", val="notifications.email_fallback"), svc)
    assert "notifications.email_fallback" not in store
    svc.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    svc.backup_set_passphrase("correct horse battery")
    await gh.gw_backup_channel(cb, GwCB(action="bk_ch", val="email"), svc)
    assert store["app.scheduler.backup_channel"] == "email"
    await gh.gw_toggle(cb, GwCB(action="tgl", val="notifications.email_fallback"), svc)
    assert store["notifications.email_fallback"] is True
