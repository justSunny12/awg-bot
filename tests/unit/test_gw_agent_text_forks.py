"""Развилки текстов агента шлюза, которые одной строкой не выразить: итог
применения при лежащем линке, шапка панели при неподтверждённой пометке,
всплывашки фильтра SSH по состоянию списка, отказ IPv6 моноширинным, итог
восстановления и отказ невыполненного действия — ⚠️, а не 🔴."""
from __future__ import annotations

from awgbot.bot import texts, ui
from awgbot.bot.texts import gateway as gwt
from awgbot.domain.gateway import GwStatus
from awgbot.domain.notices.gateway import gateway_apply_report


def test_config_result_is_partial_when_the_link_is_down_or_untouched():
    for link in ("foreign", "unconfirmed"):
        detail = gateway_apply_report({"UPLINK": "installed", "LINK": link, "GW_STATUS": "unconfirmed"})
        out = texts.gateway_config_result(True, detail)
        assert out.startswith("⚠️ <b>Конфигурация шлюза применена не полностью</b>"), out
    up = gateway_apply_report({"UPLINK": "installed", "LINK": "up", "GW_STATUS": "confirmed"})
    assert texts.gateway_config_result(True, up).startswith("✅ <b>Конфигурация шлюза применена</b>")


def test_apply_report_does_not_call_a_missing_uplink_unchanged():
    out = gateway_apply_report({"UPLINK": "unchanged", "LINK": "unconfirmed", "GW_STATUS": "unconfirmed"})
    assert "без изменений" not in out and out.startswith("Линк не тронут: аплинк этого устройства не найден"), out
    out = gateway_apply_report({"UPLINK": "unchanged", "LINK": "up"})
    assert out.startswith("Аплинк без изменений"), out


def test_config_failure_is_a_warning_with_a_capitalised_reason():
    out = texts.gateway_config_result(False, "routing-gw-setup.sh: аплинк <awg1> не поднялся — откатываю")
    lines = out.splitlines()
    assert lines[0] == "⚠️ <b>Конфигурация шлюза не применена — прежняя осталась</b>", out
    assert lines[1] == "Аплинк <code>awg1</code> не поднялся", out


def test_panel_head_does_not_claim_traffic_for_an_unconfirmed_mark():
    chan = {"online": True, "role": "active"}
    assert gwt._gw_role(GwStatus(link_up=True), chan) == "🟢 несёт трафик"
    assert gwt._gw_role(GwStatus(link_up=True, mark_status="confirmed"), chan) == "🟢 несёт трафик"
    for mark in ("unmarked", "foreign", "unconfirmed"):
        assert gwt._gw_role(GwStatus(link_up=True, mark_status=mark), chan) == "🟢 линк поднят", mark


def test_ssh_removed_toast_names_the_price_of_the_last_address():
    assert texts.gateway_ssh_removed("203.0.113.7", False) == "✅ 203.0.113.7: убран"
    out = texts.gateway_ssh_removed("203.0.113.7", True)
    assert out == "✅ 203.0.113.7: убран — список пуст, снаружи SSH открыт только серверу AWG"
    assert len(texts.GW_SSH_FILTER_ON_EMPTY_ALERT) <= 200
    assert "адреса из списка" not in texts.GW_SSH_FILTER_ON_EMPTY_ALERT


def test_ipv6_refusal_is_monospace_and_escaped():
    from awgbot.domain.gwssh import ipv6_refusal
    e = ipv6_refusal("2001:db8::<1>")
    assert e.html and str(e) == "<code>2001:db8::&lt;1&gt;</code> — IPv6, а фильтр на шлюзе только IPv4", str(e)


def test_reassert_result_and_failed_action_atom():
    ok = texts.gateway_op_result("Шлюз восстановлен", "Шлюз не восстановлен", True, "")
    bad = texts.gateway_op_result("Шлюз восстановлен", "Шлюз не восстановлен", False, "x", "дальше")
    assert ok == "✅ <b>Шлюз восстановлен</b>", ok
    assert bad == "⚠️ <b>Шлюз не восстановлен</b>\n<code>x</code>\nдальше", bad
    assert ui.fail("Не сохранено", "a<b") == "⚠️ Не сохранено: a&lt;b"


def test_lan_line_shows_the_cut_tail():
    st = {"port": 22, "lan": ["10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24", "10.0.4.0/24"],
          "new_plumbing": True}
    out = texts.gateway_ssh_text(st)
    assert "<code>10.0.2.0/24</code> и ещё 2)" in out, out
