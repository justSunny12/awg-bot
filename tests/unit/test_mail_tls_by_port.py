"""
Почта — шифрование по порту: SMTP 465 — TLS сразу (SMTPS), прочие — STARTTLS;
IMAP 143 — STARTTLS, прочие (993) — TLS сразу.

Стережём: раньше SMTP всегда шёл STARTTLS-ом, и ящик с портом 465 (так его
дают многие провайдеры) не работал вовсе: сервер ждёт TLS-рукопожатия,
проверка и письма висели до таймаута; IMAP всегда шёл TLS сразу — порт 143
не работал. Соединения — по-прежнему только по IPv4 (классы net4).
"""
from __future__ import annotations

import pytest

from awgbot.infra import mail
from awgbot.util import net4

pytestmark = pytest.mark.unit


class _Conn:
    log: list = []

    def __init__(self, host, port, **kw):
        self.log.append((type(self).__name__, port, "context" in kw or "ssl_context" in kw))

    def starttls(self, **kw):
        self.log.append(("starttls",))

    def login(self, *a):
        self.log.append(("login",))

    def send_message(self, msg):
        self.log.append(("send",))

    def logout(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SMTP4(_Conn):
    pass


class SMTP_SSL4(_Conn):
    pass


class IMAP44(_Conn):
    pass


class IMAP4_SSL4(_Conn):
    pass


@pytest.fixture()
def log(monkeypatch):
    _Conn.log = []
    for cls in (SMTP4, SMTP_SSL4, IMAP44, IMAP4_SSL4):
        monkeypatch.setattr(net4, cls.__name__, cls)
    return _Conn.log


def _acc(smtp_port=587, imap_port=993):
    return mail.MailAccount(login="a@example.org", password="DUMMY", smtp_host="smtp.example.org",
                            smtp_port=smtp_port, imap_host="imap.example.org", imap_port=imap_port)


def test_smtp_465_is_implicit_tls(log):
    mail.check_smtp(_acc(smtp_port=465))
    assert log == [("SMTP_SSL4", 465, True), ("login",)]
    log.clear()
    mail.send_mail(_acc(smtp_port=465), "b@example.org", "s", "b")
    assert log[0] == ("SMTP_SSL4", 465, True) and ("starttls",) not in log and ("send",) in log


@pytest.mark.parametrize("port", [587, 25, 2525])
def test_other_smtp_ports_use_starttls(log, port):
    mail.check_smtp(_acc(smtp_port=port))
    assert log == [("SMTP4", port, False), ("starttls",), ("login",)]


def test_imap_993_is_implicit_tls_and_143_starttls(log):
    mail.check_imap(_acc(imap_port=993))
    assert log[0] == ("IMAP4_SSL4", 993, True) and ("starttls",) not in log
    log.clear()
    mail.check_imap(_acc(imap_port=143))
    assert log[:2] == [("IMAP44", 143, False), ("starttls",)]


def test_smtps_socket_comes_from_connect4_with_sni_by_host(monkeypatch):
    """SMTPS — тоже только IPv4: сокет от connect4, TLS поверх него с SNI по имени хоста."""
    seen, wrapped = [], []

    class _Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.append((sock, server_hostname))
            return ("tls", sock)

    def fake(host, port, timeout, source_address=None):
        seen.append((host, port, timeout))
        return ("sock", host)
    monkeypatch.setattr(net4, "connect4", fake)
    # без хоста — соединения в конструкторе нет; имя для EHLO — явно (умолчание — getfqdn, запрос в сеть)
    s = net4.SMTP_SSL4(context=_Ctx(), local_hostname="client.example.org")
    s._host = "smtp.example.org"
    assert s._get_socket("smtp.example.org", 465, 10.0) == ("tls", ("sock", "smtp.example.org"))
    assert seen == [("smtp.example.org", 465, 10.0)] and wrapped[0][1] == "smtp.example.org"
