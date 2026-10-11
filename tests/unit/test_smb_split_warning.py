"""«SMB в туннеле» при раздельном туннеле: имена устройству админа видны (/32
резолвера админа в AllowedIPs), а настоящих адресов локальных сетей шлюзов в
его AllowedIPs нет — диалог включения обязан об этом предупредить."""
import pytest

from awgbot.bot import texts
from awgbot.core import config
from awgbot.domain.services.gwchannel.smbtunnel import SmbTunnelMixin


@pytest.mark.parametrize("allowed, split", [
    ("0.0.0.0/0, ::/0", False),
    ("0.0.0.0/0", False),
    ("::/0, 0.0.0.0/0", False),
    ("10.8.1.0/24", True),
    ("10.8.1.0/24, 192.168.1.0/24", True),
    ("0.0.0.0/1, 128.0.0.0/1", True),   # покрывает всё, но не одной строкой — честно «раздельный»
    ("::/0", True),
])
def test_split_is_told_by_the_allowed_ips_line(monkeypatch, allowed, split):
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", allowed)
    assert SmbTunnelMixin.smb_tunnel_split() is split


def test_the_turn_on_dialog_warns_only_for_a_split_tunnel():
    plain = texts.gateway_smb_ask(True)
    warned = texts.gateway_smb_ask(True, True)
    assert "⚠️" not in plain
    assert warned.startswith(plain) and "раздельный" in warned and "полным туннелем" in warned
    assert texts.gateway_smb_ask(False, True) == texts.gateway_smb_ask(False), "выключение не предупреждает"
