"""
Режимы `routing-gw-setup.sh --uplink <файл>`, `--uplink-commit` и
`--uplink-revert` на шлюзе: конфиг аплинка из упр. канала (человек подтвердил
в чате агента или сработало аварийное правило) и итог пробы агента; замок
обвязки и сверка прогона юнита с юнитом на диске.

Аплинк — единственная дорога агента в Telegram. Цена ошибки: конфиг с
ключом вне белого списка (PostUp от root) исполнился бы при подъёме; конфиг
без Table = off увёл бы весь трафик шлюза в туннель; не поднявшийся аплинк,
оставленный на месте, отрезает агента от чата, и чинить шлюз придётся руками;
копии прежнего, копящиеся .bak-<время>, — ключи устройства по всему каталогу;
прогон юнита, дождавшийся замка после транзакции агента, — юнит со старыми
ключами признания поверх новых.

Режим прогоняется под sh из настоящего скрипта (функции проверки, рендер
политики, установка, сам блок режима); `systemctl` и `awg-quick` — подставные
команды с журналом вызовов, `install`, `cp`, `cmp` — настоящие: что легло на
место, видно по файлу.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import SH, fake_cmd
from tests.unit._gw_setup_common import _helpers

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"

UPLINK = """[Interface]
PrivateKey = DUMMY-UPLINK-PRIVATE-KEY=
Address = 10.8.1.5/32
Table = off
Jc = 4
Jmin = 40
Jmax = 70

[Peer]
PublicKey = DUMMY-VPS-PUBLIC-KEY=
PresharedKey = DUMMY-UPLINK-PSK=
Endpoint = vps.example.org:43125
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
UPLINK_NEW = UPLINK.replace("Endpoint = vps.example.org:43125", "Endpoint = vps2.example.org:43125")

POLICY = [
    'PostUp = ip rule list | grep -q "fwmark 0x1 lookup 100" || ip rule add fwmark 0x1 lookup 100',
    "PostUp = ip route replace default dev %i table 100",
    "PostUp = sysctl -qw net.ipv4.conf.%i.rp_filter=2",
    "PostDown = ip route del default dev %i table 100 2>/dev/null || true",
]


def _rendered(text: str) -> str:
    """Конфиг аплинка так, как его ставит скрипт: строки политики сразу за Table = off."""
    return text.replace("Table = off\n", "Table = off\n" + "\n".join(POLICY) + "\n")


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


class _Gw:
    """Шлюз для режима --uplink: каталог конфигов, /etc/awg-gw, systemctl с
    исходом `start` по порядку вызовов (`start_rc` — список, последний
    повторяется), awg-quick — только журнал."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"; self.bin.mkdir()
        self.conf_dir = tmp_path / "amneziawg"; self.conf_dir.mkdir()
        self.etc = tmp_path / "awg-gw"
        self.log = tmp_path / "calls.log"; self.log.write_text("", encoding="utf-8")
        self.dst = self.conf_dir / "awg0.conf"
        self.src = tmp_path / "uplink.next"
        self.start_rc = [0]
        self.fail: dict[str, list[int]] = {}           # {"cp"|"install": [1, 0, …]} — исходы по порядку вызовов

    def run(self, script: str, src_text: str | None, *, installed: str | None = None,
            allow: Path | None = None, uplink_if: str | None = "awg0",
            mode: str = "uplink") -> subprocess.CompletedProcess:
        from awgbot.util import wgconf
        if installed is not None:
            self.dst.write_text(installed, encoding="utf-8")
        if src_text is not None:
            self.src.write_text(src_text, encoding="utf-8")
        seq = self.tmp / "start.seq"
        seq.write_text(" ".join(str(x) for x in self.start_rc), encoding="utf-8")
        # systemctl start берёт очередной исход из списка; последний повторяется
        fake_cmd(self.bin / "systemctl",
                 f'echo "systemctl $*" >> {self.log}\n'
                 f'if [ "$1" = start ]; then set -- $(cat {seq}); rc=$1; [ $# -gt 1 ] && shift; '
                 f'echo "$*" > {seq}; exit $rc; fi\nexit 0\n')
        fake_cmd(self.bin / "awg-quick", f'echo "awg-quick $*" >> {self.log}\nexit 0\n')
        # cp и install — настоящие, пока тест не велел им отказать: <имя>.fail —
        # исходы по порядку вызовов (1 — отказ), кончились — дальше настоящие
        for name, real in (("cp", "/bin/cp"), ("install", "/usr/bin/install")):
            fails = self.tmp / f"{name}.fail"
            fails.write_text(" ".join(str(x) for x in self.fail.get(name, [])), encoding="utf-8")
            fake_cmd(self.bin / name,
                     f'set -- "$@"; seq=$(cat {fails}); first=${{seq%% *}}; rest=${{seq#"$first"}}; '
                     f'echo "${{rest# }}" > {fails}\n'
                     f'if [ "$first" = 1 ]; then echo "{name} $*" >> {self.log}; exit 1; fi\n'
                     f'exec {real} "$@"\n')
        allow = wgconf.ALLOW_LIST if allow is None else allow
        start = script.index("uplink_render() {")
        fns_end = script.index('if [ "$MODE" = "link-apply" ]; then')
        block = script.index('if [ "$MODE" = "uplink" ]; then')
        end = script.index("\nfi\n", block) + 4
        fin = script.index('if [ "$MODE" = "uplink-revert" ] || [ "$MODE" = "uplink-commit" ]; then')
        fin_end = script.index("\nfi\n", fin) + 4
        prog = ("set -e\n" + _helpers(script)
                + f'\nMODE={mode}\nSRC_CONF="{self.src if src_text is not None else self.tmp / "нет"}"\n'
                f'HOST_CONF_DIR="{self.conf_dir}"\nGW_ETC="{self.etc}"\nAWG_QUICK=awg-quick\n'
                'TG_MARK=0x1\nUPLINK_TABLE=100\n'
                + script[start:fns_end]
                + f'\nallow_list_path() {{ printf "%s" "{allow}"; [ -f "{allow}" ]; }}\n'
                + script[block:end] + script[fin:fin_end])
        env = {"PATH": f"{self.bin}:/usr/bin:/bin"}
        if uplink_if is not None:
            env["UPLINK_IF"] = uplink_if
        return subprocess.run([SH, "-c", prog], capture_output=True, text=True, env=env)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


@pytest.fixture()
def gw(tmp_path):
    return _Gw(tmp_path)


def _last(r) -> str:
    return r.stdout.rstrip().splitlines()[-1] if r.stdout.strip() else ""


def test_a_new_uplink_is_installed_with_the_policy_and_brought_up_by_the_unit(script, gw):
    """Новый конфиг: на месте — он же с PostUp политики внутри [Interface]
    (в [Peer] их awg-quick не вырезает и подъём падает), права 0600, подъём —
    юнитом awg-quick@<аплинк>, итог «uplink: installed»; временный файл убран."""
    r = gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _last(r) == "uplink: installed", r.stdout
    text = gw.dst.read_text(encoding="utf-8")
    assert text == _rendered(UPLINK_NEW), text
    assert text.index(POLICY[-1]) < text.index("[Peer]"), "строки политики не в [Interface]"
    assert gw.dst.stat().st_mode & 0o777 == 0o600, "конфиг с приватным ключом читается не только root"
    calls = gw.calls()
    assert calls.index("systemctl stop awg-quick@awg0") < calls.index("systemctl start awg-quick@awg0"), calls
    assert "systemctl enable awg-quick@awg0" in calls
    assert not (gw.etc / "uplink.render").exists(), "временный конфиг с ключом остался на диске"


def test_the_same_uplink_is_left_alone(script, gw):
    """Тот же конфиг — «uplink: unchanged» без единого вызова systemctl:
    перезапуск аплинка ради ничего рвёт связь агента с Telegram."""
    r = gw.run(script, UPLINK, installed=_rendered(UPLINK))
    assert r.returncode == 0 and _last(r) == "uplink: unchanged", r.stdout + r.stderr
    assert gw.calls() == [], gw.calls()


def test_an_uplink_that_does_not_come_up_puts_the_previous_one_back(script, gw):
    """Юнит с новым конфигом не поднялся — прежний конфиг на место и юнит
    поднят на нём; код 1, строки итога нет (и не «refused» — на диске
    менялось): агент не запишет «применено» и позовёт итог пробы."""
    gw.start_rc = [1, 0]
    r = gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "uplink:" not in r.stdout and "откатываю на прежний" in r.stdout, r.stdout
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK), "на месте остался не поднявшийся аплинк"
    assert gw.calls().count("systemctl start awg-quick@awg0") == 2, f"прежний не подняли: {gw.calls()}"
    assert "systemctl enable awg-quick@awg0" not in gw.calls()


@pytest.mark.parametrize("extra", ["PostUp = /bin/sh -c id", "DNS = 10.8.1.1", "SaveConfig = true"])
def test_a_key_outside_the_allow_list_is_refused_before_anything_is_touched(script, gw, extra):
    """PostUp исполнился бы от root при подъёме; DNS переписал бы резолвер
    шлюза; SaveConfig — конфиг на диске. Отказ до установки: на месте
    прежний, systemctl не звали, код 1, итог «uplink: refused» — по нему
    агент знает, что возвращать нечего."""
    bad = UPLINK_NEW.replace("Table = off\n", f"Table = off\n{extra}\n")
    r = gw.run(script, bad, installed=_rendered(UPLINK))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "не прошёл проверку — не установлен" in r.stdout and "вне белого списка" in r.stderr, r.stdout + r.stderr
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK) and gw.calls() == []
    assert _last(r) == "uplink: refused", "агент не отличит отказ проверки от неподнявшегося аплинка"


def test_an_uplink_without_table_off_is_refused(script, gw):
    """Без Table = off awg-quick поставил бы маршрут по умолчанию в туннель —
    весь трафик шлюза, включая локальную сеть, ушёл бы на ВПС."""
    r = gw.run(script, UPLINK_NEW.replace("Table = off\n", ""), installed=_rendered(UPLINK))
    assert r.returncode == 1 and "нет Table = off" in r.stdout and _last(r) == "uplink: refused", r.stdout + r.stderr
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK) and gw.calls() == []


def test_without_the_allow_list_the_uplink_is_refused(script, gw):
    """Нет gw-wg-allow.list в поставке — закрыто по умолчанию."""
    r = gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK), allow=gw.tmp / "нет.list")
    assert r.returncode == 1 and "gw-wg-allow.list" in r.stderr and _last(r) == "uplink: refused", r.stdout + r.stderr
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK) and gw.calls() == []


def test_without_the_file_or_the_interface_name_it_is_a_usage_error(script, gw):
    """Нет файла или не назван аплинк (UPLINK_IF агент берёт из своего
    определения) — код 2, ничего не тронуто."""
    r = gw.run(script, None, installed=_rendered(UPLINK))
    assert r.returncode == 2 and "нужен файл" in r.stdout and _last(r) == "uplink: refused", r.stdout
    r = gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK), uplink_if=None)
    assert r.returncode == 2 and "UPLINK_IF" in r.stdout and _last(r) == "uplink: refused", r.stdout
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK) and gw.calls() == []


def test_a_first_uplink_without_a_previous_one_is_installed(script, gw):
    """Аплинка на месте ещё нет (имя известно, файла нет) — ставится и
    поднимается; возвращать при отказе нечего."""
    r = gw.run(script, UPLINK)
    assert r.returncode == 0 and _last(r) == "uplink: installed", r.stdout + r.stderr
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK)


# ── одна копия прежнего и итог пробы ─────────────────────────────────────────

def _baks(gw) -> list[str]:
    return sorted(p.name for p in gw.conf_dir.iterdir() if ".bak" in p.name)


def test_the_previous_uplink_is_kept_as_a_single_before_copy(script, gw):
    """Новый конфиг встал — прежний лежит ровно одной копией <аплинк>.conf.before
    (её ждёт итог пробы агента), без .bak-<время>: копии с ключом устройства
    не копятся; следующая установка заменяет копию, а не добавляет вторую."""
    r = gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    assert r.returncode == 0, r.stdout + r.stderr
    before = gw.conf_dir / "awg0.conf.before"
    assert before.read_text(encoding="utf-8") == _rendered(UPLINK), "копия — не прежний конфиг"
    assert _baks(gw) == [], _baks(gw)
    third = UPLINK.replace("Endpoint = vps.example.org:43125", "Endpoint = vps3.example.org:43125")
    gw.run(script, third)
    assert before.read_text(encoding="utf-8") == _rendered(UPLINK_NEW), "копия не заменена следующей"
    assert sorted(p.name for p in gw.conf_dir.iterdir()) == ["awg0.conf", "awg0.conf.before"]


def test_an_uplink_that_does_not_come_up_leaves_no_copy_behind(script, gw):
    """Скрипт сам вернул прежний — копия больше не нужна: итог пробы агента
    (--uplink-revert) увидит «unchanged», а не вернёт ещё раз."""
    gw.start_rc = [1, 0]
    gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    assert not (gw.conf_dir / "awg0.conf.before").exists() and _baks(gw) == []


def test_commit_drops_the_copy_and_touches_nothing_else(script, gw):
    """--uplink-commit: копия прочь, «uplink: committed», аплинк не
    перезапускается — он только что дал хендшейк."""
    gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    gw.log.write_text("", encoding="utf-8")
    r = gw.run(script, None, mode="uplink-commit")
    assert r.returncode == 0 and _last(r) == "uplink: committed", r.stdout + r.stderr
    assert not (gw.conf_dir / "awg0.conf.before").exists()
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK_NEW) and gw.calls() == []


def test_revert_puts_the_previous_file_back_byte_for_byte_without_the_allow_list(script, gw):
    """--uplink-revert: прежний назад байт в байт — даже со строками, которых
    белый список не пустил бы (файл с этого же диска, а не данные канала),
    права 0600, stop/down/start юнитом, копия прочь, «uplink: reverted»."""
    old = _rendered(UPLINK) + "# руками: прежний конфиг\n"
    gw.dst.write_text(old, encoding="utf-8")
    gw.run(script, UPLINK_NEW)
    gw.log.write_text("", encoding="utf-8")
    r = gw.run(script, None, mode="uplink-revert")
    assert r.returncode == 0 and _last(r) == "uplink: reverted", r.stdout + r.stderr
    assert gw.dst.read_text(encoding="utf-8") == old, "прежний вернулся не байт в байт"
    assert gw.dst.stat().st_mode & 0o777 == 0o600
    assert not (gw.conf_dir / "awg0.conf.before").exists()
    calls = gw.calls()
    assert calls.index("systemctl stop awg-quick@awg0") < calls.index("awg-quick down awg0") < (
        calls.index("systemctl start awg-quick@awg0")), calls


def test_revert_says_so_when_the_previous_uplink_does_not_come_up(script, gw):
    gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    gw.start_rc = [1]
    r = gw.run(script, None, mode="uplink-revert")
    assert r.returncode == 1 and "на прежнем конфиге не поднялся" in r.stdout, r.stdout + r.stderr
    assert "uplink: reverted" not in r.stdout
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK), "прежний файл не вернулся"


def test_revert_without_a_copy_changes_nothing(script, gw):
    """Копии нет (скрипт уже вернул прежний сам, или конфиг не менялся) —
    «uplink: unchanged» без перезапуска аплинка."""
    r = gw.run(script, None, installed=_rendered(UPLINK), mode="uplink-revert")
    assert r.returncode == 0 and _last(r) == "uplink: unchanged", r.stdout + r.stderr
    assert gw.calls() == [] and gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK)


@pytest.mark.parametrize("mode", ["uplink-commit", "uplink-revert"])
def test_commit_and_revert_need_the_interface_name(script, gw, mode):
    gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    gw.log.write_text("", encoding="utf-8")
    r = gw.run(script, None, mode=mode, uplink_if=None)
    assert r.returncode == 2 and "UPLINK_IF" in r.stdout, r.stdout + r.stderr
    assert (gw.conf_dir / "awg0.conf.before").exists() and gw.calls() == []


# ── замок обвязки и сверка прогона юнита ─────────────────────────────────────

OLD_PUB = "DUMMY-OLD-GATEWAY-PUBKEY="
NEW_PUB = "DUMMY-NEW-GATEWAY-PUBKEY="


def _unit_text(script: str, tmp_path, **env) -> str:
    """Юнит так, как его пишет сам скрипт (его heredoc под sh с этими значениями)."""
    tpl = script.split('cat > "$UNIT" <<UNITEOF', 1)[1].split("\nUNITEOF", 1)[0]
    base = {"CLIENT_SUBNET": "10.8.1.0/24", "LINK_IF": "awglink", "ADMIN_IPS": "10.8.1.2 10.8.1.3",
            "GATEWAY_PUBKEY": OLD_PUB, "GATEWAY_PREV_PUBKEY": "", "LAN_MODE": "1",
            "HOME_SUBNETS": "192.168.68.0/24", "RESOLVER": "10.9.1.1", "PEER_HOME_NETS": "",
            "LINK_CHANNEL": "1", "LINK_CHANNEL_PORT": "8787", "FW_ENV": "/etc/awg-gw/firewall.env",
            "SELF": "/usr/local/sbin/routing-gw-setup.sh", "HOST_CONF_DIR": "/etc/amnezia/amneziawg"}
    base.update(env)
    r = subprocess.run([SH, "-c", f"cat <<UNITEOF\n{tpl}\nUNITEOF\n"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", **base})
    assert r.returncode == 0, r.stderr
    return r.stdout


class _Lock:
    """Начало скрипта до `say()`: снимок строк юнита, замок, сверка с юнитом на
    диске. flock — подставной с журналом; после блока прогон печатает, с
    какими значениями он пошёл дальше."""

    def __init__(self, script: str, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / "lockbin"; self.bin.mkdir()
        self.log = tmp_path / "flock.log"; self.log.write_text("", encoding="utf-8")
        self.unit = tmp_path / "awg-link-gw.service"
        keys = script[script.index("UNIT_ENV_KEYS="):]
        keys = keys[:keys.index("\n", keys.index("UNIT_ENV0=")) + 1]
        start = script.index("unit_file_env() {")
        block = script[start:script.index("\nsay()  {", start)]
        assert "/run/awg-gw-apply.lock" in block, "замок ушёл с прежнего пути — сверь тест"
        block = block.replace("/run/awg-gw-apply.lock", str(tmp_path / "apply.lock"))
        self.prog = tmp_path / "prog.sh"
        self.prog.write_text(
            # STARTED — как производные переменные скрипта выше замка: перезапуск
            # (а не переприсвоение на месте) пересчитывает и их
            keys + f'MODE=apply\nUNIT="{self.unit}"\nSTARTED="$GATEWAY_PUBKEY"\n' + block
            + '\necho "RUN GATEWAY_PUBKEY=$GATEWAY_PUBKEY ADMIN_IPS=$ADMIN_IPS HELD=${AWG_GW_LOCK_HELD:-}"\n'
            + 'echo "STARTED=$STARTED"\n',
            encoding="utf-8")
        self.flock_rc = 0

    def run(self, **env) -> subprocess.CompletedProcess:
        fake_cmd(self.bin / "flock", f'echo "flock $*" >> {self.log}\nexit {self.flock_rc}\n')
        return subprocess.run([SH, str(self.prog), "--apply", "/etc/amnezia/amneziawg/awglink.conf"],
                              capture_output=True, text=True,
                              env={"PATH": f"{self.bin}:/usr/bin:/bin", **env})

    def flocks(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


_STARTED = {"CLIENT_SUBNET": "10.8.1.0/24", "LINK_IF": "awglink", "ADMIN_IPS": "10.8.1.2 10.8.1.3",
            "GATEWAY_PUBKEY": OLD_PUB, "GATEWAY_PREV_PUBKEY": "", "LAN_MODE": "1",
            "HOME_SUBNETS": "192.168.68.0/24", "RESOLVER": "10.9.1.1", "PEER_HOME_NETS": "",
            "LINK_CHANNEL_PORT": "8787"}


@pytest.fixture()
def lock(script, tmp_path):
    return _Lock(script, tmp_path)


def _runs(r) -> list[str]:
    return [line for line in r.stdout.splitlines() if line.startswith("RUN ")]


def test_a_unit_pass_restarts_itself_with_the_keys_the_agent_wrote_while_it_waited(script, lock, tmp_path):
    """Прогон юнита стартовал со старыми ключами признания, а пока ждал
    замка, агент записал в юнит новые: после замка прогон перезапускает себя
    со строками с диска (и не берёт замок второй раз) — иначе он дописал бы
    юнит старыми ключами поверх новых и обвязка опустила бы линк."""
    lock.unit.write_text(_unit_text(script, tmp_path, GATEWAY_PUBKEY=NEW_PUB), encoding="utf-8")
    r = lock.run(AWG_GW_UNIT_RUN="1", **_STARTED)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _runs(r) == [f"RUN GATEWAY_PUBKEY={NEW_PUB} ADMIN_IPS=10.8.1.2 10.8.1.3 HELD=1"], r.stdout + r.stderr
    assert "юнит переписан, пока прогон ждал замка (GATEWAY_PUBKEY)" in r.stderr, r.stderr
    assert f"STARTED={NEW_PUB}" in r.stdout, "значения переприсвоены на месте, а не перезапуском с начала"
    assert len(lock.flocks()) == 1, f"замок взят дважды: {lock.flocks()}"


def test_a_unit_pass_matching_the_unit_on_disk_goes_on_without_a_restart(script, lock, tmp_path):
    """Юнит на диске тот же, что дал systemd (с кавычками у ADMIN_IPS и
    подсетей, с пустыми строками) — прогон идёт дальше как есть: ложная
    «разница» перезапускала бы каждый прогон юнита."""
    lock.unit.write_text(_unit_text(script, tmp_path), encoding="utf-8")
    r = lock.run(AWG_GW_UNIT_RUN="1", **_STARTED)
    assert r.returncode == 0 and "юнит переписан" not in r.stderr, r.stdout + r.stderr
    assert _runs(r) == [f"RUN GATEWAY_PUBKEY={OLD_PUB} ADMIN_IPS=10.8.1.2 10.8.1.3 HELD="], r.stdout


def test_a_pass_not_started_by_the_unit_does_not_compare(script, lock, tmp_path):
    """Прогон руками или из файла конфигурации несёт свои значения — их и
    применяет, сверять с юнитом нечего (юнит он сам перепишет)."""
    lock.unit.write_text(_unit_text(script, tmp_path, GATEWAY_PUBKEY=NEW_PUB), encoding="utf-8")
    r = lock.run(**_STARTED)
    assert r.returncode == 0 and "юнит переписан" not in r.stderr, r.stdout + r.stderr
    assert _runs(r) == [f"RUN GATEWAY_PUBKEY={OLD_PUB} ADMIN_IPS=10.8.1.2 10.8.1.3 HELD="], r.stdout


def test_a_held_lock_is_not_taken_again(script, lock, tmp_path):
    """AWG_GW_LOCK_HELD=1 — замок держит агент на всю транзакцию аплинка:
    второй flock ждал бы сам себя 5 минут и уронил бы режим."""
    lock.unit.write_text(_unit_text(script, tmp_path, GATEWAY_PUBKEY=NEW_PUB), encoding="utf-8")
    r = lock.run(AWG_GW_LOCK_HELD="1", AWG_GW_UNIT_RUN="1", **_STARTED)
    assert r.returncode == 0 and lock.flocks() == [], lock.flocks()
    assert _runs(r) == [f"RUN GATEWAY_PUBKEY={OLD_PUB} ADMIN_IPS=10.8.1.2 10.8.1.3 HELD=1"], r.stdout


def test_a_lock_not_obtained_in_five_minutes_stops_the_run(script, lock, tmp_path):
    lock.flock_rc = 1
    r = lock.run(**_STARTED)
    assert r.returncode == 75 and "не дождался за 5 минут" in r.stderr, r.stdout + r.stderr
    assert _runs(r) == []


def test_the_unit_marks_its_own_runs_for_the_comparison(script, tmp_path):
    """Сверка срабатывает только у прогона юнита — строка AWG_GW_UNIT_RUN=1
    обязана быть в юните, который пишет скрипт."""
    assert "Environment=AWG_GW_UNIT_RUN=1\n" in _unit_text(script, tmp_path)


# ── отказ записи на каждом шаге ──────────────────────────────────────────────

def test_a_copy_that_cannot_be_taken_refuses_before_touching_the_uplink(script, gw):
    """Копию прежнего снять не вышло (диск) — ставить новый нельзя: вернуть
    было бы нечего. Отказ до установки: «uplink: refused», на месте прежний,
    systemctl не звали."""
    gw.fail = {"cp": [1]}
    r = gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    assert r.returncode == 1 and "копия прежнего конфига аплинка не снята" in r.stdout, r.stdout + r.stderr
    assert _last(r) == "uplink: refused", r.stdout
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK)
    assert not [c for c in gw.calls() if c.startswith("systemctl")], gw.calls()


def test_a_write_that_fails_puts_the_copy_back_and_refuses(script, gw):
    """Новый конфиг не записался — прежний из копии на месте, копия убрана,
    «uplink: refused» (агенту возвращать нечего), аплинк не перезапускали."""
    gw.fail = {"install": [1]}
    r = gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    assert r.returncode == 1 and "конфиг аплинка не записан" in r.stdout, r.stdout + r.stderr
    assert _last(r) == "uplink: refused", r.stdout
    assert gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK)
    assert not (gw.conf_dir / "awg0.conf.before").exists()
    assert not [c for c in gw.calls() if c.startswith("systemctl")], gw.calls()


def test_a_failed_start_whose_copy_cannot_come_back_keeps_the_copy(script, gw):
    """Новый не поднялся, и прежний из копии не вернулся (запись): копия
    .before остаётся — по ней --uplink-revert агента вернёт прежний позже;
    строки «refused» нет (на диске новый)."""
    gw.start_rc = [1]
    gw.fail = {"cp": [0, 1]}
    r = gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "прежний конфиг аплинка не вернулся — копия:" in r.stdout, r.stdout
    assert "uplink: refused" not in r.stdout
    before = gw.conf_dir / "awg0.conf.before"
    assert before.read_text(encoding="utf-8") == _rendered(UPLINK), "копия прежнего потеряна"
    gw.start_rc = [0]
    gw.fail = {}
    back = gw.run(script, None, mode="uplink-revert")
    assert _last(back) == "uplink: reverted" and gw.dst.read_text(encoding="utf-8") == _rendered(UPLINK), back.stdout


def test_revert_whose_write_fails_says_so(script, gw):
    """--uplink-revert: прежний не записался — код 1 и причина, а не
    «reverted»: агент скажет человеку, что прежний не вернулся."""
    gw.run(script, UPLINK_NEW, installed=_rendered(UPLINK))
    gw.fail = {"install": [1]}
    r = gw.run(script, None, mode="uplink-revert")
    assert r.returncode == 1 and "ОШИБКА: прежний конфиг аплинка не вернулся (запись)" in r.stdout, r.stdout + r.stderr
    assert "uplink: reverted" not in r.stdout
    assert (gw.conf_dir / "awg0.conf.before").exists(), "копия потеряна при неудачном возврате"


def test_commit_and_revert_come_before_the_wan_detection(script):
    """Итог пробы аплинка работает без маршрута по умолчанию (аплинк мог
    его и увести): режимы обрабатываются раньше определения выхода."""
    fin = script.index('if [ "$MODE" = "uplink-revert" ] || [ "$MODE" = "uplink-commit" ]; then')
    assert fin < script.index('WAN_IF="$(detect_wan)"'), "итог пробы ждёт интерфейса выхода"
