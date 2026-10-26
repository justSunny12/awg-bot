"""Дополнения резерва в сервисах: необязательные поля `st` — свободная
память хоста (`mem_free`), устройства основы на связи у гостя (`online`) и
чем открыт пакет (`in_by`) — от отправки через словарь канала до записи в
`rsv_peer_state`; надзор вебхука гостя — Т-РУ19 админу резерва один раз на
расхождение и 🟢 при восстановлении; сторона гостя шлёт слово о застое
один раз на результат надзора.

Цена ошибки: `st` прежней стороны без новых полей, который отвергается, —
пара «недоступна» после обновления одной стороны; «там M», не сброшенное
после ухода гостя, — админ основы ждёт устройства, которых нет; Т-РУ19 на
каждый такт надзора — шум у админа резерва, а без него он не узнает, что
хостинг закрыл порт вебхука и бот основы молчит.
"""
from __future__ import annotations

import builtins
import io
import json

import pytest

from awgbot.core import config
from awgbot.infra import rsvzone
from awgbot.util import pairproto
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
ADMIN = config.ADMIN_ID


def _notes(actions) -> list:
    return [n for a in actions or [] if a and a[0] == "notify" for n in a[1]]


# ── st: необязательные поля ─────────────────────────────────────────────────

def test_my_st_carries_memory_online_and_how_the_package_was_opened_and_passes_the_dictionary(mv, monkeypatch):
    svc = mv.svc
    monkeypatch.setattr(svc, "reserve_mem_free_mb", lambda: 812)
    svc.reserve_guest_online_set(14)
    svc.reserve_set_in("serving", by="share")
    body = svc.reserve_st_body()
    assert (body["mem_free"], body["online"], body["in_by"]) == (812, 14, "share")
    out = pairproto.check(pairproto.PAIR_PROTO, "st", body, pairproto.INVITER)
    assert (out["mem_free"], out["online"], out["in_by"]) == (812, 14, "share")


def test_an_st_of_an_older_peer_without_the_new_fields_is_taken_as_zero_and_empty(mv):
    body = rm.peer_st()
    for name in ("mem_free", "online", "in_by"):
        assert name not in body
    out = pairproto.check(pairproto.PAIR_PROTO, "st", body, pairproto.INVITER)
    mv.svc.reserve_st_in(out)
    ps = mv.svc.reserve_peer_state()
    assert (ps.get("mem_free"), ps.get("there"), ps.get("in_by")) == (0, 0, ""), ps


def test_the_new_fields_of_the_peer_land_in_its_state(mv):
    rm.st_in(mv.svc, mem_free=300, online=12, in_by="code")
    ps = mv.svc.reserve_peer_state()
    assert (ps["mem_free"], ps["there"], ps["in_by"]) == (300, 12, "code")
    rm.st_in(mv.svc)                                          # следующий st без полей — значения сброшены
    ps = mv.svc.reserve_peer_state()
    assert (ps["mem_free"], ps["there"]) == (0, 0), "«там M» пережило st без поля"


@pytest.mark.parametrize("field, value", [("mem_free", "много"), ("online", True), ("in_by", 5),
                                          ("in_by", "x" * 9)])
def test_the_new_fields_of_a_wrong_type_or_length_are_refused(field, value):
    from awgbot.util.gwlink import ProtocolError
    with pytest.raises(ProtocolError):
        pairproto.check(pairproto.PAIR_PROTO, "st", rm.peer_st(**{field: value}), pairproto.INVITER)


# ── свободная память ────────────────────────────────────────────────────────

def _meminfo(monkeypatch, text: str | None):
    real = builtins.open

    def fake(path, *a, **kw):
        if str(path) == "/proc/meminfo":
            if text is None:
                raise FileNotFoundError(path)
            return io.StringIO(text)
        return real(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", fake)


def test_free_memory_is_mem_available_in_mb(services, monkeypatch):
    _meminfo(monkeypatch, "MemTotal:  4000000 kB\nMemFree:  100000 kB\nMemAvailable:  409600 kB\n")
    assert services.reserve_mem_free_mb() == 400


@pytest.mark.parametrize("text", [None, "MemTotal: 1 kB\n", "MemAvailable: много kB\n", "MemAvailable:\n"])
def test_unreadable_memory_is_zero_which_means_no_line(services, monkeypatch, text):
    _meminfo(monkeypatch, text)
    assert services.reserve_mem_free_mb() == 0


# ── надзор вебхука: Т-РУ19 админу резерва ───────────────────────────────────

@pytest.fixture()
def wh(services, rsv, tmp_path, monkeypatch):
    rc.pair_row(services)
    monkeypatch.setattr(rsvzone, "WEBHOOK_FILE", tmp_path / "webhook.json")
    (tmp_path / "webhook.json").write_text(json.dumps({"port": 8443}))
    return services


STALLED = {"stalled": True, "since": "2026-10-21T03:12:00+05:00", "error": "Connection timed out"}


def test_a_stalled_webhook_is_told_to_the_reserve_admin_once_with_the_port(wh):
    notes = _notes(wh.reserve_guest_webhook_in(STALLED))
    assert len(notes) == 1 and notes[0].tg_id == ADMIN, notes
    text = notes[0].text
    assert "«Beta»" in text and "Connection timed out" in text and "<code>8443</code>" in text, text
    assert not text.endswith("."), "уведомления без точки в конце"
    assert wh.reserve_guest_webhook_in(STALLED) == [], "Т-РУ19 повторился на том же застое"
    assert wh.reserve_guest_webhook_in({**STALLED, "error": "другая"}) == [], "повтор на смене текста ошибки"


def test_the_recovery_is_told_once_and_only_after_a_stall(wh):
    assert wh.reserve_guest_webhook_in({"stalled": False}) == [], "«восстановлена» без застоя"
    wh.reserve_guest_webhook_in(STALLED)
    back = _notes(wh.reserve_guest_webhook_in({"stalled": False}))
    assert len(back) == 1 and back[0].text.startswith("🟢") and "«Beta»" in back[0].text
    assert wh.reserve_guest_webhook_in({"stalled": False}) == []
    assert len(_notes(wh.reserve_guest_webhook_in(STALLED))) == 1, "новый застой после восстановления не сказан"


def test_a_missing_webhook_file_does_not_stop_the_notice(wh, tmp_path):
    (tmp_path / "webhook.json").unlink()
    notes = _notes(wh.reserve_guest_webhook_in(STALLED))
    assert len(notes) == 1


# ── гость: слово о застое — один раз на результат надзора ──────────────────

class _Wh:
    def __init__(self, results):
        self.results = list(results)

    async def watch(self, bot):
        return self.results.pop(0) if self.results else {}


class _Ctl:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, kind, body=None):
        self.sent.append((kind, body))
        return True


async def test_the_guest_tells_its_reserve_about_a_stall_and_the_recovery(services, fake_bot):
    from awgbot.runtime.main import guest as guest_main
    services.guest_state_in({"name": "Gamma"})
    ctl = _Ctl()
    wh = _Wh([{}, {"stalled": True, "since": "2026-10-21T03:12:00+05:00", "error": "E" * 500}, {},
              {"restored": True}, {"foreign": True, "url": "https://203.0.113.9/x"}])
    for _ in range(5):
        await guest_main._watch(wh, fake_bot, services, ctl)
    assert [k for k, _b in ctl.sent] == ["webhook", "webhook"], ctl.sent
    assert ctl.sent[0][1]["stalled"] is True and len(ctl.sent[0][1]["error"]) == 200, "ошибка без потолка в слове"
    assert ctl.sent[1][1] == {"stalled": False}
    admin_msgs = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == ADMIN]
    assert len(admin_msgs) == 2, f"админу основы — застой и восстановление по одному разу: {admin_msgs}"


# ── основа узнаёт о переезде мимо себя: чем открыт пакет ────────────────────

@pytest.mark.parametrize("in_by", ["code", "share", "k", ""])
def test_a_move_past_the_primary_counts_as_opened_without_my_key_whatever_the_reserve_says(mv, in_by):
    """Основа лежала, резерв перевёл её пользователей сам: своего `go` с K основа
    не отправляла, значит пакет открыт по долям или по коду — что бы ни
    говорил резерв в in_by. Слово резерва здесь не слушаем: подложное
    «k» оставило бы прежнее поколение после возврата, и резерв, собравший
    M, мог бы открыть и следующий пакет. Поколение пакета — то, с которым
    стартовал гость; отметка переключения — для демпфера возврата."""
    svc = mv.svc
    over = {"in": "serving", "in_kind": "dead", "pkg": {"gen": 9, "fp": "DUMMY-fp"}}
    if in_by:
        over["in_by"] = in_by
    rm.st_in(svc, **over)
    svc.reserve_out_tick()
    out = svc.rsv_get("rsv_out")
    assert svc.reserve_out() == "away" and out.get("by") == "share", out
    assert out.get("go_sent") is False, f"переезд мимо меня записан как отданный моим K: {out}"
    assert int(out.get("gen") or 0) == 9, f"поколение не из пакета резерва — слияние не найдёт base-<gen>.db: {out}"
    assert out.get("last_switch"), "переезд мимо основы без отметки переключения"
    assert (out.get("healthy_mono"), out.get("can_return")) == (0, False), "здоровье прежнего эпизода не сброшено"
