"""ВПС-сторона: бандл шлюза кнопкой — собрать скриптом, зашифровать ключом шлюза."""
import base64
import builtins
import os
import subprocess

import awgbot.core.config as cfg
from awgbot.util import bundlecrypt as bc


def test_gw_bundle_encrypted_uses_the_gateway_private_key(services, monkeypatch, tmp_path):
    priv = base64.b64encode(os.urandom(32)).decode()
    (tmp_path / "gw-awglink.conf").write_text("[Interface]\nPrivateKey = " + priv + "\n")
    (tmp_path / "awg-gw-bundle.sh").write_bytes(b"#!/bin/sh\n#__GW_SETUP_BELOW__\n")
    monkeypatch.setattr(cfg, "ROUTING_GW_INTERFACE", "awglink")
    real_open = builtins.open

    def fake_open(path, *a, **k):
        p = str(path)
        if p.startswith("/root/"):
            return real_open(tmp_path / os.path.basename(p), *a, **k)
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    blob, name = services.gw_bundle_encrypted()
    assert name.endswith(".enc")
    assert bc.decrypt(blob, priv) == b"#!/bin/sh\n#__GW_SETUP_BELOW__\n"
