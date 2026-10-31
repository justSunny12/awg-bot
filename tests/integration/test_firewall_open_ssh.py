"""Фильтр SSH сервера не открывается всем молча: последний адрес при включённом
фильтре из чата не снимается (пустой список открывает SSH снаружи всем, а
раздел говорил «закрыт»), а список из одних имён, которые не резолвятся и
прежних адресов которых нет, — одно уведомление админу на состав имён.

Цена ошибки: админ убрал последний адрес, видит «фильтр включён» — а SSH
снаружи открыт всем; DNS лёг после рестарта — SSH открыт всем, и никто об
этом не знает.
"""
import pytest

from awgbot.domain.services import ServiceError
from awgbot.infra import nftguard
from tests.integration.test_firewall import fw  # noqa: F401


def test_the_last_address_is_not_removed_while_the_filter_is_on(services, fw):
    store, acts = fw
    store["app.firewall.enabled"] = True
    store["app.firewall.ssh_allow"] = ["203.0.113.7"]
    acts.clear()
    with pytest.raises(ServiceError, match="последний адрес"):
        services.firewall_allow_remove("203.0.113.7")
    assert store["app.firewall.ssh_allow"] == ["203.0.113.7"] and acts == [], "список и таблица не тронуты"


def test_the_last_address_goes_when_the_filter_is_off(services, fw):
    store, acts = fw
    store["app.firewall.ssh_allow"] = ["203.0.113.7"]
    assert services.firewall_allow_remove("203.0.113.7") == []
    assert store["app.firewall.ssh_allow"] == [] and acts == []


@pytest.fixture()
def dns(monkeypatch, tmp_path):
    answers: dict = {}
    monkeypatch.setattr(nftguard, "_resolve", lambda h: list(answers.get(h, [])))
    monkeypatch.setattr(nftguard, "RULES_FILE", str(tmp_path / "none.nft"))
    return answers


def test_names_that_open_ssh_are_told_once_and_the_mark_clears(services, fw, dns):
    store, _acts = fw
    store["app.firewall.enabled"] = True
    store["app.firewall.ssh_allow"] = ["dyn.example.org"]
    notes = services.firewall_open_notes()
    assert len(notes) == 1 and "открыт всем" in notes[0].text and "dyn.example.org" in notes[0].text
    assert not notes[0].text.endswith(".")
    assert services.firewall_open_notes() == [], "тот же состав имён — второй раз не говорим"
    dns["dyn.example.org"] = ["203.0.113.9"]
    assert services.firewall_open_notes() == []
    dns.clear()
    assert len(services.firewall_open_notes()) == 1, "починилось и снова легло — сказать снова"


@pytest.mark.parametrize("case", ["off", "empty", "literal", "held"])
def test_nothing_is_told_when_ssh_is_not_open_by_silent_names(services, fw, dns, tmp_path, monkeypatch, case):
    store, _acts = fw
    store["app.firewall.enabled"] = case != "off"
    store["app.firewall.ssh_allow"] = {"off": ["dyn.example.org"], "empty": [],
                                       "literal": ["dyn.example.org", "198.51.100.4"],
                                       "held": ["dyn.example.org"]}[case]
    if case == "held":
        f = tmp_path / "prev.nft"
        f.write_text("# ssh_allow: dyn.example.org = 203.0.113.9/32\n", encoding="utf-8")
        monkeypatch.setattr(nftguard, "RULES_FILE", str(f))
    assert services.firewall_open_notes() == []
