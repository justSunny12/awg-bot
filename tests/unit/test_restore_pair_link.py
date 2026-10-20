"""`awg-bot restore` и линк пары серверов: конфиг `awg/awgpair.conf` из копии
кладётся на место и поднимается скриптом линка пары (`--adopt`: юнит и
подъём) до старта сервиса; копии без него — скрипт не зовётся.

Прогоняется настоящая cmd_restore из awg-bot.sh (bash) обвязкой
test_restore_safety; `sh` подменён функцией, которая пишет в журнал, какой
скрипт и с каким PAIR_IF позван.

Цена ошибки: восстановленный сервер с записанной парой, а линк пары не
поднят (юнита нет — и после перезагрузки тоже) — резерв мёртв, пока админ не
выпустит новое приглашение.
"""
from __future__ import annotations

import io
import json
import subprocess
import tarfile

import pytest

from tests.botscript import flat
from tests.unit.test_restore_safety import Host, prelude

pytestmark = pytest.mark.unit

PAIR = b"[Interface]\nAddress = 10.99.98.2/30\nPrivateKey = DUMMY\n\n[Peer]\nAllowedIPs = 10.99.98.1/32\n"


@pytest.fixture(scope="module")
def script() -> str:
    return flat()


def _with_pair(h: Host) -> None:
    """Копия несёт конфиг линка пары; скрипт линка пары есть в поставке."""
    with tarfile.open(h.plain, "r:gz") as tar:
        files = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    files["awg/awgpair.conf"] = PAIR
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, raw in files.items():
            ti = tarfile.TarInfo(name); ti.size = len(raw); ti.mode = 0o600
            tar.addfile(ti, io.BytesIO(raw))
    h.plain.write_bytes(buf.getvalue())


def _run(script, h: Host) -> subprocess.CompletedProcess:
    (h.tmp / "opt" / "install").mkdir(parents=True, exist_ok=True)
    (h.tmp / "opt" / "install" / "reserve-pair-setup.sh").write_text("# подставной\n", encoding="utf-8")
    sh = (f'sh() {{ case "$1" in *reserve-pair-setup.sh) echo "pair PAIR_IF=${{PAIR_IF:-}} $2" >> "{h.log}" ;; '
          f'*routing-host-setup.sh) echo "host-setup $2" >> "{h.log}" ;; *) echo "adopt $LINK_IF $*" >> "{h.log}" ;; '
          f'esac; }}')
    prog = "\n".join(prelude(script, h) + [sh, 'cmd_restore "$@"'])
    return subprocess.run(["bash", "-c", prog, "x", "--yes", str(h.src)], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "TMPDIR": str(h.tmp)})


def test_a_pair_config_from_the_backup_is_laid_out_and_adopted_before_the_service_starts(script, tmp_path):
    h = Host(tmp_path)
    _with_pair(h)
    r = _run(script, h)
    assert r.returncode == 0, r.stderr
    assert (h.awg / "awgpair.conf").read_bytes() == PAIR, "конфиг линка пары не лёг на место"
    c = h.calls()
    assert "pair PAIR_IF=awgpair --adopt" in c, c
    assert c.index("pair PAIR_IF=awgpair --adopt") < c.index("systemctl start awg-bot"), \
        "линк пары поднимается после старта сервиса — канал пары стартует без линка"


def test_a_backup_without_a_pair_does_not_touch_the_pair_script(script, tmp_path):
    h = Host(tmp_path)
    r = _run(script, h)
    assert r.returncode == 0, r.stderr
    assert not [x for x in h.calls() if x.startswith("pair ")], h.calls()
    assert json.loads((h.data / "restore-done.json").read_text(encoding="utf-8"))
