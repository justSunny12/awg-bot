"""
Снимок пира линка: хендшейк и счётчики одним `awg show dump`.

По этим цифрам автомат решает, жив ли активный шлюз, и решает ЧАЩЕ, чем ходит
наружу: выросший rx — доказательство пути без единого зонда. Соврёт снимок —
бот либо переложит трафик на резерв под живым шлюзом, либо будет держать
мёртвый, пока люди сидят без интернета.
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.core import config
from awgbot.infra import routing

pytestmark = pytest.mark.unit

PUB = "GWPUB=="


class _Awg:
    """Подставной хост: что ответит `awg show <iface> dump` и о чём его спросили."""

    def __init__(self, stdout: bytes = b"", rc: int = 0, missing: bool = False):
        self.stdout, self.rc, self.missing = stdout, rc, missing
        self.calls: list = []

    def __call__(self, args, check=True, input_data=None, timeout=20):
        self.calls.append(list(args))
        if self.missing:
            raise routing.RoutingUnavailable(f"Команда не найдена на хосте: {args[0]}")
        return subprocess.CompletedProcess(args=args, returncode=self.rc,
                                           stdout=self.stdout, stderr=b"")


def _dump(*peers: tuple) -> bytes:
    """Строка интерфейса плюс пиры: pub psk endpoint allowed hs rx tx keepalive."""
    rows = ["\t".join(["IFACE_PRIV", "IFACE_PUB", "443", "off"])]
    for pub, hs, rx, tx in peers:
        rows.append("\t".join([pub, "PSK", "198.51.100.7:443", "0.0.0.0/0",
                               str(hs), str(rx), str(tx), "25-35"]))
    return ("\n".join(rows) + "\n").encode()


@pytest.fixture()
def awg(monkeypatch):
    host = _Awg()
    monkeypatch.setattr(routing, "_host", host)
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    return host


def test_snapshot_carries_the_handshake_age_and_both_counters(awg, monkeypatch):
    """Возраст и байты приходят из одной строки и об одном и том же такте:
    разъехавшись во времени (два разных вызова awg), они дали бы «свежий
    хендшейк при счётчиках прошлой минуты» — и вердикт о чужом такте."""
    monkeypatch.setattr(routing.time, "time", lambda: 1_720_000_042)
    awg.stdout = _dump((PUB, 1_720_000_000, 7000, 9000))
    st = routing.link_peer_state("awglink")
    assert st == {"age": 42, "rx": 7000, "tx": 9000}
    assert routing.link_handshake_age("awglink") == 42, "обёртка обязана отдавать тот же возраст"


def test_counters_of_all_peers_are_summed_and_the_freshest_handshake_wins(awg, monkeypatch):
    """У линка обычно один пир, но конфиг на той стороне переписывают руками, и
    лишний пир не должен делить трафик пополам: считаем весь интерфейс, возраст
    берём по самому свежему — линк жив, пока жив хоть один пир."""
    monkeypatch.setattr(routing.time, "time", lambda: 1_720_000_100)
    awg.stdout = _dump((PUB, 1_720_000_000, 1000, 2000), ("OTHER==", 1_720_000_090, 40, 4))
    assert routing.link_peer_state("awglink") == {"age": 10, "rx": 1040, "tx": 2004}


def test_a_peer_that_never_shook_hands_is_still_a_snapshot(awg):
    """Интерфейс поднят, хендшейка не было: возраста нет, а счётчики есть.

    Вернуть тут None значило бы «интерфейса нет», и активный слот ушёл бы на
    зонд вместо честного «молчит» — а линк, который ни разу не встретился с
    шлюзом, надо считать мёртвым, а не непроверенным.
    """
    awg.stdout = _dump((PUB, 0, 0, 0))
    assert routing.link_peer_state("awglink") == {"age": None, "rx": 0, "tx": 0}
    assert routing.link_handshake_age("awglink") is None


def test_clock_jumping_backwards_does_not_make_the_handshake_future_fresh(awg, monkeypatch):
    """Часы хоста подвинулись назад (ntp после долгого простоя) — возраст не
    должен становиться отрицательным: сравнения «свежее порога» в автомате
    тогда проходят вечно."""
    monkeypatch.setattr(routing.time, "time", lambda: 1_719_999_000)
    awg.stdout = _dump((PUB, 1_720_000_000, 1, 1))
    assert routing.link_peer_state("awglink")["age"] == 0


def test_no_peers_at_all_is_no_snapshot(awg):
    """Интерфейс есть, пиров нет — линк не настроен; цифр, по которым можно
    судить, не существует."""
    awg.stdout = _dump()
    assert routing.link_peer_state("awglink") is None
    assert routing.link_handshake_age("awglink") is None


def test_a_failing_awg_is_no_snapshot(awg):
    """Интерфейса нет (линк опущен) — awg отвечает ненулевым кодом. Разобрать
    его пустой вывод как «нулевые счётчики» значило бы объявить мёртвый линк
    просто бездействующим."""
    awg.rc, awg.stdout = 1, b""
    assert routing.link_peer_state("awglink") is None
    assert routing.link_handshake_age("awglink") is None


def test_a_missing_awg_binary_is_reported_not_swallowed(awg):
    """Нет самой утилиты — это отказ ХОСТА, а не молчание шлюза; проглотив его
    как «нет пира», бот переложил бы трафик на резерв, до которого тем же awg
    дотянуться тоже нечем."""
    awg.missing = True
    with pytest.raises(routing.RoutingUnavailable):
        routing.link_peer_state("awglink")


def test_the_snapshot_asks_about_the_interface_it_was_given(awg):
    """Слотов линков несколько (awglink, awglink2). Спросив не тот, автомат
    вынес бы вердикт о чужом шлюзе; пустой аргумент — активный линк из конфига."""
    awg.stdout = _dump((PUB, 0, 0, 0))
    routing.link_peer_state("awglink2")
    routing.link_peer_state()
    assert [c[:3] for c in awg.calls] == [["awg", "show", "awglink2"],
                                          ["awg", "show", "awglink"]]
