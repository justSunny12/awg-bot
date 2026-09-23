"""
Строки расхождения «что выдаёт сервер — что стоит на шлюзе» (drift_lines в
domain/services/gwchannel.py; слой текстов её только реэкспортирует).

Одна функция кормит три места: карточку слота и экран выпуска конфигурации
(HTML), напоминание о перевыпуске (HTML) и подпись напоминания (без
разметки — по ней решается «то же расхождение или новое»). Значения «на
шлюзе» приехали с чужой машины: неэкранированный `<` Telegram отвергает
сообщение целиком, и человек не узнаёт о расхождении вовсе. Режим без VPN
словами, а не 1/0: «у сервера 1, на шлюзе 0» человеку ничего не говорит.
"""
from __future__ import annotations

import pytest

from awgbot.bot import texts
from awgbot.domain.services.gwchannel import drift_lines
from awgbot.util import gwlink

H = gwlink.KEY_HUMAN


def _items():
    return [("HOME_SUBNETS", H["HOME_SUBNETS"], "192.168.68.0/24", "192.168.1.0/24"),
            ("LAN_MODE", H["LAN_MODE"], "1", "0"),
            ("RESOLVER", H["RESOLVER"], "10.9.1.1", "")]


def test_html_lines_put_values_in_code_and_the_mode_in_words():
    assert drift_lines(_items(), html=True) == [
        "локальные подсети: у сервера <code>192.168.68.0/24</code>, на шлюзе <code>192.168.1.0/24</code>",
        "режим «За шлюзом — без VPN»: у сервера включён, на шлюзе выключен",
        "резолвер: у сервера <code>10.9.1.1</code>, на шлюзе «—»",
    ]


def test_plain_lines_quote_values_and_keep_the_mode_in_words():
    """Без разметки — для подписи напоминания и для мест, где HTML нет."""
    out = drift_lines(_items(), html=False)
    assert out == [
        "локальные подсети: у сервера «192.168.68.0/24», на шлюзе «192.168.1.0/24»",
        "режим «За шлюзом — без VPN»: у сервера включён, на шлюзе выключен",
        "резолвер: у сервера «10.9.1.1», на шлюзе «—»",
    ]
    assert not any("<" in line for line in out), "разметка в строках без HTML"


def test_nothing_to_say_gives_no_lines():
    assert drift_lines([], html=True) == [] and drift_lines([], html=False) == []


def test_an_empty_mode_is_a_dash_not_an_empty_word():
    out = drift_lines([("LAN_MODE", H["LAN_MODE"], "1", "")], html=True)
    assert out == ["режим «За шлюзом — без VPN»: у сервера включён, на шлюзе —"], out


def test_markup_from_the_gateway_is_escaped_in_html():
    """Значение с малины с `<b>` и `&` доезжает текстом внутри <code>."""
    out = drift_lines([("HOME_SUBNETS", H["HOME_SUBNETS"], "192.168.68.0/24", "<b>x</b>&")], html=True)
    assert out == ["локальные подсети: у сервера <code>192.168.68.0/24</code>, "
                   "на шлюзе <code>&lt;b&gt;x&lt;/b&gt;&amp;</code>"], out


@pytest.mark.parametrize("junk", ["<b>1</b>", "1<", "a&b"])
def test_an_unknown_mode_value_from_the_gateway_is_escaped_too(junk):
    """Режим — единственное поле, которое печатается без <code>: известные
    значения словами, а неизвестное — как пришло. «Как пришло» с малины в HTML
    без экранирования — ровно та дыра, от которой экранируют остальные поля:
    Telegram отвергнет напоминание целиком."""
    out = drift_lines([("LAN_MODE", H["LAN_MODE"], "1", junk)], html=True)[0]
    assert junk not in out, f"значение с малины легло в HTML как есть: {out}"
    assert junk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") in out, out


def test_the_texts_layer_draws_the_same_lines():
    """Слой текстов строки не пересобирает — берёт из домена: два источника
    одних и тех же строк однажды разошлись бы в формулировке."""
    assert texts.drift_lines is drift_lines


def test_neighbour_subnets_have_one_human_name_everywhere(tmp_path, monkeypatch):
    """«Локальные подсети других шлюзов» — одно имя в карточке, напоминании и
    проверке монитора агента: разные имена одного и того же читаются как две
    разные поломки."""
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra import gwguard
    from awgbot.infra.db import Database
    db = Database(tmp_path / "gw.db"); db.init_schema()
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "192.168.2.0/24" if k == "PEER_HOME_NETS" else "")
    check = GatewayServices(db).peer_nets_check({"sets": {"peer_nets4": set()}})
    name = "локальные подсети других шлюзов"
    assert H["PEER_HOME_NETS"] == name
    assert check is not None and check.name == name, check
