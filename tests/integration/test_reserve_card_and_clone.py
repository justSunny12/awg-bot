"""Карточка резерва и клон гостя после ревью: ошибка сборки пакета — строкой в
карточке (`reserve_card_data()["pkg_error"]`); перезапуск AmneziaWG в гостевом
режиме (infra/awg/server.restart_server) — без скрипта зоны: конфиг для
`awg setconf` собирается из текущего .conf без расширений awg-quick, затем
down/setconf/up.

Цена ошибки: пакет не собирается молча — у резерва устаревшие данные, переезд
поднимет пользователей по ним, а админ ничего не видел; перезапуск клона через
скрипт зоны из юнита гостя — нет CAP_SYS_ADMIN, перезапуск падает, новые пиры
(устройства, добавленные за время обслуживания) не встают; setconf со
старого .setconf — пиры за время обслуживания пропадают при перезапуске;
Address в конфиге setconf — «Line unrecognized», клон не поднимается.
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.bot.texts.reserve.section import card_text
from awgbot.core import config
from awgbot.infra import awg

from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.integration
rsv = rc.rsv


def test_a_package_that_does_not_build_is_a_line_on_the_card(services, rsv):
    rc.pair_row(services)
    services.rsv_update("rsv_view", pkg_error="база заблокирована <locked>")
    d = services.reserve_card_data()
    assert d.get("pkg_error") == "база заблокирована <locked>", d.get("pkg_error")
    text = card_text({**d, "dir_out": (True, "")})            # направление «к резерву» открыто — строка о пакете
    assert "└ ⚠️ данные для переезда не собираются: база заблокирована &lt;locked&gt;" in text, text


def test_without_an_error_there_is_no_line(services, rsv):
    rc.pair_row(services)
    assert "не собираются" not in card_text({**services.reserve_card_data(), "dir_out": (True, "")})


CONF = ("[Interface]\nPrivateKey = DUMMY-priv\nAddress = 198.51.100.1/24\nListenPort = 45877\nMTU = 1376\n"
        "DNS = 198.51.100.1\nTable = off\nJc = 4\n\n[Peer]\nPublicKey = DUMMY-pub-1\nAllowedIPs = 198.51.100.2/32\n\n"
        "[Peer]\nPublicKey = DUMMY-pub-new\nAllowedIPs = 198.51.100.3/32\n")


@pytest.fixture()
def guest_awg(tmp_path, monkeypatch):
    conf = tmp_path / "awg0.conf"
    conf.write_text(CONF, encoding="utf-8")
    (tmp_path / "awg0.setconf").write_text("[Interface]\nPrivateKey = DUMMY-priv\n", encoding="utf-8")   # прежний
    monkeypatch.setattr(config, "GUEST", True)
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "CONF_PATH", str(conf))
    calls = []
    real = awg._run

    def run(argv, check=True, timeout=30, **kw):
        if argv[:2] == ["sh", "-c"]:                          # запись файла (pkg.write_file) — настоящая
            return real(argv, check=check, timeout=timeout, **kw)
        calls.append(list(argv))
        if argv[:2] == ["awg", "setconf"]:
            calls.append(("setconf-text", open(argv[3], encoding="utf-8").read()))
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(awg, "_run", run)
    return tmp_path, calls


def test_the_guest_restart_rebuilds_the_setconf_from_the_current_conf(guest_awg):
    tmp, calls = guest_awg
    awg.restart_server()
    cmds = [c for c in calls if isinstance(c, list)]
    assert cmds == [["ip", "link", "set", "awg0", "down"], ["awg", "setconf", "awg0", str(tmp / "awg0.setconf")],
                    ["ip", "link", "set", "awg0", "up"]], cmds
    text = next(c[1] for c in calls if isinstance(c, tuple))
    keys = {ln.split("=")[0].strip() for ln in text.splitlines() if "=" in ln}
    assert not keys & {"Address", "MTU", "DNS", "Table"}, f"в конфиге setconf расширения awg-quick: {keys}"
    assert "DUMMY-pub-new" in text, "пир, добавленный за время обслуживания, пропал при перезапуске"
    assert not [c for c in cmds if "netns" in c or any("reserve-zone-setup" in str(x) for x in c)], \
        "гость зовёт скрипт зоны — у юнита нет CAP_SYS_ADMIN"
    assert not [c for c in cmds if c and c[0] == "awg-quick"], "у клона нет awg-quick"
