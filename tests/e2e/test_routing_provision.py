"""
Развёртывание условной маршрутизации из бота (docs/ROADMAP.md, п.8).

Раньше это были три команды в SSH из README плюс правка app.yaml руками: каждая
с ключами, которые легко перепутать, и ни одна не проверяла, что предыдущая
отработала. Теперь — кнопка, и она обязана быть честной: не оставлять обвязку
наполовину и не притворяться, что функция заработала без перезапуска.
"""
from __future__ import annotations

import pytest

from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config
from awgbot.domain.services import ServiceError
from tests.conftest import FakeCallback, FakeMessage

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


async def test_section_offers_provisioning_before_anything_exists(services, fake_bot, monkeypatch):
    monkeypatch.setattr(config, "ROUTING_ENABLED", False)
    monkeypatch.setattr(services, "routing_provisioned", lambda: False)
    text, markup = await sh._screen("rt", services)
    assert text.startswith("🇷🇺 РФ-доступ не развёрнут\n"), text
    assert "dnsmasq" in text and "линк" in text, "сказано, что именно сделает кнопка"
    rows = [[b.text for b in row] for row in markup.inline_keyboard]
    assert rows == [["🚀 Развернуть"], ["⬅️ В меню"]], "одно действие и выход — больше тут делать нечего"
    assert SetCB.unpack(markup.inline_keyboard[0][0].callback_data) == SetCB(sec="rt", act="do", key="provision")


async def test_provisioned_but_asleep_says_how_to_wake_and_offers_only_the_exit(services, monkeypatch):
    """Обвязка есть, но интерфейс линка читается при старте — до перезапуска
    функция спит: экран предлагает перезапустить сейчас или позже и не
    предлагает кнопок, которые ничего не сделают."""
    monkeypatch.setattr(config, "ROUTING_ENABLED", False)
    monkeypatch.setattr(services, "routing_provisioned", lambda: True)
    text, markup = await sh._screen("rt", services)
    assert text == "🇷🇺 Обвязка развёрнута, функция ждёт перезапуска бота"
    assert [[b.text for b in row] for row in markup.inline_keyboard] == [["🔁 Перезапустить сейчас"], ["⬅️ Позже"]]
    assert SetCB.unpack(markup.inline_keyboard[0][0].callback_data) == SetCB(sec="svc", act="do", key="bot!")


async def test_provisioning_offers_a_restart_after_success(services, fake_bot, monkeypatch):
    """Интерфейс линка читается при старте: без перезапуска функция осталась бы
    спящей — итог предлагает перезапустить сейчас или позже, сам бот не
    перезапускается (момент выбирает человек)."""
    calls = []
    monkeypatch.setattr(services, "routing_provision", lambda: calls.append("go") or "хвост вывода")
    monkeypatch.setattr(services, "set_restart_wait", lambda c, m: calls.append("wait"))
    monkeypatch.setattr(services, "restart_bot", lambda: calls.append("restart"))
    # кнопка «🚀 Развернуть» по порядку фильтров попадает в routing_action
    # (sec="rt", act="do") — он и обязан её обработать, а не промолчать
    from tests.e2e.test_settings_layout import _first_matching_handler
    assert _first_matching_handler(sh.router, SetCB(sec="rt", act="do", key="provision")) == "routing_action"
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="provision"), services)
    assert calls == ["go"], "перезапуск — только по кнопке"
    assert cb.answers == [("Разворачиваю…", False)], "ответ на колбэк — ровно один"
    done = [s for s in nav.sent if s[0] == "answer"]
    assert done and done[-1][1].startswith("✅ Обвязка развёрнута")
    assert "назначь шлюз — «🛰 Шлюзы» на главной" in done[-1][1], "следующий шаг и где его искать названы"
    assert [[b.text for b in row] for row in done[-1][2].inline_keyboard] == [["🔁 Перезапустить сейчас"], ["⬅️ Позже"]]


async def test_provisioning_failure_shows_the_reason_and_does_not_restart(services, fake_bot, monkeypatch):
    calls = []

    def boom():
        raise ServiceError("обвязка хоста не отработал:\nнет пакета dnsmasq")
    monkeypatch.setattr(services, "routing_provision", boom)
    monkeypatch.setattr(services, "restart_bot", lambda: calls.append("restart"))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="rt", act="do", key="provision"), services)
    assert calls == [], "перезапуск после неудачи не нужен"
    said = [s[1] for s in nav.sent if s[0] == "answer"]
    assert said and "не развёрнута" in said[-1] and "dnsmasq" in said[-1]


def test_provision_runs_the_scripts_in_order_and_stops_on_the_first_failure(services, monkeypatch, tmp_path):
    """Полуразвёрнутая обвязка хуже отсутствующей: она выглядит рабочей."""
    import subprocess
    seen = []

    class CP:
        def __init__(self, rc): self.returncode, self.stdout, self.stderr = rc, b"ok", b""

    def fake_run(argv, **kw):
        seen.append(argv)
        if argv[:2] == ["systemctl", "list-unit-files"]:
            cp = CP(0); cp.stdout = b"dnsmasq.service enabled"; return cp
        return CP(1 if "routing-link-setup.sh" in " ".join(argv) else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sh.settings, "set_value", lambda k, v: [k])
    with pytest.raises(ServiceError, match="линк до шлюза"):
        services.routing_provision()
    ran = [" ".join(a) for a in seen if a[0] == "sh"]
    assert ran[0].endswith("--apply") and "routing-host-setup.sh" in ran[0]
    assert "--install-unit" in ran[1], "обвязка закрепляется от ребута"
    assert "routing-link-setup.sh" in ran[2]
    assert not any("apt-get" in " ".join(a) for a in seen), "dnsmasq уже стоял"


def test_provision_installs_dnsmasq_when_only_the_binary_is_present(services, monkeypatch):
    """Пакет dnsmasq-base даёт бинарь без юнита — обвязка на нём честно
    останавливается, и человек оставался с тупиком «СТОП» в выводе."""
    import subprocess
    seen = []

    class CP:
        def __init__(self, rc=0, out=b""): self.returncode, self.stdout, self.stderr = rc, out, b""

    def fake_run(argv, **kw):
        seen.append(" ".join(argv))
        if argv[:2] == ["systemctl", "list-unit-files"]:
            return CP(0, b"")
        return CP(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sh.settings, "set_value", lambda k, v: [k])
    services.routing_provision()
    assert any(c.startswith("apt-get install -y --no-install-recommends dnsmasq") for c in seen)


def test_provisioning_writes_the_interface_and_enables_the_feature(services, monkeypatch):
    """Финальный эффект развёртывания: интерфейс линка в конфиге и включённая
    функция. Выпади запись — скрипты отработали, хост настроен, а раздел снова
    показывает «обвязка не развёрнута», и кнопки «Назначить шлюз», на которую
    отправляет итоговое сообщение, в нём нет."""
    import subprocess
    from awgbot.infra import routing as infra_routing
    written: dict = {}
    invalidated = []

    class CP:
        def __init__(self, rc=0, out=b"dnsmasq.service enabled"):
            self.returncode, self.stdout, self.stderr = rc, out, b""

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: CP())
    monkeypatch.setattr(sh.settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    monkeypatch.setattr(infra_routing, "invalidate_self_check",
                        lambda *a, **k: invalidated.append(1))
    services.routing_provision()
    assert written["app.routing.gw_interface"] == services._RT_LINK_IF
    assert written["app.routing.enabled"] is True
    assert invalidated, "кэш вердикта не сброшен — функция считалась бы спящей ещё минуту"


def test_provisioned_flag_reads_the_config(services, monkeypatch):
    monkeypatch.setattr(sh.settings, "get", lambda k, d=None: "" if k.endswith("gw_interface") else d)
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "")
    assert services.routing_provisioned() is False
    monkeypatch.setattr(sh.settings, "get", lambda k, d=None: "awglink" if k.endswith("gw_interface") else d)
    assert services.routing_provisioned() is True
