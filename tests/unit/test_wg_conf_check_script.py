"""
Ядро проверки конфига линка в скрипте обвязки (routing-gw-setup.sh,
wg_conf_check): второй рубеж на шлюзе по тому же gw-wg-allow.list, что и
util/wgconf, — одни и те же случаи правила AllowedIPs и белого списка ключей
обязаны решаться одинаково на обоих рубежах. Иначе документ, который
сервер собрал и агент принял, скрипт отверг бы (или наоборот — пропустил
бы то, что агент отверг), и доставка молча ходила бы по кругу.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awgbot.util import wgconf
from tests.conftest import SH
from tests.unit.test_wgconf_allowed import ALLOWED_CASES, LINK

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


def _core() -> str:
    """Функции проверки — из настоящего скрипта, от allow_list_path до conf_core."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("allow_list_path() {")
    end = text.index('if [ "$MODE" = "link-apply" ]; then')
    return text[start:end]


def _check(tmp_path, conf_text: str, kind: str = "link", *, allow: Path | None = None, **env) -> tuple[int, str]:
    conf = tmp_path / "link.conf"
    conf.write_text(conf_text, encoding="utf-8")
    allow = wgconf.ALLOW_LIST if allow is None else allow
    prog = (_core() + f'\nallow_list_path() {{ printf "%s" "{allow}"; [ -f "{allow}" ]; }}\n'
            f'wg_conf_check "{conf}" {kind}\n')
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", **{k: str(v) for k, v in env.items()}})
    return r.returncode, r.stderr.strip()


def _env(rule: dict) -> dict:
    return {"CLIENT_SUBNET": rule["client_subnet"], "PEER_LAN_NETS": " ".join(rule.get("peer_nets") or []),
            "RESOLVER": rule.get("resolver", ""), "LAN_SUBNETS": " ".join(rule.get("lan_subnets") or []),
            "IFACE_NETS": " ".join(rule.get("iface_nets") or [])}


def _with_allowed(entries: str) -> str:
    return LINK.replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30", f"AllowedIPs = {entries}")


@pytest.mark.parametrize("entries,rule,ok", ALLOWED_CASES)
def test_the_script_decides_the_allowed_ips_rule_like_wgconf(tmp_path, entries, rule, ok):
    """Те же случаи, что у wgconf, — и тот же вердикт: /30 линка скрипт берёт
    из Address конфига, остальное — из юнита (окружения)."""
    cidr = rule["link_cidr"]
    text = _with_allowed(entries)
    if cidr != "10.99.99.0/30":
        text = text.replace("Address = 10.99.99.2/30", f"Address = {cidr.rsplit('.', 1)[0]}.{int(cidr.rsplit('.', 1)[1].split('/')[0]) + 2}/30")
    rc, err = _check(tmp_path, text, **_env(rule))
    assert (rc == 0) is ok, f"скрипт: rc={rc} {err}"


@pytest.mark.parametrize("extra", ["PostUp = /bin/sh -c id", "PreDown = echo", "DNS = 1.1.1.1", "Table = off", "SaveConfig = true"])
def test_forbidden_keys_are_refused_by_the_script(tmp_path, extra):
    rc, err = _check(tmp_path, LINK.replace("Jc = 4\n", f"Jc = 4\n{extra}\n"), **_env(dict(client_subnet="10.8.1.0/24", link_cidr="10.99.99.0/30")))
    assert rc == 1 and "белого списка" in err


def test_structure_errors_are_refused_by_the_script(tmp_path):
    env = _env(dict(client_subnet="10.8.1.0/24", link_cidr="10.99.99.0/30"))
    assert _check(tmp_path, LINK + "\n[Peer]\nPublicKey = x\n", **env)[0] == 1
    assert _check(tmp_path, LINK + "# ещё строка\n", **env)[0] == 1
    assert _check(tmp_path, LINK.replace("Jc = 4", "Jc = 4\nJc = 5"), **env)[0] == 1
    assert _check(tmp_path, LINK.replace("# awg-bot: контракт линка 1", "# awg-bot: контракт линка 2"), **env)[0] == 0, "штамп — не комментарий"


def test_without_the_allow_list_the_script_refuses(tmp_path):
    rc, err = _check(tmp_path, LINK, allow=tmp_path / "нет.list",
                     **_env(dict(client_subnet="10.8.1.0/24", link_cidr="10.99.99.0/30")))
    assert rc == 1 and "gw-wg-allow.list" in err


def test_the_scripts_key_set_is_the_lists_key_set(tmp_path):
    """Белый список — один файл: ключ, допустимый у wgconf, допустим и у
    скрипта, и наоборот (на примере каждой секции линка)."""
    a = wgconf.allow()
    for section in ("Interface", "Peer"):
        for key in a.keys["link"][section]:
            text = LINK
            if key not in LINK:
                marker = "Jc = 4\n" if section == "Interface" else "PersistentKeepalive = 25-35\n"
                val = "25" if key == "PersistentKeepalive" else "1"
                text = LINK.replace(marker, marker + f"{key} = {val}\n")
            rc, err = _check(tmp_path, text, **_env(dict(client_subnet="10.8.1.0/24", link_cidr="10.99.99.0/30")))
            assert rc == 0, f"{section} {key}: {err}"
