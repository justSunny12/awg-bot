"""
Итог «➕ Адрес» SSH-доступа агента, когда введённая подсеть поглотила адреса,
уже стоявшие в списке (nft не принимает пересечений — адреса уходят из
списка): какая подсеть кого поглотила (handlers/gateway/access._absorbing) и
как это сказано человеку (texts/gateway/access).

Цена ошибки: «вошли в неё» без имени подсети — человек не понимает, куда
делись его адреса, и вводит их заново; подсеть, приписанная не тому адресу, —
ложное объяснение в снимке; IPv6-адрес, сверенный с IPv4-подсетью, —
исключение в обработчике, итог ввода не приходит вовсе; подсеть жирным или
голым текстом Telegram превращает в ссылку.
"""
from __future__ import annotations

import pytest

from awgbot.bot.handlers.gateway.access import _absorbing
from awgbot.bot.texts.gateway import access as ta

pytestmark = pytest.mark.unit


# ── _absorbing: адрес → поглотившая подсеть ─────────────────────────────────

def test_each_address_is_matched_to_the_net_that_holds_it_among_several():
    """Две новые подсети в списке: каждый убранный адрес — к своей, а не к первой попавшейся."""
    after = ["203.0.113.7", "10.0.0.0/8", "192.168.5.0/24"]
    got = _absorbing(["192.168.5.1", "10.20.30.40", "192.168.5.200"], after)
    assert got == {"192.168.5.1": "192.168.5.0/24", "10.20.30.40": "10.0.0.0/8",
                   "192.168.5.200": "192.168.5.0/24"}, got


def test_a_narrower_net_absorbed_by_a_wider_one_is_matched_too():
    """Убрана не только точка, но и узкая подсеть, вошедшая в широкую."""
    assert _absorbing(["192.168.5.0/26"], ["192.168.0.0/16"]) == {"192.168.5.0/26": "192.168.0.0/16"}


def test_an_address_outside_every_net_gets_no_name():
    """Ни в одну подсеть списка не входит (ушёл по другой причине) — без имени:
    текст скажет «подсеть» без адреса, а не припишет чужую."""
    got = _absorbing(["172.16.0.9", "192.168.5.4"], ["192.168.5.0/24", "203.0.113.7"])
    assert got == {"192.168.5.4": "192.168.5.0/24"}, got
    assert "172.16.0.9" not in got, "адресу вне подсетей приписана подсеть"


def test_ipv6_and_ipv4_are_never_compared():
    """IPv6-адрес против IPv4-подсети (и наоборот) — не совпадение и не исключение:
    subnet_of на разных версиях бросает TypeError, и итог ввода не пришёл бы."""
    assert _absorbing(["2001:db8::5"], ["0.0.0.0/0"]) == {}, "IPv6 сопоставлен с IPv4-подсетью"
    assert _absorbing(["10.1.2.3"], ["::/0"]) == {}, "IPv4 сопоставлен с IPv6-подсетью"
    assert _absorbing(["2001:db8::5", "10.1.2.3"], ["::/0", "10.0.0.0/8"]) == \
        {"2001:db8::5": "::/0", "10.1.2.3": "10.0.0.0/8"}, "версия не учтена при смешанном списке"


def test_names_and_garbage_are_skipped_without_failing():
    """В списке живут и имена (home.dyn.example) — не адреса; ни в after, ни в
    убранных они не роняют разбор."""
    got = _absorbing(["home.dyn.example", "192.168.5.9"], ["other.dyn.example", "192.168.5.0/24"])
    assert got == {"192.168.5.9": "192.168.5.0/24"}, got


def test_nothing_gone_is_an_empty_map():
    assert _absorbing([], ["192.168.5.0/24"]) == {}
    assert _absorbing(["192.168.5.1"], []) == {}, "пустой список after — адрес не к чему приписать"


# ── текст итога ─────────────────────────────────────────────────────────────

def test_one_absorbing_net_is_named_in_monospace():
    text = ta.gateway_ssh_allow_added(["192.168.5.0/24"], ["192.168.5.1", "192.168.5.2"], nets=["192.168.5.0/24"])
    assert text == ("✅ Адреса для SSH-доступа: добавлено <code>192.168.5.0/24</code>; входят в уже добавленную "
                    "подсеть <code>192.168.5.0/24</code>, поэтому не добавлены в список: "
                    "<code>192.168.5.1</code>, <code>192.168.5.2</code>"), text


def test_several_nets_are_named_in_plural_through_more():
    """Две подсети — множественное число и обе моноширинным через запятую;
    больше трёх — «и ещё N» (тот же more, что у адресов)."""
    text = ta.gateway_ssh_allow_added([], ["10.1.1.1", "192.168.5.1"], nets=["10.0.0.0/8", "192.168.5.0/24"])
    assert text == ("✅ Адреса для SSH-доступа; входят в уже добавленные подсети <code>10.0.0.0/8</code>, "
                    "<code>192.168.5.0/24</code>, поэтому не добавлены в список: "
                    "<code>10.1.1.1</code>, <code>192.168.5.1</code>"), text
    five = [f"10.{i}.0.0/16" for i in range(5)]
    text = ta.gateway_ssh_allow_added([], ["10.0.0.1"], nets=five)
    assert "подсети <code>10.0.0.0/16</code>, <code>10.1.0.0/16</code>, <code>10.2.0.0/16</code> и ещё 2, поэтому" \
        in text, text


def test_without_a_known_net_the_text_says_just_the_net():
    """Подсеть не определилась — фраза без имени и без висящего пробела, а не «вошли в неё»."""
    text = ta.gateway_ssh_allow_added([], ["172.16.0.9"])
    assert text == ("✅ Адреса для SSH-доступа; входят в уже добавленную подсеть, поэтому не добавлены в список: "
                    "<code>172.16.0.9</code>"), text
    assert "в неё" not in text


def test_a_net_from_input_is_escaped_inside_code():
    text = ta.gateway_ssh_allow_added([], ["10.0.0.1"], nets=["<b>"])
    assert "<code>&lt;b&gt;</code>" in text and "<b>" not in text, text


def test_no_merged_part_without_merged_addresses():
    """Ничего не схлопнулось — про подсети ни слова, даже если nets переданы."""
    text = ta.gateway_ssh_allow_added(["203.0.113.1"], [], nets=["10.0.0.0/8"])
    assert text == "✅ Адреса для SSH-доступа: добавлено <code>203.0.113.1</code>", text


# ── строка снимка «Вошли в добавленные подсети» ─────────────────────────────

def test_the_snapshot_row_names_the_net_the_address_went_into():
    assert ta.ssh_merged_row("192.168.5.1", "192.168.5.0/24") == \
        "<code>192.168.5.1</code> (входит в <code>192.168.5.0/24</code>)"


def test_the_snapshot_row_without_a_net_is_just_the_address():
    assert ta.ssh_merged_row("172.16.0.9") == "<code>172.16.0.9</code>", "пустые скобки или «входит в» без подсети"
    assert ta.ssh_merged_row("<x>", "<y>") == "<code>&lt;x&gt;</code> (входит в <code>&lt;y&gt;</code>)"
