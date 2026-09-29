"""
Восстановление обязано быть обратимым.

`awg-bot restore` перезаписывает БД, конфиг и секреты. Запускают его ровно
тогда, когда уже всё плохо, — а значит ошибиться снимком проще всего. Без
снимка «до» старый архив молча уносит всё, что появилось после него; однажды так
и исчезли три дня состояния.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "awg-bot.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _restore(script: str) -> str:
    return script.split("cmd_restore()", 1)[1].split("\ncmd_uninstall()", 1)[0]


def test_restore_snapshots_current_state_first(script):
    body = _restore(script)
    assert "prerestore" in body, "снимок текущего состояния не делается"
    snap = body.index("prerestore")
    overwrite = body.index('cp -a {} "$DATA_DIR/"')
    assert snap < overwrite, "снимок обязан быть ДО перезаписи"


def test_restore_snapshot_is_taken_after_the_service_stops(script):
    """Иначе БД снимается под записью бота и снимок может оказаться битым."""
    body = _restore(script)
    assert body.index('systemctl stop "$SERVICE"') < body.index("prerestore")


def test_restore_tells_how_to_undo(script):
    """Путь к снимку без команды возврата бесполезен в панике."""
    body = _restore(script)
    assert "awg-bot restore $pre" in body


def test_restore_removes_stale_wal(script):
    """WAL/SHM принадлежат заменяемому файлу БД.

    Рядом с чужой базой они в лучшем случае будут отброшены по несовпадению
    соли, в худшем — доложены в файл, которому не родня.
    """
    body = _restore(script)
    assert "db-wal" in body and "db-shm" in body
    assert body.index("db-wal") < body.index('cp -a {} "$DATA_DIR/"')


def _func(script: str, name: str) -> str:
    head = f"{name}() {{"
    assert head in script, f"в awg-bot.sh нет функции {name}()"
    return head + script.split(head, 1)[1].split("\n}\n", 1)[0] + "\n}\n"


def test_backup_and_prerestore_use_one_snapshot_builder(script):
    """Три сборки с разным составом однажды дали снимок «до восстановления»
    без конфигов интерфейсов — «вернуться» возвращало базу, а не устройства.
    Теперь и backup, и снимок перед restore зовут один сборщик (tools/snapshot),
    а тот снимает БД через backup API SQLite, не cp."""
    backup = _func(script, "cmd_backup")
    assert "snapshot_state" in backup and "cp -a" not in backup, backup
    assert "snapshot_state \"$pre\"" in _restore(script)
    snap = _func(script, "snapshot_state")
    assert "tools.snapshot --out" in snap
    assert "wal_checkpoint(TRUNCATE)" in snap, "запасной снимок без чекпоинта потеряет WAL"


def test_restore_reads_the_interface_dir_before_swapping_the_config(script):
    """awgdir из app.yaml — до подмены conf: иначе копия сама выбирала бы,
    куда положить свои конфиги интерфейсов."""
    body = _restore(script)
    assert body.index('awgdir="$(awg_conf_dir)"') < body.index('cp -a {} "$DATA_DIR/"')
    assert body.count('awgdir="$(awg_conf_dir)"') == 1


def test_restore_does_not_bring_up_an_interface_with_changed_hooks(script):
    """PreUp/PostUp исполняются от root: конфиг из копии с другими хуками
    кладётся, но не поднимается сам."""
    body = _restore(script)
    assert "(Pre|Post)(Up|Down)" in body and "hooked" in body
    assert body.index('case " $hooked " in') < body.index("awg-quick up \"$i\"")


def test_lan_lists_from_a_copy_are_checked_line_by_line(script, tmp_path):
    """Файл уходит в conf-dir dnsmasq: строка conf-file= или dhcp-script= там —
    исполнение от root. Допустимы только nftset= наших наборов и комментарии."""
    import subprocess
    good = tmp_path / "awg-gw-vpn-user.conf"
    good.write_text("# awg-bot (шлюз): личный список\nnftset=/ozon.ru/inet#awg_home#lan_vpn4\n"
                    "nftset=/xn--80ak6aa92e.xn--p1ai/inet#awg_home#lan_ru4\n"
                    "nftset=/rutracker.org/4#inet#awg_home#lan_vpn4\n\n", encoding="utf-8")
    prog = 'warn() { echo "WARN $*"; }\n' + _func(script, "lan_lists_sane") + 'lan_lists_sane "$@"\n'

    def run(*files):
        return subprocess.run(["bash", "-c", prog, "x", *map(str, files)], capture_output=True,
                              text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})

    assert run(good).returncode == 0
    for bad_line in ("conf-file=/etc/passwd", "dhcp-script=/tmp/x", "server=/x.ru/1.1.1.1",
                     "nftset=/ozon.ru/inet#filter#input", "nftset=/*/inet#awg_home#lan_vpn4"):
        bad = tmp_path / "awg-gw-ru-user.conf"
        bad.write_text(f"nftset=/ok.ru/inet#awg_home#lan_ru4\n{bad_line}\n", encoding="utf-8")
        r = run(good, bad)
        assert r.returncode == 1 and "WARN" in r.stdout, (bad_line, r.stdout)
