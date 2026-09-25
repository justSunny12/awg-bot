"""Хендлеры агента шлюза: подтверждения и приём бандла."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os

import pytest

import awgbot.core.config as cfg
from awgbot.runtime import linkclient
from awgbot.bot.callbacks import GwCB, HideCB
from awgbot.bot.handlers import gateway as gh
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from awgbot.util import bundlecrypt as bc
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState


class _Svc(GatewayServices):
    def __init__(self, db):
        super().__init__(db)
        self.restarted = 0
        self.applied: list[bytes] = []

    def status(self):
        return GwStatus(link_up=True, handshake_age=5.0)

    def restart_link(self):
        self.restarted += 1
        return True, "поднят"

    def apply_bundle(self, blob, overwrite_passphrase=False):
        self.applied.append(blob)
        return True, "Готово"


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


async def test_restart_needs_confirmation(svc, fake_bot):
    """Кнопка «Рестарт линка» сама ничего не рвёт — только показывает цену."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_confirm(cb, GwCB(action="restart"), svc)
    assert svc.restarted == 0
    assert any("оборвётся" in t for kind, t, _ in msg.sent if kind == "edit_text")

    await gh.gw_execute(cb, GwCB(action="restart!"), svc)
    assert svc.restarted == 1
    edits = [t for kind, t, _ in msg.sent if kind == "edit_text"]
    assert "Перезапуск AWG: готово" in edits[-1]


async def test_foreign_document_is_refused_before_anything(svc):
    bot = _DlBot(b"#!/bin/sh\nrm -rf /\n")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    msg.document = _Doc(100)
    state = FakeState()
    await gh.gw_bundle_document(msg, svc, state)
    assert "не принят" in msg.sent[-1][1]
    assert "bundle" not in (await state.get_data())
    assert not msg.deleted, "чужой файл не наш секрет — сообщение админа не трогаем"


async def test_our_bundle_waits_for_confirmation_then_applies(svc):
    priv = base64.b64encode(os.urandom(32)).decode()
    blob = bc.encrypt(b"#__GW_SETUP_BELOW__\n__LINK_CONF_EOF__\n", priv)
    bot = _DlBot(blob)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    msg.document = _Doc(len(blob))
    state = FakeState()
    svc.db.nav_touch(cfg.ADMIN_ID, 777)                      # прежняя панель
    await gh.gw_bundle_document(msg, svc, state)
    assert svc.applied == [], "применили без подтверждения"
    assert msg.sent[-1][2] is not None, "нет кнопок подтверждения"
    # внутри ключ линка, токен агента, фраза шифрования копий и пароль почты
    assert msg.deleted, "пересланная конфигурация осталась в чате"
    # прежняя панель удалена целиком: без кнопок над итогом применения она —
    # мусор; вопрос «применить?» — теперь живое меню
    assert ("delete_message", cfg.ADMIN_ID, 777) in bot.records, bot.records
    assert svc.db.get_nav_message_id(cfg.ADMIN_ID) not in (None, 777)

    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await gh.gw_bundle_apply(cb, GwCB(action="apply!"), svc, state)
    assert svc.applied == [blob]
    assert (await state.get_data()) == {}, "бандл остался в памяти после применения"


async def test_oversized_document_is_not_downloaded(svc):
    class NoDl(FakeBot):
        async def download(self, *a, **k):
            raise AssertionError("скачали то, что заведомо не бандл")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=NoDl())
    msg.document = _Doc(50 * 1024 * 1024)
    await gh.gw_bundle_document(msg, svc, FakeState())
    assert "не принят" in msg.sent[-1][1]


async def test_gateway_update_install_runs_the_shared_updater(svc, fake_bot, monkeypatch):
    """«Обновить» у агента: следующая ступень → «дождись» → apply_update. Та же
    механика, что у клиентской роли, — sha256 и запуск вне cgroup внутри."""
    import types
    nxt = types.SimpleNamespace(tag="v9.9.9", body="", skipped=())
    applied = []
    monkeypatch.setattr(svc, "update_next", lambda: nxt)
    monkeypatch.setattr(svc, "apply_update", lambda r: applied.append(r.tag))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_update_install(cb, svc)
    assert applied == ["v9.9.9"]
    assert svc.db.get_state("update_wait"), "«дождись» не запомнено для нового процесса"


async def test_update_failure_message_can_be_hidden(svc, fake_bot, monkeypatch):
    """«Не удалось обновить: GitHub HTTP 500» — финишер со «Скрыть», панель
    следом: отказ не итог ступени, держать его в истории незачем, а меню под
    кнопкой уже удалено."""
    import types
    nxt = types.SimpleNamespace(tag="v9.9.9", body="", skipped=())
    monkeypatch.setattr(svc, "update_next", lambda: nxt)

    def boom(r):
        raise RuntimeError("GitHub HTTP 500: Internal Server Error")
    monkeypatch.setattr(svc, "apply_update", boom)
    sent = []
    real = fake_bot.send_message

    async def send_message(chat_id, text, reply_markup=None, **kw):
        sent.append((text, reply_markup))
        return await real(chat_id, text, reply_markup=reply_markup, **kw)
    monkeypatch.setattr(fake_bot, "send_message", send_message)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_update_install(cb, svc)
    text, markup = [x for x in sent if "Не удалось обновить" in x[0]][0]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["Скрыть"]
    assert markup.inline_keyboard[0][0].callback_data == HideCB().pack()
    assert not svc.db.get_state("update_pending")
    panel = [s for s in msg.sent if s[0] == "answer" and s[2] is not None]
    assert panel and "Шлюз" in panel[-1][1] or svc.db.get_nav_message_id(cfg.ADMIN_ID), "панель не пришла"


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


async def test_gateway_updates_screen_and_manual_check(svc, fake_bot, monkeypatch):
    """У агента есть ручная точка входа в обновления: экран с версией и
    «Проверить сейчас», который при наличии ступени даёт кнопку «Обновить»."""
    import types
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_updates_screen(cb, svc)
    shown = [t for k, t, _ in msg.sent if k == "edit_text"]
    assert shown and cfg.INSTALLED_VERSION in shown[-1]

    monkeypatch.setattr(svc, "update_next", lambda: None)
    await gh.gw_updates_check(cb, svc)
    assert "актуальн" in [t for k, t, _ in msg.sent if k == "edit_text"][-1].lower()

    monkeypatch.setattr(svc, "update_next",
                        lambda: types.SimpleNamespace(tag="v9.9.9", body="заметки", skipped=()))
    await gh.gw_updates_check(cb, svc)
    kind, text, markup = [x for x in msg.sent if x[0] == "edit_text"][-1]
    assert "v9.9.9" in text and markup is not None, "нет кнопки «Обновить»"
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any(d.startswith("upd:install") for d in datas)
    assert not any(d.startswith("hide") for d in datas), "мёртвая «Скрыть» у агента"


async def test_gateway_schedule_picker_mirrors_the_main_bot(svc, fake_bot, monkeypatch):
    """Пикер расписания у агента: значение пишется в conf, «никогда» глушит
    уведомления и блокирует тумблер — ровно как у основного бота."""
    from awgbot.core import settings
    written = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [])
    store = {"updates.poll_schedule": "day"}
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)

    await gh.gw_updates_sched(cb, GwCB(action="upd_sched", val="week"), svc)
    assert written["updates.poll_schedule"] == "week"
    assert not svc.updates_muted()

    await gh.gw_updates_sched(cb, GwCB(action="upd_sched", val="never"), svc)
    assert svc.updates_muted(), "«никогда» не заглушило уведомления"

    store["updates.poll_schedule"] = "never"
    cb.answers.clear()
    await gh.gw_updates_toggle(cb, svc)
    assert cb.answers and cb.answers[-1][1] is True, "тумблер при «никогда» не заблокирован"
    assert svc.updates_muted()


def test_gateway_update_check_hook_pauses_on_never_and_reschedules_otherwise(monkeypatch):
    from awgbot.core import settings
    from awgbot.runtime import scheduler as sch
    calls = []

    class FakeSched:
        def pause_job(self, jid): calls.append(("pause", jid))
        def reschedule_job(self, jid, trigger=None): calls.append(("resched", jid, type(trigger).__name__))

    hook = sch.gateway_update_check_hook(FakeSched())
    store = {"updates.poll_schedule": "never"}
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    hook("updates.poll_schedule", "never")
    assert calls[-1] == ("pause", "update_check")
    store["updates.poll_schedule"] = "week"
    hook("updates.poll_schedule", "week")
    assert calls[-1][0] == "resched" and calls[-1][2] == "CronTrigger"


# ── первая панель после установки ────────────────────────────────────────────

async def test_first_start_sends_the_panel_once_when_the_dialog_exists(svc, fake_bot, monkeypatch):
    """Опознание кодом: админ боту уже писал, установщик обещает «бот напишет
    сам» — агент присылает панель на первом старте, и только один раз."""
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    await gh.send_first_panel(fake_bot, svc)
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 1 and sent[0][1] == cfg.ADMIN_ID and "РФ-шлюз" in sent[0][2]
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


# ── локальная сеть без VPN: личные списки из чата (концепт «локальная сеть» §3.5) ──

def _lan_status():
    return GwStatus(link_up=True, handshake_age=5.0,
                    lan={"iface": "end0", "addr": "192.168.68.222", "resolver": "10.9.1.1", "domains": 3,
                         "nets": 4, "resolved": 5, "updated_at": "", "own_vpn": 1, "own_ru": 0, "lan_pkts": 9})


async def test_panel_offers_the_lan_screen_only_when_enabled(svc, fake_bot, monkeypatch):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    await gh.gw_panel(cb, svc, FakeState())
    labels = [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]
    assert "🏠 Локальная сеть без VPN" not in labels
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    await gh.gw_panel(cb, svc, FakeState())
    labels = [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]
    assert "🏠 Локальная сеть без VPN" in labels
    await gh.gw_lan(cb, svc, FakeState())
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    assert "Локальная сеть без VPN" in text and "end0" in text and "Свои списки: 1 в туннель" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    # обновления списков кнопкой нет: фиды привозит сервер или агент качает сам
    assert labels == ["📋 Свои списки", "❓ Настройка роутера", "⬅️ В меню"], labels


async def test_domain_input_goes_to_the_script_and_the_prompt_is_cleaned(svc, fake_bot, monkeypatch):
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    calls = []
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: (calls.append((cmd, domains)) or (True, "example.com: добавлен\n  example.com → 2 адрес(а) в наборе lan_vpn4")))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    st = FakeState()
    await gh.gw_lan_ask(cb, GwCB(action="lan_ru"), svc, st)
    assert "Напрямую" in msg.sent[-1][1] and await st.get_state() is not None
    reply = FakeMessage(text="Example.com shop.ru", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_domain_received(reply, st, svc)
    assert calls == [("ru", ["Example.com", "shop.ru"])], "разбор — в скрипте, бот передаёт как есть"
    assert await st.get_state() is None
    deleted = [r[2] for r in fake_bot.records if r[0] == "delete_message"]
    assert msg.message_id in deleted and reply.message_id in deleted, "приглашение и ввод убраны"
    answers = [s[1] for s in reply.sent if s[0] == "answer"]
    assert any(a.startswith("✅") and "добавлен" in a for a in answers)
    assert "Свои списки" in answers[-1], "после ввода — назад в свои списки, откуда пришли"


def _own_lists_labels(msg) -> list[str]:
    return [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]


async def test_own_lists_screen_shows_domains_as_buttons_direct_first(svc, fake_bot, monkeypatch):
    """Свои списки — кнопками «➖ домен (значок)»: сначала «напрямую» (🇷🇺), затем
    «в туннель» (📤), внутри — по алфавиту. Порядок один для экрана и для
    номеров в колбэках — иначе «➖» убрал бы соседний домен."""
    monkeypatch.setattr(svc, "lan_own_lists",
                        lambda: [("vpn", "zeta.com"), ("ru", "shop.ru"), ("vpn", "alpha.com"), ("ru", "bank.ru")])
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_list(cb, svc, FakeState())
    assert _own_lists_labels(msg) == [
        "➕ В туннель", "➕ Напрямую",
        "➖ bank.ru (🇷🇺)", "➖ shop.ru (🇷🇺)", "➖ alpha.com (📤)", "➖ zeta.com (📤)",
        "⬅️ Назад"], _own_lists_labels(msg)
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [])
    await gh.gw_lan_list(cb, svc, FakeState())
    assert "Пока пусто" in msg.sent[-1][1]
    assert _own_lists_labels(msg) == ["➕ В туннель", "➕ Напрямую", "⬅️ Назад"]


async def test_removing_an_own_domain_asks_first_and_removes_exactly_that_one(svc, fake_bot, monkeypatch):
    """«➖» — действие с последствиями: сначала подтверждение с «Отменой»
    первой; «Убрать» удаляет именно тот домен, что был на кнопке, — номер
    считается по отсортированному списку, а не по порядку в файле."""
    own = [("vpn", "zeta.com"), ("ru", "shop.ru"), ("vpn", "alpha.com")]
    monkeypatch.setattr(svc, "lan_own_lists", lambda: list(own))
    calls = []
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: (calls.append((cmd, domains)) or (True, f"{domains[0]}: убран")))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    # по отсортированному: 0 — shop.ru (🇷🇺), 1 — alpha.com, 2 — zeta.com
    await gh.gw_lan_remove(cb, GwCB(action="lan_rm", val="1"), svc)
    text = msg.sent[-1][1]
    assert "alpha.com" in text and "в туннель" in text, text
    markup = msg.sent[-1][2]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["⬅️ Отмена", "➖ Убрать"], "«Отмена» — первой"
    assert calls == [], "подтверждение само ничего не удаляет"

    # «Отмена» возвращает в свои списки
    assert GwCB.unpack(buttons[0].callback_data).action == "lan_list"
    await gh.gw_lan_list(cb, svc, FakeState())
    assert "➖ alpha.com (📤)" in _own_lists_labels(msg), "после отмены список прежний"
    assert calls == []

    # «Убрать» — тот самый домен
    go = GwCB.unpack(buttons[1].callback_data)
    assert go.action == "lan_rm!"
    await gh.gw_lan_remove(cb, go, svc)
    assert calls == [("del", ["alpha.com"])], f"убран не тот домен: {calls}"
    assert "Свои списки" in msg.sent[-1][1], "после удаления — снова свои списки"


async def test_a_stale_remove_button_does_not_touch_anything(svc, fake_bot, monkeypatch):
    """Кнопка из старого сообщения: список с тех пор укоротился, номера нет.
    Удалять наугад нельзя — только переспросить."""
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [("vpn", "alpha.com")])
    calls = []
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: calls.append((cmd, domains)) or (True, ""))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    for val in ("5", "", "x"):
        await gh.gw_lan_remove(cb, GwCB(action="lan_rm!", val=val), svc)
    assert calls == [], f"по устаревшей кнопке что-то удалено: {calls}"
    assert cb.answers[-1] == ("Список изменился — открой его заново", True)


async def test_confirming_after_the_list_changed_does_not_remove_a_neighbour(svc, fake_bot, monkeypatch):
    """Между «➖» и «Убрать» список изменился (домен добавили из консоли
    `awg-lan-domain.sh` или со второго устройства): новый домен встал перед
    выбранным. Хендлер обещает «список успел измениться — переспрос, не чужой
    домен»; по номеру из подтверждения убрать соседний — прямое нарушение."""
    own = [("vpn", "beta.com")]
    monkeypatch.setattr(svc, "lan_own_lists", lambda: list(own))
    calls = []
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: calls.append((cmd, domains)) or (True, ""))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_remove(cb, GwCB(action="lan_rm", val="0"), svc)
    assert "beta.com" in msg.sent[-1][1]
    go = GwCB.unpack([b for row in msg.sent[-1][2].inline_keyboard for b in row][1].callback_data)
    own.insert(0, ("vpn", "alpha.com"))                 # появился раньше по алфавиту
    await gh.gw_lan_remove(cb, go, svc)
    assert ("del", ["alpha.com"]) not in calls, (
        "подтверждали удаление beta.com, а убран alpha.com — соседний домен")


# ── снимок на ВПС сразу после операции из чата (канал линка) ────────────────

class _ChanSvc(_Svc):
    """Агент с каналом: снимок меняется тем, что сделал человек в чате."""

    def __init__(self, db):
        super().__init__(db)
        self.snap = {"bundle": {"lan_mode": "0"}, "plumbing_gen": "old", "egress_ok": True}

    def gw_snapshot(self):
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.snap.items()}

    def gateway_claim_if_needed(self):
        return None

    def reassert(self):
        self.snap["plumbing_gen"] = "new"            # мастер перевыставил обвязку
        return True, "перевыставлено"

    def apply_bundle(self, blob, overwrite_passphrase=False):
        self.applied.append(blob)
        self.snap["bundle"] = {"lan_mode": "1"}      # бандл включил режим без VPN
        return True, "Готово"

    def inspect_bundle(self, blob):
        return {"ok": True}

    def gateway_apply_report(self):
        return ""

    def gateway_mark_outcome(self):
        return {}


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
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "1" if k == "LINK_CHANNEL" else "")
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
    assert msgs[-1]["plumbing_gen"] == "new"


async def test_an_applied_bundle_is_on_the_server_before_the_next_tick(chan):
    """Бандл применён — итог и снимок с новым уходят сразу. Без итога файл
    конфигурации так и висел бы в чате ВПС без ответа (а внутри ключ линка);
    без дельты карточка слота минуты показывала бы «конфигурация расходится»
    и звала перевыпускать то, что уже стоит."""
    svc, sent = chan
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    state = FakeState()
    await state.update_data(bundle=base64.b64encode(b"BUNDLE").decode())
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await gh.gw_bundle_apply(cb, GwCB(action="apply!"), svc, state)
    assert svc.applied == [b"BUNDLE"]
    msgs = sent()
    assert [m["t"] for m in msgs] == ["snap", "applied", "delta"], f"после применения бандла на ВПС ушло {msgs}"
    assert (msgs[1]["ok"], msgs[1]["error"]) == (True, ""), f"итог успеха пришёл с ошибкой: {msgs[1]}"
    assert msgs[-1]["bundle"] == {"lan_mode": "1"}
    assert msgs[1]["fp"] == hashlib.sha256(b"BUNDLE").hexdigest()[:16], "итог без отпечатка файла"
    # подтверждения от сервера в этой сцене нет — итог ждёт в очереди: строка в
    # буфере сессии, которую бандл тут же перезапустил, дошла бы не всегда
    assert svc.applied_pending_get().get("ok") is True, "итог выбыл из очереди до подтверждения сервера"
    await linkclient._client._dispatch({"t": "applied_ack", "fp": msgs[1]["fp"], "at": msgs[1]["at"]})
    assert svc.applied_pending_get() == {}, "подтверждённый итог остался в очереди — уйдёт второй раз"


async def test_a_failed_bundle_sends_its_error_and_nothing_else(chan):
    """Не применилось — на ВПС уходит итог с причиной, чтобы человек у файла
    узнал об отказе, не открывая чат агента. Снимка сверх этого нет: сообщать
    о состоянии нечего сверх того, что скажет тик."""
    svc, sent = chan
    svc.apply_bundle = lambda blob, overwrite_passphrase=False: (False, "скрипт упал")
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    state = FakeState()
    await state.update_data(bundle=base64.b64encode(b"BUNDLE").decode())
    svc.snap["egress_ok"] = False                    # что-то сменилось само, до тика
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await gh.gw_bundle_apply(cb, GwCB(action="apply!"), svc, state)
    msgs = sent()
    assert [m["t"] for m in msgs] == ["snap", "applied"], f"после отказа на ВПС ушло {msgs}"
    assert (msgs[1]["ok"], msgs[1]["error"]) == (False, "скрипт упал"), msgs[1]
    # подтверждение о другом файле или другом времени очередь не трогает
    await linkclient._client._dispatch({"t": "applied_ack", "fp": "0" * 16, "at": msgs[1]["at"]})
    assert svc.applied_pending_get().get("error") == "скрипт упал", "чужое подтверждение опустошило очередь"
    await linkclient._client._dispatch({"t": "applied_ack", "fp": msgs[1]["fp"], "at": msgs[1]["at"]})
    assert svc.applied_pending_get() == {}, "подтверждённый итог остался в очереди"


async def test_an_apply_without_a_channel_keeps_the_result_for_the_next_session(tmp_path, monkeypatch):
    """Канала нет (линк ещё не поднят или LINK_CHANNEL выключен): итог не
    теряется, а ждёт в state — уйдёт первым делом при подключении. Причина
    отказа — одной строкой: многострочный хвост скрипта иначе ломал бы
    строку итога в чате ВПС."""
    from awgbot.runtime import linkclient
    monkeypatch.setattr(linkclient, "_client", None)
    d = Database(tmp_path / "gw.db"); d.init_schema()
    svc = _ChanSvc(d)
    svc.apply_bundle = lambda blob, overwrite_passphrase=False: (False, "скрипт\n   упал:\tнет  места")
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    state = FakeState()
    await state.update_data(bundle=base64.b64encode(b"BUNDLE").decode())
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await gh.gw_bundle_apply(cb, GwCB(action="apply!"), svc, state)
    pending = svc.applied_pending_get()
    assert (pending.get("ok"), pending.get("error")) == (False, "скрипт упал: нет места"), pending
    # следующее применение, уже удачное, заменяет итог, а не копит второй
    svc.apply_bundle = lambda blob, overwrite_passphrase=False: (True, "Готово")
    monkeypatch.setattr(linkclient, "poke", lambda services: asyncio.sleep(0))
    await state.update_data(bundle=base64.b64encode(b"BUNDLE").decode())
    await gh.gw_bundle_apply(cb, GwCB(action="apply!"), svc, state)
    pending = svc.applied_pending_get()
    assert (pending.get("ok"), pending.get("error")) == (True, ""), f"в очереди остался прежний отказ: {pending}"


def test_a_broken_pending_record_reads_as_nothing(svc):
    """Запись очереди испорчена (обрыв записи на SD) — читается как «итога
    нет», а не роняет подключение канала на каждом заходе."""
    for raw in ("{", "[1, 2]", '{"error": "x"}', ""):
        svc.db.set_state(svc._APPLIED_PENDING_KEY, raw)
        assert svc.applied_pending_get() == {}, raw
    svc.applied_pending_set(True, "")
    svc.applied_pending_clear()
    assert svc.applied_pending_get() == {}


def test_a_pending_result_older_than_a_day_is_not_sent(svc):
    """Канала не было больше суток — итог в очереди считается пустым: у файла
    на сервере столько никто не ждёт, а поздний «применено» пришёл бы к
    давно выданному заново файлу. Итог моложе суток — живой."""
    import datetime
    import json
    from awgbot.util import timeutil
    svc.applied_pending_set(False, "скрипт упал", "a" * 16)

    def aged(hours):
        data = json.loads(svc.db.get_state(svc._APPLIED_PENDING_KEY))
        data["at"] = timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=hours))
        svc.db.set_state(svc._APPLIED_PENDING_KEY, json.dumps(data))
    aged(23)
    got = svc.applied_pending_get()
    assert (got.get("ok"), got.get("fp")) == (False, "a" * 16), f"итог моложе суток потерян: {got}"
    aged(25)
    assert svc.applied_pending_get() == {}, "просроченный итог уйдёт на сервер"


# ── заявка на назначение шлюза после применения: канал или пересылка ─────────

class _ClaimSvc(_Svc):
    """Бандл применился, а шлюз в основном боте не назначен: есть токен заявки."""

    def inspect_bundle(self, blob):
        return {"ok": True}

    def gateway_apply_report(self):
        return ""

    def lan_lists_needed(self):
        return False

    def gateway_mark_outcome(self):
        return {"claim": "AWGGW-DUMMY-TOKEN", "status": "unmarked"}


async def _apply_with_claim(tmp_path, monkeypatch, online):
    """Применить бандл при заданном поведении канала; вернуть (ответы в чат,
    сколько секунд агент ждал канал)."""
    from awgbot.runtime import linkclient
    d = Database(tmp_path / "gw.db"); d.init_schema()
    svc = _ClaimSvc(d)
    waited = []
    real_sleep = asyncio.sleep

    async def fake_sleep(sec, *a, **k):
        waited.append(sec)
        await real_sleep(0)

    async def no_poke(services):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(linkclient, "poke", no_poke)
    monkeypatch.setattr(linkclient, "online", online)
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    state = FakeState()
    await state.update_data(bundle=base64.b64encode(b"bundle").decode())
    await gh.gw_bundle_apply(cb, GwCB(action="apply!"), svc, state)
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    return answers, sum(waited)


async def test_a_claim_goes_through_the_channel_when_it_comes_up_in_time(tmp_path, monkeypatch):
    """Бандл только что поднял канал; клиент канала подключается не мгновенно.
    Агент ждёт до трёх секунд и, дождавшись, говорит «отправлено по каналу» —
    без токена в чате: пересылать нечего, а токен в истории — лишний след."""
    calls = {"n": 0}

    def online():
        calls["n"] += 1
        return calls["n"] > 2                          # поднялся на третьей проверке

    answers, waited = await _apply_with_claim(tmp_path, monkeypatch, online)
    claim = [a for a in answers if "не назначен" in a]
    assert len(claim) == 1, answers
    assert "отправлен серверу AWG по каналу" in claim[0], claim[0]
    assert "AWGGW-DUMMY-TOKEN" not in claim[0] and "Перешли" not in claim[0], "токен в чате при живом канале"
    assert waited <= 3.0, f"ждали канал {waited} с — дольше обещанных трёх"


async def test_a_claim_falls_back_to_forwarding_when_the_channel_stays_down(tmp_path, monkeypatch):
    """Канал так и не поднялся за три секунды — единственный путь заявки —
    переслать сообщение основному боту; без токена человеку нечего пересылать."""
    answers, waited = await _apply_with_claim(tmp_path, monkeypatch, lambda: False)
    claim = [a for a in answers if "не назначен" in a]
    assert len(claim) == 1, answers
    assert "Перешли это сообщение основному боту" in claim[0] and "AWGGW-DUMMY-TOKEN" in claim[0], claim[0]
    assert "по каналу конфигурации:" not in claim[0]
    assert 2.5 <= waited <= 3.0, f"канал ждали {waited} с вместо трёх"
