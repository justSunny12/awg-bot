"""Общее для тестов агента, которые правят юнит обвязки: текст юнита во
временном каталоге (`_Unit`) и ответ подменённого systemctl (`_cp`)."""
from __future__ import annotations

import subprocess
import threading
import types
from pathlib import Path

from awgbot.core import config
from awgbot.infra import gwguard
from awgbot.util import gwlink

UNIT_TEXT = """[Unit]
Description=awg-bot: линк до сервера AWG и изоляция клиентов (шлюз)
After=network-online.target awg-quick@awg0.service
Wants=network-online.target awg-quick@awg0.service

[Service]
Type=oneshot
RemainAfterExit=yes
# Подсеть вшита: app.yaml на шлюзе нет, смена подсети = новый бандл с ВПС.
Environment=CLIENT_SUBNET=10.8.1.0/24
Environment=LINK_IF=awglink
Environment="ADMIN_IPS=10.8.1.2"
Environment=GATEWAY_PUBKEY=AAAAPUBKEY=
Environment=GATEWAY_PREV_PUBKEY=
Environment=LAN_MODE=1
Environment="LAN_SUBNETS=192.168.68.0/24"
Environment=RESOLVER=10.9.1.1
Environment="PEER_LAN_NETS="
Environment=LINK_CHANNEL_PORT=8787
EnvironmentFile=-/etc/awg-gw/firewall.env
ExecStart=/usr/local/sbin/routing-gw-setup.sh --apply /etc/amnezia/amneziawg/awglink.conf
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""

# Юнит, записанный скриптом обвязки 3.3.0: подсети под прежними именами и
# постоянная строка LINK_CHANNEL=1 (переход — gwguard.unit_rename_old)
UNIT_TEXT_330 = (UNIT_TEXT.replace('Environment="LAN_SUBNETS=', 'Environment="HOME_SUBNETS=')
                 .replace('Environment="PEER_LAN_NETS=', 'Environment="PEER_HOME_NETS=')
                 .replace("Environment=LINK_CHANNEL_PORT=8787\n",
                          "Environment=LINK_CHANNEL=1\nEnvironment=LINK_CHANNEL_PORT=8787\n"))


def _cp(rc=0, out=b"", err=b""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


class _Unit:
    """Шлюз для этих тестов: юнит обвязки во временном файле и systemctl.

    `restart` — чем отвечает `systemctl restart` юнита: список исходов по
    порядку вызовов (последний повторяется); исход — CompletedProcess или
    исключение, которое бросит subprocess.run. `started_with` — с каким
    окружением юнит стартовал при каждом рестарте (что увидел скрипт обвязки).
    """

    def __init__(self, tmp_path, monkeypatch, text: str | None = UNIT_TEXT):
        self.path = tmp_path / config.GW_UNIT
        if text is not None:
            self.path.write_text(text, encoding="utf-8")
        self.calls: list[list[str]] = []
        self.restart: list = [_cp(0)]
        self.reload: list = [_cp(0)]                   # исходы `systemctl daemon-reload`
        self.started_with: list[dict] = []
        self.gate: threading.Event | None = None      # держать рестарт, пока не отпустят
        self.in_restart = threading.Event()
        target = f"/etc/systemd/system/{config.GW_UNIT}"
        real_path = Path

        def _path(p, *rest):
            return self.path if str(p) == target and not rest else real_path(p, *rest)

        monkeypatch.setattr(gwguard, "Path", _path)
        fake = types.SimpleNamespace(run=self._run, CompletedProcess=subprocess.CompletedProcess,
                                     SubprocessError=subprocess.SubprocessError,
                                     TimeoutExpired=subprocess.TimeoutExpired)
        monkeypatch.setattr(gwguard, "subprocess", fake)

    def _run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:2] == ["systemctl", "restart"]:
            self.started_with.append({k: gwguard.unit_env(k) for k in gwlink.BUNDLE_KEYS})
            self.in_restart.set()
            if self.gate is not None:
                self.gate.wait(5)
            n = len([c for c in self.calls if c[:2] == ["systemctl", "restart"]])
            outcome = self.restart[min(n, len(self.restart)) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if argv[:2] == ["systemctl", "daemon-reload"]:
            n = len([c for c in self.calls if c[:2] == ["systemctl", "daemon-reload"]])
            outcome = self.reload[min(n, len(self.reload)) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return _cp(0)

    @property
    def restarts(self) -> int:
        return len([c for c in self.calls if c[:2] == ["systemctl", "restart"]])

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def env(self) -> dict:
        return {k: gwguard.unit_env(k) for k in gwlink.BUNDLE_KEYS}
