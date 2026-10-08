"""E2E: поведение общих разделов у обеих ролей — один тест на пару, роль
параметром. Нажатие идёт так же, как у живого диспетчера: колбэк роли
разбирается её словарём (sections.resolve — фильтр роутера разделов) и
исполняется общим диспетчером (sections.handle); ввод — общим ядром
диалогов (settingscore.receive_value) с крючками роли.

Пары: канал бэкапа с проверкой ящика и шифрования, неизвестный ключ цикла,
день и час бэкапа одним вводом, проверка обновлений при открытии, тумблер и
расписание без сети, «никогда» → «месяц», порог простоя с множителем роли.

Цена ошибки: раздел, который у одной роли ведёт себя иначе, чем у другой,
— это ровно то, что общий слой должен был убрать: у агента почтовый канал
бэкапа без шифрования отправил бы ключи линка открытым письмом, порог линка
в минутах, записанный в секундный ключ, поднимает «линк мёртв» через 5
секунд тишины, а «никогда» из старого конфига глушит строку «⬆️ Доступна vX»
навсегда.
"""
from __future__ import annotations

import types

import pytest

from awgbot.bot import roles, sections, texts
from awgbot.bot.handlers import settingscore as core
from awgbot.core import config, settings
from awgbot.infra.db import Database
from awgbot.runtime import linkclient
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID


@pytest.fixture(params=["main", "gateway"])
def env(request, monkeypatch, tmp_path):
    """Роль целиком: словарь, сервисы роли, config.ROLE (реестр экранов)."""
    if request.param == "main":
        monkeypatch.setattr(config, "ROLE", "client")
        yield types.SimpleNamespace(br=roles.MAIN, services=request.getfixturevalue("services"))
        return
    from awgbot.domain.gateway import GatewayServices
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(linkclient, "enabled", lambda: False)
    d = Database(str(tmp_path / "gw.db"))
    d.init_schema()
    yield types.SimpleNamespace(br=roles.GATEWAY, services=GatewayServices(d))
    d.close()


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


def _rows(markup):
    return [[b.text for b in r] for r in markup.inline_keyboard]


def _last_edit(nav):
    s = next(x for x in reversed(nav.sent) if x[0] == "edit_text")
    return s[1], s[2]


async def _press(env, bot, data, state=None):
    """Нажатие колбэка роли через фильтр и диспетчер общих разделов."""
    data = data if isinstance(data, str) else data.pack()
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    cb.data = data
    packed = sections.resolve(env.br, data)
    assert packed is not None, f"{env.br.name}: {data} не узнан роутером разделов — ушёл бы в устаревшую"
    await sections.handle(cb, packed, env.services, state if state is not None else FakeState(), env.br)
    return cb, nav


async def _type(env, bot, text, state):
    msg = FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)
    await core.receive_value(msg, state, env.services, sections.hooks_for(env.br), "root")
    return msg


def _button(markup, label):
    return next(b for r in markup.inline_keyboard for b in r if b.text == label)


# ── 💾 бэкапы ────────────────────────────────────────────────────────────────

async def test_backup_channel_cycle_checks_mailbox_and_encryption(env, fake_bot, store):
    """«📨 Куда» — цикл Telegram ↔ E-mail: без ящика — экран «почта не
    настроена» с возвратом в «Бэкапы», без шифрования — alert со словами
    роли (что в копии); прошло — записано, раздел перерисован, всплывашка с
    новым значением; обратно — Telegram."""
    key = "app.scheduler.backup_channel"
    store.update({"app.scheduler.backup_enabled": True, key: "telegram"})
    _, markup = await sections.screen("backup", env.br, env.services)
    cyc = _button(markup, "📨 Куда: Telegram").callback_data
    assert sections.resolve(env.br, cyc) == ("backup", "cycle", key, "")
    cb, nav = await _press(env, fake_bot, cyc)
    text, offer = _last_edit(nav)
    assert store[key] == "telegram" and text == texts.EMAIL_NOT_CONFIGURED
    assert offer.inline_keyboard[0][0].callback_data == env.br.cb.pack("backup").pack(), "отказ увёл не в «Бэкапы»"
    env.services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    cb, nav = await _press(env, fake_bot, cyc)
    assert store[key] == "telegram" and cb.answers == [(texts.backup_needs_encryption(env.br), True)], cb.answers
    env.services.backup_set_passphrase("correct horse battery")
    cb, nav = await _press(env, fake_bot, cyc)
    assert store[key] == "email" and cb.answers[-1] == ("Куда: E-mail", False), cb.answers
    text, markup = _last_edit(nav)
    assert "→ на e-mail" in text and "📨 Куда: E-mail" in [b for r in _rows(markup) for b in r], text
    cb, nav = await _press(env, fake_bot, cyc)
    assert store[key] == "telegram" and cb.answers[-1] == ("Куда: Telegram", False)


async def test_backup_day_and_hour_in_one_input(env, fake_bot, store):
    """«✏️ 1-е, 12:00» → одно приглашение «день и час»; «31 12» — переспрос
    без записи; «5 9» — записано, итог первой строкой раздела, расписание и
    кнопка — новые, ввод закрыт."""
    store.update({"app.scheduler.backup_enabled": True, "app.scheduler.backup_day": 1,
                  "app.scheduler.backup_hour": 12})
    _, markup = await sections.screen("backup", env.br, env.services)
    when = _button(markup, "✏️ 1-е, 12:00").callback_data
    assert sections.resolve(env.br, when) == ("backup", "edit", "backup_when", "")
    st = FakeState()
    cb, nav = await _press(env, fake_bot, when, st)
    assert _last_edit(nav)[0] == ("✏️ <b>День и час автобэкапа</b> · сейчас 1-го в 12:00 · пришли два числа: "
                                  "<code>1 12</code>")
    bad = await _type(env, fake_bot, "31 12", st)
    assert [s[1] for s in bad.sent if s[0] == "answer"] == [texts.BACKUP_WHEN_BAD]
    assert store["app.scheduler.backup_day"] == 1 and await st.get_state() is not None
    msg = await _type(env, fake_bot, "5 9", st)
    assert (store["app.scheduler.backup_day"], store["app.scheduler.backup_hour"]) == (5, 9)
    answers = [s for s in msg.sent if s[0] == "answer"]
    assert len(answers) == 1, answers
    lines = answers[0][1].split("\n")
    assert lines[0] == "✅ Автобэкап: 1-е, 12:00 → 5-е, 09:00", lines
    assert lines[2].startswith("💾 <b>Бэкапы"), "итог — не в разделе бэкапов"
    assert "Каждое 5-е число в 09:00 → в этот чат" in lines
    assert "✏️ 5-е, 09:00" in [b for r in _rows(answers[0][2]) for b in r]
    assert await st.get_state() is None


# ── неизвестный ключ цикла ───────────────────────────────────────────────────

@pytest.mark.parametrize("sec, key", [("email", "email.imap_port"), ("mon", "monitor")])
async def test_an_unknown_cycle_key_is_refused(env, fake_bot, store, sec, key):
    """Ключ без ряда значений (кнопка прежних выпусков, чужой ключ) — alert
    «устарела», ничего не записано: цикл не подбирает значение наугад."""
    if key == "monitor":
        key = env.br.keys.monitor_minutes                     # ключ раздела, но не цикл
    cb, _ = await _press(env, fake_bot, env.br.cb.pack(sec, "cycle", key))
    assert cb.answers == [(sections.STALE, True)], cb.answers
    assert key not in store


# ── ⬆️ обновления ────────────────────────────────────────────────────────────

def _release(tag):
    # поколение ядра 0 — блокировка обновления (update_block_reason) не срабатывает
    return types.SimpleNamespace(tag=tag, body="- пункт", title="", awg_generation=lambda: 0)


@pytest.fixture()
def upd(env, monkeypatch, store):
    """Проверка обновлений без сети: счётчик походов к списку релизов."""
    from awgbot.infra import updates
    scene = {"next": _release("v3.3.1"), "fail": False, "calls": 0}

    def _next(max_generation=None):
        scene["calls"] += 1
        if scene["fail"]:
            raise updates.UpdateError("GitHub не ответил")
        return scene["next"]
    monkeypatch.setattr(updates, "next_release", _next)
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.2.0")
    store["updates.poll_schedule"] = "day"
    return scene


async def test_opening_updates_checks_right_away(env, fake_bot, upd):
    """Проверка — при открытии раздела: сразу «Проверяю…», найденная цель — в
    шапке и кнопкой «⬆️ Обновить до vX»; тег ложится туда, откуда строку
    «⬆️ Доступна vX» читает главная роли; ступени нет — «актуальна»."""
    cb, nav = await _press(env, fake_bot, env.br.cb.pack("upd"))
    assert cb.answers[0][0] == "Проверяю…" and upd["calls"] == 1, cb.answers
    text, markup = _last_edit(nav)
    assert text.split("\n")[0] == "⬆️ <b>Обновления</b> · v3.2.0 → v3.3.1", text
    assert _rows(markup) == [["⬆️ Обновить до v3.3.1"], ["✅ Уведомлять", "📅 Проверка: день"], ["⬅️ Назад"]]
    assert env.services.update_available_tag() == "v3.3.1"
    upd["next"] = None
    cb, nav = await _press(env, fake_bot, env.br.cb.pack("upd"))
    text, markup = _last_edit(nav)
    assert text == "⬆️ <b>Обновления</b> · v3.2.0 🟢 актуальна", text
    assert _rows(markup)[0] == ["✅ Уведомлять", "📅 Проверка: день"]


async def test_notify_toggle_and_schedule_cycle_answer_at_once_without_the_network(env, fake_bot, upd, store):
    """Тумблер «Уведомлять» и цикл «📅 Проверка» отвечают сразу и рисуют
    раздел по сохранённому тегу — без похода к списку релизов на каждое
    нажатие и без потери кнопки обновления; «никогда» в цикле нет."""
    _, markup = await sections.screen("upd", env.br, env.services)
    calls = upd["calls"]
    toggle = _button(markup, "✅ Уведомлять").callback_data
    cyc = _button(markup, "📅 Проверка: день").callback_data
    cb, nav = await _press(env, fake_bot, toggle)
    assert env.services.updates_muted() and cb.answers[0][0] == "Уведомления выключены", cb.answers
    text, markup = _last_edit(nav)
    assert text.split("\n")[0] == "⬆️ <b>Обновления</b> · v3.2.0 → v3.3.1", text
    assert _rows(markup)[0] == ["⬆️ Обновить до v3.3.1"], "кнопка обновления пропала без сети"
    assert _rows(markup)[1][0] == "☑️ Уведомлять"
    seen = []
    for word in ("неделя", "месяц", "день", "неделя"):
        cb, nav = await _press(env, fake_bot, cyc)
        seen.append(store["updates.poll_schedule"])
        assert cb.answers[0][0] == f"Проверка: {word}", cb.answers
        assert _rows(_last_edit(nav)[1])[1][1] == f"📅 Проверка: {word}"
    assert seen == ["week", "month", "day", "week"]
    assert upd["calls"] == calls, "тумблер или цикл сходили в сеть"


async def test_never_from_an_old_config_becomes_month_and_mute_on_open(env, fake_bot, upd, store):
    """«никогда» из старого конфига: раздел при открытии ставит «месяц» и
    выключает уведомления — проверка идёт ради строки на главной."""
    store["updates.poll_schedule"] = "never"
    assert not env.services.updates_muted()
    cb, nav = await _press(env, fake_bot, env.br.cb.pack("upd"))
    assert store["updates.poll_schedule"] == "month" and env.services.updates_muted()
    assert _rows(_last_edit(nav)[1])[1] == ["☑️ Уведомлять", "📅 Проверка: месяц"]
    assert upd["calls"] == 1, "проверка при «никогда» не пошла"


# ── 🩺 порог простоя ─────────────────────────────────────────────────────────

async def test_the_outage_threshold_is_minutes_on_screen_and_the_role_scale_in_the_config(env, fake_bot, store):
    """Порог простоя (у агента — молчания линка) правится в минутах, а в
    br.keys.outage пишется с множителем роли (у агента секунды): «7» — это
    7 мин на экране и 7 × outage_scale в конфиге."""
    br = env.br
    store[br.keys.outage] = 5 * br.keys.outage_scale
    _, markup = await sections.screen("mon", br, env.services)
    btn = _button(markup, f"{br.mon_outage_button}: 5 мин").callback_data
    assert sections.resolve(br, btn) == ("mon", "edit", br.keys.outage, "")
    label = sections.bounds(br.keys.outage)[2]
    st = FakeState()
    cb, nav = await _press(env, fake_bot, btn, st)
    assert _last_edit(nav)[0] == f"✏️ <b>{label}</b> · сейчас 5 мин · 1–1440", _last_edit(nav)[0]
    msg = await _type(env, fake_bot, "7", st)
    assert store[br.keys.outage] == 7 * br.keys.outage_scale, store[br.keys.outage]
    answers = [s for s in msg.sent if s[0] == "answer"]
    assert len(answers) == 1 and answers[0][1].split("\n")[0] == f"✅ {label}: 5 → 7 мин", answers
    assert f"{br.mon_outage_button}: 7 мин" in [b for r in _rows(answers[0][2]) for b in r]
