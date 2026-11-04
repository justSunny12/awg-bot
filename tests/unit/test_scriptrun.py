"""infra/scriptrun.run_script — запуск скрипта обвязки одним способом — и пять
обёрток поверх него (зона резерва, зонды кворума, линк шлюза к резерву,
резолвер пользователей, резолвер админа).

Что стережём: окружение — процесса плюс добавка (строками), секрет — только
через stdin; вывод — stdout+stderr одним текстом; не запустился или завис —
исключение класса владельца «<скрипт> не запустился»; ненулевой код — то же
исключение с последними строками вывода (без вывода — «код N»); у режима
status резолверов код — состояние, не отказ. У каждой обёртки — прежние класс
ошибки, слова «не запустился» и длина хвоста.

Цена ошибки: таймаут, вырвавшийся голым TimeoutExpired, ронял такт монитора
мимо обработчика класса владельца; ненулевой код, принятый за успех, — бот
считает зону, зонд или линк поднятыми; хвост не той длины — в уведомлении
админу не та строка «ОШИБКА: …»; окружение без os.environ — скрипт без PATH.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from awgbot.infra import scriptrun

pytestmark = pytest.mark.unit


class _Err(RuntimeError):
    pass


class _Proc:
    def __init__(self, monkeypatch, rc=0, out=b"", err=b"", exc=None):
        self.calls = []
        self.rc, self.out, self.err, self.exc = rc, out, err, exc

        def fake(args, **kw):
            self.calls.append((list(args), kw))
            if self.exc:
                raise self.exc
            return types.SimpleNamespace(returncode=self.rc, stdout=self.out, stderr=self.err)
        monkeypatch.setattr(subprocess, "run", fake)


def test_output_env_and_stdin(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    p = _Proc(monkeypatch, out=b"  a\n", err=b"b  \n")
    assert scriptrun.run_script(["sh", "x.sh", "--up"], error=_Err, what="скрипт", timeout=5,
                                env={"N": 3}, stdin="DUMMY-secret") == "a\nb"
    args, kw = p.calls[0]
    assert args == ["sh", "x.sh", "--up"] and kw["timeout"] == 5 and kw["capture_output"] is True
    assert kw["env"]["PATH"] == "/usr/bin" and kw["env"]["N"] == "3", "окружение — процесса плюс добавка строками"
    assert kw["input"] == b"DUMMY-secret" and "DUMMY-secret" not in " ".join(args)
    scriptrun.run_script(["sh"], error=_Err, what="скрипт", timeout=5)
    assert p.calls[1][1]["input"] is None


@pytest.mark.parametrize("exc", [FileNotFoundError("sh"), subprocess.TimeoutExpired("sh", 5), PermissionError(13, "x")])
def test_a_script_that_does_not_start_or_hangs_is_the_owners_error(monkeypatch, exc):
    _Proc(monkeypatch, exc=exc)
    with pytest.raises(_Err, match="^скрипт X не запустился: ") as e:
        scriptrun.run_script(["sh"], error=_Err, what="скрипт X", timeout=5)
    assert e.value.__cause__ is exc


def test_a_nonzero_code_is_the_tail_of_the_output_or_the_code(monkeypatch):
    _Proc(monkeypatch, rc=1, out=b"1\n2\n3\n", err=b"4\n5\n")
    with pytest.raises(_Err) as e:
        scriptrun.run_script(["sh"], error=_Err, what="с", timeout=5)
    assert str(e.value) == "3\n4\n5"
    with pytest.raises(_Err) as e:
        scriptrun.run_script(["sh"], error=_Err, what="с", timeout=5, tail=4)
    assert str(e.value) == "2\n3\n4\n5"
    _Proc(monkeypatch, rc=7)
    with pytest.raises(_Err, match="^код 7$"):
        scriptrun.run_script(["sh"], error=_Err, what="с", timeout=5)
    _Proc(monkeypatch, rc=3, out=b"inactive\n")
    assert scriptrun.run_script(["sh"], error=_Err, what="с", timeout=5, check=False) == "inactive"


# ── обёртки: класс ошибки, «не запустился», хвост, status ───────────────────

def _wrappers():
    from awgbot.infra import admindns, quoprobe, resolver, rsvzone
    from awgbot.infra.gwguard import rsv
    return [
        # (вызов, класс, слова отказа запуска, длина хвоста, status без отказа)
        (lambda m: rsvzone.run(m), rsvzone.ZoneError, "скрипт зоны не запустился", 4, False),
        (lambda m: quoprobe.run(m), quoprobe.QuoError, "скрипт зондов не запустился", 3, False),
        (lambda m: rsv.run(m), rsv.RsvError, "скрипт линка к резерву не запустился", 3, False),
        (lambda m: admindns.run(m), admindns.AdminDnsError, "скрипт резолвера администратора не запустился", 3, True),
        (lambda m: resolver.run(m), resolver.ResolverError, "скрипт резолвера не запустился", 4, True),
    ]


@pytest.mark.parametrize("i", range(5), ids=["zone", "quorum-probes", "reserve-link", "admin-dns", "resolver"])
def test_each_wrapper_keeps_its_error_words_and_tail(monkeypatch, i):
    call, err, words, tail, status_ok = _wrappers()[i]
    _Proc(monkeypatch, exc=subprocess.TimeoutExpired("sh", 1))
    with pytest.raises(err, match=words):
        call("install")
    _Proc(monkeypatch, rc=1, out=b"1\n2\n3\n4\n5\n")
    with pytest.raises(err) as e:
        call("install")
    assert str(e.value).splitlines() == ["1", "2", "3", "4", "5"][-tail:]
    if status_ok:
        assert call("status") == "1\n2\n3\n4\n5", "код status — состояние, не отказ"
    else:
        with pytest.raises(err):
            call("status")
