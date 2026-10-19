"""«🗂 SMB в туннеле» в «🛰 Шлюзы»: тумблер, диалоги, итоги, отказ и строка
карточки слота — что нажатия меняют в настройке и в файле записей резолвера
админа, и какие кнопки и строки приходят человеку. Сами экраны — в эталоне
tests/screens/admin.txt (adm.gw.smb_*, adm.gw.card.smb.tunnel*).

Цена ошибки: тумблер, который включается без резолвера админа или без
файервола, — «включено», а имён нет (или их видят все клиенты); вопрос,
который сам что-то меняет, — включение по неосторожному нажатию."""
from __future__ import annotations

import pytest

from awgbot.bot.callbacks import GwSlotCB
from awgbot.bot.handlers import settings as sh
from awgbot.bot.handlers.settings import slots as sh_slots
from awgbot.bot.handlers.settings.render import gateways_screen
from awgbot.core import config, settings
from awgbot.infra import admindns, nftguard, resolver
from tests.conftest import FakeState
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_slots_ui import _acb, _rows, _screen, _slot1, _slot2

pytestmark = pytest.mark.e2e
slots = _slots_ui.slots
ADMIN = config.ADMIN_ID
NAS = {"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.1.10"}
NAS2 = {"t": "_smb._tcp", "n": "Backup", "h": "backup", "p": 445, "a": "192.168.68.20"}
WHY = {
    "docker": "SMB в туннеле — только при VPN прямо на хосте, без docker",
    "no_client_resolver": "Нужен свой резолвер: ⚙️ → 🖥 Сервер AWG → «🔒 Свой резолвер»",
    "no_resolver": "Резолвер админа не поднят — на сервере: awg-bot resolver admin status",
    "no_firewall": "Нужен фильтр снаружи: ⚙️ → 🛡 SSH-доступ → «☑️ Фильтр снаружи» — без него имена увидят все клиенты",
}


@pytest.fixture()
def smb(monkeypatch, tmp_path, slots):
    """Хост со своим резолвером и резолвером админа, файервол включён;
    настройки — словарь, записи резолвера админа — в `records`."""
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    conf = tmp_path / "admin-dns.conf"
    conf.write_text("listen-address=10.255.53.53\nserver=10.8.1.1\n", encoding="utf-8")
    monkeypatch.setattr(admindns, "CONF_PATH", conf)
    monkeypatch.setattr(admindns, "service_active", lambda: True)
    monkeypatch.setattr(resolver, "installed", lambda: True)
    state = {"records": None, "calls": 0, "fail": ""}

    def run(mode, arg=""):
        state["calls"] += 1
        if state["fail"] == mode:
            raise admindns.AdminDnsError("подставной отказ")
        if mode == "records":
            if arg == "-":
                state["records"] = None
            else:
                with open(arg, encoding="utf-8") as f:
                    state["records"] = f.read()
        return ""
    monkeypatch.setattr(admindns, "run", run)
    store = {"app.routing.enabled": True, "app.routing.failover.enabled": True,
             "app.routing.peer_nets.enabled": False, "app.routing.smb_tunnel": False}
    real_get = settings.get
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(store.get(k, d)))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store[k] if k in store else real_get(k, d))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    state["store"], state["conf"] = store, conf
    return state


def _smb_row(markup) -> list[list[str]]:
    return [r for r in _rows(markup) if any("SMB в туннеле" in t for t in r)]


async def test_the_toggle_sits_alone_under_the_slots_with_one_slot(services, slots, smb):
    _, pi, _ = slots
    _slot1(services, pi)
    _, markup = await gateways_screen(services)
    assert _smb_row(markup) == [["☑️ SMB в туннеле"]], _rows(markup)


async def test_the_toggle_sits_below_failover_and_peer_nets_with_two_slots(services, slots, smb):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    smb["store"]["app.routing.smb_tunnel"] = True
    _, markup = await gateways_screen(services)
    rows = _rows(markup)
    i_pair = next(i for i, r in enumerate(rows) if any("Автопереключение" in t for t in r))
    assert rows[i_pair + 1] == ["✅ SMB в туннеле"], rows


async def test_without_slots_there_is_no_toggle_at_all(services, slots, smb):
    """Слотов нет — тумблера нет, даже если настройка включена: снятие
    последнего шлюза выключает «SMB в туннеле» само (gateway_remove)."""
    _, markup = await gateways_screen(services)
    assert _smb_row(markup) == []
    smb["store"]["app.routing.smb_tunnel"] = True
    _, markup = await gateways_screen(services)
    assert _smb_row(markup) == [], _rows(markup)


async def test_in_docker_there_is_no_toggle_at_all(services, slots, smb, monkeypatch):
    _, pi, _ = slots
    _slot1(services, pi)
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    _, markup = await gateways_screen(services)
    assert _smb_row(markup) == []


async def test_turning_on_asks_first_then_writes_the_records(services, slots, smb, fake_bot):
    """Вопрос ничего не меняет; «✅ Включить» — настройка, файл записей и
    итог первой строкой «🛰 Шлюзы» с тумблером ✅."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_lan_subnets(1, "192.168.1.0/24")
    services.gwlink_services_in(1, [NAS]); services.gwlink_session_opened(1, "3.3.0", 2)
    cb, nav = _acb(fake_bot)
    await sh_slots.gw_slot_smb_ask(cb, services)
    text, labels = _screen(nav)
    assert text.startswith("🗂 SMB в туннеле — включить?") and "awg.internal" in text, text
    assert labels == ["⬅️ Отмена", "✅ Включить"], "«Отмена» первой"
    assert smb["store"]["app.routing.smb_tunnel"] is False and smb["calls"] == 0, "вопрос уже включил"
    await sh_slots.gw_slot_smb_yes(cb, GwSlotCB(action="smb_yes", val="1"), services)
    assert smb["store"]["app.routing.smb_tunnel"] is True
    assert smb["records"] and "host-record=naspi5.awg.internal,10.240.1.10" in smb["records"]
    text, labels = _screen(nav)
    assert text.splitlines()[0] == "✅ SMB в туннеле включён", text
    assert "✅ SMB в туннеле" in labels


async def test_turning_off_asks_then_takes_the_records_down(services, slots, smb, fake_bot):
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_lan_subnets(1, "192.168.1.0/24")
    services.gwlink_services_in(1, [NAS]); services.gwlink_session_opened(1, "3.3.0", 2)
    services.smb_tunnel_set(True)
    assert smb["records"]
    cb, nav = _acb(fake_bot)
    await sh_slots.gw_slot_smb_ask(cb, services)
    text, labels = _screen(nav)
    assert text.startswith("🗂 SMB в туннеле — выключить?") and labels == ["⬅️ Отмена", "☑️ Выключить"]
    assert smb["store"]["app.routing.smb_tunnel"] is True, "вопрос о выключении уже выключил"
    await sh_slots.gw_slot_smb_yes(cb, GwSlotCB(action="smb_yes", val="0"), services)
    assert smb["store"]["app.routing.smb_tunnel"] is False and smb["records"] is None
    text, labels = _screen(nav)
    assert text.splitlines()[0] == "☑️ SMB в туннеле выключен" and "☑️ SMB в туннеле" in labels


@pytest.mark.parametrize("why", list(WHY))
async def test_turning_on_without_what_it_needs_is_a_toast_not_a_dialog(services, slots, smb, fake_bot,
                                                                         monkeypatch, why):
    _, pi, _ = slots
    _slot1(services, pi)
    if why == "no_resolver":
        smb["conf"].unlink()
    elif why == "no_client_resolver":
        monkeypatch.setattr(resolver, "installed", lambda: False)
    elif why == "docker":
        monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    else:
        monkeypatch.setattr(nftguard, "enabled", lambda: False)
    cb, nav = _acb(fake_bot)
    await sh_slots.gw_slot_smb_ask(cb, services)
    assert cb.answers == [(WHY[why], True)], cb.answers
    assert not [s for s in nav.sent if s[0] == "edit_text"], "диалог нарисован, хотя включить нельзя"
    assert smb["store"]["app.routing.smb_tunnel"] is False and smb["calls"] == 0


async def test_a_stale_yes_without_the_resolver_says_why_and_shows_the_list(services, slots, smb, fake_bot):
    """Диалог открыт, резолвер админа сняли — «✅ Включить» не включает:
    причина сообщением, на месте диалога — «🛰 Шлюзы» с ☑️."""
    _, pi, _ = slots
    _slot1(services, pi)
    smb["conf"].unlink()
    cb, nav = _acb(fake_bot)
    await sh_slots.gw_slot_smb_yes(cb, GwSlotCB(action="smb_yes", val="1"), services)
    assert smb["store"]["app.routing.smb_tunnel"] is False and smb["calls"] == 0
    said = [s[1] for s in nav.sent if s[0] == "answer"]
    assert len(said) == 1 and "SMB в туннеле не переключён" in said[0] and WHY["no_resolver"] in said[0], said
    assert "☑️ SMB в туннеле" in _screen(nav)[1]


async def test_a_repeated_yes_changes_nothing(services, slots, smb, fake_bot):
    """Старый диалог: цель уже стоит — всплывашка, без второго прогона скрипта."""
    _, pi, _ = slots
    _slot1(services, pi)
    smb["store"]["app.routing.smb_tunnel"] = True
    cb, nav = _acb(fake_bot)
    await sh_slots.gw_slot_smb_yes(cb, GwSlotCB(action="smb_yes", val="1"), services)
    assert cb.answers[0][0] == "Уже включён" and smb["calls"] == 0
    assert "✅ SMB в туннеле" in _screen(nav)[1]


@pytest.mark.parametrize("on", [True, False])
async def test_records_that_did_not_land_are_said_instead_of_the_usual_result(services, slots, smb, fake_bot, on):
    """Тумблер переставлен, а скрипт отказал — итог не врёт «включён»."""
    _, pi, _ = slots
    _slot1(services, pi)
    smb["store"]["app.routing.smb_tunnel"] = not on
    smb["fail"] = "records"
    cb, nav = _acb(fake_bot)
    await sh_slots.gw_slot_smb_yes(cb, GwSlotCB(action="smb_yes", val="1" if on else "0"), services)
    assert smb["store"]["app.routing.smb_tunnel"] is on
    first = _screen(nav)[0].splitlines()[0]
    assert first == ("⚠️ SMB в туннеле включён, но имена не применились" if on else
                     "⚠️ SMB в туннеле выключен, но имена ещё не сняты") + \
        " — диагностика на сервере: <code>awg-bot resolver admin status</code>", first


async def _card(services, fake_bot, slot=1) -> str:
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
    return _screen(nav)[0]


async def test_the_card_says_own_smb_servers_are_in_the_tunnel_with_a_single_gateway(services, slots, smb,
                                                                                     fake_bot):
    """Один шлюз, связи подсетей нет и быть не может — а имена его SMB у
    админа в туннеле есть: карточка обязана это сказать (
    «🗂 SMB: свои — 2 · в туннеле ✅»)."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_lan_subnets(1, "192.168.1.0/24")
    services.gwlink_services_in(1, [NAS]); services.gwlink_session_opened(1, "3.3.0", 2)
    smb["store"]["app.routing.smb_tunnel"] = True
    text = await _card(services, fake_bot)
    assert "🗂 SMB: свои — 1 · в туннеле ✅" in text.splitlines(), text


async def test_the_card_adds_the_tunnel_mark_next_to_peer_nets(services, slots, smb, fake_bot):
    """Вместе со связью подсетей — тем же хвостом в той же строке."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_lan_subnets(1, "192.168.1.0/24"); services.gateway_set_lan_subnets(2, "192.168.68.0/24")
    services.db.gateway_update(1, lan_mode=1); services.db.gateway_update(2, lan_mode=1)
    smb["store"]["app.routing.peer_nets.enabled"] = True
    services.gwlink_services_in(1, [NAS]); services.gwlink_session_opened(1, "3.3.0", 2)
    services.gwlink_services_in(2, [NAS2]); services.gwlink_session_opened(2, "3.3.0", 2)
    smb["store"]["app.routing.smb_tunnel"] = True
    line = next((ln for ln in (await _card(services, fake_bot)).splitlines() if ln.startswith("🗂 SMB")), "")
    assert line.startswith("🗂 SMB: свои — 1, извне — 1") and line.endswith(" · в туннеле ✅"), line
    smb["store"]["app.routing.smb_tunnel"] = False
    line = next((ln for ln in (await _card(services, fake_bot)).splitlines() if ln.startswith("🗂 SMB")), "")
    assert "в туннеле" not in line, "тумблер выключен — а карточка говорит «в туннеле»"
