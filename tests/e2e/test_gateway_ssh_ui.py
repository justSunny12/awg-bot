"""Раздел «🛡 Доступ по SSH» агента: экран, порт (тот же / занят / чужой
владелец / успех), адреса, фильтр с подтверждением, финишеры, панель."""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot import keyboards as kb, texts
from awgbot.bot.keyboards import common as kbc
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.bot.states import SshPort, GwSshAllow
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.domain.gwssh import SshOwnerRefusal
from awgbot.domain.services import ServiceError
from awgbot.infra import sshd
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

async def test_section_shows_port_tunnel_lan_outside_and_buttons(svc, fake_bot):
    svc.screen = _scr(allow=["home2.dyn.example", "203.0.113.7"], unresolved=["home2.dyn.example"])
    cb, nav = _cb(fake_bot)
    await gh.gw_section(cb, GwCB(action="ssh"), svc, FakeState())
    text = [t for k, t, _ in nav.sent if k == "edit_text"][-1]
    assert "Порт SSH: 22" in text and "устройствам админа (3)" in text and "с сервера AWG" in text
    assert "Из локальной сети: открыт всегда" in text
    assert "фильтр выключен" in text and "home2.dyn.example" in text and "203.0.113.10" in text
    assert "⚠️ Не резолвится: home2.dyn.example" in text
    labels = _labels(nav.sent[-1][2])
    assert labels == ["🅿️ Изменить порт", "➕ Добавить адрес", "➖ home2.dyn.example",
                      "➖ 203.0.113.7", "🟢 Включить фильтр", "⬅️ Назад"]


async def test_section_under_omv_names_the_owner_and_old_plumbing_hides_the_filter(svc, fake_bot):
    svc.screen = _scr(port=2222, owner="omv", owner_port=2222, ports=[2222], conf_ports=[2222],
                      filter=True, allow=["203.0.113.7"], omv_rules=3)
    text = texts.gateway_ssh_text(svc.screen)
    assert "Порт SSH: 2222 — <b>контролирует OMV</b> <i>(в его UI: Службы → SSH)</i>" in text
    assert "🟢 Снаружи: фильтр включён" in text and "правила файервола (3)" in text
    assert "⚠️ В OMV задан порт" not in text and "не перезапущен" not in text, \
        "порты совпадают — предупреждать не о чем"
    text = texts.gateway_ssh_text(_scr(port=22, owner="omv", owner_port=2222))
    assert "⚠️ В OMV задан порт 2222, sshd слушает 22 — нажми «Применить» в OMV" in text
    text = texts.gateway_ssh_text(_scr(conf_ports=[2222]))
    assert "В конфиге sshd порт 2222, сервис слушает 22 — перезапусти sshd" in text
    text = texts.gateway_ssh_text(_scr(ports=[22, 2200]))
    assert "sshd слушает ещё порт (2200) — фильтр держит только 22" in text
    text = texts.gateway_ssh_text(_scr(sshd_down=True))
    assert "⚪ sshd не запущен" in text
    old = _scr(new_plumbing=False)
    assert "перевыпуска конфигурации шлюза" in texts.gateway_ssh_text(old)
    assert not any("фильтр" in l.lower() for l in _labels(kb.gateway_ssh_kb(old)))
    assert "🔴 Выключить фильтр" in _labels(kb.gateway_ssh_kb(_scr(filter=True)))


def test_panel_line_and_apply_report():
    assert texts.gateway_ssh_panel_line({"port": 2222, "owner": "omv", "filter": True, "allow": 2,
                                         "new_plumbing": True}) \
        == "🛡 SSH: порт 2222 (OMV) · снаружи: фильтр, 2 адреса"
    assert texts.gateway_ssh_panel_line({"port": 22, "owner": "", "filter": False, "allow": 0,
                                         "new_plumbing": True}) == "🛡 SSH: порт 22 · снаружи: открыт"
    assert "старого образца" in texts.gateway_ssh_panel_line({"port": 22, "new_plumbing": False})
    assert texts.gateway_ssh_panel_line({}) == ""
    st = GwStatus(link_up=True, handshake_age=1.0)
    st.ssh = {"port": 22, "owner": "", "filter": False, "allow": 0, "new_plumbing": True}
    assert "🛡 SSH: порт 22" in texts.gateway_panel(st)
    assert "SSH" not in texts.gateway_panel(GwStatus(link_up=True, handshake_age=1.0)), \
        "снимок старого агента без поля ssh — панель без строки"


# ── порт ─────────────────────────────────────────────────────────────────────

async def test_port_prompt_then_same_and_busy_are_finishers(svc, fake_bot):
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_port_ask(cb, GwCB(action="ssh_port"), svc, st)
    assert await st.get_state() == SshPort.value.state
    assert any("Порт SSH" in t and "роутере" in t for k, t, _ in nav.sent if k == "edit_text")
    msg = FakeMessage(text="22", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    sent = [s for s in msg.sent if s[0] == "answer" and "не изменился" in s[1]]
    assert sent and _labels(sent[0][2]) == ["✏️ Изменить порт", "⬅️ Назад"]
    assert await st.get_state() is None and not svc.calls
    svc.busy = "nginx"
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="8443", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    sent = [s for s in msg.sent if s[0] == "answer" and "порт 8443 уже занят процессом" in s[1]]
    assert sent and "<code>nginx</code>" in sent[0][1]
    assert ("change", 8443) not in svc.calls


async def test_port_change_success_and_sshd_refusal(svc, fake_bot, monkeypatch):
    st = FakeState()
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="2222", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert ("change", 2222) in svc.calls
    texts_sent = [s[1] for s in msg.sent if s[0] == "answer"]
    assert any("22 → <b>2222</b>" in t and "шлюз:2222" in t for t in texts_sent)
    assert any("Порт SSH: 2222" in t for t in texts_sent), "раздел перерисован"

    def boom(p):
        raise ServiceError("sshd -t: Bad configuration option")
    monkeypatch.setattr(svc, "ssh_port_change", boom)
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="2200", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert any("Порт не изменён" in s[1] and "Bad configuration" in s[1] for s in msg.sent if s[0] == "answer")
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="70000", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert await st.get_state() == SshPort.value.state, "переспрос — ввод открыт"


async def test_foreign_owner_refuses_before_touching_anything(svc, fake_bot):
    """OMV владеет sshd_config: отказ экраном с «где менять» и «что бот сделает
    сам»; ни ss, ни sshd не тронуты, ввод закрыт, «Назад» — в раздел."""
    svc.screen = _scr(port=2222, owner="omv", owner_port=2222,
                      owner_where="OMV: Службы → SSH → «Порт», затем «Применить»")
    st = FakeState()
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="2200", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    sent = [s for s in msg.sent if s[0] == "answer" and "управляет OMV" in s[1]]
    assert sent, msg.sent
    t = sent[0][1]
    assert "Службы → SSH → «Порт»" in t and "проживёт до первого применения" in t
    assert "переведёт на него фильтр" in t and "sshd слушает 2222, в OMV задан 2222" in t
    assert _labels(sent[0][2]) == ["⬅️ Назад"]
    assert GwCB.unpack(sent[0][2].inline_keyboard[0][0].callback_data).action == "ssh"
    assert not svc.calls and await st.get_state() is None
    # иной генератор — свой текст с найденной строкой
    svc.screen = _scr(owner="generator", owner_detail="Ansible managed: do not edit",
                      owner_files=["/etc/ssh/sshd_config.d/10-ansible.conf"])
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="2200", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert any("управляет другой процесс" in s[1] and "Ansible managed" in s[1]
               for s in msg.sent if s[0] == "answer")


async def test_owner_refusal_raised_by_the_service_is_shown_too(svc, fake_bot, monkeypatch):
    """Гонка: экран видел владельца «бот», а к моменту смены OMV уже
    переписал файл — сервис отказывает тем же экраном."""
    def refuse(p):
        raise SshOwnerRefusal(sshd.SshdOwner("omv", "OMV: Службы → SSH", 22), 22)
    monkeypatch.setattr(svc, "ssh_port_change", refuse)
    svc.busy = ""
    st = FakeState()
    await st.set_state(SshPort.value)
    msg = FakeMessage(text="2200", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    svc.screen = _scr()
    orig = svc.ssh_screen
    calls = {"n": 0}

    def screen(info=None):
        calls["n"] += 1
        return orig() if calls["n"] == 1 else _scr(owner="omv", owner_port=22)
    monkeypatch.setattr(svc, "ssh_screen", screen)
    await gh.gw_ssh_port_received(msg, st, svc)
    assert any("управляет OMV" in s[1] for s in msg.sent if s[0] == "answer")


async def test_finisher_buttons_reopen_prompt_or_section(svc, fake_bot):
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_port_ask(cb, GwCB(action="ssh_port_retry"), svc, st)
    assert await st.get_state() == SshPort.value.state
    assert ("edit_reply_markup", ADMIN) in fake_bot.records, "финишер остаётся с «Скрыть»"
    assert any(k == "answer" and "Порт SSH" in t for k, t, _ in nav.sent)
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_port_back(cb, svc, st)
    assert await st.get_state() is None
    assert any(k == "answer" and "Доступ по SSH" in t for k, t, _ in nav.sent)


# ── адреса и фильтр ──────────────────────────────────────────────────────────

async def test_allow_add_and_remove(svc, fake_bot):
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_allow_ask(cb, svc, st)
    assert await st.get_state() == GwSshAllow.value.state
    msg = FakeMessage(text="2001:db8::1", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_allow_received(msg, st, svc)
    assert any("IPv6" in s[1] for s in msg.sent if s[0] == "answer")
    assert await st.get_state() == GwSshAllow.value.state, "переспрос — ввод открыт"
    msg = FakeMessage(text="home2.dyn.example", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_allow_received(msg, st, svc)
    assert any("добавлено <b>home2.dyn.example</b>" in s[1] for s in msg.sent if s[0] == "answer")
    assert await st.get_state() is None
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_del", val="0"), svc, FakeState())
    assert ("remove", "home2.dyn.example") not in svc.calls, "удаление — с подтверждения"
    text = [t for k, t, _ in nav.sent if k == "edit_text"][-1]
    assert "Убрать <code>home2.dyn.example</code>" in text
    assert _labels(nav.sent[-1][2]) == ["⬅️ Отмена", "➖ Убрать"]
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_del!", val="0"), svc, FakeState())
    assert ("remove", "home2.dyn.example") in svc.calls and any("убран" in str(a) for a in cb.answers)
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_del", val="7"), svc, FakeState())
    assert any("Список изменился" in str(a) for a in cb.answers)


async def test_adding_covered_addresses_reports_the_merge(svc, fake_bot, monkeypatch):
    from awgbot.infra import gwguard
    monkeypatch.setattr(gwguard, "read_env", lambda: {"SSH_ALLOW": "203.0.113.7"})
    monkeypatch.setattr(svc, "ssh_allow_add", lambda raw: ["203.0.113.0/24"])
    st = FakeState()
    await st.set_state(GwSshAllow.value)
    msg = FakeMessage(text="203.0.113.0/24", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_allow_received(msg, st, svc)
    assert any("добавлено <b>203.0.113.0/24</b>; объединено с новой подсетью: <code>203.0.113.7</code>" in s[1]
               for s in msg.sent if s[0] == "answer"), msg.sent


async def test_filter_on_and_off_need_confirmation(svc, fake_bot):
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_on"), svc, FakeState())
    assert ("on",) not in svc.calls
    text = [t for k, t, _ in nav.sent if k == "edit_text"][-1]
    assert "Включить фильтр снаружи?" in text and "подменяет адрес" in text
    assert _labels(nav.sent[-1][2]) == ["⬅️ Отмена", "🟢 Включить"]
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_on!"), svc, FakeState())
    assert ("on",) in svc.calls and svc.screen["filter"] is True
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_off"), svc, FakeState())
    assert ("off",) not in svc.calls, "выключение — тоже с подтверждения"
    text = [t for k, t, _ in nav.sent if k == "edit_text"][-1]
    assert "Выключить фильтр снаружи?" in text and _labels(nav.sent[-1][2]) == ["⬅️ Отмена", "🔴 Выключить"]
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_off!"), svc, FakeState())
    assert ("off",) in svc.calls and any("открыт всем" in str(a) for a in cb.answers)


async def test_filter_on_refusal_from_old_plumbing_is_an_alert(svc, fake_bot, monkeypatch):
    def boom():
        raise ServiceError("обвязка шлюза старого образца: перевыпусти конфигурацию шлюза")
    monkeypatch.setattr(svc, "ssh_filter_on", boom)
    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, GwCB(action="ssh_on!"), svc, FakeState())
    assert any("старого образца" in str(a) for a in cb.answers)


async def test_owner_refusal_comes_right_on_the_button(svc, fake_bot):
    """На малине с OMV «Изменить порт» сразу показывает отказ — без приглашения
    и ввода: лишний шаг у целевых пользователей ни к чему."""
    svc.screen = _scr(owner="omv", owner_port=22)
    cb, nav = _cb(fake_bot)
    st = FakeState()
    await gh.gw_ssh_port_ask(cb, GwCB(action="ssh_port"), svc, st)
    assert await st.get_state() is None
    text = [t for k, t, _ in nav.sent if k == "edit_text"][-1]
    assert "управляет OMV" in text and _labels(nav.sent[-1][2]) == ["⬅️ Назад"]


async def test_readding_a_known_address_says_so(svc, fake_bot, monkeypatch):
    from awgbot.infra import gwguard
    monkeypatch.setattr(gwguard, "read_env", lambda: {"SSH_ALLOW": "home2.dyn.example"})
    svc.screen = _scr(allow=["home2.dyn.example"])
    monkeypatch.setattr(svc, "ssh_allow_add", lambda raw: ["home2.dyn.example"])
    st = FakeState()
    await st.set_state(GwSshAllow.value)
    msg = FakeMessage(text="home2.dyn.example", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_ssh_allow_received(msg, st, svc)
    assert any("уже в списке" in s[1] for s in msg.sent if s[0] == "answer")


def test_section_text_names_held_addresses_lan_and_caps_the_list():
    text = texts.gateway_ssh_text(_scr(allow=["home2.dyn.example"], unresolved=["home2.dyn.example"],
                                       held=["198.51.100.4"], lan=["192.168.1.0/24"]))
    assert "держу прошлый адрес: <code>198.51.100.4</code>" in text
    assert "Из локальной сети: открыт всегда (<code>192.168.1.0/24</code>)" in text
    text = texts.gateway_ssh_text(_scr(allow=["home2.dyn.example"], unresolved=["home2.dyn.example"]))
    assert "прошлого адреса нет" in text
    many = [f"h{i}.dyn.example" for i in range(20)]
    text = texts.gateway_ssh_text(_scr(allow=many))
    assert "20 адресов — редактируемый список ниже" in text and "h1.dyn.example" not in text, \
        "список в инфобокс не выносится — он кнопками ниже"
    # весь список — кнопками, но не больше десяти на экране: остальное листается
    seen: list[str] = []
    for page in range(10):
        markup = kb.gateway_ssh_kb(_scr(allow=many), page=page)
        labels = _labels(markup)
        assert len(labels) <= kbc.MAX_BUTTONS, f"страница {page}: {len(labels)} кнопок"
        seen += [t.removeprefix("➖ ") for t in labels if t.startswith("➖")]
        if kbc.NEXT_LABEL not in labels:
            break
    assert seen == many, "листанием доступен не весь список или не по порядку"


def test_warnings_go_into_their_own_block_in_both_sections():
    from awgbot.bot.texts.settings import settings_firewall_text
    t = texts.gateway_ssh_text(_scr(port=2222, ports=[2222], conf_ports=[2222], owner="omv", owner_port=22, ufw=True))
    assert "\n\n<b>Предупреждения:</b>\n⚠️ В OMV задан порт 22" in t and "\n⚠️ ufw активен" in t
    assert "<b>Предупреждения:</b>" not in texts.gateway_ssh_text(_scr()), "нечего — блока нет"
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
    cb, nav = _cb(fake_bot)
    await gh.gw_section(cb, GwCB(action="ssh"), svc, FakeState())
    first = _labels(nav.sent[-1][2])
    assert len(first) <= kbc.MAX_BUTTONS and kbc.NEXT_LABEL in first and kbc.PREV_LABEL not in first, first

    # страница запомнена листанием — экран раздела рисует её
    paging.remember(ADMIN, "gwssh", 0, 1)
    cb, nav = _cb(fake_bot)
    await gh.gw_section(cb, GwCB(action="ssh"), svc, FakeState())
    markup = nav.sent[-1][2]
    second = _labels(markup)
    assert kbc.PREV_LABEL in second and len(second) <= kbc.MAX_BUTTONS, second
    rm = [b for row in markup.inline_keyboard for b in row if b.text.startswith("➖")]
    assert rm and rm[0].text != "➖ " + many[0], "вторая страница показывает начало списка"
    target = rm[0]
    entry = target.text.removeprefix("➖ ")
    data = GwCB.unpack(target.callback_data)
    assert int(data.val) == many.index(entry), "номер кнопки — не по полному списку"

    cb, nav = _cb(fake_bot)
    await gh.gw_ssh_action(cb, data, svc, FakeState())
    ask = [t for k, t, _ in nav.sent if k == "edit_text"][-1]
    assert entry in ask, f"подтверждение спрашивает не про {entry}: {ask}"
    go = [b for row in nav.sent[-1][2].inline_keyboard for b in row if b.text == "➖ Убрать"][0]
    await gh.gw_ssh_action(cb, GwCB.unpack(go.callback_data), svc, FakeState())
    assert svc.calls == [("remove", entry)], f"убран не тот адрес: {svc.calls}"


PEER_LINE = "Из локальных сетей других шлюзов: открыт для "
PEER_OFF_LINE = ("При включении функции «Доступ между подсетями» будет открыт доступ "
                 "из локальных подсетей других шлюзов")


def test_section_names_peer_nets_or_says_how_to_get_them():
    """Из подсети другого шлюза SSH открыт сам, как только на сервере включён
    доступ между подсетями, — отдельного действия и списка адресов не нужно,
    и человек должен это видеть. Нет соседей — строка о том, что их даёт."""
    for empty in ({}, {"peer_nets": []}):
        text = texts.gateway_ssh_text(_scr(**empty))
        lines = text.splitlines()
        i = lines.index("Из локальной сети: открыт всегда")
        assert lines[i + 1] == PEER_OFF_LINE, (empty, lines)
        assert "открыт для" not in text, (empty, text)
    text = texts.gateway_ssh_text(_scr(lan=["192.168.1.0/24"], peer_nets=["10.20.0.0/16", "192.168.68.0/24"]))
    lines = text.splitlines()
    i = lines.index("Из локальной сети: открыт всегда (<code>192.168.1.0/24</code>)")
    assert lines[i + 1] == f"{PEER_LINE}<code>10.20.0.0/16</code>, <code>192.168.68.0/24</code>", lines
    assert PEER_OFF_LINE not in text
    # строка — и при включённом фильтре: соседей он не закрывает
    assert PEER_LINE in texts.gateway_ssh_text(_scr(filter=True, peer_nets=["192.168.68.0/24"]))


def test_section_peer_nets_line_is_capped_and_escaped():
    text = texts.gateway_ssh_text(_scr(peer_nets=["10.1.0.0/16", "10.2.0.0/16", "10.3.0.0/16", "10.4.0.0/16"]))
    line = next(ln for ln in text.splitlines() if ln.startswith(PEER_LINE))
    assert "10.3.0.0/16" in line and "10.4.0.0/16" not in line, line
    text = texts.gateway_ssh_text(_scr(peer_nets=["10.0.0.0/8<b>"]))
    assert "10.0.0.0/8&lt;b&gt;" in text and "10.0.0.0/8<b>" not in text, text
