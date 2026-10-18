"""Хендлеры агента шлюза: подтверждения и приём бандла."""
from __future__ import annotations

import base64
import os

import pytest

import awgbot.core.config as cfg
from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from awgbot.util import bundlecrypt as bc
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState


class _Svc(GatewayServices):
    def __init__(self, db):
        super().__init__(db)
        self.restarted = 0

    def status(self):
        return GwStatus(link_up=True, handshake_age=5.0)

    def restart_link(self):
        self.restarted += 1
        return True, "поднят"



@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return _Svc(d)


class _Doc:
    def __init__(self, size): self.file_size = size; self.file_id = "F"


class _DlBot(FakeBot):
    def __init__(self, blob): super().__init__(); self.blob = blob
    async def download(self, doc, destination=None):
        destination.write(self.blob)


def _carrying(svc, carries: bool) -> None:
    """Несёт ли шлюз трафик: роль от сервера и живой линк в снимке тика."""
    svc.set_link_role(carries, standby=False, name="NASPi")
    svc.cached_status = lambda max_age: GwStatus(link_up=True, handshake_age=5.0)


async def test_restart_needs_confirmation(svc, fake_bot):
    """«🔁 Перезапуск AWG» сам ничего не рвёт — только показывает цену одной
    строкой; «Отмена» первой и возвращает в «🔧 Сервис», откуда пришли."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    _carrying(svc, True)
    await gh.gw_confirm(cb, GwCB(action="restart"), svc)
    assert svc.restarted == 0
    kind, text, markup = msg.sent[-1]
    assert text == ("🔁 Перезапустить AWG?\nЛинк опустится и поднимется — РФ-доступ у всех "
                    "прервётся на секунды"), text
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["⬅️ Отмена", "🔁 Перезапустить"]
    assert buttons[0].callback_data == GwCB(action="svc").pack(), "отказ увёл не в «Сервис»"

    await gh.gw_execute(cb, GwCB(action="restart!"), svc)
    assert svc.restarted == 1
    # итог — первой строкой раздела «🔧 Сервис», без отдельного сообщения
    edits = [t for kind, t, _ in msg.sent if kind == "edit_text"]
    assert edits[-1].startswith("✅ AWG перезапущен\n\n🔧 <b>Сервис</b>"), edits[-1]
    assert not [s for s in msg.sent if s[0] == "answer"], "итог перезапуска ушёл отдельным сообщением"


async def test_a_foreign_document_is_silently_ignored(svc):
    """Файл, который не конфигурация и не резервная копия, бот молча
    игнорирует: «не примем» говорить незачем — конфигурацию тоже не принимаем."""
    bot = _DlBot(b"#!/bin/sh\nrm -rf /\n")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    msg.document = _Doc(100)
    state = FakeState()
    await gh.gw_bundle_document(msg, svc, state)
    assert msg.sent == [], msg.sent
    assert "bundle" not in (await state.get_data())
    assert not msg.deleted, "чужой файл не наш секрет — сообщение админа не трогаем"


async def test_a_reissue_file_from_the_chat_history_is_refused_in_words(svc):
    """Шифрованный файл перевыпуска выпусков до 3.3.1, пересланный из
    истории чата: не применяется, в памяти диалога не остаётся, из чата
    убирается (внутри ключ линка), ответ — что делать вместо: «🚑
    Восстановить» или канал конфигурации."""
    blob = bc.MAGIC + b"DUMMY-reissue-file"           # формат узнаётся по подписи
    bot = _DlBot(blob)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    msg.document = _Doc(len(blob))
    state = FakeState()
    svc.db.nav_touch(cfg.ADMIN_ID, 777)                      # прежняя панель
    await gh.gw_bundle_document(msg, svc, state)
    assert msg.deleted, "файл с ключом линка остался в чате"
    assert msg.sent[-1][1] == texts.GW_REISSUE_FILE_REFUSED, msg.sent[-1]
    assert msg.sent[-1][2] is None, "под отказом кнопки применения"
    assert (await state.get_data()) == {}, "файл остался в памяти диалога"
    assert ("delete_message", cfg.ADMIN_ID, 777) not in bot.records, "живое меню снято без замены"


def test_old_apply_buttons_are_left_to_the_stale_handler():
    """«📦 Применить», «⬅️ Оставить своё» / «✅ Принять» и «Отмена» под файлом
    прежних выпусков — снятые действия: ни один роутер агента их не
    разбирает, и до них доходит обработчик устаревших кнопок."""
    from tests.e2e.test_settings_layout import _first_matching_handler
    routers, todo = [], [gh.router]
    while todo:
        r = todo.pop(0)
        if r not in routers:
            routers.append(r)
            todo.extend(r.sub_routers)
    assert any(_first_matching_handler(r, GwCB(action="panel")) for r in routers), "сверка ничего не находит"
    for action in ("apply!", "apply_ow!", "apply_keep!", "drop"):
        hit = [(r.name, _first_matching_handler(r, GwCB(action=action))) for r in routers]
        assert not [h for h in hit if h[1]], f"снятую кнопку {action} кто-то обрабатывает: {hit}"


async def test_a_big_first_run_file_under_a_foreign_name_leaves_the_chat(svc):
    """Файл первого применения с поставкой внутри весит мегабайты: под чужим
    именем его узнают по началу — и убирают из чата (внутри ключи линка),
    а не игнорируют, оставив ключи в истории."""
    blob = b"#!/bin/sh\n# awg-gw-bundle.sh \xe2\x80\x94 x\n" + b"x" * (3 * 1024 * 1024)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=_DlBot(blob))
    msg.document = _Doc(len(blob))
    msg.document.file_name = "conf.txt"
    await gh.gw_bundle_document(msg, svc, FakeState())
    assert msg.deleted, "файл с ключами остался в чате"
    assert msg.sent[-1][1] == texts.GW_REISSUE_FILE_REFUSED, msg.sent[-1]


async def test_oversized_document_is_not_downloaded(svc):
    class NoDl(FakeBot):
        async def download(self, *a, **k):
            raise AssertionError("скачали то, что заведомо не бандл")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=NoDl())
    msg.document = _Doc(50 * 1024 * 1024)
    await gh.gw_bundle_document(msg, svc, FakeState())
    assert msg.sent == [] and not msg.deleted, "на заведомо чужой файл бот ответил или его убрал"


async def test_start_removes_all_previous_menus(svc, fake_bot):
    """Повторный /start убирает ВСЕ прошлые меню, а не только снимает кнопки с
    последнего: /start — «начать заново», и стопка мёртвых панелей над живой
    заставляла бы листать историю."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    await gh.gw_start(msg, svc, FakeState())
    first_ids = [m for k, chat, m in
                 [(r[0], r[1], r[2]) for r in fake_bot.records if r[0] == "delete_message"]]
    assert first_ids, "прошлое меню не удалено при повторном /start"
    await gh.gw_start(msg, svc, FakeState())
    deleted = [r[2] for r in fake_bot.records if r[0] == "delete_message"]
    assert len(deleted) >= 2, "удаляется не всё прошлое"


def test_gateway_update_check_hook_reschedules_and_reads_never_as_month(monkeypatch):
    from awgbot.core import settings
    from awgbot.runtime import scheduler as sch
    calls = []

    class FakeSched:
        def pause_job(self, jid): calls.append(("pause", jid))
        def reschedule_job(self, jid, trigger=None): calls.append(("resched", jid, str(trigger)))

    hook = sch.update_check_hook(FakeSched())
    store = {"updates.poll_schedule": "never"}
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    hook("updates.poll_schedule", "never")
    assert calls[-1][0] == "resched" and "day='1'" in calls[-1][2], calls[-1]   # never — как месяц
    store["updates.poll_schedule"] = "week"
    hook("updates.poll_schedule", "week")
    assert calls[-1][0] == "resched" and "day_of_week" in calls[-1][2]
    assert not any(c[0] == "pause" for c in calls), "паузы у проверки обновлений больше нет"


# ── первая панель после установки ────────────────────────────────────────────

async def test_first_start_sends_the_panel_once_when_the_dialog_exists(svc, fake_bot, monkeypatch):
    """Опознание кодом: админ боту уже писал, установщик обещает «бот напишет
    сам» — агент присылает панель на первом старте, и только один раз."""
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    await gh.send_first_panel(fake_bot, svc)
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 1 and sent[0][1] == cfg.ADMIN_ID and sent[0][2].startswith("🛰 ")
    await gh.send_first_panel(fake_bot, svc)
    assert len([r for r in fake_bot.records if r[0] == "send_message"]) == 1, "повтор на каждом старте"


async def test_first_start_stays_silent_without_a_dialog(svc, monkeypatch):
    """Из файла первого применения диалога нет: Telegram не даёт боту начать
    первым — молчим и НЕ считаем панель показанной, чтобы попробовать позже."""
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))

    class NoDialog(FakeBot):
        async def send_message(self, *a, **k):
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
    await gh.send_first_panel(NoDialog(), svc)
    assert not svc.db.get_state(gh._FIRST_PANEL_KEY)


async def test_start_marks_the_first_panel_as_shown(svc, fake_bot, monkeypatch):
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    msg = FakeMessage(text="/start", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    assert svc.db.get_state(gh._FIRST_PANEL_KEY) == "1"
    await gh.send_first_panel(fake_bot, svc)
    assert not [r for r in fake_bot.records if r[0] == "send_message"], "после /start первая панель не нужна"


# ── локальная сеть без VPN: личные списки из чата ──

def _lan_status():
    return GwStatus(link_up=True, handshake_age=5.0,
                    lan={"iface": "end0", "addr": "192.168.68.222", "resolver": "10.9.1.1", "domains": 3,
                         "nets": 4, "resolved": 5, "updated_at": "", "own_vpn": 1, "own_ru": 0, "lan_pkts": 9})


async def test_panel_offers_the_lan_screen_only_when_enabled(svc, fake_bot, monkeypatch):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [])
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    await gh.gw_panel(cb, svc, FakeState())
    labels = [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]
    assert "🔀 VPN-транзит" not in labels
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    await gh.gw_panel(cb, svc, FakeState())
    labels = [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]
    assert "🔀 VPN-транзит" in labels
    await gh.gw_transit(cb, svc, FakeState())
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    assert text.startswith("🔀 <b>VPN-транзит</b> 🟢 работает") and "end0" in text \
        and "· свои: 1 в туннель" in text, text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    # обновления списков кнопкой нет: фиды привозит сервер или агент качает сам
    assert labels == ["➕ 🌍 В туннель", "➕ 🇷🇺 Напрямую", "🛜 Роутер", "⬅️ В меню"], labels


async def test_domain_input_goes_to_the_script_and_the_prompt_is_cleaned(svc, fake_bot, monkeypatch):
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    calls = []
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: (calls.append((cmd, domains)) or (True, "example.com: добавлен\n  example.com → 2 адрес(а) в наборе lan_vpn4")))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    st = FakeState()
    await gh.gw_transit_ask(cb, GwCB(action="lan_ru"), svc, st)
    assert "Напрямую" in msg.sent[-1][1] and await st.get_state() is not None
    reply = FakeMessage(text="Example.com shop.ru", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_domain_received(reply, st, svc)
    assert calls == [("ru", ["Example.com", "shop.ru"])], "разбор — в скрипте, бот передаёт как есть"
    assert await st.get_state() is None
    deleted = [r[2] for r in fake_bot.records if r[0] == "delete_message"]
    assert msg.message_id in deleted and reply.message_id in deleted, "приглашение и ввод убраны"
    answers = [s[1] for s in reply.sent if s[0] == "answer"]
    assert any(a.startswith("✅") and "добавлен" in a for a in answers)
    assert "🔀 <b>VPN-транзит</b>" in answers[-1] and "📋 Списки:" in answers[-1], "после ввода — назад в свои списки, откуда пришли"


def _own_lists_labels(msg) -> list[str]:
    return [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]


async def test_own_lists_screen_shows_domains_as_buttons_direct_first(svc, fake_bot, monkeypatch):
    """Свои списки — кнопками «➖ домен значок» прямо на экране «🔀 VPN-транзит»:
    сначала «напрямую» (🇷🇺), затем «в туннель» (🌍), внутри — по алфавиту.
    Порядок один для экрана и для номеров в колбэках — иначе «➖» убрал бы
    соседний домен."""
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    monkeypatch.setattr(svc, "lan_own_lists",
                        lambda: [("vpn", "zeta.com"), ("ru", "shop.ru"), ("vpn", "alpha.com"), ("ru", "bank.ru")])
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit(cb, svc, FakeState())
    assert _own_lists_labels(msg) == [
        "➕ 🌍 В туннель", "➕ 🇷🇺 Напрямую",
        "➖ 🇷🇺 bank.ru", "➖ 🇷🇺 shop.ru", "➖ 🌍 alpha.com", "➖ 🌍 zeta.com",
        "🛜 Роутер", "⬅️ В меню"], _own_lists_labels(msg)
    assert "Пока пусто" not in msg.sent[-1][1]
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [])
    await gh.gw_transit(cb, svc, FakeState())
    empty = msg.sent[-1][1]
    # подсказка пустого списка — под «подробнее», открытой строки «Пока пусто» нет
    assert "Пока пусто" not in empty, empty
    about = empty[empty.index("<blockquote expandable>"):]
    assert "добавь домены кнопками «➕ 🌍 В туннель» и «➕ 🇷🇺 Напрямую»" in about, empty
    assert _own_lists_labels(msg) == ["➕ 🌍 В туннель", "➕ 🇷🇺 Напрямую", "🛜 Роутер", "⬅️ В меню"]


async def test_removing_an_own_domain_is_immediate_and_removes_exactly_that_one(svc, fake_bot, monkeypatch):
    """«➖» — сразу, без подтверждения: убирает именно тот домен, что был на
    кнопке (номер — по отсортированному списку, а не по порядку в файле),
    итог — всплывашкой, экран перерисован уже без домена."""
    own = [("vpn", "zeta.com"), ("ru", "shop.ru"), ("vpn", "alpha.com")]
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    monkeypatch.setattr(svc, "lan_own_lists", lambda: list(own))
    calls = []

    def _del(cmd, domains):
        calls.append((cmd, domains))
        own[:] = [kd for kd in own if kd[1] not in domains]
        return True, f"{domains[0]}: убран"
    monkeypatch.setattr(svc, "lan_domains", _del)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit(cb, svc, FakeState())
    btn = next(b for row in msg.sent[-1][2].inline_keyboard for b in row if b.text == "➖ 🌍 alpha.com")
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_remove(cb, GwCB.unpack(btn.callback_data), svc)
    assert calls == [("del", ["alpha.com"])], f"убран не тот домен: {calls}"
    assert cb.answers == [("✅ alpha.com: убран", False)], cb.answers
    labels = _own_lists_labels(msg)
    assert "➖ 🌍 alpha.com" not in labels and "➖ 🌍 zeta.com" in labels, labels
    assert msg.sent[-1][1].startswith("🔀 <b>VPN-транзит"), "после удаления — снова экран VPN-транзита"


async def test_a_stale_remove_button_does_not_touch_anything(svc, fake_bot, monkeypatch):
    """Кнопка из старого сообщения: список с тех пор укоротился, номера нет.
    Удалять наугад нельзя — только переспросить."""
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [("vpn", "alpha.com")])
    calls = []
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: calls.append((cmd, domains)) or (True, ""))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    for action, val in (("lan_rm", "5"), ("lan_rm", ""), ("lan_rm", "x"), ("lan_rm!", "5")):
        await gh.gw_transit_remove(cb, GwCB(action=action, val=val), svc)
    assert calls == [], f"по устаревшей кнопке что-то удалено: {calls}"
    assert cb.answers[-1] == ("Список изменился — открой раздел заново", True)


async def test_a_button_pressed_after_the_list_changed_does_not_remove_a_neighbour(svc, fake_bot, monkeypatch):
    """Экран нарисован, затем список изменился (домен добавили из консоли
    `awg-lan-domain.sh`, со второго устройства или синхронизацией с другого
    шлюза): новый домен встал перед показанным. «➖ beta.com» обязан убрать
    beta.com или переспросить — убрать по номеру соседний alpha.com, который
    человек не выбирал, — прямое нарушение обещания хендлера «список успел
    измениться — переспрос, не чужой домен»."""
    own = [("vpn", "beta.com")]
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    monkeypatch.setattr(svc, "lan_own_lists", lambda: list(own))
    calls = []
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: calls.append((cmd, domains)) or (True, ""))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit(cb, svc, FakeState())
    btn = next(b for row in msg.sent[-1][2].inline_keyboard for b in row if b.text == "➖ 🌍 beta.com")
    own.insert(0, ("vpn", "alpha.com"))                 # появился раньше по алфавиту
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_remove(cb, GwCB.unpack(btn.callback_data), svc)
    assert ("del", ["alpha.com"]) not in calls, (
        "нажали «➖ beta.com», а убран alpha.com — соседний домен")
    assert calls == [], f"по кнопке устаревшего экрана что-то убрано: {calls}"
    assert cb.answers == [("Список изменился — открой раздел заново", True)], cb.answers
    labels = _own_lists_labels(msg)
    assert "➖ 🌍 alpha.com" in labels and "➖ 🌍 beta.com" in labels, "экран не перерисован по новому списку"


async def test_a_failed_removal_is_an_alert_and_the_domain_stays(svc, fake_bot, monkeypatch):
    """Скрипт отказал — alert с его словами (разметка снята: всплывашка —
    не HTML), домен на экране остаётся."""
    own = [("ru", "shop.ru")]
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    monkeypatch.setattr(svc, "lan_own_lists", lambda: list(own))
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: (False, "awg-lan-domain.sh: занято <lock>"))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit(cb, svc, FakeState())
    btn = next(b for row in msg.sent[-1][2].inline_keyboard for b in row if b.text == "➖ 🇷🇺 shop.ru")
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_remove(cb, GwCB.unpack(btn.callback_data), svc)
    assert cb.answers == [("⚠️ shop.ru не убран: занято <lock>", True)], cb.answers
    assert "➖ 🇷🇺 shop.ru" in _own_lists_labels(msg)


# ── снимок на ВПС сразу после операции из чата (канал линка) ────────────────

class _ChanSvc(_Svc):
    """Агент с каналом: снимок меняется тем, что сделал человек в чате."""

    def __init__(self, db):
        super().__init__(db)
        self.snap = {"bundle": {"lan_mode": "0"}, "mark_status": "old", "egress_ok": True}

    def gw_snapshot(self):
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.snap.items()}

    def gateway_claim_if_needed(self):
        return None

    def reassert(self):
        self.snap["mark_status"] = "new"             # мастер перевыставил обвязку — решение скрипта другое
        return True, "перевыставлено"


class _Wire:
    def __init__(self):
        self.lines: list[bytes] = []

    def write(self, data):
        self.lines.append(data)

    async def drain(self):
        pass

    def close(self):
        pass


@pytest.fixture()
async def chan(tmp_path, monkeypatch):
    """Открытая сессия канала до ВПС: всё, что ушло, — в wire."""
    import asyncio
    from awgbot.infra import gwguard
    from awgbot.runtime import linkclient
    from awgbot.util import gwlink
    priv = base64.b64encode(os.urandom(32)).decode()
    conf = tmp_path / "awglink.conf"
    conf.write_text(f"[Interface]\nPrivateKey = {priv}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "GW_LINK_CONF", str(conf))
    monkeypatch.setattr(cfg, "ROLE", "gateway")
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    d = Database(tmp_path / "gw.db"); d.init_schema()
    svc = _ChanSvc(d)
    client = linkclient.LinkClient(svc)
    client._writer = _Wire()
    client._sn = sn = gwlink.new_nonce()                        # нонс сервера открытой сессии
    client._task = asyncio.get_running_loop().create_future()   # задача «идёт» — ensure не перезапустит
    monkeypatch.setattr(linkclient, "_client", client)
    await client.push(full=True)
    key = gwlink.channel_key(priv)
    yield svc, lambda: [gwlink.unpack(key, x, nonce=sn) for x in client._writer.lines]
    client._task.cancel()


async def test_the_recovery_wizard_sends_the_new_state_to_the_server_at_once(chan, fake_bot):
    """Мастер восстановления перевыставил обвязку. Человек идёт смотреть
    карточку слота на ВПС — там должно быть новое, а не то, что было до
    ближайшего тика монитора через минуты."""
    svc, sent = chan
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_execute(cb, GwCB(action="reassert!"), svc)
    msgs = sent()
    assert [m["t"] for m in msgs] == ["snap", "delta"], f"после мастера на ВПС ушло {msgs}"
    assert msgs[-1]["mark_status"] == "new"


# ── резервная копия агента и планировщик ─────────────────────────────────────

@pytest.mark.parametrize("name", ["awg-bot-backup-gw-20261001_120000.tgz.enc", "awg-bot-gw_20260901.tgz"])
async def test_a_gateway_backup_in_the_gateway_chat_is_refused_and_removed(svc, name):
    """Своих бэкапов у шлюза нет: копия шлюза прежних версий в чате бота
    шлюза — один ответ, сообщение убрано (внутри ключи), файл не скачивается
    и не осматривается. Бэкап сервера едет серверу — test_gateway_relay."""
    from awgbot.bot import texts

    class _Backup(_Doc):
        file_name = name

    class _NoDl(FakeBot):
        async def download(self, doc, destination=None):
            raise AssertionError("бэкап в чате шлюза скачан")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=_NoDl())
    msg.document = _Backup(100)
    await gh.gw_bundle_document(msg, svc, FakeState())
    assert msg.deleted, "файл с ключами остался в чате"
    assert msg.sent[-1][1] == texts.GW_OLD_BACKUP_REFUSED, msg.sent
    assert texts.GW_OLD_BACKUP_REFUSED.startswith("⚠️ Бэкап шлюза не применён: настройки шлюза теперь хранятся")
    assert not texts.GW_OLD_BACKUP_REFUSED.endswith(".")


async def test_gateway_scheduler_registers_its_jobs(svc, fake_bot):
    """Планировщик агента: тик монитора, списки и записи соседей — по именам
    задач; выпасть одной из них молча нельзя. Своих копий у шлюза нет — и
    заданий копии; проверки обновлений нет — агента обновляет сервер AWG."""
    from awgbot.runtime import scheduler as sch
    s = sch.setup_gateway_scheduler(svc, fake_bot)
    try:
        ids = {j.id for j in s.get_jobs()}
    finally:
        s.shutdown(wait=False)
    assert {"gw_monitor", "gw_lan_lists", "gw_lan_services"} <= ids, ids
    assert not [j for j in ids if "backup" in j or "update" in j], ids
