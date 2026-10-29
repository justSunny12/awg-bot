"""Обёртка скрипта зондов кворума (infra/quoprobe.py): запуск режимов, конфиг
зонда через временный файл, разбор `--status`, каталог конфигов по роли, порт
ответчика из настроек.

Цена ошибки: закрытый ключ зонда в аргументах скрипта или в файле с правами по
умолчанию — читаем всем на хосте; временный файл, оставшийся после `--up`, —
ключ лежит в /tmp; ненулевой код, принятый за успех, — бот считает зонд
поднятым, а голоса нет; разбор `--status`, падающий на мусоре, — такт кворума
стоит; каталог конфигов сервера на шлюзе (или наоборот) — `--wipe` при
удалении пары не находит конфигов, ключи остаются на диске.
"""
from __future__ import annotations

import os
import stat
import subprocess
import types

import pytest

from awgbot.core import config, settings
from awgbot.infra import quoprobe

pytestmark = pytest.mark.unit

CONF = "[Interface]\nAddress = 169.254.88.5/32\nPrivateKey = DUMMY-private\n\n[Peer]\nAllowedIPs = 169.254.88.1/32\n"


class _Proc:
    """subprocess.run моделью: журнал вызовов, код и вывод; `seen` — что лежало
    во временном файле конфига в момент вызова и с какими правами."""

    def __init__(self, monkeypatch, rc=0, out=b"", err=b"", exc=None):
        self.calls: list[tuple[list, dict]] = []
        self.rc, self.out, self.err, self.exc = rc, out, err, exc
        self.seen: list[tuple[str, int]] = []

        def fake(args, **kw):
            self.calls.append((list(args), kw))
            if self.exc:
                raise self.exc
            if "--up" in args:
                p = args[args.index("--up") + 1]
                self.seen.append((open(p, encoding="utf-8").read(), stat.S_IMODE(os.stat(p).st_mode)))
            return types.SimpleNamespace(returncode=self.rc, stdout=self.out, stderr=self.err)
        monkeypatch.setattr(subprocess, "run", fake)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("QUO_CONF_DIR", raising=False)


def test_up_hands_the_conf_through_a_private_temporary_file_and_removes_it(monkeypatch):
    p = _Proc(monkeypatch, out=b"up: ok\n")
    assert quoprobe.up("awgqah", CONF) == "up: ok"
    args, kw = p.calls[0]
    assert args[0] == "sh" and args[1].endswith("install/quorum-probe-setup.sh") and args[2] == "--up", args
    assert p.seen == [(CONF, 0o600)], "конфиг зонда с ключом лёг во временный файл не целиком или не 0600"
    assert not os.path.exists(args[3]), "временный файл с закрытым ключом зонда остался"
    assert "DUMMY-private" not in " ".join(args), "закрытый ключ зонда в аргументах скрипта"
    assert not any("DUMMY-private" in str(v) for v in kw["env"].values()), "ключ зонда в окружении скрипта"
    assert kw["env"]["QUO_IF"] == "awgqah", "имя интерфейса зонда не передано"


def test_up_adds_the_final_newline_the_last_line_needs(monkeypatch):
    p = _Proc(monkeypatch, out=b"up: ok")
    quoprobe.up("awgqah", CONF.rstrip("\n"))
    assert p.seen[0][0].endswith("AllowedIPs = 169.254.88.1/32\n"), "последняя строка конфига без перевода строки"


def test_a_failing_up_raises_and_still_removes_the_temporary_file(monkeypatch):
    p = _Proc(monkeypatch, rc=1, err="строка 1\nстрока 2\nстрока 3\nОШИБКА: интерфейс зонда awgqah не поднялся\n".encode())
    with pytest.raises(quoprobe.QuoError) as e:
        quoprobe.up("awgqah", CONF)
    assert "не поднялся" in str(e.value) and "строка 1" not in str(e.value), "в причине не хвост вывода скрипта"
    assert not os.path.exists(p.calls[0][0][3]), "после отказа временный файл с ключом остался"


@pytest.mark.parametrize("exc", [OSError("нет sh"), subprocess.TimeoutExpired("sh", 1)])
def test_a_script_that_cannot_start_or_hangs_is_a_quo_error(monkeypatch, exc):
    _Proc(monkeypatch, exc=exc)
    with pytest.raises(quoprobe.QuoError, match="не запустился"):
        quoprobe.down("awgqah")


def test_down_and_wipe_run_their_modes(monkeypatch):
    p = _Proc(monkeypatch, out=b"down: ok\n")
    assert quoprobe.down("awgqar") == "down: ok"
    quoprobe.wipe()
    assert [c[0][2:] for c in p.calls] == [["--down", "awgqar"], ["--wipe"]]


def test_status_parses_key_values_and_handshakes(monkeypatch):
    _Proc(monkeypatch, out=b"ns=present\nifaces=awgqah awgqar\nhs_awgqah=37\nconf_awgqah=present\nhs_awgqar=never\n"
                            b"conf_awgqar=present\n")
    assert quoprobe.status() == {"ns": "present", "ifaces": ["awgqah", "awgqar"], "hs": {"awgqah": 37, "awgqar": None}}


def test_status_of_a_failing_script_is_empty_not_an_exception(monkeypatch):
    _Proc(monkeypatch, rc=1, err=b"boom")
    assert quoprobe.status() == {}


def test_status_without_the_namespace(monkeypatch):
    _Proc(monkeypatch, out=b"ns=absent\nifaces=\n")
    assert quoprobe.status() == {"ns": "absent", "ifaces": [], "hs": {}}


def test_status_tolerates_garbage_lines(monkeypatch):
    _Proc(monkeypatch, out=b"\xff\xfe\nns=present\nifaces=awgqah\nhs_awgqah=abc\n" + "мусор\n".encode())
    assert quoprobe.status() == {"ns": "present", "ifaces": ["awgqah"], "hs": {"awgqah": None}}


def test_the_environment_carries_namespace_conf_dir_and_port(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROLE", "main")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    p = _Proc(monkeypatch, out=b"wipe: ok")
    quoprobe.wipe()
    env = p.calls[0][1]["env"]
    assert (env["QUO_NS"], env["QUO_CONF_DIR"], env["QUO_PORT"]) == ("awg-quo", str(tmp_path / "data" / "quorum"), "8789")


@pytest.mark.parametrize("role, want", [("gateway", "/var/lib/awg-gw/quorum"), ("main", "DATA/quorum")])
def test_the_conf_dir_follows_the_role(monkeypatch, tmp_path, role, want):
    monkeypatch.setattr(config, "ROLE", role)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    assert str(quoprobe.conf_dir()) == want.replace("DATA", str(tmp_path))


def test_the_conf_dir_from_the_environment_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("QUO_CONF_DIR", str(tmp_path / "x"))
    assert quoprobe.conf_dir() == tmp_path / "x"


def test_the_port_follows_the_setting_and_falls_back_on_garbage(monkeypatch):
    vals = {"app.reserve.quorum": {"port": 9911}}
    monkeypatch.setattr(settings, "get", lambda k, d=None: vals.get(k, d))
    assert quoprobe.port() == 9911
    vals["app.reserve.quorum"] = {"port": "мусор"}
    assert quoprobe.port() == 8789, "мусор в настройке уронил порт, а не дал умолчание"
    vals["app.reserve.quorum"] = None
    assert quoprobe.port() == 8789


def test_standing_lists_only_probe_confs(monkeypatch, tmp_path):
    monkeypatch.setenv("QUO_CONF_DIR", str(tmp_path))
    for n in ("awgqah.conf", "awgqbr.conf", "awg0.conf", ".awgqah.setconf", "awgqah.conf.bak"):
        (tmp_path / n).write_text("x")
    assert quoprobe.standing() == ["awgqah", "awgqbr"]
    assert quoprobe.present("awgqah") and not quoprobe.present("awgqar")
    assert quoprobe.conf_text("awgqah") == "x" and quoprobe.conf_text("awgqar") == ""


def test_standing_without_the_dir_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("QUO_CONF_DIR", str(tmp_path / "нет"))
    assert quoprobe.standing() == []
