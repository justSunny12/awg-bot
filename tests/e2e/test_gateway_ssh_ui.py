"""Раздел «🛡 SSH-доступ» агента — то, чего нет в эталонах экранов
(tests/screens/gateway.txt): что порт, адреса и фильтр реально меняются (и не
меняются при отказе), ввод открыт или закрыт, отказ владельца порта по вводу
ничего не трогает, устаревшие кнопки 3.1.0, листание длинного списка адресов
и номер кнопки «➖» по полному списку. Тексты раздела, отказов и строки
панели — в эталоне."""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot import keyboards as kb
from awgbot.bot.keyboards import common as kbc
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.bot.states import SshPort, GwSshAllow
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.domain.services import ServiceError
from awgbot.infra.db import Database
from tests.conftest import FakeCallback, FakeMessage, FakeState

ADMIN = cfg.ADMIN_ID


def _scr(**kw):
    base = {"port": 22, "sshd_down": False, "ports": [22], "env_port": 22, "conf_ports": [22],
            "owner": "", "owner_where": "", "owner_port": None, "owner_detail": "", "owner_files": [],
            "filter": False, "allow": [], "resolved": [], "unresolved": [],
            "admin_ips": ["10.9.1.2", "10.9.1.3", "10.9.1.7"], "server": "203.0.113.10",
            "new_plumbing": True, "table_ports": {"tunnel_in": 22}, "ufw": False, "omv_rules": 0}
    base.update(kw)
    return base


class _Svc(GatewayServices):
    def __init__(self, db):
        super().__init__(db)
        self.screen = _scr()
        self.calls: list = []
        self.busy = ""

    def status(self):
        return GwStatus(link_up=True, handshake_age=5.0)

    def ssh_screen(self, info=None):
        return dict(self.screen)

    def ssh_port_busy(self, port):
        self.calls.append(("busy", port))
        return self.busy

    def ssh_port_change(self, port):
        self.calls.append(("change", port))
        old = self.screen["port"]
        self.screen["port"] = port
        return old

    def ssh_allow_add(self, raw):
        if "мусор" in raw:
            raise ServiceError("«мусор» не адрес, не подсеть и не имя: …")
        if ":" in raw:
            raise ServiceError(f"{raw} — IPv6, на шлюзе фильтр только по IPv4")
        self.screen["allow"] = self.screen["allow"] + [raw]
        return list(self.screen["allow"])

    def ssh_allow_remove(self, entry):
        self.calls.append(("remove", entry))
        self.screen["allow"] = [a for a in self.screen["allow"] if a != entry]
        return list(self.screen["allow"])

    def ssh_filter_on(self):
        self.calls.append(("on",)); self.screen["filter"] = True

    def ssh_filter_off(self):
        self.calls.append(("off",)); self.screen["filter"] = False


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return _Svc(d)


def _cb(fake_bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=fake_bot), nav


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# ── экран ────────────────────────────────────────────────────────────────────


# ── порт ─────────────────────────────────────────────────────────────────────

async def test_same_and_busy_ports_change_nothing(svc, fake_bot):
    """Тот же порт — ни проверки занятости, ни смены, ввод закрыт; занятый —
    смены нет: sshd на чужом порту — потеря входа на шлюз."""
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_port_ask(cb, GwCB(action="ssh_port"), svc, st)
    assert await st.get_state() == SshPort.value.state
    msg = FakeMessage(text="22", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert await st.get_state() is None and not svc.calls, svc.calls
    svc.busy = "nginx"
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="8443", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert ("busy", 8443) in svc.calls, "занятость не проверена"
    assert ("change", 8443) not in svc.calls


async def test_port_change_reaches_the_service_and_a_bad_port_keeps_the_input_open(svc, fake_bot):
    """Ввод порта доходит до смены; «70000» — переспрос, ввод открыт: иначе
    следующее сообщение человека ушло бы в никуда."""
    st = FakeState()
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="2222", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert ("change", 2222) in svc.calls
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="70000", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert await st.get_state() == SshPort.value.state, "переспрос — ввод открыт"


@pytest.mark.parametrize("owner", [
    {"port": 2222, "owner": "omv", "owner_port": 2222},
    {"owner": "generator", "owner_detail": "Ansible managed: do not edit",
     "owner_files": ["/etc/ssh/sshd_config.d/10-ansible.conf"]},
], ids=["omv", "generator"])
async def test_foreign_owner_refuses_before_touching_anything(svc, fake_bot, owner):
    """Конфигом sshd владеет OMV или другой генератор: по вводу порта ни ss,
    ни sshd не тронуты, ввод закрыт — следующий текст в чате не меняет порт.
    Экран отказа (где менять, что бот сделает сам) — в эталоне
    (gw.ssh.port.owner_late*)."""
    svc.screen = _scr(**owner)
    st = FakeState()
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="2200", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert not svc.calls, f"при чужом владельце тронуты ss/sshd: {svc.calls}"
    assert await st.get_state() is None, "ввод порта остался открытым"


async def test_finisher_buttons_reopen_or_close_the_input(svc, fake_bot):
    """«✏️ Другой порт» открывает ввод заново, «⬅️ Назад» — закрывает: иначе
    следующий текст в чате менял бы порт SSH."""
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_port_ask(cb, GwCB(action="ssh_port_retry"), svc, st)
    assert await st.get_state() == SshPort.value.state
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_port_back(cb, svc, st)
    assert await st.get_state() is None


# ── адреса и фильтр ──────────────────────────────────────────────────────────

async def test_allow_add_and_remove(svc, fake_bot):
    """IPv6 — переспрос с открытым вводом; адрес добавлен — ввод закрыт;
    «➖» убирает ровно свою запись сразу; устаревшая кнопка 3.1.0 без метки
    записи — отказ, а не удаление соседа."""
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_allow_ask(cb, svc, st)
    assert await st.get_state() == GwSshAllow.value.state
    msg = FakeMessage(text="2001:db8::1", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_allow_received(msg, st, svc)
    assert await st.get_state() == GwSshAllow.value.state, "переспрос — ввод открыт"
    msg = FakeMessage(text="home2.dyn.example", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_allow_received(msg, st, svc)
    assert svc.screen["allow"] == ["home2.dyn.example"], svc.screen["allow"]
    assert await st.get_state() is None
    # «➖» — сразу, без подтверждения: удаление отменяется тем же «➕ Адрес»
    cb, nav = _cb(fake_bot)
    tag = kb.entry_tag("home2.dyn.example")
    await gh.gw_ssh_action(cb, GwCB(action="ssh_del!", val=f"0.{tag}"), svc, FakeState())
    assert svc.calls == [("remove", "home2.dyn.example")], svc.calls
    # старая кнопка из сообщений 3.1.0 — без метки записи: номер один мог
    # сменить хозяина, поэтому отказ, а не удаление соседа
    svc.screen["allow"] = ["203.0.113.7"]
    n = len(svc.calls)
    for val in ("0", "7", "0.deadbeef"):
        cb, nav = _cb(fake_bot)
        await gh.gw_ssh_action(cb, GwCB(action="ssh_del", val=val), svc, FakeState())
        assert len(svc.calls) == n, f"по устаревшей кнопке {val!r} что-то убрано"
        assert cb.answers[0] == ("Список изменился — открой раздел заново", True)
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_del", val=f"0.{kb.entry_tag('203.0.113.7')}"),
                           svc, FakeState())
    assert svc.calls[-1] == ("remove", "203.0.113.7")


async def test_filter_on_and_off_are_immediate(svc, fake_bot):
    """Тумблер «Фильтр снаружи» — сразу, без подтверждения: включение и
    выключение доходят до таблицы шлюза одним нажатием; старые кнопки без
    «!» из сообщений 3.1.0 — тоже."""
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_on!"), svc, FakeState())
    assert svc.calls == [("on",)] and svc.screen["filter"] is True
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_off!"), svc, FakeState())
    assert svc.calls[-1] == ("off",) and svc.screen["filter"] is False
    # старые кнопки без «!» из сообщений 3.1.0 — тоже сразу
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_on"), svc, FakeState())
    assert svc.calls[-1] == ("on",)


async def test_owner_refusal_comes_right_on_the_button(svc, fake_bot):
    """На малине с OMV «Изменить порт» сразу показывает отказ — без приглашения
    и ввода: лишний шаг у целевых пользователей ни к чему."""
    svc.screen = _scr(owner="omv", owner_port=22)
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_port_ask(cb, GwCB(action="ssh_port"), svc, st)
    assert await st.get_state() is None, "ввод порта открыт, хотя порт задаёт OMV"


def test_the_address_list_pages_through_the_whole_list():
    """Двадцать адресов доступны листанием все и по порядку, ни одна страница
    не длиннее предела рядов: адрес, до которого не долистать, не убрать."""
    many = [f"h{i}.dyn.example" for i in range(20)]
    # весь список — кнопками, но не больше десяти рядов на экране: остальное листается
    seen: list[str] = []
    for page in range(10):
        markup = kb.gateway_ssh_kb(_scr(allow=many), page=page)
        labels = _labels(markup)
        assert len(markup.inline_keyboard) <= kbc.MAX_ROWS, f"страница {page}: {len(markup.inline_keyboard)} рядов"
        seen += [t.removeprefix("➖ ") for t in labels if t.startswith("➖")]
        if kbc.NEXT_LABEL not in labels:
            break
    assert seen == many, "листанием доступен не весь список или не по порядку"


def test_warnings_go_into_their_own_block_in_the_main_firewall_too():
    """У файервола ВПС «Не резолвятся» — тем же блоком «Предупреждения», что у
    шлюза, ограничены так же, а список адресов — кнопками."""
    from awgbot.bot.texts.settings import settings_firewall_text
    t = settings_firewall_text({"enabled": True, "ssh_port": 22, "raw_allow": [f"h{i}.example" for i in range(15)],
                                "unresolved": [f"h{i}.example" for i in range(15)], "admin_ips": ["x"]})
    assert "🟢 Снаружи: фильтр включён — только адреса из списка" in t
    assert "15 адресов — редактируемый список ниже" in t and t.count("и ещё 3") == 1, \
        "«Не резолвятся» у ВПС ограничены, как у шлюза; список — кнопками"
    assert "<b>Предупреждения:</b>\n⚠️ Не резолвятся:" in t


async def test_address_list_pages_and_removal_from_page_two_hits_the_right_entry(svc, fake_bot, monkeypatch):
    """Адресов больше, чем влезает на экран: список листается, а номер в кнопке
    «➖» — по ПОЛНОМУ списку. Считай его от начала страницы — со второй страницы
    убирался бы адрес с первой, и снаружи на шлюз пускало бы не тех."""
    from awgbot.bot import paging
    monkeypatch.setattr(paging, "_pages", {})
    many = [f"198.51.100.{i}" for i in range(1, 16)]
    svc.screen = _scr(allow=list(many))
    # страница запомнена листанием — экран раздела рисует её
    paging.remember(ADMIN, "gwssh", 0, 1)
    cb, nav = _cb(fake_bot)
    await gh.gw_section(cb, GwCB(action="ssh"), svc, FakeState())
    markup = nav.sent[-1][2]
    assert len(markup.inline_keyboard) <= kbc.MAX_ROWS, len(markup.inline_keyboard)
    rm =[b for row in markup.inline_keyboard for b in row if b.text.startswith("➖")]
    assert rm and rm[0].text != "➖ " + many[0], "вторая страница показывает начало списка"
    target = rm[0]
    entry = target.text.removeprefix("➖ ")
    data = GwCB.unpack(target.callback_data)
    num, _dot, tag = data.val.partition(".")
    assert int(num) == many.index(entry), "номер кнопки — не по полному списку"
    assert tag == kb.entry_tag(entry), "метка записи — не своей записи"

    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, data, svc, FakeState())
    assert svc.calls == [("remove", entry)], f"убран не тот адрес: {svc.calls}"


PEER_LINE = "Из локальных сетей других шлюзов: открыт для "
PEER_OFF_LINE = "Когда подсети связаны, SSH доступен и из подсетей других шлюзов"


async def test_cancel_under_a_prompt_closes_the_dialog_and_returns_the_section(svc, fake_bot, monkeypatch):
    """У агента «✖️ Отмена» под приглашением шлёт CancelCB, а общий обработчик
    подключён только у основного бота: кнопка уходила в «устарела», и ввод
    оставался открытым — следующий текст менял порт. Свой обработчик агента:
    диалог сброшен, раздел на месте приглашения."""
    from awgbot.bot.callbacks import CancelCB
    monkeypatch.setattr(cfg, "ROLE", "gateway")      # реестр экранов выбирает экраны агента по роли
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_port_ask(cb, GwCB(action="ssh_port"), svc, st)
    assert await st.get_state() == SshPort.value.state
    cb2, nav2 = _cb(fake_bot)
    await gh.gw_cancel_inline(cb2, CancelCB(kind="set_ssh", ref=0), svc, st)
    assert await st.get_state() is None, "ввод остался открытым"
