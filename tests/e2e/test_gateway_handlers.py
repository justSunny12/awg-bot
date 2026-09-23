"""Хендлеры агента шлюза: подтверждения и приём бандла."""
from __future__ import annotations

import base64
import os

import pytest

import awgbot.core.config as cfg
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
    assert "🏠 Локальная сеть" not in labels
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status())
    await gh.gw_panel(cb, svc, FakeState())
    labels = [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]
    assert "🏠 Локальная сеть" in labels
    await gh.gw_lan(cb, svc, FakeState())
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    assert "Локальная сеть без VPN" in text and "end0" in text and "Свои: 1 в туннель" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels[:4] == ["➕ В туннель", "➕ Напрямую", "📋 Свои списки", "🔄 Обновить списки"]


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
    assert "Локальная сеть без VPN" in answers[-1], "снова экран локальной сети"


async def test_own_lists_screen_and_delete(svc, fake_bot, monkeypatch):
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [("vpn", "example.com"), ("ru", "shop.ru")])
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_list(cb, svc)
    text = msg.sent[-1][1]
    assert "В туннель:\n• example.com" in text and "Напрямую:\n• shop.ru" in text
    labels = [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]
    assert labels == ["🗑 Убрать", "⬅️ Назад"]
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [])
    await gh.gw_lan_list(cb, svc)
    assert "Пока пусто" in msg.sent[-1][1]


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
    """Бандл применён — снимок с новым уходит сразу. Без этого карточка слота на
    ВПС минуты показывала бы «конфигурация расходится» и звала перевыпускать то,
    что уже стоит."""
    svc, sent = chan
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    state = FakeState()
    await state.update_data(bundle=base64.b64encode(b"BUNDLE").decode())
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await gh.gw_bundle_apply(cb, GwCB(action="apply!"), svc, state)
    assert svc.applied == [b"BUNDLE"]
    msgs = sent()
    assert [m["t"] for m in msgs] == ["snap", "delta"], f"после применения бандла на ВПС ушло {msgs}"
    assert msgs[-1]["bundle"] == {"lan_mode": "1"}


async def test_a_failed_bundle_sends_nothing_extra(chan):
    """Не применилось — сообщать на ВПС нечего сверх того, что скажет тик."""
    svc, sent = chan
    svc.apply_bundle = lambda blob, overwrite_passphrase=False: (False, "скрипт упал")
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    state = FakeState()
    await state.update_data(bundle=base64.b64encode(b"BUNDLE").decode())
    svc.snap["egress_ok"] = False                    # что-то сменилось само, до тика
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await gh.gw_bundle_apply(cb, GwCB(action="apply!"), svc, state)
    assert [m["t"] for m in sent()] == ["snap"]
