"""Пиры-зонды кворума на интерфейсе пользователей: сверка пиров и опрос трафика.

Выпуск с кворумом кладёт на `awg0` служебные пиры-зонды голосующих
(169.254.88.0/24) и второй `Address` из того же блока. Откат на выпуск без
кворума оставляет их в конфиге и в ядре. Цена ошибки: сверка, не знающая
блока, заводит каждый зонд в карантин служебного профиля и шлёт
«🚨 Чужой пир в конфиге сервера» со звуком после каждого отката; опрос,
не знающий блока, показывает «📶 Онлайн» больше на число голосующих. И
наоборот — правило, задевшее настоящих пиров, прячет чужой доступ.

Реальная временная БД; конфиг и дамп — текстом в том виде, как их пишет и
отдаёт сервер, через тот же разбор, что в работе.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.infra import awg
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

# ключи зондов — заглушки: сверке и опросу формат ключа не важен
PROBES = [("DUMMY-probe-4", "169.254.88.4"), ("DUMMY-probe-5", "169.254.88.5"),
          ("DUMMY-probe-6", "169.254.88.6")]
PROBE_KEYS = {pub for pub, _ in PROBES}


def _conf_42(users, *, extra=(), comma=False):
    """awg0.conf как его пишет выпуск с кворумом: пользователи, второй адрес
    интерфейса из блока зондов и пиры-зонды .4 .5 .6 (с PSK и keepalive, как
    служебные пиры)."""
    addr = ("Address = 10.8.1.1/24, 169.254.88.1/26" if comma
            else "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26")
    header = f"[Interface]\nPrivateKey = DUMMY\n{addr}\nListenPort = 43125\nJc = 4"
    blocks = [f"[Peer]\nPublicKey = {pub}\nPresharedKey = DUMMY\nAllowedIPs = {ip}/32"
              for pub, ip in list(users) + list(extra)]
    blocks += [f"[Peer]\n# зонд кворума\nPublicKey = {pub}\nPresharedKey = DUMMY\n"
               f"AllowedIPs = {ip}/32\nPersistentKeepalive = 25" for pub, ip in PROBES]
    return header + "\n\n" + "\n\n".join(blocks) + "\n"


def _set_conf(monkeypatch, text):
    monkeypatch.setattr(awg, "read_file", lambda path: text, raising=False)


def _users(services, make_active_client, n=2, tg_id=7100):
    client = make_active_client(tg_id=tg_id, device_limit=5)
    ids = [services.add_device(client.id, f"d{i}").device_id for i in range(n)]
    return client, [services.db.get_device(i) for i in ids]


# ── сверка пиров ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("comma", [False, True], ids=["two_address_lines", "address_with_comma"])
def test_probe_peers_are_not_quarantined_and_raise_no_alarm(services, fake_awg, make_active_client,
                                                            monkeypatch, comma):
    services.ensure_admin_client()
    _client, devs = _users(services, make_active_client)
    _set_conf(monkeypatch, _conf_42([(d.public_key, d.address) for d in devs], comma=comma))
    before = {d.public_key for d in services.db.list_all_devices()}

    for sweep in range(3):                                 # и повтор ничего не меняет
        notes = services.reconcile_peers()
        assert notes == [], f"сверка {sweep + 1}: уведомления на зонды: {[n.text for n in notes]}"

    after = {d.public_key for d in services.db.list_all_devices()}
    assert not after & PROBE_KEYS, "пир-зонд заведён в базу (карантин)"
    assert after == before, f"состав устройств изменился: +{after - before} −{before - after}"
    sid = services.db.get_service_client_id()
    assert services.db.count_devices(sid) == 0, "в служебном профиле появились карантинные записи"
    assert not fake_awg.blocked & {ip for _, ip in PROBES}, "на адрес зонда наложен DROP"


def test_device_missing_count_does_not_grow_next_to_probes(services, fake_awg, make_active_client,
                                                           monkeypatch):
    """Зонды не должны сбить сопоставление: устройства, которые в конфиге
    есть, не копят пропажи и через порог не удаляются молча."""
    _client, devs = _users(services, make_active_client, n=3, tg_id=7101)
    _set_conf(monkeypatch, _conf_42([(d.public_key, d.address) for d in devs]))
    for _ in range(config.MISSING_SWEEPS_THRESHOLD + 1):
        services.reconcile_peers()
    for d in devs:
        fresh = services.db.get_device(d.id)
        assert fresh is not None, f"устройство {d.name} удалено сверкой"
        assert fresh.missing_count == 0, f"устройство {d.name}: missing_count={fresh.missing_count}"


def test_a_device_gone_from_the_conf_is_still_counted_missing_next_to_probes(
        services, fake_awg, make_active_client, monkeypatch):
    """Контроль обратной стороны: зонды в конфиге не маскируют пропажу
    настоящего устройства."""
    _client, devs = _users(services, make_active_client, n=2, tg_id=7102)
    kept, gone = devs
    _set_conf(monkeypatch, _conf_42([(kept.public_key, kept.address)]))
    services.reconcile_peers()
    assert services.db.get_device(gone.id).missing_count == 1, "пропажа устройства рядом с зондами не замечена"
    assert services.db.get_device(kept.id).missing_count == 0


def test_a_stranger_outside_the_block_still_raises_the_alarm_once(services, fake_awg, make_active_client,
                                                                  monkeypatch):
    """Рельс не должен стать лазейкой: чужой пир вне блока зондов — карантин и
    тревога со звуком, ровно одна на пир; зонды рядом в тревогу не попадают."""
    services.ensure_admin_client()
    _client, devs = _users(services, make_active_client, n=1, tg_id=7103)
    stranger = ("DUMMY-stranger", "10.8.1.77")
    _set_conf(monkeypatch, _conf_42([(d.public_key, d.address) for d in devs], extra=[stranger]))

    notes = [n for n in services.reconcile_peers() if n.tg_id == config.ADMIN_ID]
    assert len(notes) == 1, f"ожидалась одна тревога, пришло: {[n.text for n in notes]}"
    assert "10.8.1.77" in notes[0].text and notes[0].force_sound
    assert "169.254.88" not in notes[0].text
    q = next(d for d in services.db.list_all_devices() if d.public_key == stranger[0])
    assert q.client_id == services.db.get_service_client_id(), "чужой пир не в карантине"

    assert services.reconcile_peers() == [], "тревога о том же пире повторилась на второй сверке"


# ── опрос трафика ────────────────────────────────────────────────────────────

def _dump(rows):
    """Текст `awg show awg0 dump`: строка интерфейса и пиры в полном формате."""
    lines = ["DUMMY-priv\tDUMMY-pub\t43125\toff"]
    for pub, ip, hs, rx, tx in rows:
        lines.append(f"{pub}\t(none)\t198.51.100.9:5000\t{ip}/32\t{hs}\t{rx}\t{tx}\t25")
    return "\n".join(lines) + "\n"


def _set_dump(monkeypatch, rows):
    text = _dump(rows)
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: awg.parse_dump(text), raising=False)


def test_online_count_does_not_include_probes_with_a_fresh_handshake(services, fake_awg, make_active_client,
                                                                     monkeypatch):
    _client, devs = _users(services, make_active_client, n=2, tg_id=7110)
    on, off = devs
    now = int(timeutil.now().timestamp())
    _set_dump(monkeypatch, [(on.public_key, on.address, now - 10, 100, 50),
                            (off.public_key, off.address, 0, 0, 0)]
              + [(pub, ip, now - 5, 999, 999) for pub, ip in PROBES])
    services.poll_traffic()
    assert services.db.get_state("online_count") == "1", \
        f"онлайн посчитал зонды: {services.db.get_state('online_count')}"


def test_only_probes_online_gives_zero(services, fake_awg, make_active_client, monkeypatch):
    """Ни одного устройства на связи, а зонды живы — в панели «0», а не число
    голосующих."""
    _users(services, make_active_client, n=1, tg_id=7111)
    services.db.set_state("online_count", "5")             # прежняя цифра должна смениться
    now = int(timeutil.now().timestamp())
    _set_dump(monkeypatch, [(pub, ip, now - 5, 1, 1) for pub, ip in PROBES])
    services.poll_traffic()
    assert services.db.get_state("online_count") == "0"


def test_device_counters_are_untouched_by_probes(services, fake_awg, make_active_client, monkeypatch):
    """Трафик и рукопожатия устройств считаются как без зондов; зонды не
    оставляют в базе ни устройств, ни опорных выборок."""
    _client, devs = _users(services, make_active_client, n=2, tg_id=7112)
    a, b = devs
    now = int(timeutil.now().timestamp())
    devices_before = {d.id for d in services.db.list_all_devices()}

    _set_dump(monkeypatch, [(a.public_key, a.address, now - 30, 100, 50),
                            (b.public_key, b.address, 0, 0, 0)]
              + [(pub, ip, now - 5, 10_000, 20_000) for pub, ip in PROBES])
    services.poll_traffic()
    _set_dump(monkeypatch, [(a.public_key, a.address, now - 20, 130, 70),
                            (b.public_key, b.address, 0, 0, 0)]
              + [(pub, ip, now - 3, 90_000, 80_000) for pub, ip in PROBES])
    services.poll_traffic()

    fa, fb = services.db.get_device(a.id), services.db.get_device(b.id)
    assert (fa.traffic_rx_month, fa.traffic_tx_month) == (30, 20), "трафик устройства сбит зондами"
    assert (fb.traffic_rx_month, fb.traffic_tx_month) == (0, 0)
    assert fa.last_handshake == now - 20 and not fb.last_handshake
    assert {d.id for d in services.db.list_all_devices()} == devices_before, "опрос завёл записи на зонды"
    assert set(services.db.get_samples_all()) <= devices_before


# ── пир блока с чем-то ещё — чужой ───────────────────────────────────────────

@pytest.mark.parametrize("allowed", [
    "AllowedIPs = 169.254.88.9/32, 10.8.1.77/32",
    "AllowedIPs = 169.254.88.9/32\nAllowedIPs = 10.8.1.77/32",
    "AllowedIPs = 169.254.88.9/32,0.0.0.0/0",
    "AllowedIPs = 169.254.88.9/32\nAllowedIPs = 0.0.0.0/0",
    "AllowedIPs = 169.254.88.9/0",                    # адрес блока, а маршрут — весь интернет
], ids=["comma_user", "lines_user", "comma_default", "lines_default", "block_ip_wide_prefix"])
def test_a_block_peer_that_also_routes_something_else_is_a_stranger_with_one_alarm(
        services, fake_awg, make_active_client, monkeypatch, allowed):
    """Адрес из блока зондов не должен быть пропуском: пир, которому кроме
    адреса блока выдан адрес пользователей или весь интернет, — чужой доступ.
    Сверка заводит его в карантин и шлёт тревогу со звуком — ровно одну;
    настоящие зонды рядом в тревогу не попадают."""
    services.ensure_admin_client()
    _client, devs = _users(services, make_active_client, n=1, tg_id=7104)
    conf = _conf_42([(d.public_key, d.address) for d in devs])
    conf += f"\n[Peer]\nPublicKey = DUMMY-mixed\nPresharedKey = DUMMY\n{allowed}\n"
    _set_conf(monkeypatch, conf)

    notes = [n for n in services.reconcile_peers() if n.tg_id == config.ADMIN_ID]
    assert len(notes) == 1, f"ожидалась одна тревога на пир «блок + ещё», пришло: {[n.text for n in notes]}"
    assert notes[0].force_sound
    q = [d for d in services.db.list_all_devices() if d.public_key == "DUMMY-mixed"]
    assert q and q[0].client_id == services.db.get_service_client_id(), "пир «блок + ещё» не в карантине"
    assert not {d.public_key for d in services.db.list_all_devices()} & PROBE_KEYS, "настоящий зонд в карантине"
    assert services.reconcile_peers() == [], "тревога о том же пире повторилась на второй сверке"


# ── подсеть пользователей в блоке: правило зондов выключено ─────────────────

def _users_in_block(services, make_active_client, tg_id, n=3):
    """Устройства с адресами 169.254.88.2… — как у сервера, чья подсеть
    пользователей пересекает блок зондов."""
    client = make_active_client(tg_id=tg_id, device_limit=5)
    ids = [services.db.create_device(client.id, f"b{i}", f"DUMMY-user-{tg_id}-{i}", "DUMMY", f"169.254.88.{i + 2}",
                                     private_key="DUMMY") for i in range(n)]
    return [services.db.get_device(i) for i in ids]


@pytest.mark.parametrize("address", [
    "Address = 169.254.88.1/24",
    "Address = 169.254.88.1/24 # пользователи",
    "Address = 169.254.0.1/16",
    "Address = 169.254.88.1/26\nAddress = 169.254.0.1/16",
], ids=["block_only", "block_only_commented", "wider_net", "block_line_and_wider_net"])
def test_devices_of_a_users_subnet_inside_the_block_never_go_missing(services, fake_awg, make_active_client,
                                                                     monkeypatch, address):
    """Подсеть пользователей 169.254.88.x — экзотика, но законная. Прими сверка
    её устройства за зонды — каждое копит пропажи и через порог сверок
    удаляется молча, вместе с доступом людей."""
    services.ensure_admin_client()
    devs = _users_in_block(services, make_active_client, tg_id=7120)
    header = f"[Interface]\nPrivateKey = DUMMY\n{address}\nListenPort = 43125\nJc = 4"
    peers = [f"[Peer]\nPublicKey = {d.public_key}\nPresharedKey = DUMMY\nAllowedIPs = {d.address}/32" for d in devs]
    _set_conf(monkeypatch, header + "\n\n" + "\n\n".join(peers) + "\n")

    for sweep in range(config.MISSING_SWEEPS_THRESHOLD + 1):
        notes = services.reconcile_peers()
        assert notes == [], f"сверка {sweep + 1}: {[n.text for n in notes]}"
    for d in devs:
        fresh = services.db.get_device(d.id)
        assert fresh is not None, f"устройство {d.address} удалено сверкой как «пропавшее»"
        assert fresh.missing_count == 0, f"устройство {d.address}: missing_count={fresh.missing_count}"


def test_online_counts_devices_of_a_users_subnet_inside_the_block(services, fake_awg, make_active_client,
                                                                  monkeypatch):
    """Подсеть пользователей в блоке — устройства 169.254.88.x на связи считаются
    в «📶 Онлайн», а не выпадают как зонды. Подсеть опрос берёт оттуда же, откуда
    сверка, — из строк Address конфига интерфейса (один источник), а не из
    `routing.client_subnet`: с разными источниками сверка считала бы пиры
    устройствами, а опрос — зондами."""
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "", raising=False)
    devs = _users_in_block(services, make_active_client, tg_id=7121)
    header = "[Interface]\nPrivateKey = DUMMY\nAddress = 169.254.88.1/24\nListenPort = 43125\n"
    _set_conf(monkeypatch, header + "".join(f"\n[Peer]\nPublicKey = {d.public_key}\nAllowedIPs = {d.address}/32\n"
                                         for d in devs))
    now = int(timeutil.now().timestamp())
    _set_dump(monkeypatch, [(d.public_key, d.address, now - 10, 1, 1) for d in devs])
    services.poll_traffic()
    assert services.db.get_state("online_count") == "3", \
        f"устройства подсети 169.254.88.0/24 не в онлайне: {services.db.get_state('online_count')}"


def test_the_setting_alone_does_not_switch_the_probe_rule_for_the_online_count(services, fake_awg,
                                                                                 make_active_client, monkeypatch):
    """`routing.client_subnet` в блоке, а Address интерфейса — обычная подсеть:
    источник один (Address), зонды остаются зондами."""
    monkeypatch.setattr(config, "ROUTING_CLIENT_SUBNET", "169.254.88.0/24")
    _users(services, make_active_client, n=1, tg_id=7123)
    _set_conf(monkeypatch, _conf_42([]))
    now = int(timeutil.now().timestamp())
    _set_dump(monkeypatch, [(pub, ip, now - 5, 1, 1) for pub, ip in PROBES])
    services.poll_traffic()
    assert services.db.get_state("online_count") == "0", services.db.get_state("online_count")


def test_an_unreadable_conf_keeps_the_probe_rule_on_for_the_online_count(services, fake_awg, make_active_client,
                                                                          monkeypatch):
    """Конфиг не прочитан — подсети не известны, правило действует: адрес блока
    без подсети пользователей в нём — не устройство."""
    _users(services, make_active_client, n=1, tg_id=7124)

    def boom(path):
        raise awg.AwgError("нет файла")
    monkeypatch.setattr(awg, "read_file", boom, raising=False)
    now = int(timeutil.now().timestamp())
    _set_dump(monkeypatch, [(pub, ip, now - 5, 1, 1) for pub, ip in PROBES])
    services.poll_traffic()
    assert services.db.get_state("online_count") == "0"


def test_a_skipped_probe_peer_is_logged_once_per_key(services, fake_awg, make_active_client, monkeypatch, caplog):
    """В 4.1.0 законных зондов нет: пир в блоке — ручная правка, и след ему
    нужен, но одной строкой на ключ, а не на каждую сверку."""
    import logging
    services.ensure_admin_client()
    _client, devs = _users(services, make_active_client, tg_id=7125)
    _set_conf(monkeypatch, _conf_42([(d.public_key, d.address) for d in devs]))
    caplog.set_level(logging.INFO)
    for _ in range(3):
        assert services.reconcile_peers() == []
    lines = [r.getMessage() for r in caplog.records if "в блоке зондов кворума — не устройство, пропущен" in r.getMessage()]
    assert len(lines) == len(PROBES), lines
    for pub, ip in PROBES:
        assert any(pub[:8] in ln and ip in ln for ln in lines), (pub, lines)
    assert not any(d.public_key[:8] in ln for d in devs for ln in lines), "устройство названо зондом"


def test_online_counts_a_block_peer_that_also_has_another_address(services, fake_awg, make_active_client,
                                                                  monkeypatch):
    """Пир «адрес блока + адрес пользователей» — не зонд: на связи он — в
    онлайне (как и любой чужой пир), настоящие зонды — нет."""
    _users(services, make_active_client, n=1, tg_id=7122)
    now = int(timeutil.now().timestamp())
    text = _dump([(pub, ip, now - 5, 1, 1) for pub, ip in PROBES])
    text += f"DUMMY-mixed\t(none)\t198.51.100.9:5000\t169.254.88.9/32,10.8.1.77/32\t{now - 5}\t1\t1\t25\n"
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: awg.parse_dump(text), raising=False)
    services.poll_traffic()
    assert services.db.get_state("online_count") == "1", \
        f"онлайн: {services.db.get_state('online_count')} (ожидался один пир «блок + ещё»)"
