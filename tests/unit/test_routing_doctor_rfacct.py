"""Слой «учёт РФ-трафика» в докторе маршрутизации.

Доктор только читает таблицу awg_bot_acct: два чтения с паузой отвечают на
вопрос «считает ли учёт прямо сейчас». Цена ошибки: зелёная строка при
неполной цепочке — человек верит цифрам главной, которые не растут; красная
при отсутствии таблицы на свежем сервере — чинят то, что создаст первый опрос;
неверные мегабайты — сверка с потреблением клиента не сходится.

Ядро подменяется на уровне rfacct.read: разбор JSON nft покрыт в test_rfacct.
"""
from __future__ import annotations

import pytest

from awgbot.infra import rfacct
from awgbot.runtime import routing_doctor as doc

pytestmark = pytest.mark.unit

TAIL = "с перезагрузки сервера или пересоздания таблицы"


class _Kernel:
    """Таблица в ядре глазами доктора: очередь ответов rfacct.read по порядку
    (AcctState, None — таблицы нет, исключение — nft не смог) и учёт пауз."""

    def __init__(self, monkeypatch, *answers):
        self.answers = list(answers)
        self.reads = 0
        self.sleeps: list[float] = []
        monkeypatch.setattr(rfacct, "read", self._read)
        import time
        monkeypatch.setattr(time, "sleep", self.sleeps.append)

    def _read(self):
        self.reads += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _acct(up: int = 0, dn: int = 0, devices: int = 2, rules: int = 4) -> rfacct.AcctState:
    counters = {rfacct.COUNTER_UP: up, rfacct.COUNTER_DN: dn}
    for i in range(1, devices + 1):
        u, d = rfacct.counter_names(i)
        counters[u], counters[d] = 100, 100
    return rfacct.AcctState(counters=counters, rules=rules)


def test_no_table_is_a_warning_that_points_to_the_first_poll(monkeypatch):
    """Свежий сервер: таблицу создаёт первый опрос трафика — это ⚠, не СБОЙ,
    и второго чтения с паузой нет: мерить нечего."""
    k = _Kernel(monkeypatch, None)
    rows = doc._probe_rf_acct(pause=3.0)
    assert len(rows) == 1, rows
    mark, title, detail = rows[0]
    assert mark == doc._WARN and title == "Учёт РФ-трафика: таблицы awg_bot_acct нет", rows
    assert "первый опрос трафика" in detail and "мин" in detail, detail
    assert k.reads == 1 and k.sleeps == [], "таблицы нет — а доктор ждал и читал снова"


def test_an_unreadable_table_is_a_warning_with_nft_reason(monkeypatch):
    """nft отказал — причина видна дословно, доктор не падает и не ждёт."""
    k = _Kernel(monkeypatch, rfacct.AcctError("nft не найден — поставь пакет nftables"))
    rows = doc._probe_rf_acct(pause=3.0)
    assert rows == [(doc._WARN, "Учёт РФ-трафика: не читается", "nft не найден — поставь пакет nftables")], rows
    assert k.sleeps == [], "ошибка чтения — а доктор ещё и ждал паузу"


def test_a_full_chain_is_green_with_megabytes_and_device_count(monkeypatch):
    """4 правила — ок; байты — в МБ с двумя знаками, округление вверх: 12 345 байт
    — не «0.00 МБ», иначе живой учёт выглядит нулевым."""
    _Kernel(monkeypatch, _acct(up=12345, dn=2_000_000, devices=3))
    rows = doc._probe_rf_acct(pause=0)
    assert rows == [(doc._OK, f"Учёт РФ-трафика: 3 устройства, ↑ 0.02 МБ, ↓ 2.00 МБ {TAIL}", "")], rows


@pytest.mark.parametrize("n,word", [(1, "1 устройство"), (2, "2 устройства"), (5, "5 устройств"),
                                    (11, "11 устройств"), (21, "21 устройство"), (0, "0 устройств")])
def test_device_count_agrees_with_the_number(monkeypatch, n, word):
    _Kernel(monkeypatch, _acct(devices=n))
    [(_, title, _)] = doc._probe_rf_acct(pause=0)
    assert title.startswith(f"Учёт РФ-трафика: {word}, ↑ "), title


def test_a_partial_chain_is_a_warning_not_a_failure(monkeypatch):
    """2 правила из 4 — трафик идёт, но итог считается не весь: ⚠ с советом, что
    опрос перепишет, а не СБОЙ (тракт исправен)."""
    _Kernel(monkeypatch, _acct(up=1_000_000, dn=0, devices=1, rules=2))
    [(mark, title, detail)] = doc._probe_rf_acct(pause=0)
    assert mark == doc._WARN, "неполная цепочка показана зелёной"
    assert title == f"Учёт РФ-трафика: 1 устройство, ↑ 1.00 МБ, ↓ 0.00 МБ {TAIL}", title
    assert detail == "Правил в цепочке 2, ждём 4 — ближайший опрос перепишет.", detail


def test_growth_between_two_reads_is_reported(monkeypatch):
    """Два чтения через паузу: итог (↑ + ↓) вырос — доктор называет прирост,
    это и есть ответ «учёт идёт прямо сейчас». Цифры строки — по первому чтению."""
    k = _Kernel(monkeypatch, _acct(up=1_000_000, dn=0), _acct(up=1_000_000, dn=1_500_001))
    [(mark, title, _)] = doc._probe_rf_acct(pause=3.0)
    assert k.sleeps == [3.0] and k.reads == 2, (k.sleeps, k.reads)
    assert mark == doc._OK
    assert title == (f"Учёт РФ-трафика: 2 устройства, ↑ 1.00 МБ, ↓ 0.00 МБ {TAIL}; "
                     "за 3 с итог вырос на 1.51 МБ"), title


def test_no_growth_between_reads_is_said_plainly(monkeypatch):
    _Kernel(monkeypatch, _acct(up=5, dn=5), _acct(up=5, dn=5))
    [(_, title, _)] = doc._probe_rf_acct(pause=3.0)
    assert title.endswith(f"{TAIL}; за 3 с итог не изменился"), title


@pytest.mark.parametrize("second", [rfacct.AcctError("таймаут nft -j list"), None])
def test_a_failed_second_read_keeps_the_first_verdict(monkeypatch, second):
    """Второе чтение сорвалось (таймаут, таблицу пересоздали) — строка по
    первому чтению без хвоста о приросте: «не изменился» было бы неправдой."""
    _Kernel(monkeypatch, _acct(up=5, dn=5), second)
    [(mark, title, _)] = doc._probe_rf_acct(pause=3.0)
    assert mark == doc._OK and title.endswith(TAIL), title


def test_zero_pause_reads_once(monkeypatch):
    """pause=0 — одно чтение и никакого ожидания (для тестов и быстрых вызовов)."""
    k = _Kernel(monkeypatch, _acct())
    doc._probe_rf_acct(pause=0)
    assert k.reads == 1 and k.sleeps == []


@pytest.mark.parametrize("n,text", [
    (0, "0.00 МБ"), (1, "0.01 МБ"), (12345, "0.02 МБ"), (10_000, "0.01 МБ"),
    (1_000_000, "1.00 МБ"), (1_000_001, "1.01 МБ"), (570_000, "0.57 МБ"),
    (1_100_000, "1.10 МБ"), (2_300_000, "2.30 МБ"), (70_000, "0.07 МБ"),
    (999_999_999, "1 000.00 МБ"), (12_068_860_000, "12 068.86 МБ"), (12_068_860_001, "12 068.87 МБ"),
])
def test_megabytes_round_up_to_two_digits(n, text):
    """Округление вверх — но ровные значения остаются ровными: 1 100 000 байт —
    «1.10 МБ», а не «1.11 МБ» от погрешности плавающей точки; разряды целой
    части — пробелом, как в остальных числах бота."""
    assert doc._mb(n) == text, f"_mb({n}) = {doc._mb(n)!r}, ожидалось {text!r}"
