"""
net4.py — исходящие HTTP(S)-запросы только по IPv4.

Проект поддерживает только IPv4: туннели и маркировка на шлюзах — v4, у ВПС
v6-маршрута наружу нет. Резолвер при этом отдаёт для многих хостов и AAAA, и
urllib перебирает адреса по порядку: v6 первым — соединение уходит мимо
туннеля (на шлюзе) или в никуда (на ВПС) и стоит секунды на каждом запросе.
Здесь — `urlopen`, который берёт только A-записи: та же сессия aiogram у Bot
API прибита к IPv4 в runtime/tgsession.
"""
from __future__ import annotations

import http.client
import imaplib
import smtplib
import socket
import urllib.request
from typing import Optional

TIMEOUT_S = 10.0                # умолчание наружу: правило проекта — не дольше 10 с


def getaddrinfo4(host: str, port: int) -> list:
    """Только IPv4-адреса хоста (TCP); пусто — у хоста нет A-записи (connect4 даст OSError)."""
    return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)


def connect4(host: str, port: int, timeout: Optional[float] = TIMEOUT_S, source_address=None) -> socket.socket:
    """TCP-соединение по первому ответившему IPv4-адресу; все отказали — последняя ошибка."""
    last: Exception = OSError(f"у {host} нет IPv4-адреса")
    for _family, _type, _proto, _canon, sa in getaddrinfo4(host, port):
        try:
            return socket.create_connection(sa, timeout, source_address)
        except OSError as e:
            last = e
    raise last


class _HTTPConnection4(http.client.HTTPConnection):
    def connect(self):
        self.sock = connect4(self.host, self.port, self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _HTTPSConnection4(http.client.HTTPSConnection):
    def connect(self):
        self.sock = connect4(self.host, self.port, self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _HTTPHandler4(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection4, req)


class _HTTPSHandler4(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection4, req, context=self._context)


_OPENER = urllib.request.build_opener(_HTTPHandler4, _HTTPSHandler4)


def urlopen(url_or_req, timeout: Optional[float] = TIMEOUT_S, data=None):
    """Как `urllib.request.urlopen`, но соединение только по IPv4; таймаут по умолчанию —
    правило проекта (наружу не дольше 10 с), а не «без предела», как у urllib."""
    return _OPENER.open(url_or_req, data=data, timeout=timeout)


class IMAP4_SSL4(imaplib.IMAP4_SSL):
    """IMAP по TLS, соединение только по IPv4 (SNI и проверка имени — по хосту)."""

    def _create_socket(self, timeout):
        sock = connect4(self.host, self.port, timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class IMAP44(imaplib.IMAP4):
    """IMAP без TLS на входе (STARTTLS по месту вызова), соединение только по IPv4."""

    def _create_socket(self, timeout):
        return connect4(self.host, self.port, timeout)


def _ehlo_name(local_hostname):
    """Имя для EHLO: заданное — как есть; иначе локальное имя машины, если оно с точкой
    (RFC 5321 §4.1.1.1: в EHLO — полное имя или адресный литерал; голое имя строгий SMTP
    отвергает); имя без точки — адресный литерал `[адрес]` с уже открытого сокета
    (`_get_socket`), а до соединения — заглушка-литерал. smtplib по умолчанию зовёт
    socket.getfqdn() — обратный запрос в DNS без предела по времени (медленный резолвер
    держал бы проверку и отправку почты сверх «наружу ≤ 10 с»)."""
    if local_hostname:
        return local_hostname
    name = socket.gethostname() or ""
    return name if "." in name else "[127.0.0.1]"


def _ehlo_literal(obj, sock) -> None:
    """Имя машины без точки и имя не задано — EHLO адресным литералом с сокета."""
    if getattr(obj, "_ehlo_given", False) or "." in (socket.gethostname() or ""):
        return
    try:
        obj.local_hostname = "[%s]" % sock.getsockname()[0]
    except (OSError, IndexError, TypeError):
        pass


class SMTP4(smtplib.SMTP):
    """SMTP (STARTTLS по месту вызова), соединение только по IPv4."""

    def __init__(self, host="", port=0, local_hostname=None, *args, **kw):
        self._ehlo_given = bool(local_hostname)
        super().__init__(host, port, _ehlo_name(local_hostname), *args, **kw)

    def _get_socket(self, host, port, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        sock = connect4(host, port, timeout, self.source_address)
        _ehlo_literal(self, sock)
        return sock


class SMTP_SSL4(smtplib.SMTP_SSL):
    """SMTP с TLS сразу (порт 465), соединение только по IPv4 (SNI и проверка имени — по хосту)."""

    def __init__(self, host="", port=0, local_hostname=None, *args, **kw):
        self._ehlo_given = bool(local_hostname)
        super().__init__(host, port, _ehlo_name(local_hostname), *args, **kw)

    def _get_socket(self, host, port, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        sock = connect4(host, port, timeout, self.source_address)
        _ehlo_literal(self, sock)
        return self.context.wrap_socket(sock, server_hostname=self._host)
