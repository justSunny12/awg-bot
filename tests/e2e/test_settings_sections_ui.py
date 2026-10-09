"""E2E: разделы настроек основного бота — ввод с итогом первой строкой
раздела, почта (мастер «сервер:порт», проверка и тест-письмо итогом в
разделе, циклы опроса и длины кода), бэкапы (цикл канала с проверками почты и
шифрования, «день и час» одним вводом), обновления (проверка при открытии,
кэш для тумблера и цикла, «никогда» → «месяц» с выключенными уведомлениями —
и при открытии, и при старте бота).

Цена ошибки: итог ввода отдельным сообщением — второе живое меню и мусор в
чате; мастер почты, принявший «imap.example.com:0» за имя сервера, уводит
человека на шаг порта и дальше на отказ входа без объяснения; цикл, не
дошедший до записи, выглядит нажатым и ничего не меняет; «никогда» из старого
конфига — строка «⬆️ Доступна vX» на главной умирает навсегда.

Разделы, приглашения, переспросы, отказы и итоги сверяет эталон
(adm.set.*); здесь — запись значений, шаги диалогов и побочные эффекты.
"""
from __future__ import annotations

import types

import pytest

from awgbot.bot.callbacks import CancelCB, SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _msg(bot, text):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


def _last_edit(nav):
    s = next(x for x in reversed(nav.sent) if x[0] == "edit_text")
    return s[1], s[2]


@pytest.fixture()
def store(monkeypatch):
    """Настройки в памяти: запись видна чтению, как у настоящего кэша."""
    data: dict = {}
    real_get, real_int, real_bool = settings.get, settings.get_int, settings.get_bool
    monkeypatch.setattr(settings, "set_value", lambda k, v: data.__setitem__(k, v) or [k])
    monkeypatch.setattr(settings, "get", lambda k, d=None: data[k] if k in data else real_get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(data[k]) if k in data else real_int(k, d))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(data[k]) if k in data else real_bool(k, d))
    return data


def _mailbox(services):
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)


# ── ввод значения: итог первой строкой раздела ───────────────────────────────

async def test_bad_number_is_asked_again_and_nothing_is_written(services, fake_bot, store):
    """Негодное число не записано, ввод открыт (переспрос — снимок
    adm.set.mon.edit.bad; приглашение с текущим значением и границами —
    adm.set.mon.edit)."""
    st = FakeState()
    await st.update_data(key="app.scheduler.monitor_minutes", sec="mon")
    await st.set_state("SettingsInput:value")
    msg = _msg(fake_bot, "0")
    await sh.receive_value(msg, st, services)
    assert "app.scheduler.monitor_minutes" not in store, "негодное значение записано"
    assert await st.get_state() is not None, "ввод открыт — можно ответить ещё раз"


async def test_input_value_is_written(services, fake_bot, store):
    """Введённое значение записано в настройки. Итог первой строкой раздела,
    одним сообщением с кнопками — снимок adm.set.mon.edit.done."""
    store["app.scheduler.monitor_minutes"] = 3
    st = FakeState()
    await st.update_data(key="app.scheduler.monitor_minutes", sec="mon")
    msg = _msg(fake_bot, "5")
    await sh.receive_value(msg, st, services)
    assert store["app.scheduler.monitor_minutes"] == 5, "введённое значение не записано"


# ── ✉️ E-mail ─────────────────────────────────────────────────────────────────

async def _wizard_to_imap(services, fake_bot, st):
    await st.set_state("EmailSetup:address")
    a = _msg(fake_bot, "box@corp.example")
    await sh.email_address(a, st, services)
    return a


async def test_email_wizard_takes_server_and_port_in_one_line(services, fake_bot, store, monkeypatch):
    """Незнакомый домен — четыре шага: адрес → «IMAP-сервер и порт» → «SMTP-
    сервер и порт» → пароль; проверка входа до сохранения, в проверку уходят
    именно введённые серверы и порты. Тексты шагов — снимки
    adm.set.email.setup.unknown, adm.set.email.smtp, adm.set.email.password."""
    checked = []
    monkeypatch.setattr(services, "email_check", lambda acc=None: (checked.append(acc), (True, "ок"))[1])
    st = FakeState()
    await _wizard_to_imap(services, fake_bot, st)
    m = _msg(fake_bot, "imap.corp.example:993")
    await sh.email_imap_host(m, st, services)
    assert await st.get_state() == "EmailSetup:smtp_host", "шаг отдельного порта не нужен"
    m = _msg(fake_bot, "smtp.corp.example:587")
    await sh.email_smtp_host(m, st, services)
    assert await st.get_state() == "EmailSetup:password"
    pw = _msg(fake_bot, "s3cret")
    await sh.email_password(pw, st, services)
    acc = checked[0]
    assert (acc.imap_host, acc.imap_port, acc.smtp_host, acc.smtp_port) == (
        "imap.corp.example", 993, "smtp.corp.example", 587)
    assert services.email_account().login == "box@corp.example"


@pytest.mark.parametrize("raw", ["imap.corp.example:0", "imap.corp.example:99999", "imap.corp.example:abc",
                                 "imap:993", ":993", "imap.corp .example:993"])
async def test_email_wizard_asks_again_on_a_bad_server_and_port(services, fake_bot, store, raw):
    """Двоеточие есть, а порт или имя негодные — переспрос «сервер и порт», а
    не «имя сервера принято, теперь порт»: иначе «imap.example.com:0» уходит
    в поле имени и дальше в отказ входа без объяснения (текст переспроса —
    снимок adm.set.email.bad_host)."""
    st = FakeState()
    await _wizard_to_imap(services, fake_bot, st)
    m = _msg(fake_bot, raw)
    await sh.email_imap_host(m, st, services)
    assert await st.get_state() == "EmailSetup:imap_host", "переспрос на том же шаге"
    assert "imap_host" not in await st.get_data(), "негодное значение записано в имя сервера"


async def test_email_wizard_still_accepts_a_bare_server_name_with_a_port_step(services, fake_bot, store):
    """Голое имя (старый диалог в памяти) — отдельный шаг порта, как раньше
    (приглашение — снимок adm.set.email.bare_host)."""
    st = FakeState()
    await _wizard_to_imap(services, fake_bot, st)
    m = _msg(fake_bot, "imap.corp.example")
    await sh.email_imap_host(m, st, services)
    assert await st.get_state() == "EmailSetup:imap_port", "шаг порта не открыт"


@pytest.mark.parametrize("key, start, seq", [
    ("email.poll_interval_sec", 60, [300, 900, 60]),
    ("email.resume_code_len", 8, [12, 6, 8]),
])
async def test_email_cycles_go_round_and_write_the_value(services, fake_bot, store, key, start, seq):
    """Опрос 1 → 5 → 15 мин и длина кода 6 → 8 → 12 — по кругу; значение
    пишется в настройки (кнопка и всплывашка с новым значением — снимки
    adm.set.email.cycle.poll, adm.set.email.cycle.code)."""
    _mailbox(services)
    store[key] = start
    for v in seq:
        cb, nav = _acb(fake_bot)
        await sh.cycle(cb, SetCB(sec="email", act="cycle", key=key), services)
        assert store[key] == v, f"цикл не записал {v}"


async def test_a_value_outside_the_cycle_moves_to_the_next_bigger_one(services, fake_bot, store):
    """Значение из конфига руками (опрос 2 мин) — не в начало ряда, а к
    ближайшему большему: нажатие не должно откатывать на «чаще»."""
    _mailbox(services)
    store["email.poll_interval_sec"] = 120
    cb, _ = _acb(fake_bot)
    await sh.cycle(cb, SetCB(sec="email", act="cycle", key="email.poll_interval_sec"), services)
    assert store["email.poll_interval_sec"] == 300
    store["email.poll_interval_sec"] = 3600
    await sh.cycle(cb, SetCB(sec="email", act="cycle", key="email.poll_interval_sec"), services)
    assert store["email.poll_interval_sec"] == 60, "за последним — первое"


# ── 💾 Бэкапы ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["5", "31 12", "0 12", "5 24", "a b", "5 -1", ""])
async def test_backup_day_and_hour_bad_input_is_asked_again(services, fake_bot, store, raw):
    """Одно число, день за 28, час за 23, буквы — переспрос, ничего не
    записано, ввод открыт (текст переспроса — снимок adm.set.backup.when.bad)."""
    st = FakeState()
    await st.set_state("SettingsInput:value")
    await st.update_data(key="backup_when", sec="backup")
    msg = _msg(fake_bot, raw)
    await sh.receive_value(msg, st, services)
    assert "app.scheduler.backup_day" not in store and "app.scheduler.backup_hour" not in store
    assert await st.get_state() is not None, "ввод закрыт после переспроса"


# ── ⬆️ Обновления ────────────────────────────────────────────────────────────

def _release(tag, body="- пункт", title=""):
    return types.SimpleNamespace(tag=tag, body=body, title=title)


@pytest.fixture()
def upd(services, monkeypatch, store):
    """Проверка обновлений без сети: счётчик походов к списку релизов, цель —
    по ключу сцены; тег последней найденной пишет настоящий update_scan."""
    from awgbot.infra import updates
    scene = {"next": _release("v3.3.1"), "fail": False, "calls": 0}

    def _next(max_generation=None):
        scene["calls"] += 1
        if scene["fail"]:
            raise updates.UpdateError("GitHub не ответил")
        return scene["next"]
    monkeypatch.setattr(updates, "next_release", _next)
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.2.0")
    monkeypatch.setattr(services, "update_block_reason", lambda release=None: scene.get("blocked", ""))
    return scene


async def test_nothing_to_update_and_a_failed_check_are_different(services, fake_bot, upd):
    """«Актуальна» и «не проверилось» — разное: сбой сети не стирает прежний
    тег (экраны «актуальна» и «проверка не удалась» — снимки adm.set.upd,
    adm.set.upd.failed)."""
    upd["next"] = None
    await sh._screen("upd", services)
    assert services.update_available_tag() == "", "нечего ставить — тег должен уйти"
    upd["next"] = _release("v3.3.1")
    await sh._screen("upd", services)
    upd["fail"] = True
    await sh._screen("upd", services)
    assert services.update_available_tag() == "v3.3.1", "сбой проверки стёр найденную версию"


async def test_muted_updates_still_refresh_the_home_line(services, fake_bot, upd):
    """«☑️ Уведомлять» глушит уведомление, но не проверку: тег для строки
    «⬆️ Доступна vX» на главной обновляется, уведомлять — нечего."""
    services.mute_updates()
    found = services.update_scan()
    assert found.tag == "v3.3.1" and services.update_available_tag() == "v3.3.1"
    assert services.update_to_notify(found) is None


async def test_never_from_an_old_config_becomes_month_and_mute_at_startup(services, monkeypatch, tmp_path):
    """То же при старте основного бота — до первого открытия раздела: иначе
    до него периодическая проверка спит, и строка «⬆️ Доступна vX» молчит."""
    from awgbot.runtime import main as rt
    from awgbot.runtime import preflight
    (tmp_path / "updates.yaml").write_text('poll_schedule: "never"\npoll_hour: 10\npoll_minute: 0\n',
                                           encoding="utf-8")

    class _Stop(Exception):
        pass

    def _no_bot(*a, **k):
        raise _Stop()
    # вернуть кэш настроек туда, где он был до теста, — во временную копию
    # conf из conftest, а не в config.CONF_DIR: иначе следующие тесты процесса
    # писали бы настройки в conf/ рабочего дерева
    from tests.conftest import restore_settings
    monkeypatch.setattr(config, "validate", lambda: None)
    monkeypatch.setattr(preflight, "check_fatal", lambda: None)
    monkeypatch.setattr(config, "CONF_DIR", tmp_path)
    monkeypatch.setattr(config, "ROLE", "client")
    monkeypatch.setattr(rt, "Database", lambda path: services.db)
    monkeypatch.setattr(rt, "Services", lambda db: services)
    monkeypatch.setattr(rt, "Bot", _no_bot)
    try:
        with pytest.raises(_Stop):
            await rt.main()
        assert settings.get("updates.poll_schedule") == "month"
        assert "never" not in (tmp_path / "updates.yaml").read_text(encoding="utf-8")
        assert services.updates_muted()
    finally:
        restore_settings()


# ── отмена ввода ─────────────────────────────────────────────────────────────

async def test_cancel_under_a_settings_prompt_brings_the_section_back_in_place(services, fake_bot, store):
    """«✖️ Отмена» под приглашением к вводу закрывает диалог: данные ввода
    стёрты, следующее сообщение не уйдёт в настройку; раздел — правкой на
    месте приглашения, без сообщения-следа (раньше уборка при возврате в меню
    сносила живое меню). Вид раздела после отмены — снимок
    adm.set.notify.edit.cancel."""
    from awgbot.bot.handlers import reply_commands as rc
    store["app.scheduler.monitor_minutes"] = 3
    cb, nav = _acb(fake_bot)
    st = FakeState()
    await sh.edit_value(cb, SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes"), st, services)
    cancel = _last_edit(nav)[1].inline_keyboard[0][0].callback_data
    cb2, nav2 = _acb(fake_bot)
    await rc.on_cancel_inline(cb2, CancelCB.unpack(cancel), st, services, role="admin")
    assert await st.get_data() == {}, "данные ввода остались после отмены"
