"""Агент шлюза — голосующий о клоне основы у резерва (domain/gateway/reserve/quorum.py;
концепт «кворум» §3.7.8): пока пользователи основы у резерва, у шлюза два зонда —
`awgqah` (сервер дома) и `awgqar` (клон); `qview` несёт цель `t` (h | r); слова
своего сервера о клоне (`qask`/`qtake`/`qrel` с `t = r`, по каналу
конфигурации) правят голосующего о клоне, без `t` — о сервере.

Хост — подмены (infra/quoprobe — журнал поднятых), файлы quorum.json и
reserve.json — во временном каталоге; ответы — настоящие датаграммы `Reply`;
часы — модельные. Слова канала конфигурации — тем же упаковщиком подписанных
строк, что в жизни (util/gwlink).

Цена ошибки: `qtake` о клоне, принятый голосующим о доме, — его пробы основе
идут под эпохой, которой у основы нет, и вернувшаяся основа лишается голоса;
взгляд о клоне без цели — основа считает «не вижу клон» за «не вижу дом» (или
наоборот) и забирает пользователей у живого клона.
"""
from __future__ import annotations

import json
import os
import types

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.reserve import quorum as quomod
from awgbot.infra import quoprobe
from awgbot.infra.db import Database
from awgbot.util import gwlink
from awgbot.util.quorum.probe import Info, Probe, Reply

pytestmark = pytest.mark.unit

H, R = "awgqah", "awgqar"
CONF_H = ("[Interface]\nAddress = 169.254.88.5/32\nPrivateKey = DUMMY\n\n"
          "[Peer]\nPublicKey = DUMMY\nAllowedIPs = 169.254.88.1/32\nEndpoint = 203.0.113.10:51820\n")
CONF_R = ("[Interface]\nAddress = 169.254.88.69/32\nPrivateKey = DUMMY\n\n"
          "[Peer]\nPublicKey = DUMMY\nAllowedIPs = 169.254.88.2/32\nEndpoint = 198.51.100.7:51820\n")
E = 7


class _Clock:
    def __init__(self):
        self.t = 20_000.0

    def monotonic(self) -> float:
        return self.t


@pytest.fixture()
def ag(tmp_path, monkeypatch):
    clock = _Clock()
    ups: list[str] = []
    monkeypatch.setattr(quomod, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(quoprobe, "up", lambda name, conf, port=0: ups.append(name) or "up: ok")
    monkeypatch.setattr(quoprobe, "down", lambda name: "down: ok")
    monkeypatch.setattr(quoprobe, "standing", lambda: list(ups))
    monkeypatch.setattr(quoprobe, "wipe", lambda: "wipe: ok")
    (tmp_path / "reserve.json").write_text(json.dumps({"slot": 1, "name": "Alpha", "state": "away"}), encoding="utf-8")
    (tmp_path / "quorum.json").write_text(json.dumps({
        "l": "a", "g": 1, "members": ["a", "b", "a1", "a2"], "t": 2, "port": 8789, "probes": {"h": CONF_H, "r": CONF_R},
        "point": {"x": 2, "y": "cd" * 16, "q_gen": 4}, "e": E}), encoding="utf-8")
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    svc = GatewayServices(d)
    svc._RSV_STORE = tmp_path / "reserve.json"
    svc._QUO_STORE = tmp_path / "quorum.json"
    w = types.SimpleNamespace(svc=svc, clock=clock, tmp=tmp_path)

    def run(seconds: float, answer: set[str], step: float = 1.0) -> list[tuple]:
        """Такты; на пробы зондов из `answer` отвечает их цель."""
        acts: list[tuple] = []
        for _ in range(int(seconds // step)):
            clock.t += step
            out = svc.quorum_tick(clock.t)
            acts += out
            for a in out:
                if a[0] == "probe" and a[1] in answer:
                    r = Reply("a", 1, E, "lead" if a[1] == H else "follow", Probe.parse(a[2]).s, 2, 3,
                              Info(hs=4, flow=True, at="2026-10-30T10:00:00+03:00"))
                    acts += svc.quorum_reply_in(a[1], r.encode())
        return acts
    w.run = run
    w.v = lambda name: svc._gw_qvoters[name]
    w.word = lambda kind, **body: svc.quorum_guest_in(kind, {"l": "a", **body})
    svc.quorum_tick(clock.t)
    assert set(svc._gw_qvoters) == {H, R}, f"подготовка: зонды в отъезде — {sorted(svc._gw_qvoters)}"
    return w


def _views(acts, via: str = "link") -> list[dict]:
    return [a[2] for a in acts if a[0] == via and a[1] == "qview"]


def test_qview_names_its_target(ag):
    acts = ag.run(60, answer={H})
    acts += ag.run(50, answer={H})
    by_t = {v["tgt"]: v["w"] for v in _views(acts)}
    assert by_t.get("h") == "see", f"взгляд о сервере дома без t = h: {_views(acts)}"
    assert by_t.get("r") == "uns", f"взгляд о клоне без t = r: {_views(acts)}"


def test_qtake_about_the_clone_moves_only_the_clone_voter(ag):
    ag.word("qtake", e=E, tgt="r")
    assert ag.v(R).epoch == E + 1, "qtake о клоне не тронул голосующего о клоне"
    assert ag.v(H).epoch == E, "qtake о клоне сдвинул эпоху голосующего о сервере дома"


def test_qtake_without_a_target_is_about_the_server_at_home(ag):
    ag.word("qtake", e=E)
    assert ag.v(H).epoch == E + 1 and ag.v(R).epoch == E, "слово без t ушло не голосующему о доме"


def test_qask_about_the_clone_answers_the_clone_view(ag):
    ag.run(60, answer={H})
    ag.run(50, answer={H})
    assert ag.v(R).view == "uns" and ag.v(H).view == "see", "подготовка: клон не виден, дом виден"
    acts = ag.word("qask", g=1, tgt="r")
    views = _views(acts)
    assert views and views[0]["w"] == "uns" and views[0]["tgt"] == "r", f"на опрос о клоне ответ о доме: {acts}"


def test_qrel_about_the_clone_lifts_only_the_clone_pledge(ag):
    ag.run(110, answer=set())
    ag.word("qask", g=1, tgt="r")
    ag.word("qask", g=1)
    assert ag.v(R).pledge and ag.v(H).pledge, "подготовка: обязательства «нет» о клоне и о доме"
    ag.word("qrel", e=E, tgt="r")
    assert ag.v(R).pledge is None, "qrel о клоне не снял обязательство о клоне"
    assert ag.v(H).pledge is not None, "qrel о клоне снял обязательство о сервере дома"


def test_a_qtake_about_the_clone_sent_over_the_config_channel_reaches_the_clone_voter(ag, monkeypatch):
    """Сквозь канал: сервер кладёт слово действием ("gwlink", слот, "qtake",
    {..., "tgt": "r"}) — канал конфигурации упаковывает его подписанной строкой, а
    агент разбирает слово по полю `t` той же строки (runtime/linkclient/dispatch).
    Цель `t = r` обязана дойти до разбора — иначе слово о клоне правит
    голосующего о доме."""
    key = os.urandom(32)
    line = gwlink.pack(key, "qtake", {"l": "a", "e": E, "tgt": "r"}, seq=1)
    msg = gwlink.unpack(key, line)
    ag.svc.quorum_guest_in(str(msg.get("t")), msg)                 # как DispatchMixin._dispatch
    assert ag.v(R).epoch == E + 1 and ag.v(H).epoch == E, \
        f"после канала: эпоха клона {ag.v(R).epoch}, дома {ag.v(H).epoch} (поле t в строке: {msg.get('t')!r})"


def test_a_view_about_the_clone_does_not_go_to_the_reserve_as_a_view_about_the_server(ag):
    """Концепт «кворум» §3.7.8: взгляды шлюзов на клон — своему серверу по каналу
    конфигурации. Резерв разбирает `qview` гостевого канала как взгляд на сервер
    дома (reserve/quorum/inbound.reserve_quorum_guest_view_in, цели не смотрит):
    «не вижу клон» там перезаписывает «вижу сервер» шлюза — и после возврата
    кандидат на резерве считает этот шлюз голосом «нет» о живой основе."""
    acts = ag.run(60, answer={H}) + ag.run(50, answer={H})
    leaked = [v for v in _views(acts, "guest") if v.get("tgt") == "r"]
    assert not leaked, f"взгляд о клоне ушёл резерву по гостевому каналу: {leaked}"
