"""Переезд профилей на второй интерфейс: финал переезда переставляет
основной интерфейс.
"""
import pytest

from awgbot.core import config

from tests.integration._migration_common import _seen, _twin

pytestmark = pytest.mark.integration


# ── финал переезда переставляет основной интерфейс ───────────────────────────

def test_finish_promotes_the_migration_interface(services, mig, make_active_client, monkeypatch):
    """Без этого шага переезд заканчивался наполовину: пиры на новом
    интерфейсе, а бот по-прежнему считает основным старый — и каждое НОВОЕ
    устройство рождается там, то есть на параметрах, ради ухода от которых всё
    и затевалось."""
    from awgbot.core import settings
    from awgbot.infra import awglock
    c = make_active_client(name="c", tg_id=7101)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)

    written: dict = {}
    retired: list = []
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    monkeypatch.setattr(services, "_retire_interface", lambda name: retired.append(name))
    awglock.write_state(applied=1, target=2)
    monkeypatch.setattr(awglock, "protocol_id", lambda: "amnezia-awg3")

    removed, dropped, failed = services.migration_finish()
    assert not failed and removed

    assert written["app.docker.interface"] == config.MIGRATION_INTERFACE
    assert written["app.network.subnet_prefix"] == config.MIGRATION_SUBNET_PREFIX
    assert written["app.network.subnet_cidr"] == f"{config.MIGRATION_SUBNET_PREFIX}.0/24"
    assert isinstance(written["app.network.server_port"], int)
    assert written["app.docker.app_container"] == "amnezia-awg3", "новое поколение — новый протокол"
    assert written["app.docker.migration_interface"] == ""
    assert written["app.docker.migration_subnet_prefix"] == ""
    assert retired == [config.AWG_INTERFACE], "старый интерфейс не погашен"
    assert awglock.applied_generation() == 2 and awglock.target_generation() == 2
    assert services.pop_promoted_iface() == config.MIGRATION_INTERFACE
    assert services.pop_promoted_iface() == "", "имя отдаётся один раз"


def test_finish_without_generation_change_keeps_the_protocol_id(services, mig,
                                                                make_active_client, monkeypatch):
    """Ручной переезд (ротация обфускации, тот же класс совместимости) протокол
    в ссылках не трогает: значение вморожено в каждую выданную ссылку, менять
    его без нужды значит обнулить уже импортированные профили."""
    from awgbot.core import settings
    from awgbot.infra import awglock
    c = make_active_client(name="c", tg_id=7102)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    written: dict = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    monkeypatch.setattr(services, "_retire_interface", lambda name: None)
    awglock.write_state(applied=2, target=2)
    services.migration_finish()
    assert "app.docker.app_container" not in written
    assert written["app.docker.interface"] == config.MIGRATION_INTERFACE


def test_promotion_is_skipped_when_the_new_port_cannot_be_read(services, mig,
                                                               make_active_client, monkeypatch):
    """Не прочитали параметры нового интерфейса — конфиг НЕ переписываем:
    полуприменённая перестановка оставила бы бота с именем интерфейса от одного
    сервера и портом от другого."""
    from awgbot.core import settings
    from awgbot.infra import awg as infra_awg
    c = make_active_client(name="c", tg_id=7103)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    written: dict = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    monkeypatch.setattr(services, "_retire_interface", lambda name: None)

    def boom(*a, **k):
        raise infra_awg.AwgError("интерфейс не читается")
    monkeypatch.setattr(infra_awg, "read_server_params", boom)
    removed, dropped, failed = services.migration_finish()
    assert not failed and written == {} and services.pop_promoted_iface() == ""


async def test_promotion_ui_tells_and_restarts(services, mig, fake_bot,
                                              make_active_client, monkeypatch):
    """Вторая половина промоушена: сказать и предложить перезапуск кнопками
    «сейчас / позже». Без рестарта бот продолжит считать основным погашенный
    интерфейс, и каждое НОВОЕ устройство родится на мёртвом — поэтому кнопка
    стоит прямо под итогом, а сам бот не перезапускается без спроса."""
    from awgbot.bot.callbacks import SetCB
    from awgbot.bot.handlers import settings as sh
    from awgbot.core import settings
    from tests.conftest import FakeCallback, FakeMessage
    c = make_active_client(name="c", tg_id=7110)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    monkeypatch.setattr(services, "_retire_interface", lambda name: None)
    monkeypatch.setattr(services, "migration_available", lambda: True)
    monkeypatch.setattr(services, "migration_running", lambda: True)
    calls: list = []
    monkeypatch.setattr(services, "set_restart_wait", lambda c_, m: calls.append(("wait", c_, m)))
    monkeypatch.setattr(services, "restart_bot", lambda: calls.append(("restart",)))

    nav = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=config.ADMIN_ID, bot=fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="finish!"), services)

    said = [s for s in nav.sent if s[0] == "answer"]
    assert any("основным интерфейсом стал" in s[1] for s in said), said
    assert calls == [], "перезапуск — только по кнопке"
    last = [s for s in said if "основным интерфейсом стал" in s[1]][-1]
    assert [[b.text for b in row] for row in last[2].inline_keyboard] == [["🔁 Перезапустить сейчас"], ["⬅️ Позже"]]


def test_write_state_failure_does_not_pretend_it_worked(tmp_path, monkeypatch, caplog):
    """Поколение не записалось — это надо увидеть в журнале: иначе переезд
    «завершён», а бот просит его снова при каждом старте."""
    from awgbot.infra import awglock
    monkeypatch.setattr(awglock, "STATE_PATH", tmp_path / "нет-каталога" / "awg.state")
    monkeypatch.setattr(awglock.Path, "mkdir",
                        lambda self, **kw: (_ for _ in ()).throw(OSError("только чтение")))
    with caplog.at_level("WARNING"):
        awglock.write_state(applied=2, target=0)
    assert any("awg.state" in r.message for r in caplog.records)


def test_partial_config_write_during_promotion_is_reported(services, mig,
                                                           make_active_client, monkeypatch, caplog):
    """Часть ключей записалась, часть нет — конфиг с новым интерфейсом и старым
    портом. Промоушен обязан сказать об этом, а не вернуть «всё хорошо»."""
    from awgbot.core import settings
    c = make_active_client(name="c", tg_id=7111)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    written: list = []

    def flaky(key, value):
        written.append(key)
        if key == "app.network.server_port":
            raise RuntimeError("ruamel: файл занят")
        return [key]
    monkeypatch.setattr(settings, "set_value", flaky)
    monkeypatch.setattr(services, "_retire_interface", lambda name: None)
    with caplog.at_level("WARNING"):
        removed, dropped, failed = services.migration_finish()
    assert not failed and removed
    assert services.pop_promoted_iface() == "", "полуприменённая перестановка выдана за успех"
    assert any("app.yaml" in r.message for r in caplog.records)


def test_retire_interface_lowers_and_disables(monkeypatch):
    """Старый интерфейс гасится и снимается с автозагрузки — иначе он поднимется
    после ребута пустым и будет держать порт."""
    import subprocess
    from awgbot.domain.migration import MigrationMixin
    seen: list = []
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: seen.append(list(argv)) or
                        subprocess.CompletedProcess(argv, 0, b"", b""))
    MigrationMixin._retire_interface("awg0")
    assert seen == [["awg-quick", "down", "awg0"],
                    ["systemctl", "disable", "awg-quick@awg0"]]
