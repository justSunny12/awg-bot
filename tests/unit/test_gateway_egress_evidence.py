"""Выход наружу у агента шлюза: сначала улики обратного трафика, потом зонд.

Зонд наружу — коннект С АДРЕСА КВАРТИРЫ в две фиксированные цели. Тиком
монитора это четыре с лишним сотни одинаковых коннектов в сутки, то есть
ровно тот маячок, который проект сам называет опасной сигнатурой. Здесь
проверяется, что зонд стал последним доводом: пока растёт tx линка, наружу не
уходит ни одного пакета, а в простое такт зонда растягивается и джиттерится.
"""
from __future__ import annotations

import random

import pytest

from awgbot.domain import gateway as gw
from awgbot.domain.gateway import GatewayServices
from awgbot.infra.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    return d


class _Agent:
    """Агент шлюза со снятым хостом: счётчики линка, зонд наружу и часы — под
    рукой. Всё, что могло бы уйти на живую малину (nft, awg, Telegram), сюда не
    доходит: снимок собирается из подставных значений."""

    STEP = 1 << 20                      # «трафик пошёл»: заведомо больше порога улики

    def __init__(self, svc, monkeypatch, at: float = 10_000.0):
        self.svc = svc
        self.rx = self.tx = 0
        self.now = at
        self.probe_ms: float | None = 42.0
        self.probes: list[float | None] = []
        self.forbidden = False          # зонд запрещён — вызов обязан упасть
        monkeypatch.setattr(gw.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(svc, "link_status", lambda: (True, 5.0, self.rx, self.tx))
        monkeypatch.setattr(svc, "plumbing_checks", lambda: [])
        monkeypatch.setattr(svc, "tg_mark_missing", lambda info=None: [])
        monkeypatch.setattr(svc, "tg_mark_ensure", lambda missing=None: 0)
        monkeypatch.setattr(svc, "uplink_policy_heal", lambda: [])
        monkeypatch.setattr(svc, "versions", lambda: ("v", "s"))
        monkeypatch.setattr(svc, "kernel_coverage", lambda: ([], 1))
        monkeypatch.setattr(svc, "egress_probe", self._probe)

    def _probe(self) -> float | None:
        assert not self.forbidden, "зонд наружу пошёл там, где путь уже доказан"
        self.probes.append(self.probe_ms)
        return self.probe_ms

    def tick(self, seconds: float = 180.0, rx: int = 0, tx: int = 0):
        """Тик монитора: сдвинуть часы, дорастить счётчики линка, снять статус."""
        self.now += seconds
        self.rx += rx
        self.tx += tx
        self.status = self.svc.snapshot()
        return self.check()

    def check(self):
        """Проверка «выход наружу» из последнего снимка — то, что видит человек."""
        return [c for c in self.status.checks if c.name == "выход наружу"][0]


@pytest.fixture()
def agent(db, monkeypatch):
    a = _Agent(GatewayServices(db), monkeypatch)
    monkeypatch.setattr(random, "uniform", lambda a_, b_: 1.0)   # джиттер прочь — считаем такты
    return a


def _no_jitter_window(agent) -> float:
    """Такт зонда в простое по умолчанию: monitor_minutes × idle_multiplier."""
    return agent.svc._egress_idle_seconds()


# ── улики сильнее зонда ──────────────────────────────────────────────────────

def test_return_traffic_proves_the_path_and_costs_no_probe(agent):
    """Вырос tx линка — значит ответы из интернета дошли до шлюза и ушли
    клиентам: путь квартира → интернет → обратно доказан сессиями живых людей,
    и зонду тут нечего добавить. Стучаться наружу, когда трафик уже ходит, —
    платить сигнатурой за то, что и так известно."""
    agent.tick()                                  # первый тик снимает базу зондом
    assert agent.probes == [42.0], "базу без улик всё-таки снимает зонд"

    agent.forbidden = True                        # дальше любой зонд — провал теста
    c = agent.tick(tx=_Agent.STEP)
    assert c.ok is True and "по обратному трафику клиентов" in c.detail, (
        "улика обязана и доказывать выход, и объяснять человеку, чем доказан")
    assert agent.status.egress_src == "трафик" and agent.probes == [42.0]
    assert agent.svc.cached_status(60).egress_src == "трафик", (
        "снимок для панели потерял источник вердикта")


def test_return_traffic_outweighs_a_failed_probe_and_never_prints_none_ms(agent):
    """Стык, на котором легко нарисовать «None мс»: первый зонд не прошёл (мс
    нет вовсе), а следом пошёл обратный трафик. Вердикт обязан перевернуться в
    «выходит», а деталь — говорить про трафик, потому что миллисекунд у этого
    доказательства нет и быть не может."""
    agent.probe_ms = None
    c = agent.tick()
    assert c.ok is False and "не отвечает" in c.detail

    agent.forbidden = True
    c = agent.tick(tx=_Agent.STEP)
    assert c.ok is True, "улика не подняла вердикт, оставленный неудачным зондом"
    assert agent.status.egress_ms is None and agent.status.egress_src == "трафик"
    assert c.detail == "по обратному трафику клиентов", (
        f"деталь проверки должна говорить про трафик, а не про мс: {c.detail!r}")
    assert "None" not in c.detail


def test_demand_without_answers_is_probed_every_tick(agent):
    """Клиенты шлют в линк (rx растёт), обратно тихо — худший случай: у шлюза
    лёг канал квартиры, и люди прямо сейчас сидят без РФ-доступа. Экономить на
    зондах здесь нельзя: проверяем каждый тик, отказ виден в прежний срок."""
    agent.tick()                                  # база
    agent.probe_ms = None
    for _ in range(3):
        c = agent.tick(rx=_Agent.STEP)
    assert len(agent.probes) == 4, "спрос без ответа проверяется каждым тиком"
    assert c.ok is False and agent.status.egress_src == "проба"
    assert "РФ-доступ через шлюз не работает" in c.detail


# ── простой: редкий зонд и кэш вердикта ──────────────────────────────────────

def test_idle_link_is_probed_rarely_and_lives_by_the_cached_verdict(agent):
    """Через линк не ходит никто: ни спроса, ни улик. Зонд тут никого не
    спасает — ответа в простое всё равно некому ждать, — а коннект раз в три
    минуты с адреса квартиры в одну и ту же цель виден со стороны. Между
    зондами держим прошлый вердикт и не меняем его самовольно."""
    window = _no_jitter_window(agent)
    assert window == 720.0, "такт зонда в простое по умолчанию — 3 мин × 4"

    agent.tick()                                  # база: t0, зонд прошёл
    agent.probe_ms = None                         # следующий зонд, когда дойдёт дело, скажет «лежит»
    for _ in range(3):                            # +180, +360, +540 секунд
        c = agent.tick()
        assert agent.status.egress_src == "кэш" and c.ok is True, (
            "между зондами вердикт обязан держаться прошлым, а не переворачиваться")
        assert c.detail == "42 мс", "деталь несёт мс последнего замера"
    assert len(agent.probes) == 1, "первые девять минут простоя — ни одного пакета наружу"

    c = agent.tick()                              # +720: такт вышел
    assert len(agent.probes) == 2, "по истечении растянутого такта зонд всё-таки идёт"
    assert c.ok is False, "и его отказ виден в проверке"
    for _ in range(3):
        agent.tick()
    assert len(agent.probes) == 2, "свежий вердикт держится тем же кэшем"


def test_idle_probe_window_is_jittered_not_a_metronome(db, monkeypatch):
    """Ровно двенадцать минут между зондами — такой же метроном, только реже.
    Такт разбрасывается множителем, поэтому при коэффициенте 0,6 зонд повторно
    уходит заметно раньше фиксированных 720 секунд."""
    a = _Agent(GatewayServices(db), monkeypatch)
    monkeypatch.setattr(random, "uniform", lambda a_, b_: 0.6)   # 720 × 0,6 = 432 с
    a.tick()
    a.tick(seconds=360)
    assert len(a.probes) == 1, "джиттер не может укоротить такт вдвое"
    a.tick(seconds=100)                           # 460 с с прошлого зонда
    assert len(a.probes) == 2, "с укороченным множителем зонд идёт раньше 720 с"


def test_counters_going_backwards_are_a_link_restart_not_an_outage(agent):
    """awg-quick перезапустили — счётчики пошли с нуля. Разница «ушла вниз» не
    улика ни в какую сторону: сравнивать не с чем, базу снимаем заново зондом.
    Принять это за обратный трафик значило бы выдать перезапуск за доказанный
    выход наружу."""
    agent.tick(tx=_Agent.STEP)                    # база зондом
    agent.tick(tx=_Agent.STEP)                    # улика — зонда нет
    assert len(agent.probes) == 1
    agent.rx = agent.tx = 0                       # линк подняли заново
    c = agent.tick()
    assert len(agent.probes) == 2, "после сброса счётчиков базу снимает зонд"
    assert agent.status.egress_src == "проба" and c.ok is True


def test_zero_idle_multiplier_brings_the_probe_back_to_every_tick(agent, monkeypatch):
    """Рычаг на случай разбора в бою: 0 в конфигурации возвращает прежнее
    поведение — зонд каждым тиком, даже когда через линк никто не ходит."""
    real = gw.settings.get
    monkeypatch.setattr(gw.settings, "get", lambda k, d=None:
                        0 if k == "app.gateway.egress_idle_multiplier" else real(k, d))
    for _ in range(4):
        agent.tick()
    assert len(agent.probes) == 4, "с нулевым множителем растяжки нет"
    assert agent.status.egress_src == "проба"


# ── алерт «не выходит наружу» ────────────────────────────────────────────────

def test_egress_alert_fires_on_a_failed_probe_and_is_cleared_by_return_traffic(agent):
    """Отказ канала квартиры равносилен отказу линка: алерт на втором тике.
    Отбой обязан приходить и по улике — если обратный трафик пошёл, канал жив,
    и висящий алерт «шлюз не выходит наружу» врёт человеку про сломанный
    РФ-доступ, которого нет."""
    agent.probe_ms = None
    assert not _texts(agent.svc.monitor_tick(), "не выходит наружу")
    notes = [n for n in agent.svc.monitor_tick() if "не выходит наружу" in n.text]
    assert len(notes) == 1 and notes[0].force_sound, "алерт шлюза обязан быть громким"
    notes[0].on_sent()                            # рассылка доложила о доставке
    assert not _texts(agent.svc.monitor_tick(), "не выходит наружу"), "алерт повторился"

    agent.forbidden = True                        # дальше живёт только улика
    agent.tx += _Agent.STEP
    assert not _texts(agent.svc.monitor_tick(), "снова отвечает"), "отбой раньше стрика"
    agent.tx += _Agent.STEP
    off = [n for n in agent.svc.monitor_tick() if "снова отвечает" in n.text]
    assert len(off) == 1, "улика не погасила алерт: человек остался с ложной тревогой"
    off[0].on_sent()
    agent.tx += _Agent.STEP
    assert not _texts(agent.svc.monitor_tick(), "снова отвечает"), "отбой повторился"


def _texts(notes, needle: str) -> list[str]:
    return [n.text for n in notes if needle in n.text]


# ── байты канала линка — не улика ────────────────────────────────────────────

# Накладные WireGuard на один TCP-сегмент канала, как их видят счётчики линка
# (transfer в `wg show`): 16 байт заголовка + 16 тега + IP/TCP 52, дополненные
# до 16, — около 96 байт в сторону данных и один ACK той же длины навстречу.
_WG_SEGMENT = 96


def _channel(agent, rx: int = 0, tx: int = 0, msgs: int = 1) -> tuple[int, int]:
    """Канал до ВПС что-то передал тем же линком: полезная нагрузка — в счёт
    канала (как её считает клиент), а на счётчиках линка — она же плюс
    накладные: сегмент в сторону данных и ACK навстречу. Возвращает прирост
    счётчиков линка (rx, tx)."""
    ch = agent.svc.channel                       # клиент канала пишет сюда, слот 0
    ch.account(0, rx=rx, tx=tx)
    for _ in range(msgs - 1):
        ch.account(0)
    return rx + msgs * _WG_SEGMENT, tx + msgs * _WG_SEGMENT


def test_a_channel_answer_does_not_paint_a_dead_path_green(agent):
    """Канал квартиры лёг, зонд это увидел. Человек открыл на ВПС диагностику —
    агент отвечает хвостом журнала и таблицей тем же линком. Без вычета эти
    ответы сходили бы за ответы из интернета, ушедшие клиентам, и выход
    наружу «доказывался» бы трафиком, который никуда наружу не ходил — ровно
    тогда, когда человек разбирается с поломкой."""
    agent.tick()                                  # база
    agent.probe_ms = None
    c = agent.tick(rx=_Agent.STEP)                # спрос без ответа → зонд → «лежит»
    assert c.ok is False
    for _ in range(3):
        d_rx, d_tx = _channel(agent, tx=_Agent.STEP, msgs=12)   # диагностика, снимки
        c = agent.tick(rx=d_rx, tx=d_tx)
        assert c.ok is False, "ответы канала серверу приняты за обратный трафик клиентов"
        assert agent.status.egress_src != "трафик"


def test_feeds_arriving_over_the_channel_are_not_client_demand(agent):
    """Фиды локальной сети — сотни килобайт с ВПС тем же линком. Приняв их за
    спрос клиентов без ответа, агент зондировал бы наружу каждым тиком — тот
    самый маячок с адреса квартиры."""
    agent.tick()                                  # база
    agent.forbidden = True                        # путь известен, спроса нет — зонду не место
    for _ in range(3):
        d_rx, d_tx = _channel(agent, rx=_Agent.STEP, msgs=4)
        c = agent.tick(rx=d_rx, tx=d_tx)
    assert c.ok is True and agent.probes == [42.0]


def test_client_traffic_on_top_of_the_channel_still_counts(agent):
    """Вычитается только канал: настоящий обратный трафик клиентов поверх него —
    по-прежнему улика, и зонд не нужен."""
    agent.tick()
    agent.probe_ms = None
    agent.tick(rx=_Agent.STEP)
    agent.forbidden = True
    d_rx, d_tx = _channel(agent, tx=_Agent.STEP, msgs=3)
    c = agent.tick(rx=d_rx, tx=d_tx + _Agent.STEP)
    assert c.ok is True and agent.status.egress_src == "трафик"
