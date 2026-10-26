"""Установка пакетов гостевой зоны (infra/rsvzone.install_tools): apt-get —
подменой в PATH, настоящий пакетный менеджер не запускается.

Цена ошибки: без dnsmasq резолвер зоны не поднимется, без conntrack хвост
соединений после возврата не сбросится — резерв «готов», а принять пользователей
второго сервера не может. Установка, молча проглотившая отказ apt-get, —
админ узнаёт об этом в день аварии; установка, спросившая что-то у терминала
(без DEBIAN_FRONTEND=noninteractive), — бот висит до таймаута; установка
чужих имён из списка — бот ставит на сервер что попало.
"""
from __future__ import annotations

import pytest

from awgbot.infra import rsvzone
from tests.conftest import fake_cmd

# настоящая функция — до автоподмены conftest (она не даёт тестам звать apt-get)
_install_tools = rsvzone.install_tools

APT = """#!/bin/sh
echo "frontend=$DEBIAN_FRONTEND args=$*" >> "$APT_LOG"
case "$*" in
  *" update"*) [ -n "$APT_FAIL_UPDATE" ] && { echo "E: списки не скачались" >&2; exit 100; } ;;
  *" install "*)
    [ -n "$APT_SLEEP" ] && exec sleep "$APT_SLEEP"
    if [ -n "$APT_FAIL_INSTALL" ]; then
      echo "Reading package lists..."
      echo "E: Unable to locate package dnsmasq" >&2
      echo "E: Unable to locate package conntrack" >&2
      exit 100
    fi ;;
esac
exit 0
"""


@pytest.fixture()
def apt(tmp_path, monkeypatch):
    """Каталог с подставным apt-get — единственный в PATH; журнал вызовов."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_cmd(bindir / "apt-get", APT)
    log = tmp_path / "apt.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("APT_LOG", str(log))
    for v in ("APT_FAIL_UPDATE", "APT_FAIL_INSTALL", "APT_SLEEP", "DEBIAN_FRONTEND"):
        monkeypatch.delenv(v, raising=False)
    return lambda: [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln]


def test_both_zone_tools_are_installed_after_a_lists_update_without_questions(apt):
    """Норма: списки обновлены, затем установка ровно двух пакетов зоны, без
    вопросов терминалу и без рекомендуемых пакетов; ответ — что поставлено."""
    assert _install_tools(["dnsmasq", "conntrack"]) == "dnsmasq conntrack"
    calls = apt()
    assert len(calls) == 2, f"ожидались update и install: {calls}"
    assert calls[0].endswith("args=-q update"), f"сначала обновление списков: {calls}"
    assert "install dnsmasq conntrack" in calls[1] and "-y" in calls[1] and "--no-install-recommends" in calls[1], calls
    assert all(c.startswith("frontend=noninteractive ") for c in calls), \
        f"apt-get без DEBIAN_FRONTEND=noninteractive может ждать ответа в терминале: {calls}"


def test_names_outside_the_zone_tools_are_never_installed(apt):
    """Список приходит из плана зоны; чужое имя в нём не превращается в
    установку постороннего пакета."""
    assert _install_tools(["dnsmasq", "curl", "../../evil"]) == "dnsmasq"
    assert "install dnsmasq" in apt()[-1] and "curl" not in apt()[-1] and "evil" not in apt()[-1], apt()


@pytest.mark.parametrize("names", [[], ["curl"]])
def test_nothing_to_install_runs_no_apt_get_at_all(apt, names):
    assert _install_tools(names) == ""
    assert apt() == [], "пустой список — а apt-get всё равно запускался"


def test_a_failed_install_is_a_zone_error_with_the_tail_of_apt_output(apt, monkeypatch):
    """Отказ apt-get — ZoneError с последними строками его вывода: эту причину
    админ получит в уведомлении."""
    monkeypatch.setenv("APT_FAIL_INSTALL", "1")
    with pytest.raises(rsvzone.ZoneError) as e:
        _install_tools(["dnsmasq", "conntrack"])
    msg = str(e.value)
    assert msg.startswith("apt-get: ") and "Unable to locate package conntrack" in msg, msg


def test_a_failed_lists_update_does_not_stop_the_install(apt, monkeypatch):
    """Обновление списков — попытка: кэш мог быть свежим, установка решает сама."""
    monkeypatch.setenv("APT_FAIL_UPDATE", "1")
    assert _install_tools(["conntrack"]) == "conntrack"
    assert "install conntrack" in apt()[-1], apt()


def test_without_apt_get_the_error_says_what_to_install_by_hand(tmp_path, monkeypatch):
    """Не Debian/Ubuntu: внятная причина с именами пакетов, а не FileNotFoundError."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(rsvzone.ZoneError) as e:
        _install_tools(["dnsmasq", "conntrack"])
    assert str(e.value) == "нет apt-get — поставь руками: dnsmasq conntrack", str(e.value)


def test_a_hanging_install_ends_by_the_timeout_as_a_zone_error(apt, monkeypatch):
    """apt-get завис (замок dpkg, медленное зеркало) — не вечное ожидание в
    фоне, а ZoneError по таймауту."""
    monkeypatch.setenv("APT_SLEEP", "5")
    with pytest.raises(rsvzone.ZoneError) as e:
        _install_tools(["dnsmasq"], timeout=1)
    assert "apt-get не запустился" in str(e.value), str(e.value)
