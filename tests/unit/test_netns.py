"""Сокеты в сетевом пространстве из процесса бота (util/netns.py).

Гостевой зоне резерва нужен слушающий сокет внутри пространства `awg-guest`,
а сам процесс бота остаётся в пространстве хоста. `listen_in` делает это
отдельным потоком: вошёл — создал сокет — вернулся.

Цена ошибки: сокет, созданный не в том пространстве, — порт вебхука бота
основы на резерве слушает хост, а не зона (или наоборот), и Telegram стучится
в никуда; поток, не вернувшийся в своё пространство, — следующий вызов из
пула потоков бота уходит в сеть зоны. На не-Linux (разработка на macOS)
функция отказывает внятной ошибкой, а не падает на `os.setns`.

Тест с настоящим пространством идёт только на Linux под root (стенд);
пространство создаётся `ip netns add` и удаляется в конце.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import uuid

import pytest

from awgbot.util import netns

pytestmark = pytest.mark.unit

_LINUX_ROOT = sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0 \
    and shutil.which("ip") is not None


def test_the_path_of_a_named_namespace_is_under_run_netns():
    assert netns.path_of("awg-guest") == "/run/netns/awg-guest"


@pytest.mark.skipif(hasattr(os, "setns"), reason="на Linux есть os.setns")
def test_without_setns_the_listener_refuses_clearly():
    with pytest.raises(netns.NetnsError, match="только Linux"):
        netns.listen_in("/run/netns/awg-guest", lambda: None)


@pytest.mark.skipif(not hasattr(os, "setns"), reason="только Linux")
def test_a_missing_namespace_is_a_netns_error_not_a_crash():
    with pytest.raises(netns.NetnsError):
        netns.listen_in("/run/netns/awgtst-нет-такого", lambda: None)


def _own_ns() -> int:
    return os.stat("/proc/thread-self/ns/net").st_ino


@pytest.fixture()
def space():
    name = "awgtst-" + uuid.uuid4().hex[:8]
    subprocess.run(["ip", "netns", "add", name], check=True)
    try:
        subprocess.run(["ip", "netns", "exec", name, "ip", "link", "set", "lo", "up"], check=True)
        yield name
    finally:
        subprocess.run(["ip", "netns", "del", name], check=False)


@pytest.mark.skipif(not _LINUX_ROOT, reason="настоящее пространство — Linux и root (стенд)")
def test_the_socket_belongs_to_the_namespace_and_the_caller_stays_home(space):
    path = netns.path_of(space)
    home = _own_ns()
    seen = {}

    def make():
        seen["ns"] = _own_ns()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s

    sock = netns.listen_in(path, make)
    try:
        port = sock.getsockname()[1]
        assert seen["ns"] == os.stat(path).st_ino, "make() выполнился не в пространстве"
        assert seen["ns"] != home
        assert _own_ns() == home, "вызвавший поток остался в чужом пространстве"
        # из пространства хоста порт закрыт: петля хоста — другая
        with socket.socket() as c:
            c.settimeout(2)
            assert c.connect_ex(("127.0.0.1", port)) != 0, "сокет слушает в пространстве хоста"
        # изнутри пространства — открыт
        r = subprocess.run(["ip", "netns", "exec", space, sys.executable, "-c",
                            f"import socket; socket.create_connection(('127.0.0.1', {port}), 2).close()"],
                           capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, f"в пространстве порт не слушается: {r.stderr}"
        # и следующий сокет без перехода — снова у хоста
        with socket.socket() as plain:
            plain.bind(("127.0.0.1", 0))
            plain.listen(1)
            r = subprocess.run(["ip", "netns", "exec", space, sys.executable, "-c",
                                f"import socket; socket.create_connection(('127.0.0.1', {plain.getsockname()[1]}), 2)"],
                               capture_output=True, text=True, timeout=20)
            assert r.returncode != 0, "обычный сокет после listen_in оказался в пространстве"
    finally:
        sock.close()


@pytest.mark.skipif(not _LINUX_ROOT, reason="настоящее пространство — Linux и root (стенд)")
def test_an_error_inside_make_comes_out_as_is(space):
    class Boom(RuntimeError):
        pass

    def make():
        raise Boom("порт занят")

    with pytest.raises(Boom):
        netns.listen_in(netns.path_of(space), make)
    assert netns.listen_in(netns.path_of(space), _own_ns) == os.stat(netns.path_of(space)).st_ino, \
        "после ошибки следующий вызов не вошёл в пространство"


@pytest.mark.skipif(not _LINUX_ROOT or shutil.which("nsenter") is None, reason="Linux, root и nsenter")
def test_a_command_runs_inside_the_namespace(space):
    r = netns.run_in(netns.path_of(space), ["ip", "-o", "link", "show"])
    assert r.returncode == 0
    names = {line.split(":")[1].strip() for line in r.stdout.decode().splitlines()}
    assert names == {"lo"}, f"команда увидела интерфейсы хоста: {names}"
