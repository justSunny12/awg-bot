"""
Уведомления о расхождении конфигурации шлюза по снимку канала
(domain/services/gateway_link: gw_bundle_drift_notes, _gw_drift_note,
_gw_drift_pending, _gw_drift_text, _gw_admin_drift_lines) и окружение сборки
бандла (LINK_KEEPALIVE).

Канал обязателен, и настройки обвязки он довозит сам: памяти о выдаче больше
нет, судит только снимок — что реально стоит на шлюзе против того, что ВПС
выдал бы сейчас. Человеку пишем лишь о том, что канал не закроет: подсети
других шлюзов (едут только файлом) и отказ шлюза применить ТЕКУЩИЙ набор.
Цена ошибок: уведомление за уведомлением о том, что канал довезёт сам (и
привычка их не читать), — или месяцы с файерволом шлюза, который не знает
новых устройств админа, потому что шлюз молча отверг набор.
"""
from __future__ import annotations

import datetime

import pytest

from awgbot.core import config, settings
from awgbot.util import gwlink, timeutil
from tests.conftest import SH

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID


@pytest.fixture()
def slot(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Слот шлюза с режимом без VPN; маршрутизация включена; снимка и сессии нет."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    real = settings.get_bool
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False:
                        True if k == "app.routing.enabled" else real(k, d))
    return services.db.gateway(1)


def _admin(services):
    return services.db.get_client_by_tg(ADMIN)


def _installed(services, **over) -> dict:
    """То, что стоит на шлюзе, когда всё доехало, — с правками по сценарию."""
    env = services.gwlink_issued_env(services.db.gateway(1))
    got = {gwlink.snap_field(k): v for k, v in env.items()}
    got.update(over)
    return got


def _snap(services, bundle: dict) -> None:
    services.gwlink_snapshot_in(1, {"bundle": bundle, "agent_version": "3.3.0", "rev": 1}, 1, True)


def _online(services) -> None:
    services.gwlink_session_opened(1, "3.3.0", 2)


def _refuse(services, error: str = "exit 1") -> None:
    """Шлюз ответил отказом на набор, который ВПС хочет сейчас: отпечаток в
    ответе ставит слушатель из того, что ушло в сессии."""
    want = services.gwlink_settings_want(services.db.gateway(1))
    services.gwlink_ack_in(1, {"ok": False, "error": error, "hash": gwlink.settings_hash(want)})


def _offline_for(services, days: float) -> None:
    """Канал лёг давно: сессии нет, последний раз на связи и снимок — дни назад."""
    services.gwlink_session_closed(1)
    old = timeutil.to_iso(timeutil.now() - datetime.timedelta(days=days))
    services.db.set_state("gwlink_seen_1", old)
    services.db.set_state("gwlink_snap_at_1", old)


# ── когда молчим ─────────────────────────────────────────────────────────────

def test_a_gateway_off_the_channel_is_never_notified_however_long_it_is_silent(services, slot):
    """Шлюз не на связи месяц, на нём другие подсети, чужие подсети соседей и
    прежний список устройств админа. Канал довезёт ключи обвязки, как только
    шлюз вернётся, а расхождение всё это время видно в карточке; уведомление
    по возрасту молчания — тот самый шум «перевыпусти», от которого ушли."""
    services.add_device(_admin(services).id, "Ноутбук")
    _snap(services, _installed(services, home_subnets="192.168.1.0/24",
                               peer_home_nets="192.168.2.0/24", admin_ips="10.8.1.250"))
    _refuse(services)
    _offline_for(services, 30)
    assert services.gwlink_config_drift_items(slot), "сценарий без расхождения ничего не проверяет"
    assert services.gw_bundle_drift_notes() == [], "шлюз не на связи, а бот шлёт уведомление"
    _offline_for(services, 400)
    assert services.gw_bundle_drift_notes() == [], "молчание дольше — и уведомление пришло"


def test_a_delivery_in_flight_is_not_notified(services, slot):
    """Шлюз на связи, свои подсети на нём прежние: канал их уже везёт (ответа
    нет) или шлюз применил и снимок ещё не догнал (ответ «ок»). Уведомление
    здесь сообщило бы о том, что вот-вот закроется само."""
    _snap(services, _installed(services, home_subnets="192.168.1.0/24", resolver="1.1.1.1"))
    _online(services)
    assert services.gw_bundle_drift_notes() == [], "доставка в пути, а бот уже шлёт уведомление"
    want = services.gwlink_settings_want(slot)
    services.gwlink_ack_in(1, {"ok": True, "error": "", "hash": gwlink.settings_hash(want)})
    assert services.gw_bundle_drift_notes() == [], "шлюз ответил «ок», а бот шлёт уведомление"


def test_without_a_snapshot_there_is_nothing_to_notify_about(services, slot):
    """Шлюз ещё не сообщал, что у него стоит: судить не по чему. Ни смена
    устройств админа, ни открытая сессия, ни отказ шлюза уведомления без
    снимка не порождают — иначе вернулась бы догадка по памяти о выдаче."""
    services.add_device(_admin(services).id, "Ноутбук")
    _online(services)
    _refuse(services)
    assert services.gwlink_snapshot(1) == {}, "сценарий — без снимка"
    assert services.gw_bundle_drift_notes() == []
    # снимок без блока конфигурации (агент его не прислал) — то же «не по чему»
    services.gwlink_snapshot_in(1, {"agent_version": "3.3.0", "rev": 1}, 1, True)
    assert services.gw_bundle_drift_notes() == [], "снимок без bundle сошёл за расхождение"


def test_nothing_is_notified_while_routing_is_off(services, slot, monkeypatch):
    """Маршрутизация выключена — шлюзы не несут трафик, и уведомлять о их
    конфигурации незачем, даже при живом отказе."""
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    _online(services)
    _refuse(services)
    monkeypatch.setattr(config, "ROUTING_ENABLED", False)
    assert services.gw_bundle_drift_notes() == []
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    assert len(services.gw_bundle_drift_notes()) == 1, "проверка выше ничего не доказывала"


# ── отказ шлюза ──────────────────────────────────────────────────────────────

def test_a_refusal_of_the_current_set_is_notified_once(services, slot):
    """Шлюз на связи и отказался применить именно то, что ВПС хочет сейчас:
    канал этого сам не закроет. Одно уведомление: что разошлось, почему шлюз
    не применил (экранировано — причина с чужой машины), кнопка выпуска файла.
    Хвоста «Перевыпусти» нет: канал обязателен, ссылка — кнопкой."""
    services.bot_username = "awgtestbot"
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    _online(services)
    _refuse(services, "dnsmasq <b>не</b> поднялся & всё")
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, [n.text for n in notes]
    note = notes[0]
    lines = note.text.splitlines()
    assert lines[0] == ('🛰 <a href="https://t.me/awgtestbot?start=gw-1">NASPi</a>: '
                        "конфигурация неактуальна — локальные подсети"), lines[0]
    assert lines[1] == "Шлюз не применил: dnsmasq &lt;b&gt;не&lt;/b&gt; поднялся &amp; всё", lines[1]
    assert ("локальные подсети: у сервера <code>192.168.68.0/24</code>, "
            "на шлюзе <code>192.168.1.0/24</code>") in lines, "не сказано, что с чем разошлось"
    assert "Перевыпусти" not in note.text
    assert note.action == ("gwcfg", 1) and note.tg_id == ADMIN
    assert services.gw_bundle_drift_notes() == [], "то же расхождение — второе уведомление"


def test_a_refusal_of_a_previous_set_is_not_taken_for_a_refusal_of_this_one(services, slot):
    """Шлюз отверг прежний набор, человек поправил подсети на ВПС — новый набор
    ещё в пути. Старый отказ не про него: уведомление «шлюз не применил»
    сейчас было бы ложью, и человек полез бы чинить то, что уже исправил."""
    _refuse(services)                                    # отказ на набор до правки
    services.gateway_set_lan_subnets(1, "192.168.70.0/24")
    _snap(services, _installed(services, home_subnets="192.168.68.0/24"))
    _online(services)
    assert services.gwlink_ack(1)["ok"] is False
    assert services.gw_bundle_drift_notes() == [], "отказ на прежний набор сошёл за отказ на текущий"
    _refuse(services)                                    # а вот на текущий — уже повод
    assert len(services.gw_bundle_drift_notes()) == 1, "проверка выше ничего не доказывала"


def test_a_new_set_of_differences_is_notified_again_once(services, slot):
    """Уведомление — одно на набор расхождений: добавилось новое — ещё одно,
    и снова одно."""
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    _online(services)
    _refuse(services)
    assert len(services.gw_bundle_drift_notes()) == 1
    _snap(services, _installed(services, home_subnets="192.168.1.0/24", resolver="1.1.1.1"))
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "резолвер" in notes[0].text, "новое расхождение прошло молча"
    assert services.gw_bundle_drift_notes() == []


# ── подсети других шлюзов ────────────────────────────────────────────────────

def test_neighbour_subnets_are_notified_even_with_a_live_channel(services, slot):
    """Подсети соседей канал не везёт — они живут и в конфиге линка. Живой
    канал не повод молчать: без файла доступ между подсетями не заработает.
    Свои подсети, расходящиеся тут же, канал довезёт — их в тексте нет."""
    _snap(services, _installed(services, peer_home_nets="192.168.2.0/24",
                               home_subnets="192.168.1.0/24"))
    _online(services)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, [n.text for n in notes]
    text = notes[0].text
    assert text.splitlines()[0].endswith("конфигурация неактуальна — локальные подсети других шлюзов"), text
    assert ("локальные подсети других шлюзов: у сервера «—», на шлюзе <code>192.168.2.0/24</code>"
            in text.splitlines()), text
    assert "локальные подсети: у сервера" not in text, "в уведомлении то, что канал довезёт сам"
    assert "Шлюз не применил" not in text, "отказа не было, а он назван"
    assert notes[0].action == ("gwcfg", 1)
    assert services.gw_bundle_drift_notes() == []


def test_markup_from_the_gateway_does_not_reach_the_message(services, slot):
    """Значения на шлюзе приехали с чужой машины. `<` без экранирования
    Telegram отвергает целиком — уведомление не дошло бы вовсе."""
    _snap(services, _installed(services, peer_home_nets="<b>x</b>&"))
    _online(services)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1
    assert "<b>x</b>" not in notes[0].text and "&lt;b&gt;x&lt;/b&gt;&amp;" in notes[0].text, notes[0].text


# ── устройства админа ────────────────────────────────────────────────────────

def _admin_note(services) -> str:
    _online(services)
    _refuse(services)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, [n.text for n in notes]
    assert notes[0].text.splitlines()[0].endswith("конфигурация неактуальна — устройства админа"), notes[0].text
    return notes[0].text


def test_admin_devices_missing_on_the_gateway_are_named(services, slot):
    """Человек завёл два устройства, шлюз отверг новый список. Адреса без имён
    ему ничего не говорят — строка на устройство с именем и адресом, по
    порядку адресов; заголовок группы во множественном числе."""
    before = services.gwlink_issued_env(slot)["ADMIN_IPS"]
    a = services.add_device(_admin(services).id, "Ноутбук")
    b = services.add_device(_admin(services).id, "Телефон")
    _snap(services, _installed(services, admin_ips=before))
    lines = _admin_note(services).splitlines()
    i = lines.index("На шлюзе не помечены админскими устройства:")
    assert lines[i + 1:i + 3] == [f" · Ноутбук (<code>{a.address}</code>)",
                                  f" · Телефон (<code>{b.address}</code>)"], lines
    assert not any("удалённ" in x for x in lines), "лишних адресов на шлюзе нет, а группа есть"


def test_a_single_missing_admin_device_reads_in_singular(services, slot):
    before = services.gwlink_issued_env(slot)["ADMIN_IPS"]
    a = services.add_device(_admin(services).id, "<i>Ноут</i>")
    _snap(services, _installed(services, admin_ips=before))
    lines = _admin_note(services).splitlines()
    i = lines.index("На шлюзе не помечено админским устройство:")
    assert lines[i + 1] == f" · &lt;i&gt;Ноут&lt;/i&gt; (<code>{a.address}</code>)", (
        "имя устройства задаёт человек: без экранирования Telegram отверг бы сообщение")


def test_a_removed_admin_device_left_on_the_gateway_is_named_from_history(services, slot):
    """Устройство удалено, а шлюз всё ещё пускает его адрес как админский —
    это дыра: адрес достанется следующему устройству любого клиента. Имя
    удалённого берётся из истории, чтобы человек понял, о чём речь."""
    old = services.add_device(_admin(services).id, "Старый планшет")
    with_old = services.gwlink_issued_env(slot)["ADMIN_IPS"]
    services.remove_device(old.device_id)
    _snap(services, _installed(services, admin_ips=with_old))
    lines = _admin_note(services).splitlines()
    i = lines.index("На шлюзе помечено админским удалённое устройство:")
    assert lines[i + 1] == f" · Старый планшет (<code>{old.address}</code>)", lines
    assert not any("не помечен" in x for x in lines), "недостающих нет, а группа есть"


def test_a_reused_address_is_named_by_its_latest_history_record(services, slot):
    """Адрес освобождался дважды: сначала его держал «Первый», потом —
    «Второй». На шлюзе он остался от последнего — назвать надо его."""
    admin = _admin(services)
    first = services.add_device(admin.id, "Первый")
    services.remove_device(first.device_id)
    second = services.add_device(admin.id, "Второй")
    assert second.address == first.address, "аллокатор не отдал адрес повторно — сценарий не тот"
    with_second = services.gwlink_issued_env(slot)["ADMIN_IPS"]
    services.remove_device(second.device_id)
    # первая запись — на час раньше: в одну секунду порядок записей не различить
    services.db._connection().execute(
        "UPDATE devices_histories SET archived_at = ? WHERE name = 'Первый'",
        (timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=1)),))
    _snap(services, _installed(services, admin_ips=with_second))
    assert f" · Второй (<code>{second.address}</code>)" in _admin_note(services).splitlines()


def test_unknown_and_many_addresses_are_listed_whole_from_the_snapshot(services, slot):
    """Адреса, которых нет ни у живых устройств, ни в истории, — строкой с
    одним адресом. Их много: строка расхождения обрезана до 160 знаков, но
    группы строятся по самому снимку — ни один адрес не теряется. Разметка в
    списке со шлюза до сообщения не доходит."""
    mine = services.gwlink_issued_env(slot)["ADMIN_IPS"]
    extra = [f"10.8.1.{n}" for n in range(200, 216)]
    _snap(services, _installed(services, admin_ips=" ".join([mine, *extra, "<b>x</b>"])))
    assert services.gwlink_config_drift_items(slot)[0][3].endswith("…"), "строка не обрезана — сценарий не тот"
    text = _admin_note(services)
    lines = text.splitlines()
    i = lines.index("На шлюзе помечены админскими удалённые устройства:")
    assert lines[i + 1:] == [f" · <code>{a}</code>" for a in extra], lines[i:]
    assert "<b>x</b>" not in text, "разметка со шлюза дошла до сообщения"


# ── отбой ────────────────────────────────────────────────────────────────────

def test_a_difference_that_went_away_gets_one_quiet_all_clear(services, slot):
    """Шлюз применил — на нём то же, что выдаёт сервер. После уведомления —
    один отбой, чтобы было видно, что дело закрыто; дальше тишина."""
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    _online(services)
    _refuse(services)
    assert len(services.gw_bundle_drift_notes()) == 1
    _snap(services, _installed(services))
    notes = services.gw_bundle_drift_notes()
    assert [n.text for n in notes] == ["✅ NASPi: конфигурация актуализирована"]
    assert services.gw_bundle_drift_notes() == [], "отбой повторился"


def test_no_all_clear_without_a_notification_before_it(services, slot):
    """Расхождение было, но о нём не уведомляли (канал вёз его сам). Сошлось —
    отбой о том, о чём человек не слышал, был бы шумом."""
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    _online(services)
    assert services.gw_bundle_drift_notes() == []
    _snap(services, _installed(services))
    assert services.gw_bundle_drift_notes() == [], "отбой без уведомления"


# ── окружение сборки бандла ──────────────────────────────────────────────────

@pytest.mark.parametrize("value, want", [(25, "25"), ("20-30", "20-30"), ("25-35", "25-35")])
def test_the_bundle_carries_the_link_keepalive_from_the_client_config(services, slot, monkeypatch,
                                                                     value, want):
    """Ритм keepalive линка — тот же, что у клиентских пиров. Скрипт линка
    читает YAML awk-ом только в двойных кавычках: `keepalive_seconds: 25` без
    них у него пропадал в «25-35». Бот читает YAML целиком и везёт значение
    окружением — иначе линк шёл бы не в том ритме, что клиенты."""
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None:
                        value if k == "app.client_config.keepalive_seconds" else real(k, d))
    env, _ = services._gw_bundle_env(services.db.gateway(1))
    assert env["LINK_KEEPALIVE"] == want


def test_the_keepalive_the_bundle_carries_survives_the_link_script(services, slot, monkeypatch):
    """Значение из окружения проходит разбор скрипта линка как есть: если
    скрипт счёл бы его мусором, он молча откатил бы линк к «25-35»."""
    import subprocess
    from pathlib import Path
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None:
                        25 if k == "app.client_config.keepalive_seconds" else real(k, d))
    env, _ = services._gw_bundle_env(services.db.gateway(1))
    script = (Path(__file__).resolve().parents[2] / "install" / "routing-link-setup.sh").read_text(encoding="utf-8")
    head = script.split('LINK_KEEPALIVE="${LINK_KEEPALIVE', 1)[1].split("\nesac\n", 1)[0]
    prog = '_cfg_keepalive=""\nLINK_KEEPALIVE="${LINK_KEEPALIVE' + head + '\nesac\nprintf "%s" "$LINK_KEEPALIVE"'
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "LINK_KEEPALIVE": env["LINK_KEEPALIVE"]})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "25", f"скрипт линка превратил {env['LINK_KEEPALIVE']!r} в {r.stdout!r}"


def test_lan_subnets_take_only_the_three_private_ranges(services, fake_awg, fake_routing,
                                                          make_active_client, monkeypatch):
    """Подсеть уходит в ip route replace основной таблицы ВПС: публичная или
    служебная (198.18/15, 192.0.2/24 — для is_private они «частные») уронила
    бы доступ к серверу с этих адресов. Плюс границы: не уже /30 (шире трёх
    диапазонов не бывает), не шире /16 (подсеть соседа уходит в AllowedIPs конфига
    линка другого шлюза, где строгое правило такой не пропустит), без пересечений
    в одном списке; снятая подсеть убирается из маршрутов."""
    from awgbot.infra import routing
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    bad = {"8.8.8.0/24": "частная", "198.18.0.0/15": "частная", "192.0.2.0/24": "частная",
           "100.64.0.0/10": "частная", "127.0.0.0/8": "частная", "192.168.1.5/32": "один адрес",
           "10.0.0.0/7": "частная", "10.0.0.0/8": "шире /16"}
    res = services.gateway_set_lan_subnets(1, "192.168.1.0/24 " + " ".join(bad))
    assert res["kept"] == ["192.168.1.0/24"]
    reasons = {tok: why for tok, why in res["rejected"]}
    for tok, want in bad.items():
        assert want in reasons.get(tok, ""), (tok, reasons.get(tok))
    res = services.gateway_set_lan_subnets(1, "192.168.1.0/24 192.168.0.0/16 172.16.5.0/24")
    assert res["kept"] == ["192.168.1.0/24", "172.16.5.0/24"]
    assert any("пересекается" in why for _t, why in res["rejected"])
    dropped = []
    monkeypatch.setattr(routing.marking, "drop_lan_routes", lambda pairs: dropped.extend(pairs))
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    monkeypatch.setattr(services, "_ensure_gateway_policy", lambda: None)
    services.gateway_set_lan_subnets(1, "172.16.5.0/24")
    assert dropped == [("192.168.1.0/24", "awglink")], dropped


# ── временный отказ, устройства других профилей, предел строк ────────────────

def test_a_temporary_refusal_is_not_a_refusal(services, slot):
    """Юнит обвязки ещё стартовал — агент ответил «повтори через минуту».
    Слушатель такой ответ записывает без отпечатка набора: сервер повторит
    набор сам, а уведомление и строка «не применил» в карточке были бы ложью
    про откат, которого не было."""
    services.add_device(_admin(services).id, "Ноутбук")
    _snap(services, _installed(services, admin_ips="10.8.1.250"))
    _online(services)
    services.gwlink_ack_in(1, {"ok": False, "error": "юнит обвязки ещё стартует", "hash": ""})
    assert services.gw_bundle_drift_notes() == [], "временный отказ принят за настоящий"
    assert services.gwlink_card(slot, 10)["ack_current"] is False, "карточка покажет «не применил» на retry"


def test_an_address_handed_to_another_profile_is_named_as_such_not_as_removed(services, slot, make_active_client):
    """Адрес устройства админа передан другому профилю, а шлюз всё ещё пускает
    его как админский. Это не «удалённое устройство» — оно живо и чужое,
    и подпись обязана сказать именно это."""
    other = make_active_client(name="Гость", tg_id=ADMIN + 1, device_limit=0)
    dev = services.add_device(other.id, "Чужой ноутбук")
    _snap(services, _installed(services, admin_ips=services.gwlink_issued_env(slot)["ADMIN_IPS"] + f" {dev.address}"))
    lines = _admin_note(services).splitlines()
    i = lines.index("На шлюзе помечено админским устройство другого профиля:")
    assert lines[i + 1] == f" · Чужой ноутбук (<code>{dev.address}</code>)", lines
    assert not any("удалённ" in x for x in lines), "живое устройство подписано как удалённое"


def test_a_long_group_is_cut_with_a_tail_count(services, slot):
    """Сорок устройств в группе — сорок строк уведомления, и у больших парков
    текст перерос бы предел сообщения, а подпись набора уже записана: второго
    шанса не было бы. Группа режется, хвост называет остаток."""
    mine = services.gwlink_issued_env(slot)["ADMIN_IPS"]
    extra = [f"10.8.1.{n}" for n in range(100, 125)]
    _snap(services, _installed(services, admin_ips=" ".join([mine, *extra])))
    lines = _admin_note(services).splitlines()
    i = lines.index("На шлюзе помечены админскими удалённые устройства:")
    assert lines[i + 1:] == [f" · <code>{a}</code>" for a in extra[:20]] + [" · …и ещё 5"], lines[i:]


def test_the_notification_signature_sees_the_whole_list_not_its_first_160_characters(services, slot):
    """Двадцать устройств: строка расхождения обрезана, и два разных списка
    совпадали бы в первых 160 знаках. Подпись набора считается по полным
    значениям — новое устройство после отказа даёт новое уведомление."""
    admin = _admin(services)
    for n in range(19):
        services.add_device(admin.id, f"Устройство {n}")
    _snap(services, _installed(services, admin_ips="10.8.1.250"))
    _online(services)
    _refuse(services)
    assert len(services.gw_bundle_drift_notes()) == 1
    services.add_device(admin.id, "Ещё одно")
    _refuse(services)                                    # отказ и на новый набор
    assert services.gwlink_config_drift_items(slot)[0][2].endswith("…"), "строка не обрезана — сценарий не тот"
    assert len(services.gw_bundle_drift_notes()) == 1, "новый список устройств сошёл за прежний"


# ── агент с документом конфигурации (cfg1) ───────────────────────────────────
# Документ везёт и подсети соседей, и конфиг линка: человеку пишем только об
# отказе шлюза применить ТЕКУЩИЙ документ. Конфиг линка здесь не собирается
# (источника на тестовой машине нет) — его сборка проверена отдельно.

@pytest.fixture()
def cfg1(services, slot, monkeypatch):
    """Шлюз на связи с агентом, который принимает документ; конфиг линка —
    подменён пустым."""
    monkeypatch.setattr(services, "gwcfg_link_conf", lambda gw, env: "")
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1"])
    return slot


def _snap_cfg(services, bundle: dict, **over) -> None:
    """Снимок агента с полем cfg: отпечатки — как у желаемого, с правками."""
    doc = services.gwcfg_doc(services.db.gateway(1))
    services.gwlink_snapshot_in(1, {"bundle": bundle, "cfg": {"gen": doc["gen"], **doc["prints"], **over},
                                    "agent_version": "3.3.0", "rev": 1}, 1, True)


def _cfg_refuse(services, failed: dict, *, digest: str | None = None, **extra) -> None:
    doc = services.gwcfg_doc(services.db.gateway(1))
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"] if digest is None else digest,
                              "applied": [], "failed": failed, **extra})


def test_a_cfg1_refusal_of_the_current_document_is_notified_once(services, cfg1):
    """Шлюз с документом отверг текущий документ — расхождение пережило
    доставку, и канал его сам не закроет. Одно уведомление: что разошлось и
    почему шлюз не применил (экранировано — причина с чужой машины)."""
    _snap_cfg(services, _installed(services, home_subnets="192.168.1.0/24"), env="0" * 16)
    _cfg_refuse(services, {"env": "dnsmasq <b>не</b> поднялся", "link": "dnsmasq <b>не</b> поднялся"})
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, [n.text for n in notes]
    lines = notes[0].text.splitlines()
    assert lines[0].endswith("конфигурация неактуальна — локальные подсети, конфиг линка"), lines[0]
    assert lines[1].startswith("Шлюз не применил: dnsmasq &lt;b&gt;не&lt;/b&gt; поднялся"), lines[1]
    assert ("локальные подсети: у сервера <code>192.168.68.0/24</code>, "
            "на шлюзе <code>192.168.1.0/24</code>") in lines, lines
    assert services.gw_bundle_drift_notes() == [], "тот же отказ — второе уведомление"


def test_a_cfg1_gateway_without_a_refusal_is_silent_whatever_drifts(services, cfg1):
    """Без отказа молчим даже при расхождении — и подсетей соседей тоже: агенту
    с документом их везёт канал, а не файл. Ответ «применено» — тоже тишина:
    снимок вот-вот догонит."""
    _snap_cfg(services, _installed(services, home_subnets="192.168.1.0/24", peer_home_nets="192.168.2.0/24",
                                   admin_ips="10.8.1.250"), env="0" * 16)
    assert services.gwlink_config_drift_items(cfg1), "сценарий без расхождения ничего не проверяет"
    assert services.gw_bundle_drift_notes() == [], "агенту с документом пришло уведомление без отказа"
    doc = services.gwcfg_doc(services.db.gateway(1))
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "applied": ["env"]})
    assert services.gw_bundle_drift_notes() == [], "шлюз ответил «применено», а бот шлёт уведомление"


def test_a_cfg1_refusal_of_a_previous_document_is_not_notified(services, cfg1):
    """Шлюз отверг прежний документ, человек уже поправил подсети на ВПС.
    Старый отказ — не про новый документ: «не применил» сейчас было бы ложью."""
    _cfg_refuse(services, {"env": "exit 1"})
    services.gateway_set_lan_subnets(1, "192.168.70.0/24")
    _snap_cfg(services, _installed(services, home_subnets="192.168.68.0/24"), env="0" * 16)
    assert services.gw_bundle_drift_notes() == [], "отказ на прежний документ сошёл за отказ на текущий"
    _cfg_refuse(services, {"env": "exit 1"})
    assert len(services.gw_bundle_drift_notes()) == 1, "проверка выше ничего не доказывала"


def test_a_refused_link_is_named_in_the_head_without_values(services, cfg1):
    """Шлюз отверг только конфиг линка. У конфига нет «значений у сервера и на
    шлюзе» — он назван в заголовке, а строк расхождения подсетей нет: их
    шлюз не отвергал."""
    _snap_cfg(services, _installed(services, home_subnets="192.168.1.0/24"), link="0" * 16)
    _cfg_refuse(services, {"link": "AllowedIPs: 172.16.0.0/12 шире /16"})
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, [n.text for n in notes]
    lines = notes[0].text.splitlines()
    assert lines[0].endswith("конфигурация неактуальна — конфиг линка"), lines[0]
    assert lines[1] == "Шлюз не применил: AllowedIPs: 172.16.0.0/12 шире /16", lines
    assert len(lines) == 2, f"лишние строки у конфига линка: {lines}"


def test_a_whole_document_refused_names_what_drifts(services, cfg1):
    """Документ не прошёл проверку целиком (`*`): расходящиеся значения названы
    все, причина — второй строкой."""
    _snap_cfg(services, _installed(services, resolver="1.1.1.1"), env="0" * 16)
    _cfg_refuse(services, {"*": "PEER_LAN_NETS: 172.16.0.0/12 — префикс вне /16–/30"})
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "резолвер" in notes[0].text.splitlines()[0], [n.text for n in notes]
    assert "Шлюз не применил: PEER_LAN_NETS" in notes[0].text


def test_a_cfg1_temporary_refusal_is_not_a_refusal(services, cfg1):
    """Агент отвечает «повтори» (юнит обвязки ещё работал) с причиной в
    failed — так его собирает сам агент. Сервер повторит документ сам; ни
    уведомления «не применил», ни отказа в карточке: отката не было."""
    from awgbot.domain import gwcfg
    _snap_cfg(services, _installed(services, home_subnets="192.168.1.0/24"), env="0" * 16)
    doc = services.gwcfg_doc(services.db.gateway(1))
    services.gwcfg_ack_in(1, gwcfg.ack_build(doc["gen"], doc["hash"], retry=True,
                                             failed={"env": "юнит обвязки ещё работает"}))
    assert services.gw_bundle_drift_notes() == [], "временный отказ агента принят за настоящий"
    card = services.gwcfg_card(services.db.gateway(1), services.gwlink_snapshot(1))
    assert card["failed"] == {}, "карточка покажет «не применил — вернул прежнюю» на «повтори»"


def test_a_cfg1_notification_carries_no_file_button(services, cfg1):
    """Агенту с документом файл не нужен: кнопка «📤 Конфигурация» под
    уведомлением — только агентам 3.2, у которых часть едет файлом. Кнопка
    звала бы перевыпускать то, что канал уже пытался довезти."""
    _snap_cfg(services, _installed(services, home_subnets="192.168.1.0/24"), env="0" * 16)
    _cfg_refuse(services, {"env": "exit 1"})
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1
    assert notes[0].action is None, f"кнопка файла под уведомлением агенту с документом: {notes[0].action}"


def test_a_refused_link_alone_is_notified_too(services, cfg1, monkeypatch):
    """Сменили keepalive клиентов — меняется только конфиг линка, окружение
    обвязки совпадает. Шлюз отверг конфиг линка: это отказ на текущий
    документ, и о нём должно прийти уведомление, как об отказе в env, —
    иначе человек узнает о нём, только открыв карточку."""
    _snap_cfg(services, _installed(services), link="0" * 16)
    assert not services.gwlink_config_drift_items(cfg1), "сценарий — без расхождения окружения"
    _cfg_refuse(services, {"link": "проба линка не прошла — вернул прежний"})
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, "отказ применить конфиг линка прошёл без уведомления"
    assert "Шлюз не применил: проба линка не прошла" in notes[0].text
