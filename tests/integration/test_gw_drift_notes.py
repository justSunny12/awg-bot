"""
Напоминания о перевыпуске конфигурации шлюза по снимку канала
(domain/services/gateway_link: gw_bundle_drift_notes, _gw_snapshot_drift_note)
и окружение сборки бандла (LINK_KEEPALIVE).

Со снимком напоминание перестаёт быть памятью о выдаче: его и глушит, и будит
то, что реально стоит на шлюзе. Раньше снимок умел только глушить: выпустил
файл, но не применил — «выдано то же, что надо», и напоминания не было ни
одного, хотя на шлюзе стоит прежнее. Цена ошибок: человек месяцами живёт с
файерволом шлюза, не знающим его новых устройств, — или получает напоминание
за напоминанием о том, что канал довезёт сам.
"""
from __future__ import annotations

import datetime

import pytest

from awgbot.core import config, settings
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID


@pytest.fixture()
def slot(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Слот шлюза с режимом без VPN; маршрутизация включена."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    real = settings.get_bool
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False:
                        True if k == "app.routing.enabled" else real(k, d))
    # файл слота выдан ровно под текущее состояние: по памяти о выдаче
    # напоминать не о чем — всё, что ниже, решает только снимок
    g = services.db.gateway(1)
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1),
                          services._gw_bundle_deps(g))
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_SSH_KEY, 1),
                          " ".join(services._gw_ssh_allow()))
    return g


def _installed(services, **over) -> dict:
    """То, что стоит на шлюзе, когда всё доехало, — с правками по сценарию."""
    env = services.gwlink_issued_env(services.db.gateway(1))
    got = {k.lower(): v for k, v in env.items()}
    got.update(over)
    return got


def _snap(services, bundle: dict) -> None:
    services.gwlink_snapshot_in(1, {"bundle": bundle, "agent_version": "3.1.0", "rev": 1}, 1, True)


def _channel_silent_for(services, hours: float) -> None:
    services.gwlink_session_closed(1)
    services.db.set_state("gwlink_seen_1",
                          timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=hours)))


def test_a_bundle_issued_but_never_applied_is_reminded_by_the_snapshot(services, slot):
    """Человек перевыпустил файл и не применил его на шлюзе. По памяти о выдаче
    всё сошлось («выдано то же, что надо») — раньше это была тишина. Снимок
    показывает прежние подсети на шлюзе, канала давно нет: одно напоминание."""
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    _channel_silent_for(services, 30)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, f"напоминаний {len(notes)}: {[n.text for n in notes]}"
    text = notes[0].text
    assert "стоит не то, что выдаёт сервер" in text and "локальные подсети" in text
    assert "«192.168.68.0/24»" in text and "«192.168.1.0/24»" in text, "не сказано, что с чем разошлось"
    assert "Перевыпусти" in text
    assert services.gw_bundle_drift_notes() == [], "то же расхождение напомнено дважды"


def test_a_new_drift_is_reminded_again_once(services, slot):
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    _channel_silent_for(services, 30)
    assert len(services.gw_bundle_drift_notes()) == 1
    _snap(services, _installed(services, home_subnets="192.168.1.0/24", resolver="1.1.1.1"))
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "резолвер" in notes[0].text, "новое расхождение прошло молча"
    assert services.gw_bundle_drift_notes() == []


def test_a_drift_that_went_away_gets_one_quiet_all_clear(services, slot):
    """Человек применил файл — на шлюзе то же, что выдаёт сервер. После
    напоминания — один отбой, чтобы было видно, что дело закрыто; дальше
    тишина."""
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    _channel_silent_for(services, 30)
    assert len(services.gw_bundle_drift_notes()) == 1
    _snap(services, _installed(services))
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "совпадает с выданной" in notes[0].text, [n.text for n in notes]
    assert services.gw_bundle_drift_notes() == [], "отбой повторился"


def test_no_all_clear_without_a_reminder_before_it(services, slot):
    """Отбой без напоминания — шум: человеку не о чем было беспокоиться."""
    _snap(services, _installed(services))
    assert services.gw_bundle_drift_notes() == []


def test_what_the_live_channel_will_deliver_is_not_reminded(services, slot):
    """Канал жив — локальные подсети он довезёт сам. Напоминать перевыпустить
    файл ради них — шум: человек перевыпустит, а канал довёз бы то же самое."""
    _snap(services, _installed(services, home_subnets="192.168.1.0/24"))
    services.gwlink_session_opened(1, "3.1.0", 1)
    assert services.gw_bundle_drift_notes() == []


def test_what_only_the_file_carries_is_reminded_even_with_a_live_channel(services, slot):
    """Подсети соседей канал не везёт — они живут и в конфиге линка. Живой
    канал не повод молчать о них."""
    _snap(services, _installed(services, peer_home_nets="192.168.2.0/24"))
    services.gwlink_session_opened(1, "3.1.0", 1)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "подсети за другими шлюзами" in notes[0].text
    assert "локальные подсети" not in notes[0].text


def test_a_reminder_does_not_carry_markup_from_the_gateway(services, slot):
    """Значения на шлюзе приехали с чужой машины. `<` в них без экранирования
    Telegram отвергает целиком — напоминание не дошло бы вовсе."""
    _snap(services, _installed(services, home_subnets="<b>x</b>&"))
    _channel_silent_for(services, 30)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1
    assert "<b>x</b>" not in notes[0].text and "&lt;b&gt;x&lt;/b&gt;&amp;" in notes[0].text


def test_the_ssh_path_is_skipped_while_the_channel_covers_the_slot(services, slot, make_active_client):
    """Снимка ещё нет, но сессия канала открыта: список устройств админа он
    довезёт сам. Напоминание по памяти о выдаче («список устройств изменился»)
    здесь шум; канал замолчал больше суток — оно возвращается, один раз."""
    admin = services.db.get_client_by_tg(ADMIN)
    services.add_device(admin.id, "Ноутбук")              # устройств админа стало больше
    services.gwlink_session_opened(1, "3.1.0", 1)
    assert services.gwlink_snapshot(1) == {}, "сценарий — без снимка"
    assert services.gw_bundle_drift_notes() == [], "канал на связи, а бот гонит перевыпускать"
    _channel_silent_for(services, 30)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Список твоих устройств изменился" in notes[0].text
    assert services.gw_bundle_drift_notes() == []


def test_with_a_snapshot_the_memory_path_says_nothing(services, slot):
    """Снимок есть — решает он, и напоминание по памяти о выдаче не дублирует
    его вторым сообщением о том же."""
    admin = services.db.get_client_by_tg(ADMIN)
    services.add_device(admin.id, "Ноутбук")
    _snap(services, _installed(services))                  # на шлюзе ровно выдаваемое сейчас
    _channel_silent_for(services, 30)
    assert services.gw_bundle_drift_notes() == [], "снимок совпал, а напоминание по памяти пришло"


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
    r = subprocess.run(["sh", "-c", prog], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "LINK_KEEPALIVE": env["LINK_KEEPALIVE"]})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "25", f"скрипт линка превратил {env['LINK_KEEPALIVE']!r} в {r.stdout!r}"
