"""
Разделы «🖥 Сервер AWG» и «🛡 SSH-доступ» в настройках (docs/ROADMAP.md, п.8).

Это то, что раньше спрашивал установщик и делал CLI. Правки уезжают в новые
ссылки, а включение фильтра может запереть SSH — оба экрана обязаны говорить
об этом прямо и проверять ввод до записи.

Экраны разделов, подготовки переезда, резолвера и порта SSH в снятых
состояниях сверяет эталон (adm.set.srv*, adm.set.fw*, adm.set.dns*,
adm.set.mig_prep*, adm.set.svc*), в том числе отказы nft и sshd, кнопки
таймера CLI, устаревший список и предупреждения раздела; здесь — проверка
ввода, побочные эффекты и порядок обработчиков.
"""
from __future__ import annotations

import pytest

from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.bot.handlers import settingscore as core
from awgbot.core import config, settings
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


# ── сервер ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, ok", [
    ("203.0.113.10", True), ("vpn.example.org", True),
    ("не адрес", False), ("", False), ("10.8.1.300", False),
])
def test_server_host_is_validated_before_it_reaches_a_link(raw, ok):
    valid, err = core._validate_server_value("app.network.server_host", raw)
    assert valid is ok
    assert valid or err


def test_dns_accepts_one_or_two_addresses():
    v = core._validate_server_value
    assert v("app.client_config.dns1", "10.8.1.1")[0]
    assert v("app.client_config.dns1", "10.8.1.1, 1.1.1.1")[0]
    assert not v("app.client_config.dns1", "1.1.1.1 1.0.0.1 8.8.8.8")[0]
    assert not v("app.client_config.dns1", "резолвер")[0]


async def test_single_dns_is_written_to_both_fields(services, fake_bot, monkeypatch):
    """Публичный адрес вторым номером вернул бы утечку резолва мимо dnsmasq:
    стеки опрашивают список не строго по порядку."""
    written: dict = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    monkeypatch.setattr(services, "server_screen", lambda: {
        "host": "h", "name": "n", "dns": "10.8.1.1", "mtu": 1376, "keepalive": "25",
        "iface": "awg0", "port": 1, "subnet": "s", "kernel": "", "generation": 1})
    st = FakeState()
    await st.update_data(key="app.client_config.dns1", sec="srv")
    msg = FakeMessage(text="10.8.1.1", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.receive_value(msg, st, services)
    assert written["app.client_config.dns1"] == "10.8.1.1"
    assert written["app.client_config.dns2"] == "10.8.1.1"


# ── файервол ─────────────────────────────────────────────────────────────────

def _fw(**kw):
    base = {"enabled": False, "present": True, "rollback": False, "ufw": False,
            "ssh_port": 22, "allow": [], "raw_allow": [], "unresolved": [],
            "admin_ips": ["10.8.1.2"], "nat": True}
    base.update(kw)
    return base


async def test_enable_applies_at_once_and_cli_timer_buttons_still_work(services, fake_bot, monkeypatch):
    """Из чата — сразу, без таймера и без «подтверди». Кнопки confirm/rollback
    рисует CLI (его таймер остался) — бот их по-прежнему обслуживает."""
    calls = []
    monkeypatch.setattr(services, "firewall_enable", lambda: calls.append("on"))
    monkeypatch.setattr(services, "firewall_confirm", lambda: calls.append("confirm") or True)
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(enabled=True, raw_allow=["203.0.113.7"]))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="on"), services)
    assert calls == ["on"], "фильтр не включён или включён дважды"
    # предупреждение alert'ом и экран после включения — снимок adm.set.fw.on.do;
    # «Таймер снят» — снимок adm.set.fw.confirm
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="confirm"), services)
    assert calls == ["on", "confirm"], "кнопка таймера CLI не сняла таймер"


async def test_rollback_now_disarms_and_removes_the_filter(services, fake_bot, monkeypatch):
    """«Откатить сейчас» с кнопки CLI: таймер снят, фильтр убран (итог —
    снимок adm.set.fw.rollback)."""
    calls = []
    monkeypatch.setattr(services, "firewall_confirm", lambda: calls.append("disarm") or True)
    monkeypatch.setattr(services, "firewall_disable", lambda: calls.append("off") or ["снято"])
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="rollback"), services)
    assert calls == ["disarm", "off"], "сначала снять таймер, иначе он сработает по пустому"


async def test_adding_a_bad_address_is_refused_without_writing(services, fake_bot, monkeypatch):
    """Отказ «не адрес» (снимок adm.set.fw.add.bad) оставляет ввод открытым."""
    from awgbot.domain.services import ServiceError
    seen = []

    def add(raw):
        seen.append(raw)
        raise ServiceError("«мусор» не адрес, не подсеть и не имя")
    monkeypatch.setattr(services, "firewall_allow_add", add)
    st = FakeState()
    await st.update_data(key="app.firewall.ssh_allow", sec="fw")
    msg = FakeMessage(text="мусор", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.receive_value(msg, st, services)
    assert seen == ["мусор"]
    assert await st.get_data(), "ввод остаётся открытым — можно поправить, не начиная заново"

# ── остальные ветки действий файервола ───────────────────────────────────────

async def test_removing_an_address_by_its_number(services, fake_bot, monkeypatch):
    """В кнопке номер записи, а не адрес: двоеточие IPv6 ломало упаковку. Номер
    обязан разрешаться в тот же адрес, что показан в списке."""
    removed: list = []
    monkeypatch.setattr(services, "firewall_screen",
                        lambda: _fw(raw_allow=["203.0.113.7", "2001:db8::1"]))
    monkeypatch.setattr(services, "firewall_allow_remove", lambda e: removed.append(e))
    cb, nav = _acb(fake_bot)
    tag = kb.entry_tag("2001:db8::1")
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="del", val=f"1.{tag}"), services)
    assert removed == ["2001:db8::1"]


async def test_stale_list_does_not_remove_a_neighbour(services, fake_bot, monkeypatch):
    """Список изменился с момента отрисовки — номер указывает уже на другого.
    Удалить соседа молча хуже, чем отказаться (отказ — снимок
    adm.set.fw.del.stale)."""
    removed: list = []
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(raw_allow=["203.0.113.7"]))
    monkeypatch.setattr(services, "firewall_allow_remove", lambda e: removed.append(e))
    for val in ("5", "0", "0.deadbeef"):          # вне списка, без метки, чужая метка
        cb, nav = _acb(fake_bot)
        await sh.do_action(cb, SetCB(sec="fw", act="do", key="del", val=val), services)
        assert removed == [], val


# ── переезд из раздела «Сервер AWG» ─────────────────────────────────────────

async def test_prepare_runs_and_offers_a_restart(services, fake_bot, monkeypatch):
    """Подготовка поднимает интерфейс, а бот не перезапускается сам — только
    по кнопке (итог с кнопками — снимок adm.set.mig_prep.go)."""
    calls: list = []
    monkeypatch.setattr(services, "migration_prepare",
                        lambda port=None: calls.append(port) or
                        {"iface": "awg1", "subnet": "10.9.1.0/24", "port": "443"})
    monkeypatch.setattr(services, "set_restart_wait", lambda c, m: calls.append("wait"))
    monkeypatch.setattr(services, "restart_bot", lambda: calls.append("restart"))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="mig_prep", act="do", key="go", val="443"), services)
    assert calls == [443], "перезапуск — только по кнопке"


async def test_prepare_failure_does_not_restart(services, fake_bot, monkeypatch):
    """Отказ подготовки — без перезапуска (текст — снимок adm.set.mig_prep.go.fail)."""
    calls: list = []

    def boom(port=None):
        raise RuntimeError("порт 443 занят")
    monkeypatch.setattr(services, "migration_prepare", boom)
    monkeypatch.setattr(services, "restart_bot", lambda: calls.append("restart"))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="mig_prep", act="do", key="go"), services)
    assert calls == [], "после отказа бот перезапущен"


# ── свой DNS-резолвер из раздела «Сервер AWG» ────────────────────────────────

def _srv(private_dns: dict, blocked: str = "") -> dict:
    return {"host": "vpn.example.org", "name": "Сервер 1", "dns": "1.1.1.1, 1.0.0.1",
            "mtu": 1376, "keepalive": "25-35", "iface": "awg0", "port": 51820,
            "port_conf": 51820, "subnet": "10.8.1.0/24", "kernel": "v3.1.20260906",
            "generation": 1, "migration_blocked": blocked, "private_dns": private_dns}


@pytest.mark.parametrize("key, decision", [("later", "pending"), ("never", "dismissed")])
async def test_later_and_never_record_the_decision(services, fake_bot, monkeypatch, key, decision):
    """Выбор записан: «при переезде» исполнит следующий переезд, «не нужно»
    гасит предложение. Экраны после выбора — снимки adm.set.dns.later/never."""
    cb, _ = _acb(fake_bot)
    await sh.private_dns_action(cb, SetCB(sec="dns", act="do", key=key), services, FakeState())
    assert services.private_dns_decision() == decision, "решение о резолвере не записано"


async def test_now_records_pending_before_the_migration_preparation(services, fake_bot, monkeypatch):
    """«Переехать сейчас» записывает решение до подготовки: переезд, поднятый
    с этого экрана, выдаст конфиги уже со своим резолвером. Экран подготовки —
    снимок adm.set.dns.now."""
    monkeypatch.setattr(services, "migration_blocked_reason", lambda: "")
    monkeypatch.setattr(services, "migration_prepare_data", lambda want_port=0: {
        "iface": "awg0", "port": 45871, "subnet": "10.8.1.0/24", "clients": 1, "devices": 2,
        "want_port": want_port, "private_dns": True, "blocked": ""})
    cb, nav = _acb(fake_bot)
    await sh.private_dns_action(cb, SetCB(sec="dns", act="do", key="now"), services, FakeState())
    assert services.private_dns_decision() == "pending", "решение о резолвере не записано"
    assert any(s[0] == "edit_text" for s in nav.sent), "подготовка переезда не открылась"


async def test_now_while_a_migration_runs_explains_and_stays(services, fake_bot, monkeypatch):
    """Переезд идёт — решение всё равно записано (объяснение — снимок
    adm.set.dns.now.blocked)."""
    monkeypatch.setattr(services, "migration_blocked_reason", lambda: "идёт переезд")
    monkeypatch.setattr(services, "server_screen", lambda: _srv(
        {"mode": "public", "dns1": "1.1.1.1", "dns2": "1.0.0.1", "target": "10.8.1.1",
         "decision": "pending"}, blocked="идёт переезд"))
    cb, nav = _acb(fake_bot)
    await sh.private_dns_action(cb, SetCB(sec="dns", act="do", key="now"), services, FakeState())
    assert services.private_dns_decision() == "pending", "решение записано — исполнит идущий/следующий переезд"


def test_dns_handler_is_registered_before_the_generic_do_action():
    order = [h.callback.__name__ for h in sh.router.callback_query.handlers]
    assert order.index("private_dns_action") < order.index("do_action")


# ── порт SSH ─────────────────────────────────────────────────────────────────

async def test_port_button_opens_the_port_input(services, fake_bot, monkeypatch):
    """«🅿️ Порт» открывает ввод именно порта: следующее сообщение разбирается
    как порт SSH. Кнопка и приглашение — снимки adm.set.fw, adm.set.fw.port."""
    from awgbot.bot.states import SshPort
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    cb, _ = _acb(fake_bot)
    st = FakeState()
    await sh.ssh_port_ask(cb, st, services)
    assert await st.get_state() == SshPort.value.state, "ввод порта не открыт"


async def test_busy_port_is_refused_with_retry_and_back(services, fake_bot, monkeypatch):
    """Занятый порт — отказ финишером, а не переспрос: ввод закрыт, порт не
    менялся. Текст с именем процесса и кнопки — снимок adm.set.fw.port.busy."""
    changed = []
    monkeypatch.setattr(services, "ssh_port_busy", lambda p: "nginx")
    monkeypatch.setattr(services, "ssh_port_change", lambda p: changed.append(p) or 22)
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    st = FakeState()
    await st.set_state("SshPort:value")
    msg = FakeMessage(text="8443", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert not changed, "занятый порт всё-таки применён"
    assert await st.get_state() is None, "ввод остался открытым после отказа"


async def test_finisher_buttons_reopen_the_prompt_or_the_section(services, fake_bot, monkeypatch):
    """Кнопка финишера: «Другой порт» снова открывает ввод порта (финишер с
    «Скрыть» и приглашение — снимок adm.set.fw.port.retry); «Назад» — ввод
    закрыт (раздел новым сообщением — снимок adm.set.fw.port.back)."""
    from awgbot.bot.states import SshPort
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    cb, nav = _acb(fake_bot)
    st = FakeState()
    await sh.ssh_port_finisher_action(cb, SetCB(sec="fw", act="do", key="port_retry"), st, services)
    assert await st.get_state() == SshPort.value.state, "«Другой порт» не открыл ввод"
    cb, nav = _acb(fake_bot)
    st = FakeState()
    await sh.ssh_port_finisher_action(cb, SetCB(sec="fw", act="do", key="port_back"), st, services)
    assert await st.get_state() is None, "«Назад» оставил ввод порта открытым"


async def test_free_port_is_applied_and_the_input_closes(services, fake_bot, monkeypatch):
    """Свободный порт уходит в sshd ровно один раз, ввод закрыт. Итог первой
    строкой перерисованного раздела — снимок adm.set.fw.port.done."""
    changed = []
    monkeypatch.setattr(services, "ssh_port_busy", lambda p: "")
    monkeypatch.setattr(services, "ssh_port_change", lambda p: changed.append(p) or 22)
    # экран отдаёт текущий порт: до смены 22, после — новый
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(ssh_port=changed[-1] if changed else 22))
    st = FakeState()
    await st.set_state("SshPort:value")
    msg = FakeMessage(text="2222", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert changed == [2222], "порт не применён или применён дважды"
    assert await st.get_state() is None, "ввод остался открытым"


async def test_bad_port_is_asked_again(services, fake_bot, monkeypatch):
    """Порт вне 1–65535 — переспрос, ввод открыт (текст — снимок
    adm.set.fw.port.range; отказ sshd — adm.set.fw.port.refused)."""
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    st = FakeState()
    await st.set_state("SshPort:value")
    msg = FakeMessage(text="70000", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert await st.get_state() == "SshPort:value", "ввод открыт — можно поправить"


async def test_same_port_is_a_finisher_not_a_refusal(services, fake_bot, monkeypatch):
    """Текущий порт формально «занят» (им же sshd) — но человеку это не
    отказ: закрываем ввод, ни проверки занятости, ни смены не зовём. Финишер
    — снимок adm.set.fw.port.same."""
    touched = []
    monkeypatch.setattr(services, "ssh_port_busy", lambda p: touched.append(("busy", p)) or "sshd")
    monkeypatch.setattr(services, "ssh_port_change", lambda p: touched.append(("change", p)) or 22)
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(ssh_port=22))
    st = FakeState()
    await st.set_state("SshPort:value")
    msg = FakeMessage(text="22", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert not touched, f"тот же порт задел sshd: {touched}"
    assert await st.get_state() is None, "ввод остался открытым"


async def test_foreign_owner_refuses_the_port_input(services, fake_bot, monkeypatch):
    """Конфигом sshd владеет другая программа — ввод не открывается (отказ —
    снимок adm.set.fw.port.owner; предупреждения раздела о чужом владельце, о
    расхождении порта sshd и фильтра и о firewalld — adm.set.fw.owner,
    adm.set.fw.drift, adm.set.fw.firewalld)."""
    monkeypatch.setattr(services, "firewall_screen",
                        lambda: _fw(owner="generator", owner_detail="managed by ansible",
                                    owner_files=["/etc/ssh/sshd_config"], listening=22, drift=False))
    cb, _ = _acb(fake_bot)
    st = FakeState()
    await sh.ssh_port_ask(cb, st, services)
    assert await st.get_state() is None, "ввод порта открыт при чужом владельце конфига"
