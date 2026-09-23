"""
Установщик поверх рабочей установки: одно меню обеим ролям — обновить с
сохранением данных (только вверх), снести и поставить заново, отмена.

Это то место, где человек повторно запускает «ту же команду из инструкции»
на живом хосте. Ошибка здесь стоит дорого в обе стороны: снести данные без
второго «да» — потерять клиентов и ключи; тихо ничего не сделать — оставить
шлюз со старым кодом и старой конфигурацией. Поэтому фрагменты установщика
и awg-bot.sh гоняются настоящим bash с подставными systemctl, nft, tar и
awg-bot.sh; все пути хоста уведены во временный корень.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install" / "awg-bot-install.sh"
BOT_SCRIPT = ROOT / "awg-bot.sh"

# bash 3.2 (macOS) не знает ${x,,}; на хостах поставки (Debian) bash 5. Подмену
# делаем только там, где без неё фрагмент не разобрать, и только если строка
# в коде есть — иначе тест молча гонял бы не то.
_BASH_MAJOR = int(subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"],
                                 capture_output=True, text=True).stdout.strip() or 0)


def _lower_compat(text: str, var: str) -> str:
    if _BASH_MAJOR >= 4:
        return text
    src = "${" + var + ",,}"
    assert src in text, f"в фрагменте нет {src} — тест устарел"
    return text.replace(src, f"$(printf '%s' \"${var}\" | tr '[:upper:]' '[:lower:]')")


def _func(src: str, name: str) -> str:
    """Тело функции целиком — от «name() {» до закрывающей скобки в начале строки."""
    head = f"{name}() {{"
    assert head in src, f"нет функции {name}() — тест устарел"
    return head + src.split(head, 1)[1].split("\n}\n", 1)[0] + "\n}\n"


def _existing_install_fragment() -> str:
    src = INSTALLER.read_text(encoding="utf-8")
    start = "# ── прошлая установка"
    end = "# Полу-остаток"
    assert start in src and end in src, "не нашли раздел про прошлую установку — тест устарел"
    return src[src.index(start):src.index(end)]


# ── ver_lt: сравнение версий X.Y.Z.P ─────────────────────────────────────────

def _ver_lt(a: str, b: str) -> bool:
    frag = _existing_install_fragment()
    prog = _func(frag, "ver_lt") + f'ver_lt "{a}" "{b}"\n'
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True,
                          timeout=10).returncode == 0


@pytest.mark.parametrize("a,b,lt", [
    ("2.25.2", "3.0.0", True),
    ("3.0.0", "2.25.2", False),
    ("3.0.0", "3.0.0.1", True),        # четвёртая цифра — тоже новее
    ("3.0.0.1", "3.0.0", False),
    ("3.0.0", "3.0.0", False),         # та же версия — не обновление
    ("3.0.0.2", "3.0.0.2", False),
    ("2.9.0", "2.10.0", True),         # числа, а не строки: «2.10» новее «2.9»
    ("3.0.0.9", "3.0.0.10", True),
    ("", "3.0.0", False),              # версию установки не прочитали
    ("3.0.0", "", False),              # версию поставки не прочитали
])
def test_version_order_is_numeric_and_four_digit(a, b, lt):
    """Ошибись сравнение — меню предложит «обновить» на ту же или более старую
    версию (откат кода поверх новой БД), или откажет в настоящем обновлении."""
    assert _ver_lt(a, b) is lt, f"ver_lt {a!r} {b!r} должно быть {lt}"


# ── подставной хост для меню ─────────────────────────────────────────────────

_HOST_PATHS = (
    "/etc/systemd/system/", "/usr/local/sbin/", "/etc/awg-gw", "/var/lib/awg-gw",
    "/etc/nftables.d/", "/opt/awg-gw", "/etc/amnezia/amneziawg", "/etc/dnsmasq.d/",
    "/root/",
)
# одним проходом: иначе «/root/» из уже подставленного корня подменился бы ещё раз
_HOST_RE = re.compile("|".join(re.escape(p) for p in sorted(_HOST_PATHS, key=len, reverse=True)))


class _Host:
    """Хост с рабочей установкой: /opt, /etc, /var под временным корнем,
    журнал вызовов systemctl/nft/tar/обвязки и подставной awg-bot.sh."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "root"
        self.bin = tmp_path / "bin"; self.bin.mkdir()
        self.log = tmp_path / "calls.log"; self.log.write_text("")
        self.install = self.root / "opt" / "awg-bot"
        self.etc = self.root / "etc" / "awg-bot"
        self.data = self.root / "var" / "lib" / "awg-bot"
        self.link = self.root / "usr" / "local" / "bin" / "awg-bot"
        self.unit = self.root / "etc" / "systemd" / "system" / "awg-bot.service"
        self.sbin = self.root / "usr" / "local" / "sbin"
        self.gw_etc = self.root / "etc" / "awg-gw"
        self.gw_var = self.root / "var" / "lib" / "awg-gw"
        self.guard = self.root / "etc" / "nftables.d" / "awg-bot-guard.nft"
        self.gw_opt = self.root / "opt" / "awg-gw"
        self.amnezia = self.root / "etc" / "amnezia" / "amneziawg"
        self.dnsmasq_d = self.root / "etc" / "dnsmasq.d"
        self.resolver_dropin = (self.root / "etc" / "systemd" / "system" / "dnsmasq.service.d"
                                / "awgbot-resolver.conf")
        self.root_home = self.root / "root"
        self.tar_version = ""          # что «лежит» в архиве поставки
        self.tmpdir = tmp_path / "tmpdir"; self.tmpdir.mkdir()
        for name, body in {
            "systemctl": 'echo "systemctl $*" >> "$CALLS"',
            "nft": 'echo "nft $*" >> "$CALLS"',
            "awg-quick": 'echo "awg-quick $*" >> "$CALLS"',
            "docker": 'echo "docker $*" >> "$CALLS"',
            # mktemp у macOS не смотрит на TMPDIR — свой, чтобы не мусорить на хосте
            "mktemp": ('[ "$1" = -d ] || exit 2\n'
                       'd="$TMPDIR/mktemp.$$"; mkdir "$d" && echo "$d"'),
            "tar": (
                'echo "tar $*" >> "$CALLS"\n'
                'case "$1" in\n'
                '  -xzOf) [ -n "$TAR_VERSION" ] && printf \'__version__ = "%s"\\n\' "$TAR_VERSION"; exit 0 ;;\n'
                '  czf) : > "$2"; exit 0 ;;\n'
                'esac\nexit 2'),
        }.items():
            p = self.bin / name
            p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
            p.chmod(0o755)

    # ── состояние ──

    def installed(self, version: str, role: str, container: str = "") -> None:
        (self.install / "venv" / "bin").mkdir(parents=True)
        py = self.install / "venv" / "bin" / "python"
        py.write_text("#!/bin/sh\n"); py.chmod(0o755)
        (self.install / "awgbot").mkdir()
        (self.install / "awgbot" / "__version__.py").write_text(f'__version__ = "{version}"\n')
        bot = self.install / "awg-bot.sh"
        bot.write_text(
            '#!/bin/sh\n'
            'echo "awg-bot $* KEEP=${AWG_UPDATE_KEEP-<unset>} '
            'BUNDLE=${AWG_UPDATE_THEN_BUNDLE-<unset>}" >> "$CALLS"\n', encoding="utf-8")
        bot.chmod(0o755)
        (self.etc / "conf").mkdir(parents=True)
        (self.etc / "conf" / "app.yaml").write_text(
            f'role: "{role}"\ninterface: awg0\n' + (f'container: "{container}"\n' if container else ""))
        (self.etc / "awg-bot.env").write_text("BOT_TOKEN=1:x\n")
        self.data.mkdir(parents=True)
        (self.data / "awgbot.db").write_text("db")
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(bot)
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("[Unit]\n")
        if role == "gateway":
            self.sbin.mkdir(parents=True)
            for s in ("routing-gw-setup.sh", "awg-lan-lists.sh", "awg-lan-domain.sh"):
                (self.sbin / s).write_text(f'#!/bin/sh\necho "{s} $*" >> "$CALLS"\n')
                (self.sbin / s).chmod(0o755)
            self.gw_etc.mkdir(parents=True); (self.gw_etc / "firewall.env").write_text("X=1\n")
            self.gw_var.mkdir(parents=True); (self.gw_var / "lists.status").write_text("rc=0\n")
            self.gw_opt.mkdir(parents=True); (self.gw_opt / "link.conf").write_text("[Interface]\n")
        else:
            self.guard.parent.mkdir(parents=True)
            self.guard.write_text("table inet awg_bot_guard {}\n")
            # всё, что бот ставит на ВПС: обвязка линков и хоста, резолвер,
            # интерфейсы awg (клиентский и два линка), файлы шлюзов в /root
            self.sbin.mkdir(parents=True)
            for s in ("routing-link-setup.sh", "routing-host-setup.sh"):
                (self.sbin / s).write_text(
                    f'#!/bin/sh\necho "{s} LINK_IF=${{LINK_IF-}} $*" >> "$CALLS"\n')
                (self.sbin / s).chmod(0o755)
            self.amnezia.mkdir(parents=True)
            for i in ("awg0", "awglink1", "awglink2"):
                (self.amnezia / f"{i}.conf").write_text("[Interface]\nPrivateKey = DUMMY\n")
            self.dnsmasq_d.mkdir(parents=True)
            (self.dnsmasq_d / "awgbot-resolver.conf").write_text("listen-address=10.8.1.1\n")
            (self.dnsmasq_d / "local.conf").write_text("# чужой файл\n")
            self.resolver_dropin.parent.mkdir(parents=True)
            self.resolver_dropin.write_text("[Unit]\n")
            self.root_home.mkdir(parents=True)
            (self.root_home / "gw-awglink1.conf").write_text("[Interface]\n")
            (self.root_home / "awg-gw-bundle-slot1.sh").write_text("#!/bin/sh\n")
            (self.root_home / "notes.txt").write_text("чужое\n")

    def unpacked_delivery(self, version: str) -> Path:
        src = self.tmp / "delivery"
        (src / "awgbot").mkdir(parents=True)
        (src / "awgbot" / "__version__.py").write_text(f'__version__ = "{version}"\n')
        return src

    def archive(self, version: str) -> Path:
        self.tar_version = version
        tgz = self.tmp / "awg-bot.tgz"
        tgz.write_text("архив")
        return tgz

    def snapshot(self) -> set[str]:
        return {str(p.relative_to(self.root)) for p in self.root.rglob("*")}

    # ── прогон ──

    def run(self, answers: list[str], *, role: str = "client", extra: tuple[str, ...] = (),
            tgz: Path | None = None, src_root: Path | None = None) -> subprocess.CompletedProcess:
        frag = _existing_install_fragment()
        for p in _HOST_PATHS:
            assert p in frag, f"в фрагменте нет {p} — тест устарел"
        frag = _HOST_RE.sub(lambda m: f"{self.root}{m.group(0)}", frag)
        assert "< /dev/tty" in frag
        frag = _lower_compat(frag.replace("< /dev/tty", "<&3"), "__a")
        ans = self.tmp / "answers"
        ans.write_text("".join(a + "\n" for a in answers), encoding="utf-8")
        extra_sh = " ".join(f'"{e}"' for e in extra)
        prog = (
            "set -euo pipefail\n"
            f'INSTALL_DIR="{self.install}"; ETC_DIR="{self.etc}"; DATA_DIR="{self.data}"\n'
            f'SELF_LINK="{self.link}"\n'
            "c_info=''; c_err=''; c_off=''\n"
            "log() { printf '[install] %s\\n' \"$*\"; }\n"
            "die() { printf '[install:ОШИБКА] %s\\n' \"$*\" >&2; exit 1; }\n"
            f'ROLE="{role}"; EXTRA=({extra_sh})\n'
            f'TGZ="{tgz or ""}"; SRC_ROOT="{src_root or ""}"\n'
            f'exec 3<"{ans}"\n'
            + frag +
            "echo CONTINUE_FRESH_INSTALL\n"
        )
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "CALLS": str(self.log),
               "TAR_VERSION": self.tar_version, "TMPDIR": str(self.tmpdir)}
        return subprocess.run(["bash", "-c", prog], capture_output=True, text=True,
                              timeout=30, env=env)

    def calls(self) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln]

    def changes(self) -> list[str]:
        """Вызовы, кроме чтения версии из архива: оно ничего не меняет."""
        return [c for c in self.calls() if not c.startswith("tar -xzOf")]

    def updates(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("awg-bot ")]


@pytest.fixture
def host(tmp_path):
    return _Host(tmp_path)


# ── меню ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["client", "gateway"])
def test_both_roles_get_the_same_three_item_menu(host, role):
    """Шлюз раньше молча применял файл, ВПС показывал четыре пункта. Теперь
    одно меню: без него повтор команды на шлюзе поверх другой версии ставил
    бы непонятно что, а «Восстановить» из установщика вело не туда."""
    host.installed("2.25.2", role)
    r = host.run(["3"], role=role, tgz=host.archive("3.0.0"))
    assert "версия 2.25.2, в поставке 3.0.0" in r.stderr, r.stderr
    assert "1) Обновить до 3.0.0, сохранив данные и настройки" in r.stderr
    assert "2) Удалить прошлую установку вместе с данными и настройками" in r.stderr
    assert "3) Отмена" in r.stderr
    assert "4)" not in r.stderr and "restore" not in r.stderr.split("Что делаем?")[1].split("[install:")[0], \
        "в меню остались прежние пункты"


def test_newer_delivery_updates_keeping_data_with_the_archive(host):
    """Пункт 1: `awg-bot update <архив>` с AWG_UPDATE_KEEP=1 — иначе update
    переспросит про удаление данных, и одна неосторожная «y» снесёт БД."""
    host.installed("2.25.2", "client")
    tgz = host.archive("3.0.0")
    before = host.snapshot()
    r = host.run(["1"], tgz=tgz)
    assert r.returncode == 0, r.stderr
    assert host.updates() == [f"awg-bot update {tgz} KEEP=1 BUNDLE="], host.calls()
    assert "CONTINUE_FRESH_INSTALL" not in r.stdout, "после exec установка не должна идти дальше"
    assert host.snapshot() == before, "обновление из меню что-то удалило до запуска update"
    assert not any(c.startswith(("systemctl", "nft")) for c in host.calls())


def test_four_digit_hotfix_is_offered_as_an_update(host):
    """3.0.0 → 3.0.0.1: хотфикс, выпущенный четвёртой цифрой, обязан ставиться
    тем же пунктом 1."""
    host.installed("3.0.0", "client")
    tgz = host.archive("3.0.0.1")
    r = host.run(["1"], tgz=tgz)
    assert r.returncode == 0, r.stderr
    assert host.updates() == [f"awg-bot update {tgz} KEEP=1 BUNDLE="]


@pytest.mark.parametrize("have,new", [("3.0.0", "3.0.0"), ("3.0.1", "3.0.0"), ("3.0.0.1", "3.0.0")])
def test_same_or_older_delivery_refuses_to_update(host, have, new):
    """Обновление «вниз» — старый код поверх мигрированной БД; «на ту же» —
    лишний перезапуск ради ничего. Пункт 1 говорит, что обновлять нечего, и
    ничего не запускает."""
    host.installed(have, "client")
    before = host.snapshot()
    r = host.run(["1"], tgz=host.archive(new))
    assert r.returncode != 0, "отказ должен кончаться ненулевым кодом"
    assert "1) Обновление не нужно" in r.stderr, r.stderr
    assert "обновление не требуется" in r.stderr
    assert host.updates() == [], f"update запущен вопреки версии: {host.calls()}"
    assert host.snapshot() == before
    assert "CONTINUE_FRESH_INSTALL" not in r.stdout


def test_refused_update_on_gateway_says_how_to_apply_a_new_configuration(host):
    """На шлюзе ту же команду повторяют ради нового файла конфигурации, а не
    ради кода. Отказ «обновлять нечего» без подсказки оставил бы человека
    без пути: прежде установщик применял файл сам."""
    host.installed("3.0.0", "gateway")
    before = host.snapshot()
    r = host.run(["1"], role="gateway", extra=("--bundle", "/root/gw.sh"), tgz=host.archive("3.0.0"))
    assert r.returncode != 0 and host.updates() == []
    assert "обновление не требуется" in r.stderr
    assert "sudo sh <файл> (без --install)" in r.stderr, r.stderr
    assert host.snapshot() == before


def test_refused_update_on_vps_has_no_gateway_hint(host):
    host.installed("3.0.0", "client")
    r = host.run(["1"], tgz=host.archive("3.0.0"))
    assert r.returncode != 0 and "обновление не требуется" in r.stderr
    assert "sudo sh <файл>" not in r.stderr, "подсказка шлюза ушла на ВПС"


def test_unreadable_installed_version_is_not_treated_as_older(host):
    """Версию установки не прочитали (файл побит) — «обновить» не предлагаем:
    сравнивать не с чем, а снести и поставить заново можно пунктом 2."""
    host.installed("3.0.0", "client")
    (host.install / "awgbot" / "__version__.py").write_text("мусор\n")
    r = host.run(["1"], tgz=host.archive("3.0.0.1"))
    assert r.returncode != 0 and host.updates() == []
    assert "версия ?, в поставке 3.0.0.1" in r.stderr, r.stderr


@pytest.mark.parametrize("second", ["", "n", "да"])
def test_wipe_without_an_explicit_y_changes_nothing(host, second):
    """Пункт 2 необратим: секреты, БД, копии. Без явного «y» на втором вопросе
    (Enter, «n», что-то ещё) — выход без единой правки."""
    host.installed("2.25.2", "gateway")
    before = host.snapshot()
    r = host.run(["2", second], role="gateway", tgz=host.archive("3.0.0"))
    # отказ — не ошибка: строка лога и код 0, но установка дальше не идёт
    assert r.returncode == 0, r.stderr
    assert "отмена — ничего не изменено" in r.stdout, r.stdout + r.stderr
    assert host.snapshot() == before, "удалено без подтверждения"
    assert host.changes() == [], f"вызваны команды без подтверждения: {host.calls()}"
    assert "CONTINUE_FRESH_INSTALL" not in r.stdout


def test_wipe_on_gateway_rolls_back_the_link_and_removes_its_state(host):
    """Снос шлюза — это ещё линк, юнит реассерта и таблицы: без --rollback
    обвязка осталась бы жить без агента, а /etc/awg-gw, /var/lib/awg-gw и
    /opt/awg-gw (скрипт обвязки и link.conf из прошлого файла) подсунули бы
    новой установке прошлое состояние."""
    host.installed("2.25.2", "gateway")
    r = host.run(["2", "y"], role="gateway", tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    calls = host.calls()
    assert "routing-gw-setup.sh --rollback" in calls, calls
    assert "systemctl disable --now awg-bot" in calls
    assert not any(c.startswith("nft ") for c in calls), "у шлюза нет таблицы awg_bot_guard"
    for p in (host.gw_etc, host.gw_var, host.gw_opt, host.install, host.etc, host.data, host.unit,
              host.sbin / "routing-gw-setup.sh", host.sbin / "awg-lan-lists.sh",
              host.sbin / "awg-lan-domain.sh"):
        assert not p.exists(), f"после сноса осталось: {p}"
    assert not host.link.is_symlink(), "симлинк awg-bot остался"
    assert host.updates() == []
    assert "CONTINUE_FRESH_INSTALL" in r.stdout, "после сноса установка должна идти дальше"


def test_wipe_on_vps_removes_everything_the_bot_put_there(host):
    """Снос ВПС — всё, что ставил бот: линки до шлюзов и обвязка (своими
    --rollback, по одному на линк), резолвер клиентов, интерфейсы awg,
    файлы шлюзов в /root, таблица файервола. Останься что-то — новая
    установка начинала бы с чужими правилами, пирами и маршрутами."""
    host.installed("2.25.2", "client", container="amnezia-awg")
    r = host.run(["2", "y", "УДАЛИТЬ"], tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    calls = host.calls()
    links = [c for c in calls if c.startswith("routing-link-setup.sh")]
    assert sorted(links) == ["routing-link-setup.sh LINK_IF=awglink1 --rollback",
                             "routing-link-setup.sh LINK_IF=awglink2 --rollback"], calls
    assert "routing-host-setup.sh LINK_IF= --rollback" in calls, calls
    assert calls.index(max(links)) < calls.index("routing-host-setup.sh LINK_IF= --rollback"), \
        "обвязку хоста снимают после линков"
    for i in ("awg0", "awglink1", "awglink2"):
        assert f"systemctl disable --now awg-quick@{i}" in calls, calls
        assert f"awg-quick down {i}" in calls, calls
    assert "systemctl try-restart dnsmasq" in calls
    assert "docker rm -f amnezia-awg" in calls, calls
    assert "nft delete table inet awg_bot_guard" in calls, calls
    assert not any(c.startswith("routing-gw-setup.sh") for c in calls)
    for p in (host.guard, host.install, host.etc, host.data, host.unit, host.amnezia,
              host.sbin / "routing-link-setup.sh", host.sbin / "routing-host-setup.sh",
              host.dnsmasq_d / "awgbot-resolver.conf", host.resolver_dropin,
              host.root_home / "gw-awglink1.conf", host.root_home / "awg-gw-bundle-slot1.sh"):
        assert not p.exists(), f"после сноса осталось: {p}"
    assert (host.dnsmasq_d / "local.conf").exists(), "снесён чужой файл dnsmasq"
    assert (host.root_home / "notes.txt").exists(), "снесён чужой файл в /root"
    assert "CONTINUE_FRESH_INSTALL" in r.stdout


def test_wipe_on_vps_without_a_container_does_not_call_docker(host):
    """Host-режим: контейнера нет — docker rm с пустым именем не зовём."""
    host.installed("2.25.2", "client")
    r = host.run(["2", "y", "УДАЛИТЬ"], tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("docker") for c in host.calls()), host.calls()


def test_wipe_on_vps_warns_that_clients_lose_access(host):
    """На ВПС пункт 2 оставляет всех клиентов без VPN: человек должен узнать
    это до ответа, а не после."""
    host.installed("2.25.2", "client")
    r = host.run(["2", ""], tgz=host.archive("3.0.0"))
    assert "Клиенты останутся БЕЗ ДОСТУПА" in r.stderr, r.stderr
    assert "Аплинк остаётся" not in r.stderr


def test_wipe_on_gateway_lists_what_goes_and_keeps_the_uplink(host):
    host.installed("2.25.2", "gateway")
    r = host.run(["2", ""], role="gateway", tgz=host.archive("3.0.0"))
    warn = r.stderr.split("Будут удалены:", 1)[1].split("[y/N]", 1)[0]
    # пути в тексте подменены вместе с остальными — сверяем их концы
    for piece in ("линк до сервера AWG", "юнит и таблицы обвязки", "/etc/awg-gw,", "/var/lib/awg-gw,",
                  "/opt/awg-gw.", "Аплинк остаётся."):
        assert piece in warn, f"в предупреждении нет «{piece}»: {warn}"
    assert "БЕЗ ДОСТУПА" not in r.stderr


@pytest.mark.parametrize("word", ["", "удалить", "y", "УДАЛИТЬ!"])
def test_wipe_on_vps_needs_the_typed_word(host, word):
    """На ВПС одного «y» мало: снос отрезает всех клиентов. Без слова
    «УДАЛИТЬ» дословно — отмена без единой правки."""
    host.installed("2.25.2", "client", container="amnezia-awg")
    before = host.snapshot()
    r = host.run(["2", "y", word], tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    assert "отмена — ничего не изменено" in r.stdout, r.stdout + r.stderr
    assert "CONTINUE_FRESH_INSTALL" not in r.stdout, "после отказа установка пошла дальше"
    assert host.snapshot() == before, "ВПС снесён без слова УДАЛИТЬ"
    assert host.changes() == [], host.calls()


def test_gateway_wipe_needs_only_one_confirmation(host):
    """Шлюзу слово не нужно: снос агента клиентов ВПС не трогает, а лишний
    вопрос на малине только мешает переустановке."""
    host.installed("2.25.2", "gateway")
    r = host.run(["2", "y"], role="gateway", tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    assert "УДАЛИТЬ" not in r.stderr and "CONTINUE_FRESH_INSTALL" in r.stdout


def test_vps_wipe_asks_for_the_word_even_when_run_as_gateway(host):
    """На ВПС запустили установщик с --role gateway (скопировали команду для
    малины). Снос всё равно ВПС-ный — по роли из app.yaml: интерфейсы со
    всеми пирами клиентов. Значит и подтверждение — ВПС-ное: одного «y» под
    текстом «Аплинк остаётся» мало."""
    host.installed("2.25.2", "client")
    before = host.snapshot()
    r = host.run(["2", "y"], role="gateway", tgz=host.archive("3.0.0"))
    assert host.snapshot() == before, (
        "ВПС снесён (интерфейсы awg с пирами клиентов) по одному «y» под текстом шлюза:\n"
        + r.stderr)


def test_wipe_follows_the_installed_role_not_the_flag(host):
    """На малине запустили установщик без --role gateway (скопировали команду
    ВПС) — сносить нужно то, что стоит: обвязку шлюза, а не таблицу ВПС."""
    host.installed("2.25.2", "gateway")
    r = host.run(["2", "y", "УДАЛИТЬ"], role="client", tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    assert "routing-gw-setup.sh --rollback" in host.calls(), host.calls()
    assert not any(c.startswith("nft ") for c in host.calls())
    assert not host.gw_etc.exists()


def test_wipe_survives_missing_pieces_and_failing_commands(host):
    """Прошлая установка могла быть снята наполовину: нет юнита, нет обвязки,
    systemctl, nft, awg-quick, docker и скрипты обвязки отвечают ошибкой.
    Снос не должен падать под `set -e` — иначе человек застревает между
    старой и новой установкой."""
    host.installed("2.25.2", "client", container="amnezia-awg")
    host.unit.unlink(); host.guard.unlink(); host.resolver_dropin.unlink()
    for name in ("systemctl", "nft", "awg-quick", "docker"):
        (host.bin / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "$CALLS"\nexit 1\n')
    for s in ("routing-link-setup.sh", "routing-host-setup.sh"):
        (host.sbin / s).write_text(f'#!/bin/sh\necho "{s} $*" >> "$CALLS"\nexit 3\n')
    r = host.run(["2", "y", "УДАЛИТЬ"], tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    assert not host.install.exists() and not host.data.exists() and not host.amnezia.exists()
    assert "CONTINUE_FRESH_INSTALL" in r.stdout


def test_wipe_on_a_bare_vps_without_interfaces_or_scripts(host):
    """Ни конфигов awg, ни скриптов обвязки (снимали руками) — глобы пусты,
    снос проходит и установка идёт дальше."""
    host.installed("2.25.2", "client")
    import shutil
    shutil.rmtree(host.amnezia); shutil.rmtree(host.sbin)
    r = host.run(["2", "y", "УДАЛИТЬ"], tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith(("awg-quick", "routing-")) for c in host.calls()), host.calls()
    assert "CONTINUE_FRESH_INSTALL" in r.stdout


@pytest.mark.parametrize("answer", ["3", "", "7"])
def test_cancel_changes_nothing(host, answer):
    """«Отмена» (и Enter, и ерунда) — выход без правок и без запусков."""
    host.installed("2.25.2", "client")
    before = host.snapshot()
    r = host.run([answer], tgz=host.archive("3.0.0"))
    # отмена — не ошибка: обычная строка лога и код 0
    assert r.returncode == 0, r.stderr
    assert "отмена — ничего не изменено" in r.stdout, r.stdout + r.stderr
    assert "CONTINUE_FRESH_INSTALL" not in r.stdout, "после отмены установка пошла дальше"
    assert host.snapshot() == before and host.changes() == [], host.calls()
    assert "sudo sh <файл>" not in r.stdout + r.stderr, "подсказка шлюза ушла на ВПС"


def test_cancel_on_gateway_says_how_to_apply_a_new_configuration(host):
    """Шлюз раньше сам применял файл при повторе команды. Теперь отмена —
    значит нужна подсказка, как применить файл без переустановки."""
    host.installed("2.25.2", "gateway")
    before = host.snapshot()
    r = host.run(["3"], role="gateway", extra=("--bundle", "/root/awg-gw-bundle.sh"),
                 tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    assert "sudo sh <файл> (без --install)" in r.stdout, r.stdout + r.stderr
    assert "CONTINUE_FRESH_INSTALL" not in r.stdout
    assert host.snapshot() == before and host.changes() == [], host.calls()


def test_gateway_update_carries_the_bundle_to_apply_after(host):
    """Шлюз пришли обновлять вместе с новым файлом конфигурации: путь уходит в
    AWG_UPDATE_THEN_BUNDLE, и файл применяется уже новым кодом. Потеряйся
    путь — код обновится, а конфигурация останется старой без единого слова."""
    host.installed("2.25.2", "gateway")
    tgz = host.archive("3.0.0")
    r = host.run(["1"], role="gateway", extra=("--port", "51820", "--bundle", "/root/gw.sh"), tgz=tgz)
    assert r.returncode == 0, r.stderr
    assert host.updates() == [f"awg-bot update {tgz} KEEP=1 BUNDLE=/root/gw.sh"], host.calls()


def test_gateway_update_without_a_bundle_applies_nothing_after(host):
    host.installed("2.25.2", "gateway")
    tgz = host.archive("3.0.0")
    r = host.run(["1"], role="gateway", tgz=tgz)
    assert r.returncode == 0, r.stderr
    assert host.updates() == [f"awg-bot update {tgz} KEEP=1 BUNDLE="], host.calls()


def test_unpacked_delivery_without_an_archive_is_packed_for_update(host):
    """Обычный путь — поставку распаковали и запустили установщик изнутри, а
    архив удалили. `awg-bot update` ждёт архив: без сборки пункт 1 падал бы."""
    host.installed("2.25.2", "client")
    src = host.unpacked_delivery("3.0.0")
    r = host.run(["1"], src_root=src)
    assert r.returncode == 0, r.stderr
    packs = [c for c in host.calls() if c.startswith("tar czf")]
    assert len(packs) == 1 and packs[0].endswith(f"-C {src} ."), host.calls()
    built = packs[0].split()[2]
    assert built.startswith(str(host.tmpdir)), "архив собран не во временном каталоге"
    assert Path(built).is_file()
    assert host.updates() == [f"awg-bot update {built} KEEP=1 BUNDLE="], host.calls()
    assert not any(c.startswith("tar -xzOf") for c in host.calls()), \
        "версию распакованной поставки читают из файла, а не из архива"


def test_version_of_an_archive_only_delivery_is_read_from_the_archive(host):
    """Скрипт вытащили отдельно, рядом только архив — версия поставки из него.
    Не прочитай её — меню не предложит обновление вовсе."""
    host.installed("2.25.2", "client")
    tgz = host.archive("3.0.0")
    r = host.run(["1"], tgz=tgz)
    assert r.returncode == 0, r.stderr
    assert any(c.startswith(f"tar -xzOf {tgz}") for c in host.calls()), host.calls()
    assert not any(c.startswith("tar czf") for c in host.calls()), "архив уже есть — собирать нечего"


def test_without_a_working_install_there_is_no_menu(host):
    """Чистая машина (или только что снесённая пунктом 2) — никаких вопросов,
    сразу установка."""
    r = host.run([], tgz=host.archive("3.0.0"))
    assert r.returncode == 0, r.stderr
    assert "Что делаем?" not in r.stderr and "CONTINUE_FRESH_INSTALL" in r.stdout
    assert host.calls() == []


# ── awg-bot.sh: update и post_update ─────────────────────────────────────────

def _run_update(tmp_path: Path, keep: str | None, answers: str = "y") -> subprocess.CompletedProcess:
    """cmd_update до решения про данные: ensure_python — первая команда после
    вопроса, на ней и останавливаемся, печатая решение."""
    src = BOT_SCRIPT.read_text(encoding="utf-8")
    prog = (
        "set -euo pipefail\n"
        'DATA_DIR=/x; ENV_FILE=/x/env\n'
        "log() { echo \"LOG $*\"; }\n"
        "die() { echo \"DIE $*\"; exit 1; }\n"
        "require_root() { :; }; require_installed() { :; }\n"
        'locate_tgz() { echo "$2"; }\n'
        f'confirm() {{ echo "ASKED $1"; [[ "{answers}" == y ]]; }}\n'
        'ensure_python() { echo "WIPE=$wipe"; exit 0; }\n'
        + _func(src, "cmd_update") +
        'cmd_update /tmp/awg-bot.tgz\n'
    )
    env = {"PATH": "/usr/bin:/bin"}
    if keep is not None:
        env["AWG_UPDATE_KEEP"] = keep
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=10, env=env)


def test_update_from_the_installer_does_not_ask_about_deleting_data(tmp_path):
    """Человек уже выбрал «обновить, сохранив данные». Второй вопрос про
    удаление — ловушка: «y» по привычке снесла бы БД и секреты."""
    r = _run_update(tmp_path, "1")
    assert r.returncode == 0, r.stderr
    assert "ASKED" not in r.stdout, f"вопрос задан при KEEP=1: {r.stdout}"
    assert "WIPE=0" in r.stdout, r.stdout


@pytest.mark.parametrize("keep", [None, "0", ""])
def test_plain_update_still_asks_about_data(tmp_path, keep):
    """`sudo awg-bot update` руками — вопрос остаётся: это единственный путь
    обновиться с чистого листа без переустановки."""
    r = _run_update(tmp_path, keep)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("ASKED") == 2, r.stdout
    assert "WIPE=1" in r.stdout


class _PostUpdate:
    """cmd_post_update с подставными шагами сборки и подставным awg-bot.sh,
    который записывает, с чем его перезапустили."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.log = tmp_path / "calls.log"; self.log.write_text("")
        self.self_path = tmp_path / "awg-bot.sh"
        self.self_path.write_text('#!/bin/sh\necho "self $*" >> "$CALLS"\n')
        self.self_path.chmod(0o755)

    def run(self, bundle: str | None, wipe: str = "0") -> subprocess.CompletedProcess:
        src = BOT_SCRIPT.read_text(encoding="utf-8")
        stubs = " ".join(f"{n}() {{ :; }};" for n in (
            "require_root", "post_update_rescue", "ensure_python", "ensure_ping", "build_venv",
            "install_unit", "ensure_host_autostart", "ensure_awg_module_loaded",
            "ensure_awg_generation", "adopt_resolver", "seed_conf", "validate_config",
            "prune_old_kernel_builds", "sleep"))
        prog = (
            "set -euo pipefail\n"
            f'SELF_PATH="{self.self_path}"; SERVICE=awg-bot\n'
            f'DATA_DIR="{self.tmp}/data"; ENV_FILE="{self.tmp}/env"; CONF_DIR="{self.tmp}/conf"\n'
            + stubs + "\n"
            "systemctl() { return 0; }\n"
            "log() { echo \"LOG $*\"; }; ok() { echo \"OK $*\"; }; warn() { echo \"WARN $*\"; }\n"
            "die() { echo \"DIE $*\"; exit 1; }\n"
            + _func(src, "cmd_post_update") +
            f'cmd_post_update {wipe}\necho AFTER_POST_UPDATE\n'
        )
        env = {"PATH": "/usr/bin:/bin", "CALLS": str(self.log)}
        if bundle is not None:
            env["AWG_UPDATE_THEN_BUNDLE"] = bundle
        return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=10, env=env)

    def calls(self) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln]


def test_post_update_applies_the_gateway_bundle_with_the_new_code(tmp_path):
    """Обновили шлюз вместе с новым файлом: после «Обновление завершено» тот
    же процесс применяет файл уже новым скриптом — ровно этим путём."""
    pu = _PostUpdate(tmp_path)
    bundle = tmp_path / "awg-gw-bundle.sh"; bundle.write_text("#!/bin/sh\n")
    r = pu.run(str(bundle))
    assert r.returncode == 0, r.stdout + r.stderr
    assert pu.calls() == [f"self reconfigure --role gateway --bundle {bundle}"], pu.calls()
    assert r.stdout.index("Обновление завершено") < r.stdout.index("применяю конфигурацию шлюза")
    assert "AFTER_POST_UPDATE" not in r.stdout, "reconfigure должен сменить процесс (exec)"


@pytest.mark.parametrize("bundle", [None, ""])
def test_post_update_without_a_bundle_just_finishes(tmp_path, bundle):
    """Обычное обновление (ВПС, шлюз без файла) — никакого reconfigure."""
    pu = _PostUpdate(tmp_path)
    r = pu.run(bundle)
    assert r.returncode == 0, r.stdout + r.stderr
    assert pu.calls() == [], pu.calls()
    assert "Обновление завершено" in r.stdout and "AFTER_POST_UPDATE" in r.stdout


def test_post_update_skips_a_bundle_that_is_gone(tmp_path):
    """Файл успели удалить (или путь был опечаткой) — обновление всё равно
    завершено; reconfigure с несуществующим файлом упал бы уже после успеха."""
    pu = _PostUpdate(tmp_path)
    r = pu.run(str(tmp_path / "нет-такого.sh"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert pu.calls() == [] and "AFTER_POST_UPDATE" in r.stdout


def test_post_update_with_wiped_data_does_not_apply_the_bundle(tmp_path):
    """Данные удалены по запросу — дальше только ручной reconfigure; применять
    файл поверх пустой установки из хвоста обновления нельзя."""
    pu = _PostUpdate(tmp_path)
    bundle = tmp_path / "awg-gw-bundle.sh"; bundle.write_text("#!/bin/sh\n")
    r = pu.run(str(bundle), wipe="1")
    assert r.returncode != 0 and "DIE запусти: sudo awg-bot reconfigure" in r.stdout, r.stdout
    assert pu.calls() == []
