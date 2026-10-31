"""Вайтлист SSH сервера в таблице awg_bot_guard (nftguard/allow): имя, которое
не резолвится, держит прежние адреса из файла таблицы; пустые наборы — только
когда адресов не знаем вовсе, и это видно на экране; наборы `flags interval`
без пересечений.

Цена ошибки: (а) список из одних имён DynDNS после рестарта бота при молчащем
DNS давал пустые наборы — таблица открывала SSH снаружи всем, а экран говорил
«🟢 только адреса из списка»; (б) пересекающиеся записи (подсеть и адрес в
ней, имя в подсеть списка, анонс Telegram шире выученной сети) — nft отвергал
всю таблицу на «conflicting intervals»: фильтр, пул Telegram вебхука и зона не
вставали, сверка падала каждый тик.
"""
from __future__ import annotations

import ipaddress
import json

import pytest

from awgbot.core import config
from awgbot.infra import nftguard
from awgbot.bot.texts.settings.firewall import settings_firewall_text
from tests.unit.test_nftguard import _conf, host_mode  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture()
def rules(tmp_path, monkeypatch):
    f = tmp_path / "awg-bot-guard.nft"
    monkeypatch.setattr(nftguard, "RULES_FILE", str(f))
    return f


def _dns(monkeypatch, answers: dict):
    monkeypatch.setattr(nftguard, "_resolve", lambda h: list(answers.get(h, [])))


def _ssh_lines(text: str, port: int = 2222) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith(f"tcp dport {port}")]


# ── (а) имена без DNS ────────────────────────────────────────────────────────

def test_a_name_keeps_its_previous_addresses_across_a_restart_when_dns_is_silent(host_mode, rules, monkeypatch):
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["dyn.example.org"]})
    _dns(monkeypatch, {"dyn.example.org": ["203.0.113.9"]})
    text = nftguard.render(nftguard.build_spec([]))
    assert "# ssh_allow: dyn.example.org = 203.0.113.9/32" in text.splitlines()
    rules.write_text(text, encoding="utf-8")                     # применено — файл и есть прежний набор
    _dns(monkeypatch, {})                                        # рестарт: кэша нет, DNS молчит
    spec = nftguard.build_spec([])
    assert spec.ssh_allow4 == ["203.0.113.9/32"] and not spec.ssh_open
    assert spec.unresolved == ["dyn.example.org"] and spec.ssh_held == ["dyn.example.org"]
    again = nftguard.render(spec)
    assert "tcp dport 2222 accept" not in _ssh_lines(again), "SSH открыт всем при известных прежних адресах"
    assert again == text, "удержанный набор — тот же текст: сверка не дёргает таблицу каждый тик"


def test_a_removed_address_is_not_held_back_through_a_silent_name(host_mode, rules, monkeypatch):
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["203.0.113.7", "dyn.example.org"]})
    _dns(monkeypatch, {"dyn.example.org": ["198.51.100.4"]})
    rules.write_text(nftguard.render(nftguard.build_spec([])), encoding="utf-8")
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["dyn.example.org"]})
    _dns(monkeypatch, {})
    spec = nftguard.build_spec([])
    assert spec.ssh_allow4 == ["198.51.100.4/32"], "удержаны адреса имени, а не весь прежний набор"


def test_names_without_dns_and_without_a_previous_table_open_ssh_and_say_so(host_mode, rules, monkeypatch):
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["dyn.nowhere.example"]})
    _dns(monkeypatch, {})
    spec = nftguard.build_spec([])
    assert spec.ssh_open and spec.unresolved == ["dyn.nowhere.example"] and spec.ssh_held == []
    assert "tcp dport 2222 accept" in _ssh_lines(nftguard.render(spec)), \
        "адресов не знаем вовсе — запереть админа снаружи хуже, чем открыть"
    scr = settings_firewall_text({"enabled": True, "raw_allow": ["dyn.nowhere.example"], "allow": [],
                                  "unresolved": ["dyn.nowhere.example"], "ssh_port": 2222})
    line = next(ln for ln in scr.splitlines() if "Снаружи" in ln)
    assert "SSH открыт всем" in line and "🟢" not in line, line


def test_an_enabled_filter_with_an_empty_list_is_shown_open(host_mode, monkeypatch):
    """Пустой список при включённом фильтре — SSH открыт всем (так его
    оставляет `firewall setup` с пустым списком); экран говорил «закрыт»."""
    _conf(monkeypatch, **{"app.firewall.ssh_allow": []})
    spec = nftguard.build_spec([])
    assert spec.ssh_open and "tcp dport 2222 accept" in _ssh_lines(nftguard.render(spec))
    scr = settings_firewall_text({"enabled": True, "raw_allow": [], "allow": [], "ssh_port": 2222})
    line = next(ln for ln in scr.splitlines() if "Снаружи" in ln)
    assert "SSH открыт всем" in line and "закрыт" not in line, line


def test_held_lines_in_the_file_are_parsed_strictly(rules):
    rules.write_text("# ssh_allow: a.example = 203.0.113.9/32, junk, 2001:db8::1\n"
                     "# ssh_allow: bad name = 198.51.100.1/32\n"
                     "    # ssh_allow: b.example = 198.51.100.0/24\n", encoding="utf-8")
    assert nftguard.file_hosts() == {"a.example": ["203.0.113.9/32", "2001:db8::1/128"],
                                     "b.example": ["198.51.100.0/24"]}


# ── (б) пересечения в наборах interval ───────────────────────────────────────

def _overlaps(elems) -> bool:
    nets = [ipaddress.ip_network(e) for e in elems]
    return any(a.overlaps(b) for i, a in enumerate(nets) for b in nets[i + 1:])


def _set_elems(text: str, name: str) -> list[str]:
    block = text.split(f"set {name} {{", 1)[1].split("}", 1)[0] + "}"
    line = next((ln for ln in block.splitlines() if "elements" in ln), "")
    return [e.strip() for e in line.split("{", 1)[1].rsplit("}", 1)[0].split(",")] if line else []


def test_overlapping_allow_entries_are_collapsed(host_mode, rules, monkeypatch):
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["198.51.100.0/24", "198.51.100.7", "dyn.example.org",
                                                     "203.0.113.0/24", "2001:db8::/48", "2001:db8::5"]})
    _dns(monkeypatch, {"dyn.example.org": ["203.0.113.9"]})
    spec = nftguard.build_spec([])
    assert spec.ssh_allow4 == ["198.51.100.0/24", "203.0.113.0/24"]
    assert spec.ssh_allow6 == ["2001:db8::/48"]
    text = nftguard.render(spec)
    assert not _overlaps(_set_elems(text, nftguard.SET_ALLOW4))
    assert not _overlaps(_set_elems(text, nftguard.SET_ALLOW6))


def test_a_learned_supernet_does_not_break_the_tg_pool_set(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "reserve").mkdir()
    (tmp_path / "reserve" / "tg_pool.json").write_text(
        json.dumps({"pool": ["149.154.160.0/19", "91.108.0.0/16"]}), encoding="utf-8")
    pool = nftguard.tg_pool()
    assert not _overlaps(pool), pool
    covered = [ipaddress.ip_network(p) for p in pool]
    for want in ("149.154.160.0/20", "91.108.4.0/22", "149.154.160.0/19", "91.108.0.0/16"):
        assert any(ipaddress.ip_network(want).subnet_of(c) for c in covered), want
    block = nftguard._set_block(nftguard.SET_TG_POOL, "ipv4_addr",
                                ["149.154.160.0/20", "91.108.4.0/22", "149.154.160.0/19"], True)
    assert "149.154.160.0/20" not in block and "149.154.160.0/19" in block


def test_sets_without_overlaps_keep_their_order():
    elems = ["91.108.4.0/22", "149.154.160.0/20", "10.0.0.0/24", "10.0.1.0/24"]
    assert nftguard.noverlap(elems) == elems, "текст таблицы без пересечений не меняется"
    assert nftguard.noverlap(["203.0.113.9/32", "203.0.113.9/32"]) == ["203.0.113.9/32"]
    assert nftguard.noverlap(["не адрес", "10.0.0.0/8"]) == ["не адрес", "10.0.0.0/8"]
