"""Переезд профилей на второй интерфейс: переезд по кнопке — подготовка
второго интерфейса и симметричный запрет.
"""
import pytest

from awgbot.core import config

from tests.integration._migration_common import _seen, _twin

pytestmark = pytest.mark.integration


# ── переезд по кнопке: подготовка и симметричный запрет ──────────────────────

def test_prepare_is_blocked_while_a_generation_migration_runs(services, mig,
                                                              make_active_client, monkeypatch):
    """Переезд по поколению и переезд по кнопке — один механизм, и цель у него
    ровно одна. Затей второй поверх первого, и профили размажутся по трём
    интерфейсам, откуда их нечем мигрировать."""
    from awgbot.domain.migration import ServiceErrorMigration
    from awgbot.infra import awglock
    c = make_active_client(name="c", tg_id=7120)
    services.add_device(c.id, "Тел")
    services.migration_start()
    awglock.write_state(applied=1, target=2)
    assert services.migration_generation_driven() is True
    reason = services.migration_blocked_reason()
    assert "поколение 2" in reason and "завершиться" in reason
    with pytest.raises(ServiceErrorMigration):
        services.migration_prepare()


def test_prepare_is_blocked_while_any_migration_runs(services, mig, make_active_client,
                                                     monkeypatch):
    from awgbot.domain.migration import ServiceErrorMigration
    from awgbot.infra import awglock
    c = make_active_client(name="c", tg_id=7121)
    services.add_device(c.id, "Тел")
    services.migration_start()
    awglock.write_state(applied=1, target=1)
    assert not services.migration_generation_driven()
    assert services.migration_blocked_reason() == "переезд уже идёт"
    with pytest.raises(ServiceErrorMigration):
        services.migration_prepare()


def test_prepare_raises_the_interface_and_writes_the_keys(services, monkeypatch, tmp_path):
    """Подготовка делает ровно две вещи: поднимает интерфейс тем же скриптом,
    что и установщик, и записывает ключи переезда. Без второго рычаг не
    появится, без первого рождать двойников будет негде."""
    import subprocess
    from awgbot.core import settings
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "SUBNET_PREFIX", "10.8.1")
    (tmp_path / "awg0.conf").write_text("Address = 10.8.1.1/24\n", encoding="utf-8")
    written: dict = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = list(argv), dict(kw.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, b"LISTEN_PORT=443\nCREATED=1\n", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)

    res = services.migration_prepare(port=443)
    assert res["iface"] == "awg1" and res["port"] == "443"
    assert res["subnet"] == "10.9.1.0/24", "подсеть выбрана свободная"
    assert seen["argv"][0] == "bash" and seen["argv"][1].endswith("awg-server-init.sh")
    assert seen["env"]["AWG_IF"] == "awg1" and seen["env"]["LISTEN_PORT"] == "443"
    assert seen["env"]["SUBNET_PREFIX"] == "10.9.1"
    assert written["app.awg.migration_interface"] == "awg1"
    assert written["app.awg.migration_subnet_prefix"] == "10.9.1"


def test_prepare_refuses_a_nonsense_port(services, monkeypatch):
    from awgbot.domain.migration import ServiceErrorMigration
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    with pytest.raises(ServiceErrorMigration, match="порт"):
        services.migration_prepare(port=70000)


def test_failed_interface_leaves_the_config_untouched(services, monkeypatch, tmp_path):
    """Скрипт не отработал — ключи не пишем: рычаг, указывающий на
    несуществующий интерфейс, роняет старт переезда, а выглядит как готовность."""
    import subprocess
    from awgbot.core import settings
    from awgbot.domain.migration import ServiceErrorMigration
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
    written: dict = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v))
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", "порт занят".encode()))
    with pytest.raises(ServiceErrorMigration, match="не поднят"):
        services.migration_prepare()
    assert written == {}


def test_finish_prunes_old_kernel_builds_only_after_a_generation_change(services, mig,
                                                                        make_active_client, monkeypatch):
    """Прежняя сборка ядра держится до финала переезда по поколению — и
    убирается ровно на нём. Ручной переезд (то же поколение) сборки не трогает:
    там и убирать нечего."""
    from awgbot.core import settings
    from awgbot.infra import awglock
    pruned: list = []
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    monkeypatch.setattr(services, "_retire_interface", lambda name: None)
    monkeypatch.setattr(services, "_prune_old_kernel_builds", lambda: pruned.append(1))

    c = make_active_client(name="c", tg_id=7130)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    awglock.write_state(applied=1, target=2)
    services.migration_finish()
    assert pruned == [1], "на финале смены поколения прежняя сборка должна уйти"

    pruned.clear()
    c2 = make_active_client(name="c2", tg_id=7131)
    dc2 = services.add_device(c2.id, "Тел")
    services.migration_start()
    _seen(services, _twin(services, dc2.device_id).id, ago_days=0)
    awglock.write_state(applied=2, target=2)
    services.migration_finish()
    assert pruned == [], "ручной переезд сборки ядра не касается"
