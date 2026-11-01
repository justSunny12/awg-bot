"""routing-gw-setup.sh, п.3a и откат: ACCEPT в чужой ip filter FORWARD — с
меткой `-m comment --comment awg-bot`; `--rollback` снимает помеченные на любом
интерфейсе, а прежние без метки — только на своих (линк, аплинк, линк к
резерву), не на интерфейсе локальной сети и не на чужих.

Функции скрипта гоняются под sh с подставным `iptables`: цепочка FORWARD —
файл, строка — правило в том виде, как его печатает `iptables -S` (без
`-A FORWARD`).

Цена ошибки: откат, снимающий `-i/-o <интерфейс> -j ACCEPT` на всех
интерфейсах, ломает чужой файервол хозяина устройства (Docker, OMV, ufw с
политикой DROP) — после `awg-bot uninstall` пропадает транзит, который ставили
не мы; правило без метки, не снятое на своём линке, — открытый транзит из
туннеля мимо чужой политики DROP после снятия шлюза; агент, не узнающий
помеченное правило, — ложная красная проверка «политика FORWARD» на каждом
шлюзе за чужим файерволом.
"""
from __future__ import annotations

import pytest

from tests.conftest import fake_cmd
from tests.unit import _gw_setup_common as _c
from tests.unit._gw_setup_common import _helpers, _sh

pytestmark = pytest.mark.unit

script = _c.script

TAG = "-m comment --comment awg-bot -j ACCEPT"


def _fn(script: str, name: str) -> str:
    fn = script[script.index(f"\n{name}() {{") + 1:]
    return fn[:fn.index("\n}\n") + 3]


class _Fwd:
    """Подставной iptables: -S, -C, -D, -I по файлу правил цепочки FORWARD."""

    def __init__(self, tmp_path, rules: list[str]):
        self.tmp = tmp_path
        self.rules = tmp_path / "forward.rules"
        self.rules.write_text("".join(r + "\n" for r in rules), encoding="utf-8")
        fake_cmd(tmp_path / "iptables",
                 '[ "$1" = "-w" ] && shift\n'
                 'op="$1"; shift; [ "$1" = FORWARD ] || exit 2; shift\n'
                 f'R={self.rules}\n'
                 'case "$op" in\n'
                 '  -S) echo "-P FORWARD DROP"; sed "s/^/-A FORWARD /" "$R"; exit 0 ;;\n'
                 '  -C) grep -qxF -- "$*" "$R"; exit $? ;;\n'
                 '  -D) grep -qxF -- "$*" "$R" || exit 1\n'
                 '      awk -v r="$*" \'!d && $0 == r {d = 1; next} {print}\' "$R" > "$R.t" && mv "$R.t" "$R"; exit 0 ;;\n'
                 '  -I) shift; { printf "%s\\n" "$*"; cat "$R"; } > "$R.t" && mv "$R.t" "$R"; exit 0 ;;\n'
                 'esac\nexit 2\n')

    def now(self) -> list[str]:
        return [ln for ln in self.rules.read_text(encoding="utf-8").splitlines() if ln]

    def run(self, script: str, body: str):
        prog = ('MODE="apply"\n' + _helpers(script) + 'FWD_TAG="awg-bot"\n'
                + "".join(_fn(script, n) for n in ("fwd_allow", "fwd_tagged", "fwd_rollback")) + body)
        r = _sh(prog, path=f"{self.tmp}:/usr/bin:/bin")
        assert r.returncode == 0, r.stdout + r.stderr
        return r


def test_the_accepts_of_3a_carry_the_tag(script, tmp_path):
    f = _Fwd(tmp_path, ["-i eth0 -j ACCEPT"])
    f.run(script, "fwd_allow -i awglink; fwd_allow -o awglink; fwd_allow -i awglink\n")
    assert f.now() == [f"-o awglink {TAG}", f"-i awglink {TAG}", "-i eth0 -j ACCEPT"], \
        "правило без метки или дважды при повторном прогоне"
    sec = script.split('step "3a. Чужая политика FORWARD"', 1)[1].split('step "4.', 1)[0]
    assert "fwd_allow -i" in sec and '"$LINK_IF" "${UPLINK_IF:-}" "${LAN_IF_PRE:-}"' in sec


def test_rollback_takes_tagged_rules_anywhere_and_untagged_only_on_our_interfaces(script, tmp_path):
    f = _Fwd(tmp_path, [
        f"-i awglink {TAG}", f"-o awglink {TAG}", f"-i eth0 {TAG}", f"-o eth0 {TAG}",
        "-i awglink -j ACCEPT", "-o awg0 -j ACCEPT", "-i awgrsv -j ACCEPT",      # прежний выпуск, свои
        "-i eth0 -j ACCEPT", "-i docker0 -j ACCEPT", "-o br0 -j ACCEPT",        # без метки, не свои
        "-i br0 ! -o br0 -j ACCEPT", "-j ufw-user-forward",
    ])
    r = f.run(script, "fwd_rollback awglink awg0 awgrsv\n")
    assert f.now() == ["-i eth0 -j ACCEPT", "-i docker0 -j ACCEPT", "-o br0 -j ACCEPT",
                       "-i br0 ! -o br0 -j ACCEPT", "-j ufw-user-forward"], f.now()
    assert "остались ACCEPT без метки (br0 docker0 eth0)" in r.stdout, r.stdout


def test_a_quoted_tag_is_recognised_too(script, tmp_path):
    """iptables прежних сборок печатает комментарий в кавычках."""
    f = _Fwd(tmp_path, ['-i awglink -m comment --comment "awg-bot" -j ACCEPT',
                        '-o awglink -m comment --comment "awg-botx" -j ACCEPT',
                        '-i eth0 -m comment --comment docker -j ACCEPT'])
    r = f.run(script, "fwd_tagged\n")
    assert r.stdout.splitlines() == ["-i awglink"], r.stdout


def test_nothing_left_untagged_says_nothing(script, tmp_path):
    f = _Fwd(tmp_path, [f"-i awglink {TAG}", "-j ufw-user-forward"])
    r = f.run(script, "fwd_rollback awglink '' awgrsv\n")
    assert f.now() == ["-j ufw-user-forward"]
    assert "без метки" not in r.stdout, r.stdout


def test_rollback_passes_the_link_the_uplink_and_the_reserve_link(script):
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "grep -v '^lo$'" not in rollback, "откат снова перебирает все интерфейсы хоста"
    assert "s/^UPLINK_IF=//p" in rollback and '"$GW_STATUS_FILE"' in rollback
    assert 'fwd_rollback "$LINK_IF" "$_up_if"' in rollback and "awgrsv" in rollback
    assert rollback.index("fwd_rollback") < rollback.index("rm -f $GUARD_FILE $GW_STATUS_FILE"), \
        "аплинк читается из статуса после его удаления"
