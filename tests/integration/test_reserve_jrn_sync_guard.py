"""Журнал данных резерва — две правки по сверке документации:

- сторож терминала tools/rsvbusy.py (`awg-bot update` / `restore`) отказывает
  кодом 3 с причиной одной строкой, как бот (journal/tail
  `reserve_refuse_update_if_waiting`): ждём правки бота на резерве после
  «вернуть без резерва» (`rsv_out.await_merge`) или свой хвост ещё не перенесён
  и с отметки `rsv_tail.at` прошло не больше TAIL_WAIT_S (общая проверка по базе
  `own_tail_blocks_raw`); после срока, без хвоста и на базе без таблиц журнала —
  свободен; прежние причины (мои пользователи едут или у меня пользователи
  резерва) — раньше новых;
- строка карточки резерва «данные для переезда у … от …» при журнале — время
  сверки с резервом (`reserve_jrn_synced_at`): всё подтверждено, сессия сверена и
  связь есть — «сейчас»; есть неподтверждённое или связи нет — последнее
  подтверждение сессии (`jack`, сверка по `st`, принятый снимок); журнала нет —
  время планового снимка `rsv_pkg_sent.at`, как раньше; из двух — большее.

Мир — tests/integration/_reserve_jrn_duo.py (основа «Alpha» и резерв «Beta»
на двух базах), стенные часы — замороженные (`_Wall`). Ключи — заглушки.

Цена ошибки: обновление из терминала посреди переноса хвоста или пока ждём
правки бота на резерве — новый выпуск без этого пути их не примет, правки людей
за время аварии теряются молча, хотя бот в чате тот же шаг отказывает; сторож,
который держит после срока или на старой базе, — сервер не обновить вовсе;
карточка с временем снимка раз в сутки при живом журнале — админ видит
«данные от вчера» и боится переезжать; «сейчас» при неподтверждённых правках
или без связи — админ уверен, что резерв знает последние правки, и переезд
поднимает пользователей без них.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve.section import card_text
from awgbot.domain.services.reserve.journal import tail as tailmod
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo
from tests.integration.test_reserve_jrn_tail_pair import _crash, _jtail_parts, _tail_vera

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


class _Wall:
    """Стенные часы бота (timeutil.now и всё, что от него: now_iso, elapsed):
    время идёт только по слову теста."""

    def __init__(self, monkeypatch) -> None:
        self.t = timeutil.parse_iso("2026-10-28T12:00:00+03:00")
        monkeypatch.setattr(timeutil, "now", lambda: self.t.astimezone(timeutil.TZ))

    def advance(self, minutes: float) -> None:
        self.t = timeutil.shift(self.t, minutes * 60)

    def iso(self) -> str:
        return timeutil.to_iso(self.t)

    def ago(self, minutes: float) -> str:
        return timeutil.to_iso(timeutil.shift(self.t, -minutes * 60))


@pytest.fixture()
def wall(monkeypatch):
    return _Wall(monkeypatch)


@pytest.fixture()
def duo(wall, mv, tmp_path, monkeypatch, fake_awg):
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    yield d
    d.close()


def _busy(capsys) -> tuple[int, str]:
    from tools import rsvbusy
    code = rsvbusy.main()
    return code, capsys.readouterr().out.strip()


def _tail_on_its_way(duo) -> None:
    """Авария: правка основы до неё не дошла, бот на резерве открылся, основа следует
    и отправила свой хвост (`jtail`) — ответа нет. Пользователи — снова «дома»
    (чтобы прежние причины сторожа не заслоняли новую)."""
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    _jtail_parts(duo)
    assert duo.a.db.jrn_tail_rows(), "условие: свой хвост не перенесён"
    duo.a.reserve_set_out("home")


# ── сторож терминала: ждём правки бота на резерве ───────────────────────────

def test_the_busy_guard_refuses_while_waiting_for_the_reserves_edits(mv, capsys):
    """«Вернуть без резерва»: правки бота на резерве ещё не пришли — обновление из
    терминала отказывает так же, как кнопка в боте."""
    mv.svc.rsv_update("rsv_out", await_merge=True)
    code, text = _busy(capsys)
    assert code == 3, f"обновление пустили, пока ждём правки бота на резерве: {code} ({text!r})"
    assert "вернуть без резерва" in text and "«Beta»" in text, f"причина без сути или без имени резерва: {text!r}"
    assert "\n" not in text, f"причина не одной строкой: {text!r}"
    mv.svc.rsv_update("rsv_out", await_merge=False)
    assert _busy(capsys) == (0, ""), "правки пришли — сторож всё ещё держит"


def test_the_busy_guard_ignores_await_merge_without_a_pair(services, rsv, tmp_path, monkeypatch, capsys):
    """Без пары следы автомата не держат обновление (пара снята — ждать некого)."""
    from awgbot.core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    services.rsv_update("rsv_out", await_merge=True)
    assert _busy(capsys)[0] == 0, "след «вернуть без резерва» без пары держит обновление"


def test_the_moving_clients_reason_comes_before_await_merge(mv, capsys):
    """Обе причины сразу — сначала прежняя: пользователи у резерва важнее."""
    mv.svc.rsv_update("rsv_out", await_merge=True)
    mv.svc.reserve_set_out("away")
    code, text = _busy(capsys)
    assert code == 3 and "(away)" in text, f"прежняя причина не первой: {text!r}"
    assert "вернуть без резерва" not in text, f"две причины в одной строке или не та: {text!r}"


# ── сторож терминала: свой хвост ────────────────────────────────────────────

def test_the_busy_guard_refuses_while_the_own_tail_is_on_its_way(duo, wall, capsys):
    """Хвост ушёл резерву, ответа нет, 10 минут не прошло — обновление отказывает;
    после 10 минут — свободно (после возврата основа переносит хвост сама)."""
    _tail_on_its_way(duo)
    duo.a.rsv_update("rsv_tail", at=wall.ago(9))
    code, text = _busy(capsys)
    assert code == 3, f"обновление пустили посреди переноса своего хвоста: {code} ({text!r})"
    assert "хвост" in text and "«Beta»" in text and "\n" not in text, f"причина не та или не одной строкой: {text!r}"
    duo.a.rsv_update("rsv_tail", at=wall.ago(11))
    assert _busy(capsys) == (0, ""), "сторож держит обновление дольше TAIL_WAIT_S"


def test_the_busy_guard_tail_wait_ends_exactly_at_tail_wait_s(duo, wall, capsys):
    """Граница — включительно, как у бота: ровно TAIL_WAIT_S ещё держит, секундой
    позже — нет."""
    _tail_on_its_way(duo)
    duo.a.rsv_update("rsv_tail", at=wall.ago(tailmod.TAIL_WAIT_S / 60))
    assert _busy(capsys)[0] == 3, "ровно на сроке сторож уже отпустил (у бота — ещё держит)"
    duo.a.rsv_update("rsv_tail", at=wall.ago(tailmod.TAIL_WAIT_S / 60 + 1 / 60))
    assert _busy(capsys)[0] == 0, "после срока сторож держит"


def test_the_busy_guard_is_free_once_the_tail_is_moved(duo, wall, capsys):
    """Хвост принят (строк нет) — свежая отметка `rsv_tail.at` не держит."""
    _tail_on_its_way(duo)
    duo.a.rsv_update("rsv_tail", at=wall.iso())
    duo.a.db.jrn_tail_drop()
    assert not duo.a.db.jrn_tail_rows(), "условие: хвост снят"
    assert _busy(capsys) == (0, ""), "хвоста нет, а сторож держит"


def test_the_busy_guard_is_free_without_a_tail_mark(duo, capsys):
    """Строки хвоста есть, отметки времени нет (или она испорчена) — как у бота, не держит."""
    _tail_on_its_way(duo)
    duo.a.rsv_put("rsv_tail", None)
    assert _busy(capsys)[0] == 0, "без отметки времени сторож держит вечно"
    duo.a.db.set_state("rsv_tail", "{\"at\": \"не время\"}")
    assert _busy(capsys)[0] == 0, "испорченная отметка держит обновление"


def test_the_hosted_clients_reason_comes_before_the_own_tail(duo, wall, capsys):
    """У меня пользователи и бот резерва — эта причина первой, хвост — потом."""
    _tail_on_its_way(duo)
    duo.a.rsv_update("rsv_tail", at=wall.iso())
    duo.a.reserve_set_in("serving")
    code, text = _busy(capsys)
    assert code == 3 and "(serving)" in text and "хвост" not in text, f"не прежняя причина первой: {text!r}"


# ── сторож терминала: база старше журнала ───────────────────────────────────

def _drop_journal_tables(db) -> None:
    """Как у базы выпуска до журнала: ни таблиц jrn_*, ни триггеров захвата и
    запрета (временные, на соединении сервисов теста)."""
    con = db._connection()
    for (name,) in con.execute("SELECT name FROM sqlite_temp_master WHERE type = 'trigger'").fetchall():
        con.execute(f"DROP TRIGGER IF EXISTS temp.\"{name}\"")
    for t in ("jrn_tail", "jrn_events", "jrn_counts", "jrn_seen", "jrn_meta"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    con.commit()


def test_the_busy_guard_on_a_base_without_journal_tables(mv, capsys):
    """База выпуска без журнала: новая проверка хвоста не роняет сторож и не
    держит; прежние причины и «вернуть без резерва» по-прежнему работают."""
    mv.svc.rsv_update("rsv_tail", at=timeutil.now_iso())
    _drop_journal_tables(mv.svc.db)
    assert _busy(capsys) == (0, ""), "на базе без таблиц журнала сторож держит или пишет лишнее"
    mv.svc.rsv_update("rsv_out", state="away")             # как записал бы прежний выпуск
    assert _busy(capsys)[0] == 3, "на старой базе пропала прежняя причина"
    mv.svc.rsv_update("rsv_out", state="home")
    mv.svc.rsv_update("rsv_out", await_merge=True)
    assert _busy(capsys)[0] == 3, "на старой базе пропал отказ «вернуть без резерва»"


# ── одна проверка хвоста у бота и у сторожа ─────────────────────────────────

def test_the_bot_and_the_terminal_guard_judge_the_tail_the_same(duo, wall, capsys):
    """`reserve_tail_blocks` у сервиса и `own_tail_blocks_raw` по базе — одно и то же
    во всех состояниях; сторож отказывает ровно тогда, когда бот."""
    a = duo.a
    seen = []

    def agree(label: str) -> None:
        bot, raw = a.reserve_tail_blocks(), tailmod.own_tail_blocks_raw(a.db)
        code, _ = _busy(capsys)
        assert bot == raw == (code == 3), f"{label}: бот {bot}, по базе {raw}, сторож {code}"
        seen.append(bot)

    agree("хвоста нет")
    _tail_on_its_way(duo)
    a.rsv_update("rsv_tail", at=wall.ago(1))
    agree("хвост свежий")
    a.rsv_update("rsv_tail", at=wall.ago(11))
    agree("хвост после срока")
    a.rsv_put("rsv_tail", None)
    agree("без отметки")
    a.rsv_update("rsv_tail", at=wall.iso())
    a.db.jrn_tail_drop()
    agree("хвост перенесён")
    _drop_journal_tables(a.db)
    agree("база без журнала")
    assert seen == [False, True, False, False, False, False], f"контроль: состояния не те, что задумано: {seen}"


def test_the_bot_and_the_terminal_guard_judge_the_c9_wait_the_same(mv, capsys):
    """Ожидание правок бота на резерве после «вернуть без резерва» — одно правило по базе
    (`c9_wait_raw`): бот отказывает обновлению ровно тогда, когда сторож терминала."""
    from awgbot.domain.services.types import ServiceError
    svc = mv.svc
    rm.go_away(svc)
    svc.reserve_set_out("home", await_merge=True)

    def agree(label: str) -> None:
        raw = tailmod.c9_wait_raw(svc.db)
        try:
            svc.reserve_refuse_update_if_waiting()
            bot = False
        except ServiceError as e:
            bot = True
            assert str(e) == tailmod.C9_WAIT_REFUSED.format(name="Beta"), e
        code, _ = _busy(capsys)
        assert bot == raw == (code == 3), f"{label}: бот {bot}, по базе {raw}, сторож {code}"

    agree("ждём правки")
    svc.rsv_update("rsv_out", await_merge=False)
    agree("ожидание снято")
    svc.db.set_state("rsv_out", "не JSON")
    agree("отметка битая")


# ── карточка: «данные для переезда у … от …» ────────────────────────────────

def _card_at(svc) -> str:
    return svc.reserve_card_data()["pkg_sent_at"]


def _card_line(svc) -> str:
    text = card_text({**svc.reserve_card_data(), "dir_out": (True, "")})
    lines = [s for s in text.splitlines() if "данные для переезда у" in s]
    assert len(lines) == 1, f"строки о данных для переезда нет или их несколько: {text}"
    return lines[0]


def _clock(iso: str) -> str:
    return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))


def test_all_acked_and_online_shows_now(duo, wall):
    """Журнал идёт, резерв подтвердил всё, связь есть — «от» сейчас, а не время
    снимка (снимок раз в сутки, данные у резерва при этом свежие)."""
    duo.journal_on()
    snap_at = duo.a.rsv_get("rsv_pkg_sent")["at"]
    duo.edit("Вера")
    duo.tick()
    assert duo.a.reserve_jrn_all_acked(), "условие: всё подтверждено"
    wall.advance(95)
    assert _card_at(duo.a) == wall.iso() != snap_at, f"не «сейчас» при всём подтверждённом: {_card_at(duo.a)}"
    assert _card_line(duo.a).startswith(f"└ данные для переезда у «Beta» от {_clock(wall.iso())}"), _card_line(duo.a)


def test_unacked_events_show_the_last_ack(duo, wall):
    """Правка не подтверждена — «от» время последнего `jack`, а не «сейчас»: у
    резерва данных этой правки ещё нет."""
    duo.journal_on()
    wall.advance(5)
    duo.edit("Вера")
    duo.tick()
    acked_at = wall.iso()
    wall.advance(7)
    duo.edit("Олег")                                       # ещё не ушла
    assert not duo.a.reserve_jrn_all_acked(), "условие: есть неподтверждённое"
    assert _card_at(duo.a) == acked_at, f"не время последнего подтверждения: {_card_at(duo.a)} (ждал {acked_at})"
    wall.advance(3)
    duo.a.reserve_jrn_tick()                               # ушла, подтверждение потерялось
    assert _card_at(duo.a) == acked_at, "отправленное без подтверждения сдвинуло время"
    assert _clock(wall.iso()) not in _card_line(duo.a), f"на карточке «сейчас» при неподтверждённом: {_card_line(duo.a)}"


def test_without_a_link_all_acked_shows_the_last_ack(duo, wall):
    """Связи с резервом нет — «сейчас» нельзя, даже если всё подтверждено до обрыва."""
    duo.journal_on()
    wall.advance(4)
    duo.edit("Вера")
    duo.tick()
    acked_at = wall.iso()
    duo.a.rsv_update("rsv_peer_state", session=False)
    wall.advance(30)
    assert duo.a.reserve_jrn_all_acked(), "условие: всё подтверждено"
    assert _card_at(duo.a) == acked_at, f"без связи — не время подтверждения: {_card_at(duo.a)}"


def test_the_st_check_of_a_new_session_counts_as_an_ack(duo, wall):
    """Новая сессия: до сверки по `st` резерва — «сейчас» нельзя и подтверждений
    сессии нет (время снимка); сверка по `st` — подтверждение этого момента."""
    duo.journal_on()
    snap_at = duo.a.rsv_get("rsv_pkg_sent")["at"]
    wall.advance(10)
    duo.a.reserve_session_opened({})
    assert _card_at(duo.a) == snap_at, f"до сверки новой сессии — не время снимка: {_card_at(duo.a)}"
    wall.advance(1)
    duo.st_b()
    st_at = wall.iso()
    wall.advance(1)
    duo.edit("Вера")                                       # не подтверждена
    assert _card_at(duo.a) == st_at, f"сверка по st не дала время подтверждения: {_card_at(duo.a)} (ждал {st_at})"


def test_an_accepted_snapshot_counts_as_an_ack(duo, wall):
    """Резерв принял снимок — с этого момента его данные такие же; дальше
    неподтверждённая правка держит это время."""
    duo.journal_on()
    wall.advance(20)
    duo.edit("Вера")
    duo.snapshot()
    snap_at = wall.iso()
    wall.advance(5)
    duo.edit("Олег")
    assert _card_at(duo.a) == snap_at, f"не время принятого снимка: {_card_at(duo.a)}"


def test_without_the_journal_the_card_shows_the_snapshot_time(mv, wall):
    """Резерв без `jrn1` — прежний путь: «от» время планового пакета, даже когда
    связь есть (не «сейчас»)."""
    assert not mv.svc.reserve_jrn_on(), "условие: журнал не идёт"
    mv.svc.rsv_update("rsv_pkg_sent", at=wall.ago(600))
    assert mv.svc.reserve_jrn_synced_at() == "", "время сверки журнала без журнала"
    assert _card_at(mv.svc) == wall.ago(600), f"без журнала — не время пакета: {_card_at(mv.svc)}"


def test_the_later_of_the_snapshot_and_the_last_ack_wins(duo, wall):
    """Пакет подтверждён позже последнего `jack` — показывается он (из двух — большее)."""
    duo.journal_on()
    wall.advance(3)
    duo.edit("Вера")
    duo.tick()
    wall.advance(3)
    duo.edit("Олег")                                       # не подтверждена
    acked_at = duo.a.reserve_jrn_synced_at()
    wall.advance(3)
    duo.a.rsv_update("rsv_pkg_sent", at=wall.iso())
    assert acked_at < wall.iso() and _card_at(duo.a) == wall.iso(), f"не большее из двух: {_card_at(duo.a)}"
