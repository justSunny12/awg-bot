"""Второй адрес интерфейса AWG сервера — ответчик кворума резерва
(infra/awg/addrs.py): отдельной строкой `Address` сразу после основной, без
комментария (инвариант записи концепта «кворум» §3.1); снятие; запись конфига
с бэкапом и `ip addr replace`/`ip addr del` на живом интерфейсе.

Цена ошибки: второй адрес в одной строке с основным или с комментарием в
конце — читатели первой строки (`awg-server-init.sh`, часть VPN 4.0.x,
`extract_param`) видят «169.254.88.1/26 # …» или чужой адрес, и
`ip_interface` падает — подсеть пользователей не определяется; второй адрес
перед основным — прежние читатели берут адрес блока зондов как подсеть
пользователей; строка, добавленная дважды, — awg-quick не поднимает
интерфейс; конфиг, переписанный без бэкапа, — откат правки пира некуда.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config
from awgbot.infra import awg

pytestmark = pytest.mark.unit

CONF = ("[Interface]\nPrivateKey = DUMMY\nAddress = 10.8.1.1/24\nListenPort = 43125\nJc = 5\n\n"
        "[Peer]\nPublicKey = DUMMY-PUB\nAllowedIPs = 10.8.1.2/32\n")


def test_the_second_address_goes_right_after_the_first_as_its_own_line():
    out, added = awg.with_second(CONF, "169.254.88.1/26")
    assert added
    lines = out.split("\n")
    i = lines.index("Address = 10.8.1.1/24")
    assert lines[i + 1] == "Address = 169.254.88.1/26", f"второй адрес не следующей строкой:\n{out}"
    assert "#" not in lines[i + 1], "комментарий в строке второго адреса"
    assert out.replace("Address = 169.254.88.1/26\n", "") == CONF, "кроме строки адреса что-то изменилось"


def test_the_first_address_line_still_reads_the_users_address():
    """Прежний читатель первой строки (extract_param) видит адрес пользователей."""
    out, _ = awg.with_second(CONF, "169.254.88.1/26")
    assert awg._extract_param(out, "Address").strip() == "10.8.1.1/24"


def test_adding_twice_changes_nothing():
    once, _ = awg.with_second(CONF, "169.254.88.1/26")
    twice, added = awg.with_second(once, "169.254.88.1/26")
    assert not added and twice == once, "строка второго адреса добавлена второй раз"


def test_an_existing_line_with_a_comment_counts_as_present():
    conf = CONF.replace("Address = 10.8.1.1/24\n", "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26 # кворум\n")
    out, added = awg.with_second(conf, "169.254.88.1/26")
    assert not added and out == conf


def test_with_several_address_lines_the_second_goes_after_the_first_one():
    conf = CONF.replace("Address = 10.8.1.1/24\n", "Address = 10.8.1.1/24\nAddress = fd00::1/64\n")
    lines = awg.with_second(conf, "169.254.88.65/26")[0].split("\n")
    assert lines[lines.index("Address = 10.8.1.1/24") + 1] == "Address = 169.254.88.65/26"


def test_a_conf_without_address_is_left_alone():
    conf = "[Interface]\nPrivateKey = DUMMY\n"
    assert awg.with_second(conf, "169.254.88.1/26") == (conf, False)


def test_without_drops_only_that_line():
    out, _ = awg.with_second(CONF, "169.254.88.1/26")
    back, dropped = awg.without(out, "169.254.88.1/26")
    assert dropped and back == CONF
    assert awg.without(CONF, "169.254.88.1/26") == (CONF, False), "снятие отсутствующей строки что-то сделало"
    assert awg.without(CONF, "10.8.1.1/24")[0].count("Address") == 0


def test_without_also_drops_a_commented_line():
    conf = CONF.replace("Address = 10.8.1.1/24\n", "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26 # кворум\n")
    assert awg.without(conf, "169.254.88.1/26") == (CONF, True)


class _Host:
    """Конфиг интерфейса в памяти, бэкапы и вызовы `ip` — журналом."""

    def __init__(self, monkeypatch, conf: str = CONF, ip_rc: int = 0, ip_exc=None):
        self.files = {config.CONF_PATH: conf}
        self.backups: list[str] = []
        self.ip: list[list[str]] = []
        self.ip_rc, self.ip_exc = ip_rc, ip_exc
        monkeypatch.setattr(awg, "read_file", lambda p: self.files[p])
        monkeypatch.setattr(awg, "write_file", lambda p, c: self.files.__setitem__(p, c))
        monkeypatch.setattr(awg, "_backup_conf", lambda iface=None: self.backups.append(self.files[awg.conf_path(iface)]))

        def run(args, **kw):
            self.ip.append(list(args))
            if self.ip_exc:
                raise self.ip_exc
            return types.SimpleNamespace(returncode=self.ip_rc)
        monkeypatch.setattr(awg, "subprocess", types.SimpleNamespace(run=run, SubprocessError=Exception))

    @property
    def conf(self) -> str:
        return self.files[config.CONF_PATH]


def test_ensure_writes_the_conf_with_a_backup_and_sets_the_live_address(monkeypatch):
    h = _Host(monkeypatch)
    assert awg.ensure_second_address("169.254.88.1/26") is True
    assert "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26\n" in h.conf
    assert h.backups == [CONF], "конфиг переписан без бэкапа прежнего"
    assert h.ip == [["ip", "addr", "replace", "169.254.88.1/26", "dev", config.AWG_INTERFACE]]


def test_ensure_again_does_not_rewrite_but_still_sets_the_live_address(monkeypatch):
    """После перезапуска интерфейса адрес из конфига поднимет awg-quick, но
    syncconf его не ставит: ip addr replace — каждый раз, он идемпотентен."""
    h = _Host(monkeypatch)
    awg.ensure_second_address("169.254.88.1/26")
    assert awg.ensure_second_address("169.254.88.1/26") is False
    assert len(h.backups) == 1, "повтор переписал конфиг"
    assert len(h.ip) == 2


@pytest.mark.parametrize("rc, exc", [(2, None), (0, OSError("нет ip"))])
def test_a_failing_ip_does_not_undo_the_conf(monkeypatch, rc, exc):
    h = _Host(monkeypatch, ip_rc=rc, ip_exc=exc)
    assert awg.ensure_second_address("169.254.88.1/26") is True
    assert "Address = 169.254.88.1/26" in h.conf


def test_drop_removes_the_line_and_the_live_address(monkeypatch):
    h = _Host(monkeypatch)
    awg.ensure_second_address("169.254.88.1/26")
    assert awg.drop_second_address("169.254.88.1/26") is True
    assert h.conf == CONF
    assert h.ip[-1] == ["ip", "addr", "del", "169.254.88.1/26", "dev", config.AWG_INTERFACE]
    assert awg.drop_second_address("169.254.88.1/26") is False, "повторное снятие переписало конфиг"
    assert len(h.backups) == 2
