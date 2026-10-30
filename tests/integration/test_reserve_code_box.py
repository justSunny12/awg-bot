"""Ящик кода с сопряжения и путь кода без прямой самого `M`: основа «Alpha»
(сторона линии `a`, quorum/keys, keys) отдаёт второму серверу `qkeys` без
многочлена; второй сервер «Beta» (сторона `b`, вторые сервисы поверх своей
временной базы) хранит точку прямой кода и ящик рядом с пакетом; процесс
открытия (tools/rsvopen) собирает `M` кодом. Предусловие открытия
(`reserve_open_block`) — ящик кода того же поколения `M`, что и пакет; защитная
миграция `reserve_keys_ensure` — состояние прежних выпусков (прямая `M` без
ящика кода) переводится один раз; копия базы ящиков не несёт — восстановленный
сервер получает новый код.

Ключи и пакеты — настоящие (util/rsvkeys, util/rsvpkg/seal), хост и сеть не
трогаются; канал пары — прямой вызов разбора второй стороны после проверки
схемой (`pairproto.check`).

Цена ошибки: пара без ящика кода с первого дня — в аварию до первого шлюза в
составе админу нечем открыть пакет; ящик чужой основы или код другой прямой,
принятые процессом открытия, — `M` собран не тот, переезд падает на последнем
шаге, а попытка кода сгорает; «данные есть» без ящика текущего `M` — карточка
обещает переезд, которого не будет; миграция, срабатывающая на каждом такте, —
админу шлётся новый код каждые 5 секунд, а прежний в сейфе перестаёт
действовать; восстановление из копии со старыми ящиками — второй сервер держит
точку прямой, код которой админ уже не знает.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services import Services
from awgbot.infra.db import Database
from awgbot.util import pairproto, rsvkeys, shamir
from awgbot.util.rsvpkg import seal
from tests.integration import _reserve_common as rc
from tests.integration._reserve_quorum import line
from tests.integration._reserve_vote_common import box
from tools import rsvopen

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["line", "box"]


def _blob(s, pkg_gen: int = 1) -> bytes:
    """Пакет переезда текущего поколения основы: заголовок под `M`, части под `K`."""
    k = s.reserve_keys()
    return seal.seal({"vpn": {"peers": []}}, k=bytes.fromhex(k["k"]), m=bytes.fromhex(k["m"]), pkg_gen=pkg_gen,
                     m_gen=int(k["m_gen"]))


def _fresh_qkeys(s) -> dict:
    """Тело qkeys основы прямо сейчас (часы повтора не мешают собрать его снова)."""
    s.__dict__.pop("_qkeys_sent", None)
    s.rsv_update("rsv_qkeys", acked={})
    body = s.reserve_qkeys_body()
    assert body is not None, "предпосылка: qkeys не собрался"
    return body


class _Beta:
    """Второй сервер на своей базе: сторона `b` (принявший), пакет основы и её ящики."""

    def __init__(self, tmp_path) -> None:
        d = tmp_path / "beta"
        d.mkdir()
        self.db = Database(str(d / "bot.db"))
        self.db.init_schema()
        self.s = Services(self.db)
        rc.pair_row(self.s, listener=False)
        assert self.s.reserve_quorum_side() == "b", "предпосылка: второй сервер не принявший"

    def take_qkeys(self, body: dict) -> list[tuple]:
        """qkeys основы по каналу пары: схема канала, затем разбор."""
        return self.s.reserve_qkeys_in(pairproto.check(pairproto.PAIR_PROTO, "qkeys", dict(body), pairproto.INVITER))

    def take_package(self, blob: bytes, gen: int = 1) -> tuple[str, str]:
        return self.s.reserve_pkg_in(gen, seal.digest(blob), blob)

    def close(self) -> None:
        self.db.close()


@pytest.fixture()
def beta(tmp_path):
    b = _Beta(tmp_path)
    yield b
    b.close()


# ── сопряжение: прямая кода и qkeys без многочлена ──────────────────────────

def test_pairing_creates_the_code_line_and_box_and_no_polynomial(line):
    """С сопряжения: ящик кода есть (прямая `Mc` первого поколения), многочлена нет;
    показанный код — точка x = 255 этой прямой, а не что-то вокруг самого `M`."""
    q, k = line.s.reserve_qkeys(), line.s.reserve_keys()
    assert line.s.reserve_quorum_keys_on() and q["on"] is True, "ящика кода с сопряжения нет"
    assert (q["c_gen"], q["q_gen"], q["t"], q["poly"]) == (1, 0, 0, []), f"не то состояние с сопряжения: {q}"
    assert not line.s.reserve_quorum_poly_on(), "многочлен при одном голосующем — порог 1"
    assert "a" not in k and set(k) >= {"m", "m_gen", "k", "k_gen"}, f"ключи основы несут наклон прямой M: {sorted(k)}"
    line_mc = shamir.Line(bytes.fromhex(q["mc"]), bytes.fromhex(q["ma"]))
    assert line.s.reserve_code() == rsvkeys.code_encode(line_mc.point(rsvkeys.CODE_X)[1]), "код — не точка прямой Mc"
    assert q["mc"] != k["m"], "прямая кода — сама M"


def test_the_second_server_takes_qkeys_from_pairing_confirms_it_and_the_code_opens_the_package(line, beta):
    """Сквозной путь без шлюзов: qkeys основы без многочлена → второй сервер хранит
    точку и ящик, отвечает qkeys_ok → основа ключи закрыла; пакет принят без доли, код
    админа с точкой второго сервера и его ящиком открывает пакет — `M` и `K` основы."""
    a = line.s
    line.propose()
    body = _fresh_qkeys(a)
    acts = beta.take_qkeys(body)
    assert [x[:2] for x in acts] == [("send", "qkeys_ok")], acts
    ok = pairproto.check(pairproto.PAIR_PROTO, "qkeys_ok", dict(acts[0][2]), pairproto.ACCEPTOR)
    a.reserve_qkeys_ok_in(ok)
    assert a.reserve_quorum_keys_ok(a.reserve_quorum_proposed()), "основа не сочла ящик кода доставленным"
    assert a.reserve_qkeys_body() is None, "после qkeys_ok ключи шлются снова"
    blob = _blob(a)
    assert beta.take_package(blob) == ("pkg_ok", ""), "пакет без доли не принят"
    secret = beta.s.reserve_quorum_code_secret(rsvkeys.normalize(a.reserve_code()))
    assert secret is not None, "у второго сервера нет ящика кода поколения пакета"
    m, k = rsvopen.recover_keys(secret, blob)
    keys = a.reserve_keys()
    assert (m.hex(), k.hex()) == (keys["m"], keys["k"]), "код с ящиком второго сервера не открыл пакет"


def test_a_foreign_code_box_does_not_open_the_package(line, beta):
    """Ящик кода чужой основы (другая пара, тот же номер поколения) у второго сервера:
    верный код этой основы его не открывает — процесс открытия отказывает, а не
    собирает мусорный `M`."""
    a = line.s
    beta.take_qkeys(_fresh_qkeys(a))
    blob = _blob(a)
    beta.take_package(blob)
    peer = beta.s.reserve_quorum_peer_keys()
    other = rsvkeys.CodeGen.new(1)
    beta.s.rsv_update("rsv_qkeys", peer={**peer, "cbox": other.cbox(rsvkeys.new_master()).hex()})
    secret = beta.s.reserve_quorum_code_secret(rsvkeys.normalize(a.reserve_code()))
    with pytest.raises(rsvopen.OpenError, match="аварийный код"):
        rsvopen.recover_keys(secret, blob)


def test_a_code_of_another_generation_does_not_open_the_package(line, beta):
    """Код прежней прямой (до «🔄 Сменить код» или переезда по коду) с ящиком новой —
    отказ: старый код после смены не действует."""
    a = line.s
    old_code = a.reserve_code()
    a.reserve_new_generation("смена кода", code=True)
    assert a.reserve_code() != old_code, "предпосылка: код не сменился"
    beta.take_qkeys(_fresh_qkeys(a))
    blob = _blob(a)
    beta.take_package(blob)
    secret = beta.s.reserve_quorum_code_secret(rsvkeys.normalize(old_code))
    with pytest.raises(rsvopen.OpenError, match="аварийный код"):
        rsvopen.recover_keys(secret, blob)
    m, _k = rsvopen.recover_keys(beta.s.reserve_quorum_code_secret(rsvkeys.normalize(a.reserve_code())), blob)
    assert m.hex() == a.reserve_keys()["m"], "новый код не открыл пакет"


def test_a_package_of_a_newer_m_without_its_code_box_is_no_data(line, beta):
    """Пакет нового поколения `M` пришёл раньше qkeys с ящиком под него: ящик кода
    прежнего `M` для открытия не годится — код не уходит в процесс открытия."""
    a = line.s
    beta.take_qkeys(_fresh_qkeys(a))
    a.reserve_new_generation("снятие шлюза", code=False)
    beta.take_package(_blob(a))
    assert beta.s.reserve_quorum_code_secret(rsvkeys.normalize(a.reserve_code())) is None, \
        "ящик прежнего M выдан процессу открытия под пакет нового"


# ── предусловие открытия: ящик кода поколения пакета ────────────────────────

def _code_box(s, m_gen: int) -> None:
    s.rsv_update("rsv_qkeys", peer={"l": "b", "m_gen": int(m_gen), "x": 1, "c_gen": 1, "cy": "ab" * 16,
                                    "cbox": "00" * rsvkeys.BOX_LEN, "q_gen": 0, "t": 0, "y": "", "qbox": ""})


def test_the_opening_block_says_no_data_without_the_code_box_of_the_package_generation(box):
    """Пакет на месте, а ящика кода его поколения `M` нет (не пришёл или прежнего) —
    «данных для переезда нет»; ящик того же поколения — пусто (можно); открытие по K
    основы (кнопка) ящика не требует."""
    s = box.s
    m_gen = int(s.rsv_get("rsv_pkg_in").get("m_gen") or 0)
    s.rsv_update("rsv_qkeys", peer={})
    assert s.reserve_open_block() == "данных для переезда нет", "без ящика кода открытие не заблокировано"
    _code_box(s, m_gen + 1)
    assert s.reserve_open_block() == "данных для переезда нет", "ящик другого поколения M сочтён годным"
    _code_box(s, m_gen)
    assert s.reserve_open_block() == "", "ящик кода поколения пакета есть, а открытие заблокировано"
    s.rsv_update("rsv_qkeys", peer={})
    assert s.reserve_open_block(need_box=False) == "", "кнопка (K основы) заблокирована отсутствием ящика кода"


def test_the_code_without_a_code_box_is_refused_with_the_reason_and_the_window_stays_open(box):
    """Окно кода открыто, ящика кода нет: процесс открытия не запускается, ответ —
    «данных для переезда нет: ящик кода не получен», окно остаётся открытым, попытка
    не сгорает (код верный — виноват не админ)."""
    s = box.s
    s.rsv_update("rsv_qkeys", peer={})
    s.reserve_code_window_quiet(True)
    assert s.reserve_code_window()["open"], "предпосылка: окно кода не открылось"
    code = rsvkeys.code_encode(bytes(range(16)))
    res = s.reserve_code_in(rc.PEER_ADMIN, code)
    assert not res["ok"] and res["why"] == "open_failed", res
    assert res["reason"] == "данных для переезда нет: ящик кода не получен", res
    assert box.launched == [], "процесс открытия запущен без ящика кода"
    assert s.reserve_code_window()["open"], "окно кода закрылось без попытки"
    again = s.reserve_code_in(rc.PEER_ADMIN, code)
    assert again["why"] == "open_failed" and again["left"] == res["left"], "попытка без ящика засчитана в лимит"


# ── миграция прежнего состояния ─────────────────────────────────────────────

def _as_before_420(s) -> dict:
    """Состояние прежнего выпуска: прямая самого `M` (наклон `a`), ящика кода нет."""
    s.rsv_put("rsv_qkeys", None)
    s.rsv_update("rsv_keys", a="cd" * 16, code_notify_why="")
    return dict(s.reserve_keys())


def test_the_old_line_of_m_is_migrated_once_with_one_new_code_notice(line):
    """Пара с ключами прежнего выпуска: первый такт кворума — ящик кода, новое
    поколение `M`, наклон прямой прочь, новый код админу (Т-РУ14); следующие такты
    ничего не меняют и второй раз код не просят."""
    s = line.s
    before = _as_before_420(s)
    assert not s.reserve_quorum_keys_on(), "предпосылка: ящик кода есть"
    s.quorum_tick()
    after = s.reserve_keys()
    assert s.reserve_quorum_keys_on(), "миграция не создала ящик кода"
    assert int(after["m_gen"]) == int(before["m_gen"]) + 1 and after["m"] != before["m"], "M прежний после миграции"
    assert "a" not in after, "наклон прямой M пережил миграцию"
    why = s.reserve_code_notify_pending()
    assert why, "новый код админу (Т-РУ14) не поставлен"
    code = s.reserve_code()
    s.reserve_code_notify_clear()                             # такт планировщика отправил код
    for _ in range(3):
        s.quorum_tick()
    assert int(s.reserve_keys()["m_gen"]) == int(after["m_gen"]) and s.reserve_code() == code, \
        "миграция сработала второй раз — код в сейфе админа перестал действовать"
    assert s.reserve_code_notify_pending() == "", "Т-РУ14 поставлен второй раз"
    body = _fresh_qkeys(s)
    m, _k = rsvopen.recover_keys({"code": code, "cpoint": [body["x"], body["cy"]], "cbox": body["cbox"]}, _blob(s))
    assert m.hex() == s.reserve_keys()["m"], "новый код после миграции не открывает ящик"


def test_a_fresh_pair_is_not_migrated(line):
    """Пара 4.2.0 (ящик кода с сопряжения): такт не трогает ни `M`, ни код."""
    s = line.s
    k0, code0 = dict(s.reserve_keys()), s.reserve_code()
    s.reserve_keys_ensure()
    s.quorum_tick()
    assert s.reserve_keys()["m"] == k0["m"] and s.reserve_code() == code0, "миграция тронула свежую пару"
    assert s.reserve_code_notify_pending() == ""


def test_without_a_pair_the_migration_does_nothing(services, rsv):
    """Пары нет (ключи остались от снятой): ни ящика кода, ни нового кода админу."""
    services.rsv_put("rsv_keys", {"m": "ab" * 16, "m_gen": 2, "k": "cd" * 32, "k_gen": 1, "a": "ef" * 16})
    services.reserve_keys_ensure()
    assert not services.reserve_quorum_keys_on() and services.reserve_code_notify_pending() == ""
    assert services.reserve_keys()["m_gen"] == 2


# ── копия базы ──────────────────────────────────────────────────────────────

def test_a_backup_does_not_carry_the_code_box_and_the_restored_server_gets_a_new_code(line, tmp_path):
    """Копия основы — без `rsv_qkeys` (прямая кода и коэффициенты остаются только на
    живом сервере); восстановленная база с парой первым тактом получает свою прямую,
    новое поколение `M` и новый код админу — прежний код из копии не восстановить."""
    s = line.s
    code0, m0 = s.reserve_code(), s.reserve_keys()["m"]
    raw = s.db_snapshot_bytes(s.db.path)
    path = tmp_path / "restored.db"
    path.write_bytes(raw)
    rdb = Database(str(path))
    try:
        rdb.init_schema()
        restored = Services(rdb)
        assert restored.rsv_get("rsv_qkeys") == {}, "ящик кода и прямая Mc доехали в копию"
        assert restored.reserve_paired() and restored.reserve_keys()["m"] == m0, "предпосылка: пара и M в копии"
        restored.reserve_keys_ensure()                        # первым тактом кворума (quorum_tick)
        assert restored.reserve_quorum_keys_on(), "восстановленный сервер без ящика кода"
        assert restored.reserve_code() and restored.reserve_code() != code0, "восстановленный сервер со старым кодом"
        assert restored.reserve_keys()["m"] != m0, "M из копии не сменён — второй сервер держит ящик под него"
        assert restored.reserve_code_notify_pending(), "новый код админу после восстановления не поставлен"
    finally:
        rdb.close()
