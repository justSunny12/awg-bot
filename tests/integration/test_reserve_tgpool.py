"""Пул адресов Telegram на резерве (domain/services/reserve/tgpool.py):
анонсы AS Telegram раз в tg_pool_hours с джиттером и только при паре,
проверка ядра, снятие неанонсируемого из выученного, Т-РУ25 один раз на
расхождение; обучение по SYN на порт вебхука (набор tg_seen4) — только в
serving, только префиксом анонса, не шире /12, с потолком. Файл пула —
тот, что читает файервол хоста (nftguard.tg_pool).

Источник анонсов и счётчики nft подменены; сеть и хост не трогаются.
Адреса стучавшихся и «анонсы» чужих AS — документационные сети; ядро —
константы кода.

Цена ошибки: пул шире анонсов AS — DNAT порта вебхука в бота основы (с
токеном и данными клиентов основы) открыт чужим сетям; ядро пропало из пула
— Telegram не доставляет вебхуки, бот основы на резерве молчит; источник
недоступен и пул обнулён — то же; Т-РУ25 каждый такт — шум у админа
резерва; периодический запрос без джиттера — маячок для наблюдателя.
"""
from __future__ import annotations

import json

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import tgpool
from awgbot.infra import nftguard
from awgbot.util import timeutil
from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.integration
rsv = rc.rsv

CORE = list(nftguard.DEFAULT_TG_POOL)
AS_A, AS_B = 62041, 59930
HOUR = 3600.0


class _Source:
    """Источник анонсов: AS → список префиксов или строка ошибки."""

    def __init__(self, monkeypatch):
        self.by_asn: dict[int, list[str] | str] = {}
        self.calls: list[int] = []
        monkeypatch.setattr(tgpool, "fetch_announced", self.fetch)

    def fetch(self, asn, url_tpl):
        self.calls.append(int(asn))
        got = self.by_asn.get(int(asn), [])
        if isinstance(got, str):
            return [], got
        return list(got), ""

    def down(self):
        for asn in (AS_A, AS_B):
            self.by_asn[asn] = "URLError: нет сети"


@pytest.fixture()
def pool(services, rsv, monkeypatch):
    """Резерв в паре; AS Telegram — две (AS_A анонсирует ядро, AS_B — две
    документационные сети и широкий /8)."""
    rc.pair_row(services)
    settings.set_value("app.reserve.tg_pool_asns", [AS_A, AS_B])
    src = _Source(monkeypatch)
    src.by_asn = {AS_A: list(CORE), AS_B: ["203.0.113.0/24", "198.51.100.0/24", "203.0.112.0/20", "198.0.0.0/8"]}
    services.src = src
    return services


def _due_tick(s, at: float):
    """Такт анонсов, который заведомо «пора»: срок первого обновления после
    старта — ≤ 84 с без кэша анонсов и ≤ 840 с с ним."""
    s.reserve_tg_pool_due(at)                    # заводит срок первого обновления
    return s.reserve_tg_pool_tick(at + 900)


def _file() -> dict:
    return json.loads(tgpool.pool_path().read_text(encoding="utf-8"))


def _put(**data) -> None:
    tgpool._write(data)


def _days_ago(days: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -days * 86400))


# ── когда ходить за анонсами ────────────────────────────────────────────────

def test_without_a_pair_the_announces_are_never_fetched(services, monkeypatch):
    src = _Source(monkeypatch)
    for t in (0, 100, 10 * HOUR, 100 * HOUR):
        assert services.reserve_tg_pool_tick(float(t)) == []
    assert src.calls == [], "без пары резерв ходил в RIPEstat — запрос ни к чему, и он заметен"


def test_the_first_fetch_comes_soon_and_then_once_per_period_with_jitter(pool):
    """Первый — в пределах 36–84 с после старта (кэша нет); дальше — раз в
    tg_pool_hours ± 40 %, и интервалы не одинаковые."""
    s = pool
    settings.set_value("app.reserve.tg_pool_hours", 1)
    assert s.reserve_tg_pool_tick(0.0) == [] and s.src.calls == [], "первый запрос в момент старта"
    t, fetched_at = 0.0, []
    while len(fetched_at) < 8 and t < 20 * HOUR:
        t += 30.0
        before = len(s.src.calls)
        s.reserve_tg_pool_tick(t)
        if len(s.src.calls) > before:
            fetched_at.append(t)
    assert fetched_at and 30 <= fetched_at[0] <= 90, f"первое обновление не «скоро»: {fetched_at[:1]}"
    gaps = [b - a for a, b in zip(fetched_at, fetched_at[1:])]
    assert len(gaps) >= 6, fetched_at
    assert all(0.6 * HOUR - 30 <= g <= 1.4 * HOUR + 30 for g in gaps), f"интервал вне ±40 %: {gaps}"
    assert len({round(g / 60) for g in gaps}) > 1, f"интервалы одинаковые — строгая периодичность: {gaps}"


def test_each_announce_tick_asks_every_listed_asn_once(pool):
    _due_tick(pool, 0.0)
    assert sorted(pool.src.calls) == sorted([AS_A, AS_B])


# ── ядро и выученное ────────────────────────────────────────────────────────

def test_a_good_tick_stores_announces_and_says_nothing(pool):
    assert _due_tick(pool, 0.0) == [], "ядро на месте — сообщать нечего"
    data = _file()
    assert set(data["announced"]) == {str(AS_A), str(AS_B)} and data["fetched_at"] and data["error"] == ""
    assert nftguard.tg_pool() == CORE, "без выученного пул файервола — ровно ядро"


def test_learned_prefixes_that_are_no_longer_announced_are_dropped(pool):
    _put(learned=[{"prefix": "203.0.113.0/24", "asn": str(AS_B)}, {"prefix": "198.51.100.0/24", "asn": str(AS_B)}],
         pool=["203.0.113.0/24", "198.51.100.0/24"])
    pool.src.by_asn[AS_B] = ["203.0.113.0/24"]                  # 198.51.100.0/24 больше не анонсируется
    _due_tick(pool, 0.0)
    data = _file()
    assert [x["prefix"] for x in data["learned"]] == ["203.0.113.0/24"]
    assert "198.51.100.0/24" not in nftguard.tg_pool(), "в пуле вебхука осталась сеть, которой нет в анонсах"
    assert nftguard.tg_pool() == CORE + ["203.0.113.0/24"]


def test_the_core_stays_in_the_pool_even_when_nothing_announces_it(pool):
    pool.src.by_asn[AS_A] = ["149.154.160.0/21"]               # пол-ядра пропало из анонсов
    _due_tick(pool, 0.0)
    assert nftguard.tg_pool()[:2] == CORE, "ядро — всегда, даже без анонса"


def test_a_lost_core_prefix_is_told_once_per_discrepancy(pool):
    s = pool
    settings.set_value("app.reserve.tg_pool_hours", 1)
    s.src.by_asn[AS_A] = ["149.154.160.0/20"]                  # 91.108.4.0/22 больше не анонсирует никто
    notes = _due_tick(s, 0.0)
    assert len(notes) == 1 and notes[0].tg_id == config.ADMIN_ID and "91.108.4.0/22" in notes[0].text, notes
    assert not notes[0].text.endswith("."), "уведомления без точки в конце"
    # следующие такты с тем же расхождением — молчат
    later = s.reserve_tg_pool_tick(10 * HOUR) + s.reserve_tg_pool_tick(20 * HOUR)
    assert later == [] and len(s.src.calls) >= 6, f"Т-РУ25 повторился: {later}"
    # ядро снова на месте — тишина; пропало снова — снова одно
    s.src.by_asn[AS_A] = list(CORE)
    assert s.reserve_tg_pool_tick(30 * HOUR) == []
    s.src.by_asn[AS_A] = ["149.154.160.0/20"]
    assert len(s.reserve_tg_pool_tick(40 * HOUR)) == 1, "новое расхождение после возврата не сказано"


# ── источник недоступен ─────────────────────────────────────────────────────

def test_an_unreachable_source_keeps_the_pool_and_the_cached_announces(pool):
    _due_tick(pool, 0.0)
    _put(**{**_file(), "learned": [{"prefix": "203.0.113.0/24", "asn": str(AS_B)}], "pool": ["203.0.113.0/24"]})
    before = _file()
    pool.src.down()
    assert pool.reserve_tg_pool_tick(100 * HOUR) == [], "анонсы свежие — сообщать рано"
    after = _file()
    assert after["announced"] == before["announced"] and after["learned"] == before["learned"], \
        "сбой источника стёр кэш анонсов или выученное"
    assert after["fetched_at"] == before["fetched_at"] and "нет сети" in after["error"]
    assert nftguard.tg_pool() == CORE + ["203.0.113.0/24"], "пул файервола сменился из-за сбоя источника"


def test_one_failing_asn_does_not_drop_the_prefixes_it_announced_before(pool):
    """Ответила одна AS из двух: выученное от неответившей пропасть не должно."""
    _due_tick(pool, 0.0)
    _put(**{**_file(), "learned": [{"prefix": "203.0.113.0/24", "asn": str(AS_B)}], "pool": ["203.0.113.0/24"]})
    pool.src.by_asn[AS_B] = "URLError: таймаут"
    pool.reserve_tg_pool_tick(100 * HOUR)
    assert "203.0.113.0/24" in nftguard.tg_pool(), \
        "AS не ответила — её выученная сеть снята, будто перестала анонсироваться"


def test_stale_announces_are_told_once_and_the_recovery_once(pool):
    s = pool
    settings.set_value("app.reserve.tg_pool_hours", 1)
    _put(announced={str(AS_A): list(CORE)}, fetched_at=_days_ago(4), learned=[], pool=[])
    s.src.down()
    notes = _due_tick(s, 0.0)
    assert len(notes) == 1 and "не обновлялись" in notes[0].text and notes[0].tg_id == config.ADMIN_ID, notes
    assert s.reserve_tg_pool_tick(10 * HOUR) == [] and s.reserve_tg_pool_tick(20 * HOUR) == [], "Т-РУ25 повторился"
    s.src.by_asn = {AS_A: list(CORE), AS_B: []}
    back = s.reserve_tg_pool_tick(30 * HOUR)
    assert len(back) == 1 and "снова обновляются" in back[0].text, back
    assert s.reserve_tg_pool_tick(40 * HOUR) == [], "«снова обновляются» повторилось"


def test_stale_is_not_told_before_stale_days(pool):
    _put(announced={str(AS_A): list(CORE)}, fetched_at=_days_ago(2), learned=[], pool=[])
    pool.src.down()
    assert _due_tick(pool, 0.0) == [], "Т-РУ25 раньше tg_pool_stale_days"


def test_a_source_down_from_the_very_first_fetch_is_not_stale_at_once(pool):
    """Свежая пара, RIPEstat не ответил на первый же запрос: анонсов ещё не
    было ни разу — «не обновлялись с …» через минуту после пары — ложная
    тревога; порог — tg_pool_stale_days, как у прежнего кэша."""
    pool.src.down()
    notes = _due_tick(pool, 0.0)
    assert notes == [], f"Т-РУ25 на первом же сбое источника: {[n.text for n in notes]}"


def test_the_role_screen_view_is_not_stale_before_the_first_fetch(pool):
    """Экран роли сразу после пары: анонсы ещё не запрашивались — строки
    «анонсы давно не обновлялись» быть не должно."""
    assert pool.reserve_tg_pool_view()["stale"] is False, "«давно не обновлялись» до первого запроса"


def test_the_view_counts_the_learned_and_marks_old_announces(pool):
    _put(announced={str(AS_A): list(CORE)}, fetched_at=_days_ago(4), learned=[{"prefix": "203.0.113.0/24"}],
         pool=["203.0.113.0/24"])
    v = pool.reserve_tg_pool_view()
    assert v["learned"] == 1 and v["stale"] is True
    _put(announced={str(AS_A): list(CORE)}, fetched_at=timeutil.now_iso(), learned=[], pool=[])
    assert pool.reserve_tg_pool_view()["stale"] is False


# ── обучение по SYN ─────────────────────────────────────────────────────────

class _Seen:
    """Набор tg_seen4: адрес → число SYN за 10 мин (как nft -j list set)."""

    def __init__(self, monkeypatch):
        self.data: dict[str, int] = {}
        self.calls = 0
        self.fail = False
        # имя пакета nftguard, которым его зовёт обучение; raising=False —
        # см. test_the_package_exposes_the_counters_reader ниже
        monkeypatch.setattr(nftguard, "live_set_counters", self.read, raising=False)

    def read(self, name):
        self.calls += 1
        assert name == nftguard.SET_TG_SEEN
        if self.fail:
            raise nftguard.GuardError("nft: нет таблицы")
        return dict(self.data)


@pytest.fixture()
def learn(pool, monkeypatch):
    _due_tick(pool, 0.0)                         # анонсы в кэше
    pool.reserve_set_in("serving")
    pool.seen = _Seen(monkeypatch)

    def announce(by_asn):                        # кэш анонсов — как после такта с таким ответом
        _put(**{**_file(), "announced": by_asn})
    pool._tg_announce = announce
    return pool


def test_learning_runs_only_while_serving(pool, monkeypatch):
    _due_tick(pool, 0.0)
    seen = _Seen(monkeypatch)
    seen.data = {"203.0.113.5": 50}
    for state in ("idle", "taking"):
        pool.reserve_set_in(state)
        assert pool.reserve_tg_learn_tick() == []
    assert seen.calls == 0, "вне serving набор читался — порта вебхука в зону ещё нет"
    assert nftguard.tg_pool() == CORE


def test_an_address_of_a_telegram_announce_is_learned_by_its_narrowest_prefix(learn):
    learn.seen.data = {"203.0.113.5": 3}
    assert learn.reserve_tg_learn_tick() == [("fw",)], "выучено — файервол перерисовывается"
    assert nftguard.tg_pool() == CORE + ["203.0.113.0/24"], "выучен не самый узкий анонс (/20 или /8 шире)"
    rec = _file()["learned"][0]
    assert (rec["prefix"], rec["asn"], rec["addr"]) == ("203.0.113.0/24", str(AS_B), "203.0.113.5") and rec["at"]


def test_fewer_syn_than_the_threshold_teach_nothing(learn):
    learn.seen.data = {"203.0.113.5": 2}
    assert learn.reserve_tg_learn_tick() == [] and nftguard.tg_pool() == CORE


def test_an_address_outside_every_announce_is_not_learned(learn):
    learn._tg_announce({str(AS_A): list(CORE), str(AS_B): ["203.0.113.0/24"]})
    learn.seen.data = {"198.51.100.10": 100}                    # чужая сеть — сканер
    assert learn.reserve_tg_learn_tick() == []
    assert nftguard.tg_pool() == CORE, "в пул вебхука попала сеть вне анонсов AS Telegram"


def test_an_announce_wider_than_12_is_not_learned(learn):
    learn._tg_announce({str(AS_A): list(CORE), str(AS_B): ["198.0.0.0/8"]})   # держит его только /8
    learn.seen.data = {"198.51.100.10": 100}
    assert learn.reserve_tg_learn_tick() == []
    assert "198.0.0.0/8" not in nftguard.tg_pool(), "выучен анонс шире /12"


def test_an_address_already_in_the_pool_is_not_learned_again(learn):
    learn.seen.data = {"203.0.113.5": 5}
    learn.reserve_tg_learn_tick()
    learn.seen.data = {"203.0.113.5": 9, "203.0.113.77": 4}       # тот же /24 — гонка с перерисовкой
    assert learn.reserve_tg_learn_tick() == [], "повтор такта — снова «выучено» и лишняя перерисовка"
    assert [x["prefix"] for x in _file()["learned"]] == ["203.0.113.0/24"]


def test_an_address_of_the_core_is_not_learned(learn, monkeypatch):
    """Адрес из ядра уже в пуле — учить нечего (ядро подменено документационной сетью)."""
    monkeypatch.setattr(nftguard, "DEFAULT_TG_POOL", ("198.51.100.0/24",))
    learn.seen.data = {"198.51.100.5": 9}
    assert learn.reserve_tg_learn_tick() == [] and not _file().get("learned"), "адрес ядра выучен заново"


def test_the_learned_count_has_a_ceiling(learn):
    settings.set_value("app.reserve.tg_pool_max", 1)
    learn.seen.data = {"203.0.113.5": 5, "198.51.100.5": 5}
    assert learn.reserve_tg_learn_tick() == [("fw",)]
    assert len(_file()["learned"]) == 1, "потолок выученных не держит"
    learn.seen.data = {"198.51.100.5": 50}
    assert learn.reserve_tg_learn_tick() == [] and len(_file()["learned"]) == 1


def test_a_failing_set_read_learns_nothing_and_does_not_raise(learn):
    learn.seen.fail = True
    assert learn.reserve_tg_learn_tick() == []
    learn.seen.fail = False
    learn.seen.data = {}
    assert learn.reserve_tg_learn_tick() == []


def test_learning_without_cached_announces_learns_nothing(pool, monkeypatch):
    """Анонсов ещё не было (источник не отвечал) — учить не по чему."""
    pool.reserve_set_in("serving")
    seen = _Seen(monkeypatch)
    seen.data = {"203.0.113.5": 50}
    assert pool.reserve_tg_learn_tick() == [] and nftguard.tg_pool() == CORE


def test_a_learned_prefix_goes_away_when_its_announce_does(learn):
    learn.seen.data = {"203.0.113.5": 5}
    learn.reserve_tg_learn_tick()
    learn.src.by_asn[AS_B] = ["198.51.100.0/24"]
    learn.reserve_tg_pool_tick(100 * HOUR)
    assert "203.0.113.0/24" not in nftguard.tg_pool(), "выученное без анонса осталось в пуле"


def test_learning_reads_the_real_counters_through_nft(pool, monkeypatch):
    """Сквозь настоящий путь: обучение читает набор через пакет nftguard
    (`nft -j list set … tg_seen4`) — без подмены читателя."""
    import subprocess
    _due_tick(pool, 0.0)
    pool.reserve_set_in("serving")
    doc = {"nftables": [{"set": {"name": "tg_seen4", "elem": [
        {"elem": {"val": "203.0.113.5", "counter": {"packets": 7, "bytes": 420}}}]}}]}
    monkeypatch.setattr(nftguard, "_nft", lambda a, **k: subprocess.CompletedProcess(a, 0, json.dumps(doc).encode(), b""))
    assert pool.reserve_tg_learn_tick() == [("fw",)], "обучение по SYN не дошло до счётчиков набора"
    assert "203.0.113.0/24" in nftguard.tg_pool()


def test_the_package_exposes_the_counters_reader():
    """Обучение зовёт nftguard.live_set_counters — имя должно быть у пакета,
    иначе такт молча пишет «набор не прочитан» и пул не учится никогда."""
    assert callable(getattr(nftguard, "live_set_counters", None)), \
        "nftguard.live_set_counters нет в пакете (в __all__ есть, импорта из apply нет)"
