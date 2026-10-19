"""
`awg-bot restore`: безопасность — порядком, а не снимком.

Снимка «до восстановления» нет. Всё, что может отказать (расшифровка,
распаковка, сторож метки, план), — до остановки сервиса; слияние — после
остановки, но пишет только в распакованное; подготовка — <имя>.restore-new
рядом с целью; замена — переименованием через <имя>.restore-old под ловушкой.
Отказ на любом шаге — живые файлы байт в байт прежние, сервис поднят.
DROP заблокированным — до подъёма интерфейсов и ещё раз после, до старта
сервиса. Расшифрованное — только во временном каталоге операции; файл из
чата удаляется при любом исходе.

Прогоняется настоящая cmd_restore из awg-bot.sh (bash) с каталогами во
временной папке; python-инструменты, systemctl, awg-quick и скрипт линка
подменены функциями, которые пишут вызовы в журнал по порядку.
"""
from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.unit.test_cli_script import SCRIPT, _extract_func

pytestmark = pytest.mark.unit

FUNCS = ("yaml_get", "is_gateway_role", "awg_conf_dir", "json_str", "restore_die", "restore_stage", "restore_unstage",
         "restore_rollback", "restore_mv", "restore_swap", "restore_finish", "restore_prune_mark",
         "restore_fail_marker", "restore_exit", "restore_blocks", "restore_provision", "cmd_restore",
         "restore_apply", "restore_code_back", "restore_kernel_back", "tree_version", "compat_floor", "version_lt",
         "update_lock")
OLD = {"db": b"OLD-DB", "app": b"network:\n  server_host: vpn.example.org\n", "env": b"BOT_TOKEN=DUMMY\n",
       "awg0": b"[Interface]\nPrivateKey = DUMMY-OLD\n", "gw": b"[Interface]\nPrivateKey = DUMMY-GW-OLD\n"}
NEW = {"db": b"NEW-DB", "app": b"network:\n  server_host: vpn.example.org\nx: 1\n", "env": b"BOT_TOKEN=DUMMY2\n",
       "awg0": b"[Interface]\nPrivateKey = DUMMY-NEW\n", "gw": b"[Interface]\nPrivateKey = DUMMY-GW-NEW\n"}


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, raw in files.items():
            ti = tarfile.TarInfo(name); ti.size = len(raw); ti.mode = 0o600
            tar.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


class Host:
    """Живые файлы установки во временной папке и копия, которую «расшифрует»
    подставной restore_backup."""

    def __init__(self, tmp: Path, *, role: str = "client", hooks: bool = False):
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.data = tmp / "data"; self.data.mkdir()
        self.backups = self.data / "backups"; self.backups.mkdir()
        self.conf = tmp / "conf"; self.conf.mkdir()
        self.awg = tmp / "awg"; self.awg.mkdir()
        self.root = tmp / "root"; self.root.mkdir()
        self.net = tmp / "net"; (self.net / "awg0").mkdir(parents=True)
        self.env = tmp / "env"
        self.log = tmp / "calls"
        (self.data / "bot.db").write_bytes(OLD["db"])
        (self.conf / "app.yaml").write_bytes(OLD["app"] + (b"role: gateway\n" if role == "gateway" else b"")
                                            + f"conf_dir: {self.awg}\n".encode())
        self.env.write_bytes(OLD["env"])
        (self.awg / "awg0.conf").write_bytes(OLD["awg0"])
        (self.root / "gw-awglink.conf").write_bytes(OLD["gw"])
        awg0 = NEW["awg0"] + (b"PostUp = iptables -A FORWARD -j ACCEPT\n" if hooks else b"")
        self.plain = tmp / "plain.tgz"
        self.plain.write_bytes(_tgz({
            "state/backup-meta.json": json.dumps({"schema": 2, "role": "main",
                                                  "created_at": "2026-10-05T14:00:00+03:00"}).encode(),
            "state/bot.db": NEW["db"], "state/conf/app.yaml": NEW["app"] + f"conf_dir: {self.awg}\n".encode(),
            "state/env": NEW["env"], "awg/awg0.conf": awg0, "root/gw-awglink.conf": NEW["gw"]}))
        self.src = self.backups / "awg-bot-backup-main-20261005_140000.tgz.enc"
        self.src.write_bytes(b"AWGBK1P" + b"\0" * 64)

    def live(self) -> dict[str, bytes]:
        app = b"".join(x for x in (self.conf / "app.yaml").read_bytes().splitlines(True)
                       if not x.startswith(b"conf_dir:"))
        return {"db": (self.data / "bot.db").read_bytes(), "app": app,
                "env": self.env.read_bytes(), "awg0": (self.awg / "awg0.conf").read_bytes(),
                "gw": (self.root / "gw-awglink.conf").read_bytes()}

    def leftovers(self) -> list[str]:
        return sorted(str(p) for p in self.tmp.rglob("*") if p.name.endswith((".restore-new", ".restore-old")))

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


def run(script: str, h: Host, *args: str, **kw) -> subprocess.CompletedProcess:
    prog = "\n".join(prelude(script, h, **kw) + ['cmd_restore "$@"'])
    return subprocess.run(["bash", "-c", prog, "x", *args], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "TMPDIR": str(h.tmp)})


def prelude(script: str, h: Host, *, merge_fails: bool = False, mv_fail_at: int = 0,
            stage_fails: bool = False, plain: Path | None = None, blocked=("10.8.1.5",), adopt=(),
            blocks_fail: bool = False, stays_active: bool = False, merge_kill: bool = False,
            provision: bool = False, extra=(), funcs=(), start_fails: bool = False) -> list[str]:
    """Подмены и настоящие функции awg-bot.sh — без вызова: его дописывает
    вызывающий (run — cmd_restore; переход на выпуск бэкапа — и скрипт выпуска)."""
    log = h.log
    report = json.dumps({"blocked": list(blocked), "adopt": list(adopt), "slots": [], "provision": provision})
    return [
        "set -uo pipefail",
        f'INSTALL_DIR="{h.tmp}/opt"; CONF_DIR="{h.conf}"; ENV_FILE="{h.env}"; DATA_DIR="{h.data}"',
        f'BACKUP_DIR="{h.backups}"; SERVICE=awg-bot; ROOT_GW_DIR="{h.root}"; SYS_NET="{h.net}"',
        'GW_NO_BACKUPS="на шлюзе бэкапов нет: настройки шлюза — в бэкапе сервера AWG"',
        f'UPDATE_LOCK="{h.data}/update-guard.lock"',
        "RESTORE_TARGETS=(); RESTORE_DONE_T=(); RESTORE_HAD_OLD=(); RESTORE_PHASE=''; RESTORE_ERR=''",
        "RESTORE_TMP=''; RESTORE_PENDING=''; RESTORE_PLAN_FILE=''; RESTORE_FAIL=''; RESTORE_YES=0",
        'log() { echo "LOG $*"; }; ok() { echo "OK $*"; }; warn() { echo "WARN $*" >&2; }',
        'die() { echo "DIE $*" >&2; exit 1; }',
        "require_root() { :; }; confirm() { return 0; }; adopt_timezone() { :; }",
        f'apt-get() {{ echo "apt-get $*" >> "{log}"; }}',
        # сервис: после start — активен, после stop — нет (stays_active — stop не остановил)
        f'SVC_UP={1 if stays_active else 0}',
        f'systemctl() {{ case "$1" in is-active) [[ "$SVC_UP" -eq 1 ]]; return ;; list-unit-files) '
        f'echo dnsmasq.service; return 0 ;; start|restart) SVC_UP={0 if start_fails else 1} ;; '
        f'stop) SVC_UP={1 if stays_active else 0} ;; esac; '
        f'echo "systemctl $*" >> "{log}"; }}',
        f'awg-quick() {{ echo "awg-quick $*" >> "{log}"; }}',
        f'sh() {{ case "$1" in *routing-host-setup.sh) echo "host-setup $2" >> "{log}" ;; '
        f'*) echo "adopt $LINK_IF $*" >> "{log}" ;; esac; }}',
        "MV_N=0",
        "mv() { MV_N=$((MV_N+1)); "
        + (f'if [[ "$MV_N" -eq {mv_fail_at} ]]; then echo "нет места" >&2; return 1; fi; ' if mv_fail_at else "")
        + 'command mv "$@"; }',
        ("chmod() { case \"$2\" in *.restore-new) return 1 ;; esac; command chmod \"$@\"; }"
         if stage_fails else ""),
        "bot_tool() {",
        '  local m="$1"; shift',
        f'  echo "py $m $*" >> "{log}"',
        '  case "$m" in',
        '    tools.restore_backup)',
        '      local out=""; while [[ $# -gt 0 ]]; do [[ "$1" == "--out" ]] && out="$2"; shift; done',
        f'      cp "{plain or h.plain}" "$out"; echo "decrypted-in $(dirname "$out")" >> "{log}" ;;',
        '    tools.check_backup) return 0 ;;',
        '    tools.restore_merge)',
        '      case "$1" in',
        '        --plan) echo "ПЛАН"; [[ "$3" == "--save" ]] && echo "{}" > "$4" ;;',
        ('        --copy) echo "слияние не прошло: база бэкапа не читается" >&2; return 1 ;;' if merge_fails else
         '        --copy) kill -TERM $$ ;;' if merge_kill else
         f"        --copy) printf '%s' '{report}' > \"$2/restore-merge.json\" ;;"),
        ('        --apply-blocks) echo "iptables: отказ" >&2; return 1 ;;' if blocks_fail else
         '        --apply-blocks) return 0 ;;'),
        '      esac ;;',
        *extra,                                         # другие инструменты: строки `case` целиком
        '  esac',
        "}",
        *(_extract_func(script, f) for f in FUNCS + tuple(funcs)),
    ]


def test_success_replaces_everything_and_leaves_no_duplicates(script, tmp_path):
    h = Host(tmp_path)
    r = run(script, h, "--yes", str(h.src))
    assert r.returncode == 0, r.stderr
    assert h.live() == NEW, "живое не стало бэкапом"
    assert h.leftovers() == [], "временные дубли остались"
    assert not list(h.backups.glob("*prerestore*")) and "до восстановления" not in r.stdout + r.stderr
    marker = json.loads((h.data / "restore-done.json").read_text(encoding="utf-8"))
    assert marker["created_at"] == "2026-10-05T14:00:00+03:00" and marker["merge"]["blocked"] == ["10.8.1.5"]
    assert (h.root / "gw-awglink.conf").stat().st_mode & 0o777 == 0o600


def test_drop_goes_before_the_interface_comes_up_and_again_before_the_service_starts(script, tmp_path):
    h = Host(tmp_path)
    assert run(script, h, "--yes", str(h.src), adopt=("awglink2",)).returncode == 0
    c = h.calls()
    blocks = [i for i, x in enumerate(c) if x.startswith("py tools.restore_merge --apply-blocks")]
    up = c.index("awg-quick up awg0")
    start = c.index("systemctl start awg-bot")
    assert len(blocks) == 2 and blocks[0] < up < blocks[1] < start, c
    assert c.index("systemctl stop awg-bot") < c.index(next(x for x in c if x.startswith("py tools.restore_merge --copy")))
    adopt = next(i for i, x in enumerate(c) if x.startswith("adopt awglink2 ") and x.endswith(" --adopt"))
    assert adopt < blocks[1], c


def test_meta_guard_and_plan_go_before_the_service_stops(script, tmp_path):
    h = Host(tmp_path)
    assert run(script, h, "--yes", str(h.src)).returncode == 0
    c = h.calls()
    stop = c.index("systemctl stop awg-bot")
    assert c.index(next(x for x in c if x.startswith("py tools.check_backup --meta"))) < stop
    assert c.index(next(x for x in c if x.startswith("py tools.restore_merge --plan"))) < stop


def test_a_merge_refusal_changes_nothing_and_brings_the_service_up(script, tmp_path):
    h = Host(tmp_path)
    r = run(script, h, "--yes", str(h.src), merge_fails=True)
    assert r.returncode != 0 and "изменения отменены, сервис поднят" in r.stderr, r.stderr
    assert h.live() == OLD and h.leftovers() == []
    assert h.calls()[-1] == "systemctl start awg-bot"
    assert not (h.data / "restore-done.json").exists()


def test_a_preparation_refusal_changes_nothing(script, tmp_path):
    h = Host(tmp_path)
    r = run(script, h, "--yes", str(h.src), stage_fails=True)
    assert r.returncode != 0 and "подготовка не прошла — изменения не применены, сервис поднят" in r.stderr, r.stderr
    assert h.live() == OLD and h.leftovers() == []
    assert "systemctl start awg-bot" in h.calls()


def test_a_failure_in_the_middle_of_the_swap_brings_everything_back(script, tmp_path):
    """Подставной mv падает на третьем вызове: замена базы прошла, следующий
    файл — нет; ловушка вернула заменённое, дублей нет, сервис поднят."""
    h = Host(tmp_path)
    r = run(script, h, "--yes", str(h.src), mv_fail_at=3)
    assert r.returncode != 0 and "замена не прошла — вернул прежнее, сервис поднят" in r.stderr, r.stderr
    assert h.live() == OLD, "заменённые файлы не вернулись"
    assert h.leftovers() == []
    assert h.calls()[-1] == "systemctl start awg-bot"
    assert not (h.data / "restore-done.json").exists()


def test_decrypted_content_lives_only_in_the_operation_dir_and_the_chat_file_goes(script, tmp_path):
    h = Host(tmp_path)
    pending = h.backups / "restore-pending.tgz.enc"
    pending.write_bytes(h.src.read_bytes())
    r = run(script, h, "--yes", str(pending), merge_fails=True)
    assert r.returncode != 0
    assert not pending.exists(), "файл из чата остался после отказа"
    c = h.calls()
    assert any("--phrase-from-db" in x for x in c), "файл из чата расшифрован не фразой из базы"
    where = next(x for x in c if x.startswith("decrypted-in")).split(" ", 1)[1]
    assert not Path(where).exists(), "временный каталог операции остался"
    assert not [p for p in h.backups.iterdir() if p.suffix == ".tgz" or p.name.endswith(".tgz")]


def test_an_open_archive_is_refused_before_anything(script, tmp_path):
    h = Host(tmp_path)
    opened = h.backups / "awg-bot-backup-main-x.tgz.enc"
    opened.write_bytes(h.plain.read_bytes())
    r = run(script, h, "--yes", str(opened))
    assert r.returncode != 0 and "бэкап не зашифрован — восстанавливаются только шифрованные бэкапы" in r.stderr
    assert h.live() == OLD and "systemctl stop awg-bot" not in h.calls()


def test_a_config_with_other_hooks_is_laid_out_but_not_brought_up_and_drop_still_stands(script, tmp_path):
    h = Host(tmp_path, hooks=True)
    r = run(script, h, "--yes", str(h.src))
    assert r.returncode == 0, r.stderr
    c = h.calls()
    assert "awg-quick up awg0" not in c and "другие хуки" in r.stderr
    assert b"PostUp" in (h.awg / "awg0.conf").read_bytes()
    assert sum(1 for x in c if x.startswith("py tools.restore_merge --apply-blocks")) == 2


def test_the_gateway_has_no_restore(script, tmp_path):
    h = Host(tmp_path, role="gateway")
    r = run(script, h, "--yes", str(h.src))
    assert r.returncode != 0 and "на шлюзе бэкапов нет" in r.stderr
    assert h.calls() == []


def test_unpacking_reads_the_format_from_the_content(script):
    body = _extract_func(script, "cmd_restore")
    assert 'tar xf "$tmp/backup.tgz"' in body and "tar xzf" not in body
    assert "snapshot" not in body and "prerestore" not in body


# ── правки по ревью: фазы, точка невозврата, маркеры ─────────────────────────

def test_a_service_that_did_not_stop_means_nothing_changed(script, tmp_path):
    h = Host(tmp_path)
    r = run(script, h, "--yes", str(h.src), stays_active=True)
    assert r.returncode != 0 and "не остановился — ничего не изменилось" in r.stderr, r.stderr
    assert h.live() == OLD and not any(x.startswith("py tools.restore_merge --copy") for x in h.calls())


def test_a_signal_during_the_merge_brings_the_service_up_and_leaves_no_staging(script, tmp_path):
    """Сигнал или set -e посреди слияния: живое не тронуто, сервис поднят."""
    h = Host(tmp_path)
    run(script, h, "--yes", str(h.src), merge_kill=True)
    assert h.live() == OLD and h.leftovers() == []
    assert h.calls()[-1] == "systemctl start awg-bot", h.calls()


def test_a_refused_first_drop_means_nothing_changed_and_the_bot_prunes_on_start(script, tmp_path):
    h = Host(tmp_path)
    r = run(script, h, "--yes", str(h.src), blocks_fail=True)
    assert r.returncode != 0 and "DROP заблокированным не наложен — ничего не изменилось" in r.stderr, r.stderr
    assert h.live() == OLD and h.leftovers() == []
    assert (h.data / ".restore-prune").exists(), "DROP первого наложения не снимутся"
    assert h.calls()[-1] == "systemctl start awg-bot"


def test_a_rolled_back_swap_asks_the_bot_to_prune_drops(script, tmp_path):
    h = Host(tmp_path)
    run(script, h, "--yes", str(h.src), mv_fail_at=3)
    assert h.live() == OLD and (h.data / ".restore-prune").exists()


def test_wal_and_shm_go_into_the_duplicate_first(script, tmp_path):
    """Журналы заменяемой базы — первыми: отказ на базе не оставит её с чужим WAL."""
    h = Host(tmp_path)
    (h.data / "bot.db-wal").write_bytes(b"WAL"); (h.data / "bot.db-shm").write_bytes(b"SHM")
    r = run(script, h, "--yes", str(h.src), mv_fail_at=3)   # 1 — wal в дубль, 2 — shm, 3 — база
    assert r.returncode != 0
    assert (h.data / "bot.db-wal").read_bytes() == b"WAL" and (h.data / "bot.db").read_bytes() == OLD["db"]
    h2 = Host(tmp_path / "ok")
    (h2.data / "bot.db-wal").write_bytes(b"WAL")
    assert run(script, h2, "--yes", str(h2.src)).returncode == 0
    assert not (h2.data / "bot.db-wal").exists() and h2.live() == NEW


def test_the_point_of_no_return_is_right_after_the_swap(script, tmp_path):
    """После замены дублей нет: подъём интерфейсов и маркер — уже вперёд, не назад."""
    body = _extract_func(script, "restore_apply")
    assert body.index("restore_finish") < body.index('awg-quick up "$i"') < body.index("restore-done.json")
    assert body.index("restore_swap") < body.index("restore_finish") < body.index("restore_provision")


def test_a_chat_restore_that_fails_before_the_stop_tells_the_admin(script, tmp_path):
    h = Host(tmp_path)
    pending = h.backups / "restore-pending.tgz.enc"
    pending.write_bytes(h.plain.read_bytes())          # не зашифрован — отказ до остановки
    r = run(script, h, "--yes", str(pending))
    assert r.returncode != 0
    marker = json.loads((h.data / "restore-failed.json").read_text(encoding="utf-8"))
    assert marker["failed"].startswith("бэкап не зашифрован")
    assert "systemctl restart awg-bot" in h.calls(), "бот не узнает об отказе"


def test_the_marker_is_valid_json_whatever_the_file_name(script, tmp_path):
    h = Host(tmp_path)
    odd = h.backups / 'awg-bot-backup-main-"x"\\y.tgz.enc'
    odd.write_bytes(h.src.read_bytes())
    assert run(script, h, "--yes", str(odd)).returncode == 0
    marker = json.loads((h.data / "restore-done.json").read_text(encoding="utf-8"))
    assert marker["source"] == odd.name


def test_on_a_new_server_the_plumbing_is_provisioned_before_the_links(script, tmp_path):
    h = Host(tmp_path)
    assert run(script, h, "--yes", str(h.src), provision=True, adopt=("awglink",)).returncode == 0
    c = h.calls()
    i_apply, i_unit = c.index("host-setup --apply"), c.index("host-setup --install-unit")
    i_adopt = next(i for i, x in enumerate(c) if x.startswith("adopt awglink "))
    assert i_apply < i_unit < i_adopt, c
    h2 = Host(tmp_path / "no")
    run(script, h2, "--yes", str(h2.src))
    assert not any(x.startswith("host-setup") for x in h2.calls())


def test_the_plan_from_the_chat_is_checked_after_the_stop(script, tmp_path):
    h = Host(tmp_path)
    pending = h.backups / "restore-pending.tgz.enc"
    pending.write_bytes(h.src.read_bytes())
    plan = h.backups / "restore-pending.plan.json"
    plan.write_text('{"line": "same"}', encoding="utf-8")
    assert run(script, h, "--yes", str(pending)).returncode == 0
    copy_call = next(x for x in h.calls() if x.startswith("py tools.restore_merge --copy"))
    assert "--expect" in copy_call
    assert not plan.exists() and not pending.exists()


def test_the_random_key_is_asked_without_echo_and_not_on_stdout(tmp_path, monkeypatch, capsys):
    """Режим случайного ключа: stdout скрипт глушит — подсказка и ввод идут мимо него, без эха."""
    import base64, getpass, sys
    from awgbot.util import secrets_util as su
    from tools import restore_backup
    key = bytes(32)                                   # DUMMY
    src = tmp_path / "awg-bot-backup-main-x.tgz.enc"
    src.write_bytes(su.encrypt(b"payload", key=key))
    asked = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": asked.append(prompt) or base64.b64encode(key).decode())
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("ключ спрошен открытым вводом"))
    monkeypatch.delenv("BACKUP_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["restore_backup", str(src), "--out", str(tmp_path / "out.tgz")])
    restore_backup.main()
    assert asked and (tmp_path / "out.tgz").read_bytes() == b"payload"
    assert "BACKUP_KEY" not in capsys.readouterr().out
