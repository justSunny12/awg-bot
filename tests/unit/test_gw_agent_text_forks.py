"""Развилки текстов агента шлюза, которые одной строкой не выразить: шапка панели при неподтверждённой пометке,
всплывашки фильтра SSH по состоянию списка, отказ IPv6 моноширинным, итог
восстановления и отказ невыполненного действия — ⚠️, а не 🔴."""
from __future__ import annotations

from awgbot.bot import texts, ui
from awgbot.bot.texts import gateway as gwt
from awgbot.domain.gateway import GwStatus


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


def test_a_configuration_file_in_the_agent_chat_gets_one_answer():
    """Файл конфигурации шлюза в чате агента — один ответ: настройки идут по
    каналу конфигурации, файл убран (внутри ключи)."""
    t = texts.GW_REISSUE_FILE_REFUSED
    assert t.startswith("⚠️ Файл конфигурации не применён") and "по каналу конфигурации" in t
    assert t.endswith("Сообщение с файлом из чата убрал: внутри ключи")
    assert "перевыпус" not in t and "📤" not in t


