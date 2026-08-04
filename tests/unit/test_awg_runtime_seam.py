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


# ── контейнерное плечо условной маршрутизации ────────────────────────────────

def test_nat_exempt_is_a_noop_on_host(monkeypatch):
    """Исключения из MASQUERADE — плата за чужой netns, и только за него.

    На хосте MASQUERADE один и наш; клиент доходит до маркировки с настоящим
    адресом. Попытайся бот всё равно собрать цепочку — он полез бы `docker exec`
    в несуществующий контейнер и уронил бы реконсиляцию целиком, а с ней и всю
    условную маршрутизацию.
    """
    from awgbot.infra import routing

    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    called: list = []
    monkeypatch.setattr(routing, "_cont", lambda *a, **k: called.append(a))

    routing.sync_nat_exempt(["10.8.1.5", "10.8.1.6"])
    assert called == [], "на хосте в контейнер ходить незачем"


def test_nat_exempt_still_builds_the_chain_in_docker_mode(monkeypatch):
    from awgbot.infra import routing

    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    seen: list[list[str]] = []
    monkeypatch.setattr(routing, "_cont",
                        lambda args, **k: seen.append(list(args)) or _cp())
    monkeypatch.setattr(routing, "_cont_ok", lambda args: True)

    routing.sync_nat_exempt(["10.8.1.5"])
    flat = [" ".join(a) for a in seen]
    assert any("-A " + config.ROUTING_NAT_CHAIN in f or config.ROUTING_NAT_CHAIN in f
               for f in flat), "цепочка исключений в docker-режиме обязана собираться"
    assert any("10.8.1.5/32" in f for f in flat)


def test_static_plumbing_does_not_inspect_docker_on_host(monkeypatch):
    """Сверка маршрута с адресом контейнера на хосте бессмысленна.

    Там маршрут до клиентской подсети connected через awg-интерфейс, переезжать
    ему некуда. Оставь мы вызов `docker inspect` — он вернул бы ошибку, а код
    трактует непустой вывод как «адрес контейнера», и фича могла бы погаснуть
    из-за сравнения с мусором.
    """
    from awgbot.infra import routing

    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    seen: list[list[str]] = []

    def fake_host(args, check=True, input_data=None, timeout=20):
        seen.append(list(args))
        if args[:3] == ["ip", "route", "show"]:
            return _cp(b"10.8.1.0/24 dev awg0 proto kernel scope link src 10.8.1.0\n")
        if args[:2] == ["systemctl", "list-unit-files"]:
            return _cp(f"{config.ROUTING_DNSMASQ_SERVICE}.service enabled".encode())
        return _cp()

    monkeypatch.setattr(routing, "_host", fake_host)
    monkeypatch.setattr(routing, "_host_ok", lambda args: True)
    routing._check_static_plumbing()

    assert not any(a and a[0] == "docker" for a in seen), \
        "docker inspect в host-режиме не должен звучать вовсе"


# ── fail-closed SSH: форма правила зависит от режима ─────────────────────────

def test_ssh_failsafe_targets_the_gateway_in_container(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    line = awg._ssh_failsafe_postup()
    assert "/^default/" in line, "в контейнере дефолтный шлюз и есть хост"
    assert "addrtype" not in line


def test_ssh_failsafe_targets_local_addresses_on_host(monkeypatch):
    """На хосте дефолтный шлюз — роутер провайдера, а не мы.

    Контейнерная форма там встала бы, вернула ноль и выглядела бы на месте, не
    закрыв ничего: fail-closed стал бы fail-open без единого признака. Поэтому
    цель задаётся признаком «локальный адрес», а не вычисленным адресом.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    line = awg._ssh_failsafe_postup()
    assert "--dst-type LOCAL" in line
    assert "/^default/" not in line, "шлюз на хосте — чужая машина"


def test_ssh_failsafe_never_fails_the_bringup(monkeypatch):
    """awg-quick прерывает подъём, если PostUp вернул не ноль.

    Строка обязана всегда завершаться успехом — иначе отсутствие xt_addrtype или
    iptables уронило бы весь сервер вместо потери одной защитной строки.
    """
    for mode in ("docker", "host"):
        monkeypatch.setattr(config, "AWG_RUNTIME", mode)
        assert awg._ssh_failsafe_postup().rstrip().endswith("; true"), mode


def test_failsafe_is_rewritten_when_it_no_longer_matches(monkeypatch):
    """Сверяем содержимое, а не наличие маркера.

    Проверка «есть ли в header слово AWGBOT_SSH» молча консервировала старый
    ssh_port после его смены и контейнерную форму правила после переезда. Оба
    случая выглядели как исправная защита.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    stale = ("[Interface]\nAddress = 10.8.1.0/24\n"
             "PostUp = iptables -A AWGBOT_SSH -i awg0 -d 1.2.3.4 "
             "-p tcp --dport 22 -j DROP; true\n")
    written: dict = {}
    monkeypatch.setattr(awg, "read_file", lambda p: stale + "\n[Peer]\nPublicKey = x\n")
    monkeypatch.setattr(awg, "write_file",
                        lambda p, c: written.update(conf=c))
    monkeypatch.setattr(awg, "_backup_conf", lambda: None)

    assert awg.ensure_ssh_failsafe() is True
    conf = written["conf"]
    assert conf.count("AWGBOT_SSH") >= 1
    assert "--dst-type LOCAL" in conf, "должна встать актуальная форма"
    assert "-d 1.2.3.4" not in conf, "устаревшая строка обязана исчезнуть, а не остаться рядом"
