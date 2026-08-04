"""
Шов между контейнером и хостом: config.AWG_RUNTIME.

Переезд с контейнера Amnezia на хост (docs/ROADMAP.md, шаг 2) переключается
одной настройкой, потому что весь доступ к awg сведён в `_exec`. Здесь
проверяется, что шов действительно один и что недоделанная половина падает
громко, а не деградирует молча.
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.core import config
from awgbot.infra import awg

pytestmark = pytest.mark.unit


def _cp(stdout=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=b"")


@pytest.fixture
def calls(monkeypatch):
    """Перехват самого низа: что реально ушло в subprocess."""
    seen: list[list[str]] = []

    def fake_run(args, input_data=None, timeout=15, check=True):
        seen.append(list(args))
        return _cp()

    monkeypatch.setattr(awg, "_run", fake_run)
    return seen


# ── переключение режима ──────────────────────────────────────────────────────

def test_docker_mode_wraps_the_command(monkeypatch, calls):
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    awg._exec(["awg", "show", "awg0"])
    assert calls[0][:3] == ["docker", "exec", config.CONTAINER]
    assert calls[0][3:] == ["awg", "show", "awg0"]


def test_host_mode_runs_the_command_as_is(monkeypatch, calls):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    awg._exec(["awg", "show", "awg0"])
    assert calls[0] == ["awg", "show", "awg0"], "в host-режиме docker не при чём"


def test_stdin_variant_switches_too(monkeypatch, calls):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    awg._exec_i(["sh", "-c", "cat > /x"], input_data=b"data")
    assert calls[0] == ["sh", "-c", "cat > /x"]


def test_mode_is_read_at_call_time_not_at_import(monkeypatch, calls):
    """Режим — функция, а не константа модуля.

    Заморозь его на импорте — и тест, подменяющий config.AWG_RUNTIME, проверял бы
    не то, что выполняется в бою.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    assert awg.in_container() is False
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    assert awg.in_container() is True


# ── шов ровно один ───────────────────────────────────────────────────────────

def test_docker_exec_appears_only_in_the_seam():
    """`docker exec` не должен расползаться по модулю.

    Расползись он — переезд перестал бы быть переключением настройки и стал бы
    поиском по коду, а забытое место деградировало бы молча.
    """
    import pathlib

    src = pathlib.Path(awg.__file__).read_text(encoding="utf-8")
    lines = [ln.strip() for ln in src.splitlines()
             if '"docker", "exec"' in ln]
    # _exec, _exec_i и detect_topology (её зовёт установщик до финализации
    # конфига, с именем контейнера параметром) — больше нигде.
    assert len(lines) == 3, f"docker exec вне шва: {lines}"


def test_docker_exec_is_absent_from_the_rest_of_the_codebase():
    import pathlib

    root = pathlib.Path(awg.__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "awg.py":
            continue
        text = path.read_text(encoding="utf-8")
        if '"docker", "exec"' in text or "'docker', 'exec'" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"docker exec мимо awg._exec: {offenders}"


# ── непортированное падает громко ────────────────────────────────────────────

@pytest.mark.parametrize("call", [
    lambda: awg.restart_container(),
    lambda: awg.container_pid(),
])
def test_docker_only_helpers_refuse_host_mode(monkeypatch, calls, call):
    """Что осталось docker-только — падает громко.

    Заслон бросает HostModeUnsupported, а не AwgError: эти функции сами глотают
    AwgError и вернули бы None/False, то есть «контейнер лежит». В host-режиме
    это увело бы чинить несуществующее.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    with pytest.raises(awg.HostModeUnsupported):
        call()


# ── портированное работает в обоих режимах ───────────────────────────────────

def test_ssh_targets_use_the_awg_interface_on_host(monkeypatch):
    """На хосте адрес, по которому виден хост из туннеля, — это адрес awg0.

    Молчаливо пропустить его нельзя: список сузился бы до egress-адреса, SSH
    остался бы открыт с туннельного адреса хоста, и ни один признак поломки не
    появился бы. Поэтому чтение адреса интерфейса идёт с check=True.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    seen: list[list[str]] = []

    def fake_run(args, input_data=None, timeout=15, check=True):
        seen.append(list(args))
        if args[:2] == ["ip", "-4"]:
            return _cp(b"3: awg0    inet 10.8.1.0/24 brd 10.8.1.255 scope global awg0\\\n")
        return _cp(b"1.1.1.1 via 10.0.0.1 dev eth0 src 203.0.113.7 uid 0\n")

    monkeypatch.setattr(awg, "_run", fake_run)
    targets = awg.host_ssh_targets()

    assert "10.8.1.0" in targets, "адрес awg-интерфейса обязан попасть в цели"
    assert "203.0.113.7" in targets, "egress нужен для hairpin через публичный IP"
    assert not any("docker" in " ".join(a) for a in seen)


def test_service_started_at_is_boot_time_on_host(monkeypatch):
    """На хосте метка старта — время загрузки системы, а не подъёма интерфейса.

    Наши цепочки живут в хостовых таблицах и переживают awg-quick down/up.
    Возьми мы момент подъёма интерфейса — реконсиляция гонялась бы впустую при
    каждом рестарте туннеля.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(awg, "_boot_time_iso", lambda: "2026-08-04T09:00:00Z")
    assert awg.service_started_at() == "2026-08-04T09:00:00Z"

    from awgbot.util import timeutil
    assert timeutil.parse_docker_time(awg.service_started_at()) is not None, \
        "формат обязан разбираться тем же парсером, что и StartedAt докера"


def test_restart_server_uses_awg_quick_on_host(monkeypatch, calls):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    awg.restart_server()
    assert calls[0][:2] == ["awg-quick", "down"]
    assert calls[1][:2] == ["awg-quick", "up"]


def test_container_pid_does_not_swallow_the_guard(monkeypatch, calls):
    """container_pid глотает AwgError и вернул бы None — «контейнер не найден».

    В host-режиме это неотличимо от «контейнер лежит», и вотчдог пошёл бы чинить
    несуществующее. Заслон обязан пробиваться наружу.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    with pytest.raises(awg.HostModeUnsupported):
        awg.container_pid()


# ── идентификатор протокола расцеплен с docker-именем ────────────────────────

def test_vpn_link_carries_app_container_not_docker_name(monkeypatch):
    """В vpn:// уезжает APP_CONTAINER.

    После переезда docker-имя исчезнет. Останься оно источником значения для
    ссылок — первая же уборка конфига обесценила бы все выданные профили: их
    просто перестало бы опознавать приложение.
    """
    from awgbot.domain import configgen

    monkeypatch.setattr(config, "CONTAINER", "docker-имя-которое-умрёт")
    monkeypatch.setattr(config, "APP_CONTAINER", "amnezia-awg2")

    obj = configgen._build_vpn_json(
        "priv", "pub", "10.8.1.5", {}, "spub", "psk", "example.org", 42755)

    assert obj["defaultContainer"] == "amnezia-awg2"
    assert obj["containers"][0]["container"] == "amnezia-awg2"
    assert "docker-имя-которое-умрёт" not in str(obj)


def test_app_container_defaults_to_the_docker_name():
    """Без ключа в yaml значение обязано совпадать с docker-именем.

    Боевой app.yaml при обновлении не мигрирует, и новый ключ до сервера не
    доедет. Разойдись дефолты — ссылки поехали бы у всех после обновления бота.
    """
    import pathlib

    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    assert 'APP_CONTAINER = _docker.get("app_container") or CONTAINER' in src


# ── неизвестный режим не должен молча означать docker ────────────────────────

def test_unknown_runtime_is_rejected(monkeypatch):
    """Опечатка не должна возвращать docker-режим.

    Сервер, который уже переехал, начал бы ходить `docker exec` в
    несуществующий контейнер и слёг бы целиком, а выглядело бы это как «awg не
    отвечает» — то есть увело бы чинить не туда.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "hosts")
    monkeypatch.setattr(config, "BOT_TOKEN", "x")
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "SERVER_HOST", "h")
    monkeypatch.setattr(config, "SERVER_PORT", 1)
    with pytest.raises(RuntimeError, match="runtime"):
        config.validate()


def test_preflight_refuses_unfinished_host_mode(monkeypatch, tmp_path):
    """Пока перенос не доделан, host-режим не стартует.

    Иначе функции отваливались бы поодиночке — статус сервиса, SSH-фильтр,
    вотчдог, — и каждую пришлось бы диагностировать отдельно.
    """
    from awgbot.runtime import preflight

    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "нет.db")
    with pytest.raises(preflight.PreflightError, match="ROADMAP"):
        preflight.check_fatal()
