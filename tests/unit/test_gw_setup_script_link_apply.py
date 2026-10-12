"""
routing-gw-setup.sh --link-apply: конфиг линка из канала — проверка, установка,
syncconf или перезапуск, возврат прежнего.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.conftest import SH, fake_cmd
from tests.unit import _gw_setup_common as _c
from tests.unit._gw_setup_common import _helpers

script = _c.script


# ── --link-apply: конфиг линка из канала ─────────────────────────────────────

def _link_apply_block(script: str) -> tuple[str, str]:
    """(функции проверки, сам режим) — из настоящего скрипта: от
    allow_list_path до блока режима и блок до раздела отката."""
    start = script.index("allow_list_path() {")
    mode = script.index('if [ "$MODE" = "link-apply" ]; then')
    end = script.index("# ── откат", mode)
    return script[start:mode], script[mode:end]


def _link_host(tmp_path, *, up: bool = True, syncconf_rc: int = 0, up_rc: int = 0):
    """Шлюз для --link-apply: awg, awg-quick, ip и nft записывают вызовы в
    журнал; `up` — поднят ли интерфейс линка (ip link show). install и cmp —
    настоящие: что легло на место конфига, видно по файлу."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "calls.log"; log.write_text("", encoding="utf-8")
    fake_cmd(bin_dir / "awg", f'echo "awg $*" >> {log}\n'
                              f'[ "$1" = syncconf ] && exit {syncconf_rc}\nexit 0\n')
    # up_rc — чем кончается `awg-quick up` (первый вызов; повторный — возврат прежнего — успешен)
    fake_cmd(bin_dir / "awg-quick", f'echo "awg-quick $*" >> {log}\n'
                                    '[ "$1" = strip ] && echo "[Interface]"\n'
                                    f'if [ "$1" = up ] && [ ! -f {tmp_path}/up.done ]; then : > {tmp_path}/up.done; exit {up_rc}; fi\n'
                                    'exit 0\n')
    fake_cmd(bin_dir / "ip", f'echo "ip $*" >> {log}\n'
                             f'[ "$1" = link ] && exit {0 if up else 1}\nexit 0\n')
    fake_cmd(bin_dir / "nft", f'echo "nft $*" >> {log}\nexit 0\n')
    conf_dir = tmp_path / "amneziawg"; conf_dir.mkdir()
    guard = tmp_path / "guard.nft"; guard.write_text("table inet awg_gw_guard {}\n", encoding="utf-8")
    return bin_dir, log, conf_dir, guard


def _link_apply(script, tmp_path, new_text: str, *, installed: str | None, up: bool = True,
                allow: Path | None = None, syncconf_rc: int = 0, env: dict | None = None,
                src_missing: bool = False, up_rc: int = 0):
    """Прогнать режим --link-apply под sh: (CompletedProcess, журнал вызовов,
    файл установленного конфига)."""
    from awgbot.util import wgconf
    bin_dir, log, conf_dir, guard = _link_host(tmp_path, up=up, syncconf_rc=syncconf_rc, up_rc=up_rc)
    dst = conf_dir / "awglink.conf"
    if installed is not None:
        dst.write_text(installed, encoding="utf-8")
    src = tmp_path / "link.next"
    if not src_missing:
        src.write_text(new_text, encoding="utf-8")
    allow = wgconf.ALLOW_LIST if allow is None else allow
    fns, mode = _link_apply_block(script)
    prog = ("set -e\n" + _helpers(script)
            + f'\nMODE=link-apply\nSRC_CONF="{src}"\nHOST_CONF_DIR="{conf_dir}"\nLINK_IF=awglink\n'
            f'AWG_QUICK=awg-quick\nAWG_BIN=awg\nGUARD_FILE="{guard}"\n'
            + fns + f'\nallow_list_path() {{ printf "%s" "{allow}"; [ -f "{allow}" ]; }}\n' + mode)
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                       env={"PATH": f"{bin_dir}:/usr/bin:/bin", "CLIENT_SUBNET": "10.8.1.0/24",
                            "RESOLVER": "10.9.1.1", "HOME_SUBNETS": "192.168.68.0/24", **(env or {})})
    return r, log.read_text(encoding="utf-8").splitlines(), dst


def _acts(calls: list[str]) -> list[str]:
    """Вызовы, которые что-то меняют на хосте (чтение маршрутов и состояния
    интерфейса — не в счёт)."""
    return [c for c in calls if not c.startswith(("ip -4 route show", "ip link show"))]


def test_link_apply_with_the_same_conf_does_nothing(script, tmp_path):
    """Тот же конфиг — «link: unchanged»: ни установки, ни syncconf, ни
    перезапуска линка (он рвал бы РФ-доступ у всех ради ничего)."""
    from tests.unit.test_wgconf_allowed import LINK
    r, calls, dst = _link_apply(script, tmp_path, LINK, installed=LINK)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().endswith("link: unchanged"), r.stdout
    assert not [c for c in calls if c.startswith(("awg", "nft", "ip route"))], calls
    assert "install -m" not in r.stdout


def test_link_apply_with_new_allowed_ips_goes_by_syncconf(script, tmp_path):
    """Сменились подсети соседей и keepalive: syncconf без перезапуска —
    сессия канала переживает применение; маршрут новой подсети поставлен,
    ушедшей — снят, прочие записи не тронуты."""
    from tests.unit.test_wgconf_allowed import LINK
    old = LINK.replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30",
                       "AllowedIPs = 10.8.1.0/24, 10.99.99.0/30, 192.168.71.0/24")
    new = LINK.replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30",
                       "AllowedIPs = 10.8.1.0/24, 10.99.99.0/30, 192.168.70.0/24").replace(
        "PersistentKeepalive = 25-35", "PersistentKeepalive = 25")
    r, calls, dst = _link_apply(script, tmp_path, new, installed=old, env={"PEER_HOME_NETS": "192.168.70.0/24"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().endswith("link: syncconf"), r.stdout
    assert dst.read_text(encoding="utf-8") == new, "новый конфиг не лёг на место"
    assert oct(dst.stat().st_mode & 0o777) == "0o600", "конфиг с приватным ключом читается не только root"
    assert "awg-quick strip awglink" in calls and any(c.startswith("awg syncconf awglink ") for c in calls), calls
    assert not [c for c in calls if c.startswith(("awg-quick down", "awg-quick up"))], "линк перезапущен ради AllowedIPs"
    assert "ip route replace 192.168.70.0/24 dev awglink" in calls, calls
    assert "ip route del 192.168.71.0/24 dev awglink" in calls, "маршрут ушедшей подсети остался"
    assert not [c for c in calls if c.startswith("ip route del") and "192.168.71.0/24" not in c], (
        f"сняты маршруты оставшихся записей: {calls}")


def test_link_apply_with_a_new_endpoint_restarts_the_link_behind_the_guard(script, tmp_path):
    """Сменился Endpoint: только down/up; таблица защиты загружается до
    подъёма — окна без неё между up и nft -f нет."""
    from tests.unit.test_wgconf_allowed import LINK
    new = LINK.replace("Endpoint = vps.example.org:51821", "Endpoint = vps2.example.org:51821")
    r, calls, dst = _link_apply(script, tmp_path, new, installed=LINK)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().endswith("link: restarted"), r.stdout
    assert dst.read_text(encoding="utf-8") == new
    down = calls.index("awg-quick down awglink")
    nft = next(i for i, c in enumerate(calls) if c.startswith("nft -f "))
    up = calls.index("awg-quick up awglink")
    assert down < nft < up, f"порядок down → nft -f → up нарушен: {calls}"
    assert not [c for c in calls if c.startswith("awg syncconf")]


def test_link_apply_with_the_link_down_brings_it_up_instead_of_syncconf(script, tmp_path):
    """Интерфейса линка нет — syncconf не к чему: поднимаем, не опуская."""
    from tests.unit.test_wgconf_allowed import LINK
    new = LINK.replace("PersistentKeepalive = 25-35", "PersistentKeepalive = 25")
    r, calls, _ = _link_apply(script, tmp_path, new, installed=LINK, up=False)
    assert r.returncode == 0 and r.stdout.rstrip().endswith("link: restarted"), r.stdout
    assert "awg-quick up awglink" in calls and "awg-quick down awglink" not in calls, calls


def test_link_apply_refuses_a_conf_that_fails_the_check_and_installs_nothing(script, tmp_path):
    """AllowedIPs = 0.0.0.0/0, поданный мимо агента: второй рубеж отказывает
    до установки — конфиг на месте не тронут, линк не тронут, код 1."""
    from tests.unit.test_wgconf_allowed import LINK
    bad = LINK.replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30", "AllowedIPs = 0.0.0.0/0")
    r, calls, dst = _link_apply(script, tmp_path, bad, installed=LINK)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "не прошёл проверку — не установлен" in r.stdout and "AllowedIPs" in r.stderr, r.stdout + r.stderr
    assert dst.read_text(encoding="utf-8") == LINK, "конфиг на месте подменён отвергнутым"
    assert not [c for c in calls if c.startswith(("awg", "nft", "ip route"))], calls
    assert "link: " not in r.stdout


def test_link_apply_refuses_hooks_slipped_into_the_conf(script, tmp_path):
    """PostUp исполнился бы от root при следующем awg-quick up."""
    from tests.unit.test_wgconf_allowed import LINK
    r, calls, dst = _link_apply(script, tmp_path, LINK.replace("Jc = 4\n", "Jc = 4\nPostUp = /bin/sh -c id\n"),
                                installed=LINK)
    assert r.returncode == 1 and dst.read_text(encoding="utf-8") == LINK and not _acts(calls)


def test_link_apply_without_the_allow_list_refuses(script, tmp_path):
    """Нет gw-wg-allow.list в поставке — закрыто по умолчанию: конфиг каналом
    не принимается вовсе."""
    from tests.unit.test_wgconf_allowed import LINK
    new = LINK.replace("PersistentKeepalive = 25-35", "PersistentKeepalive = 25")
    r, calls, dst = _link_apply(script, tmp_path, new, installed=LINK, allow=tmp_path / "нет.list")
    assert r.returncode == 1 and "gw-wg-allow.list" in r.stderr, r.stdout + r.stderr
    assert dst.read_text(encoding="utf-8") == LINK and not _acts(calls)


def test_link_apply_without_the_file_is_a_usage_error(script, tmp_path):
    r, calls, dst = _link_apply(script, tmp_path, "", installed=None, src_missing=True)
    assert r.returncode == 2 and "нужен файл" in r.stdout, r.stdout
    assert not dst.exists() and not calls


def test_link_apply_reports_a_failed_syncconf(script, tmp_path):
    """syncconf отказал — код не 0 и без строки итога: агент обязан откатить,
    а не записать «применено»."""
    from tests.unit.test_wgconf_allowed import LINK
    new = LINK.replace("PersistentKeepalive = 25-35", "PersistentKeepalive = 25")
    r, calls, _ = _link_apply(script, tmp_path, new, installed=LINK, syncconf_rc=1)
    assert r.returncode == 1 and "awg syncconf отказал" in r.stdout, r.stdout
    assert "link: " not in r.stdout


def test_link_apply_puts_the_previous_conf_back_when_the_link_does_not_come_up(script, tmp_path):
    """Новый Endpoint не поднялся (`awg-quick up` упал): прежний конфиг
    возвращается на место и поднимается — шлюз не остаётся без линка и без
    канала, по которому пришла бы правка; код 1, строки итога нет."""
    from tests.unit.test_wgconf_allowed import LINK
    new = LINK.replace("Endpoint = vps.example.org:51821", "Endpoint = vps2.example.org:51821")
    r, calls, dst = _link_apply(script, tmp_path, new, installed=LINK, up_rc=1)
    assert r.returncode == 1, r.stdout + r.stderr
    assert dst.read_text(encoding="utf-8") == LINK, "на месте остался не поднявшийся конфиг"
    assert oct(dst.stat().st_mode & 0o777) == "0o600"
    assert calls.count("awg-quick up awglink") == 2, f"прежний конфиг не подняли: {calls}"
    assert "link: " not in r.stdout and "прежний конфиг линка возвращён" in r.stdout
    assert not (dst.parent / "awglink.conf.before").exists(), "копия прежнего конфига осталась"


def test_link_apply_puts_the_previous_conf_back_when_syncconf_fails(script, tmp_path):
    """syncconf отказал: интерфейс жив на прежних значениях — файл на диске
    обязан совпасть с ними, иначе следующий реассерт поднял бы непроверенное."""
    from tests.unit.test_wgconf_allowed import LINK
    new = LINK.replace("PersistentKeepalive = 25-35", "PersistentKeepalive = 25")
    r, calls, dst = _link_apply(script, tmp_path, new, installed=LINK, syncconf_rc=1)
    assert r.returncode == 1, r.stdout + r.stderr
    assert dst.read_text(encoding="utf-8") == LINK, "после отказа syncconf на месте новый конфиг"
    assert not [c for c in calls if c.startswith("awg-quick up")], "интерфейс жив — поднимать нечего"


def test_link_apply_uses_the_interface_subnets_the_agent_passed(script, tmp_path):
    """Подсети интерфейсов агент передаёт скрипту тем же списком, по которому
    судил сам: оба рубежа решают одинаково. Пересечение с переданной подсетью —
    отказ, даже если маршрутов такой подсети на хосте не видно."""
    from tests.unit.test_wgconf_allowed import LINK
    bad = LINK.replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30",
                       "AllowedIPs = 10.8.1.0/24, 10.99.99.0/30, 192.168.70.0/24")
    r, calls, dst = _link_apply(script, tmp_path, bad, installed=LINK,
                                env={"PEER_HOME_NETS": "192.168.70.0/24", "IFACE_NETS": "192.168.70.0/25"})
    assert r.returncode == 1 and "пересекается" in r.stderr, r.stdout + r.stderr
    assert dst.read_text(encoding="utf-8") == LINK
