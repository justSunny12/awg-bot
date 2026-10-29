"""Хвост старой эпохи и передачи эпохи — правки по ревью 4.1.0 на паре из трёх баз
(основа, резерв, бот основы на резерве — мир tests/integration/_reserve_jrn_duo.py,
сценарии аварии — tests/integration/test_reserve_jrn_tail_pair.py).

- свой хвост при держателе дома переносится тактом (`reserve_tail_settle`):
  «вернуть без резерва», резерв вернулся пустым, — правки последних секунд перед
  аварией снова в базе, ключи устройств на месте, Т-Ж5 «после возврата»;
  неудача — повтор раз в минуту, после пяти подряд — админу один раз;
- точка гостя дальше моих номеров (база потеряла хвост при пропаже питания
  после отправки) — применяю с того, что база имеет, недостающее резерв отдаёт
  сам из хранилища её событий; базы сходятся;
- возврат по сроку без правок гостя — строкой в Т-РУ5б;
- после перебазирования — пересчёт сроков: продление из хвоста поверх истечения
  у держателя снова активирует подписку и снимает EXPIRY;
- перебазированный держателем мой хвост возвращается его событиями — в сводке
  Т-РУ5б он не «сделан на резерве»; правки гостя в С9 — в сводку и «проверь
  глазами»; «продления» — только рост срока, по профилю раз;
- возврат без реплики на журнальном пути — сводка эпохи, а не «не вольются»;
- разрыв журнала при открытии гостя — админу резерва, в st.jrn.keep; основа
  принимает точку гостя и говорит о своих решениях под его в Т-РУ5б;
- эпоха, взятая у бота на резерве без его released (срыв, зависший вебхук),
  — его правки после точки одним путём С9 (rsv_c9 → reserve_c9_tail_in);
- «вернуть без резерва» при журнале говорит о хвосте; кнопка возврата у гостя —
  только когда мой хвост перенесён.

Ключи — заглушки (`DUMMY`), K — настоящие 32 байта.

Цена ошибки: хвост, оставшийся откачённым после «вернуть без резерва», —
правки людей за секунды до аварии пропадают из базы основы навсегда, а при
следующем переезде старый хвост шлётся и отвергается вместе с новым; точка
гостя, отброшенная как «вне номеров», — следование встаёт намертво (jneed по
кругу), Т-Ж6 навсегда; потеря по сроку без строки — админ не узнаёт о правках,
которые не доехали; продление, перебазированное без пересчёта, — платящий
пользователь заблокирован; свой хвост в сводке «с резерва» — админ ищет
правки, которых на резерве никто не делал; разрыв журнала без слова — решения
гостя ложатся поверх решений основы молча; С9 двумя механизмами — правки
гостя после взятия эпохи теряются на одном из путей.
"""
from __future__ import annotations

import sqlite3
import types

import pytest

from awgbot.domain.services.reserve.journal import tail as tailmod
from awgbot.infra import awg
from awgbot.util import timeutil

from tests import jrnequiv
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import JCAPS, Duo, checked, sends, to_guest
from tests.integration.test_reserve_jrn_tail_pair import (_base, _crash, _force_home, _guest_events_to_main,
                                                         _guest_rename_ivan, _jtail_parts, _names, _rename_ivan,
                                                         _return_to_done, _tail_vera, _to_guest, _to_main)

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    monkeypatch.setattr(tailmod, "time", types.SimpleNamespace(monotonic=d.clock.monotonic))
    yield d
    d.close()


def _diff(duo) -> list[str]:
    return jrnequiv.diff(duo.a.db._connection(), duo.gdb._connection())


def _priv(db, dev_id: int) -> str:
    return str(db._connection().execute("SELECT private_key FROM devices WHERE id = ?", (dev_id,)).fetchone()[0] or "")


# ── A3: свой хвост при держателе дома — переносится тактом ──────────────────

def _tail_left_behind(duo) -> dict:
    """Авария: основа завела Веру (до резерва не дошла), бот на резерве открылся, основа
    следует и отправила свой хвост — ответа нет; резерв лёг; «вернуть без резерва»."""
    ids = _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    _jtail_parts(duo)
    assert "Вера" not in _names(duo.a.db) and duo.a.db.jrn_tail_rows(), "условие: свой хвост откачен и ждёт"
    _force_home(duo)
    return ids


def test_after_force_home_and_an_empty_reserve_the_own_tail_is_moved_here(duo):
    """«Вернуть без резерва», резерв вышел на связь пустым (_forced_no_merge): свой хвост
    переносится тем же тактом — Вера снова в базе с закрытым ключом устройства, хвоста
    и отложенных ключей нет, Т-Ж5 «перенесены после возврата»."""
    ids = _tail_left_behind(duo)
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": duo.b.reserve_keep_view()}, **{"in": "idle"})
    acts = duo.a.reserve_out_tick()
    assert duo.a.reserve_out() == "home" and not duo.a.rsv_get("rsv_out").get("await_merge"), acts
    assert "Вера" in _names(duo.a.db), f"свой хвост остался откачен после возврата без резерва: {acts}"
    assert duo.a.db.jrn_tail_rows(("wait", "hold")) == [], "хвост остался — уйдёт при следующем переезде"
    assert duo.a.rsv_get("rsv_tail") == {}
    assert (duo.a.db.get_state("rsv_tail_keys") or "") == "", "отложенные ключи хвоста не сняты"
    assert _priv(duo.a.db, ids["vera_dev"]) == "DUMMY-priv-vera", "закрытый ключ устройства из хвоста не вернулся"
    texts = [n.text for n in rm.notices(acts)]
    tj5 = [t for t in texts if t.startswith("ℹ️ Изменения, сделанные здесь перед переездом на «Beta», перенесены после возврата: ")]
    assert len(tj5) == 1, texts
    assert rm.notices(duo.a.reserve_out_tick()) == [], "Т-Ж5 повторилось на следующем такте"


def test_after_the_stuck_webhook_was_taken_the_own_tail_is_moved_here(duo):
    """Тот же откаченный хвост, эпоха взята снятием вебхука (reserve_stuck_home без
    «вернуть без резерва»): такт дома переносит хвост сам."""
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    _jtail_parts(duo)
    duo.a.reserve_stuck_home()
    duo.a.reserve_epoch_sync()
    m = duo.a.db.jrn_meta()
    assert (m["holder"], m["role"]) == ("self", "holder"), m
    acts = duo.a.reserve_out_tick()
    assert "Вера" in _names(duo.a.db), f"хвост после снятого вебхука не перенесён: {acts}"
    assert not duo.a.db.jrn_tail_rows()


def test_the_own_tail_is_not_moved_here_while_following(duo):
    """Пока основа следует (держатель — бот на резерве), хвост ждёт ответа держателя:
    перенос здесь лёг бы поверх событий, которых здесь нет."""
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    _jtail_parts(duo)
    duo.clock.advance(600)
    assert duo.a.reserve_tail_settle() == []
    assert duo.a.db.jrn_tail_rows() and "Вера" not in _names(duo.a.db)


def test_a_failing_own_tail_move_is_retried_each_minute_and_told_once_after_five(duo, monkeypatch):
    """Сбой базы при переносе: хвост цел, повтор не чаще раза в минуту, после пяти
    неудач подряд — админу один раз; потом перенос проходит — Т-Ж5."""
    _tail_left_behind(duo)
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": duo.b.reserve_keep_view()}, **{"in": "idle"})
    real = duo.a.db.jrn_rebase
    calls = []

    def locked(evs, before_commit=None):
        calls.append(len(evs))
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(duo.a.db, "jrn_rebase", locked)
    told = []
    for n in range(7):
        told += rm.notices(duo.a.reserve_out_tick())
        told += rm.notices(duo.a.reserve_out_tick())     # второй такт в ту же минуту — без новой попытки
        assert len(calls) == n + 1, f"перенос повторён не раз в минуту: {len(calls)} попыток за {n + 1} мин"
        assert duo.a.db.jrn_tail_rows(), "хвост снят при сбое переноса"
        duo.clock.advance(61)
    failed = [t for t in (n.text for n in told) if t.startswith("⚠️ Изменения, сделанные здесь перед переездом на «Beta», пока не перенесены")]
    assert len(failed) == 1 and "database is locked" in failed[0], [n.text for n in told]
    assert "journalctl -u awg-bot" in failed[0], failed[0]
    monkeypatch.setattr(duo.a.db, "jrn_rebase", real)
    ok = [n.text for n in rm.notices(duo.a.reserve_out_tick())]
    assert "Вера" in _names(duo.a.db) and any("перенесены после возврата" in t for t in ok), ok


# ── A4: точка гостя дальше моих номеров — пропажа питания после отправки ────

def _power_loss_crash(duo) -> dict:
    """Основа завела Анну (до сброса WAL — переживёт, подтверждения ещё нет), затем
    Петра и Павла; резерв всё сохранил и подтвердил, а питание у основы пропало раньше
    сброса WAL: после подъёма Анна есть, но не подтверждена, Петра и Павла нет — ни
    строк, ни событий, sqlite_sequence — как до них. Бот на резерве открылся по журналу
    (у него есть всё) и завёл Ольгу; основа уходит в следование."""
    import sqlite3 as sq
    ids = _base(duo)
    duo.a.db.create_client("Анна", 1, _START, _FUTURE, "INV-DUMMY-A")
    x = duo.a.db.jrn_last_i()
    ids["acked"] = int(duo.a.db.jrn_meta()["acked"])
    assert ids["acked"] < x, "условие: Анна не подтверждена"
    saved = sq.connect(":memory:")
    duo.a.db._connection().backup(saved)                 # что переживёт пропажу питания
    for name in ("Пётр", "Павел"):
        duo.a.db.create_client(name, 1, _START, _FUTURE, f"INV-DUMMY-{name}")
    duo.tick()
    top = duo.a.db.jrn_last_i()
    assert duo.b.reserve_keep_view()["last"] == top > x, "условие: резерв сохранил события, которых не станет"
    saved.backup(duo.a.db._connection())                  # питание пропало: суффикс WAL потерян
    saved.close()
    assert duo.a.db.jrn_last_i() == x and "Пётр" not in _names(duo.a.db), "условие: база основы — как до Петра"
    assert int(duo.a.db.jrn_meta()["acked"]) == ids["acked"] and "Анна" in _names(duo.a.db)
    duo.open_guest(button=False)
    with duo.as_guest():
        assert duo.gdb.jrn_meta()["fork_i"] == top, "гость открылся не по всем событиям у резерва"
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
    from tests.integration.test_reserve_jrn_tail_pair import _follow_after_crash
    ids.update(x=x, top=top, gm=_follow_after_crash(duo))
    return ids


def test_a_guest_fork_beyond_my_numbers_applies_from_what_the_base_has(duo):
    """Точка гостя дальше моих номеров: применённое — то, что база правда имеет (Анна в
    ней есть), а не подтверждённое резервом: иначе свои же события Анны пришли бы
    повторно поверх её строк."""
    ids = _power_loss_crash(duo)
    m = duo.a.db.jrn_meta()
    assert (m["role"], m["holder"]) == ("follower", "peer"), m
    assert int(m["applied"]) == ids["x"], f"применённое не по базе: {m['applied']} при последнем {ids['x']}"


def test_the_events_the_primary_lost_come_back_from_the_reserve_store_and_the_bases_converge(duo):
    """Первая пачка гостя — с номера после его точки: основе не хватает своих же событий,
    jneed идёт резерву, он отдаёт их из хранилища её линии (а не гостю, у которого их
    в журнале нет — прежде jneed шёл по кругу навсегда); затем — события гостя."""
    ids = _power_loss_crash(duo)
    with duo.as_guest():
        msgs = duo.g.guest_jrn_tick()
    at_a = _to_main(duo, msgs)
    assert sends(at_a) == [("jneed", {"line": duo.line, "from": ids["x"] + 1})], at_a
    at_b = duo.a_to_b(at_a)
    assert not to_guest(at_b, "jneed"), f"просьба ушла гостю — у него этих событий нет: {at_b}"
    served = [checked("jev", b) for k, b in sends(at_b) if k == "jev"]
    assert served and served[0]["from"] == ids["x"] + 1, f"резерв не отдал события основы из хранилища: {at_b}"
    duo.b_to_a(at_b)
    assert {"Пётр", "Павел"} <= _names(duo.a.db), "потерянные при пропаже питания правки не вернулись"
    assert len([c for c in duo.a.db.list_clients() if c.name == "Анна"]) == 1, "свои события применены второй раз"
    duo.clock.advance(25)                                 # повтор гостя с подтверждённого
    _guest_events_to_main(duo)
    assert "Ольга" in _names(duo.a.db), "правки гостя после точки не применены"
    assert _diff(duo) == [], "базы основы и гостя разошлись"
    assert int(duo.a.db.jrn_meta()["applied"]) == duo.gdb.jrn_last_i()


def test_a_jneed_beyond_the_reserve_store_goes_to_the_guest_as_before(duo):
    """Хранилище резерва не начинается с просимого номера (разрыв не прикрывать) — просьба
    уходит гостю, как прежде."""
    ids = _power_loss_crash(duo)
    at_b = duo.b.reserve_jrn_in("jneed", {"line": duo.line, "from": ids["top"] + 50})
    assert to_guest(at_b, "jneed") and not sends(at_b, "jev"), at_b


def test_a_return_that_never_catches_up_says_so_in_the_return_report(duo, monkeypatch):
    """Возврат по сроку без недостающих правок бота на резерве: эпоха здесь, а в Т-РУ5б —
    строка, сколько не дошло (прежде — только строка журнала)."""
    from awgbot.domain.notices import reserve_tail as rn
    from awgbot.domain.services.reserve.outbound import RELEASED_WAIT_S
    from tests.integration.test_reserve_jrn_follow_pair import _return_lagging
    body, _acts = _return_lagging(duo, monkeypatch)
    applied = int(duo.a.db.jrn_meta()["applied"])
    duo.clock.advance(RELEASED_WAIT_S + 1)
    acts = duo.a.reserve_jrn_back_tick(duo.a.rsv_get("rsv_out"))
    assert ("take_tg",) in acts, acts
    lost = duo.a.rsv_get("rsv_out")["merge_report"]["lost"]
    want = rn.jrn_lost("Beta", applied + 1, int(body["last"]))
    assert lost.count(want) == 1, f"в Т-РУ5б нет строки о недошедших правках: {lost}"
    assert want.startswith("⚠️ ") and "проверь глазами" in want and not want.endswith("."), want
    assert duo.a.rsv_get("rsv_out").get("jrn_lost") is None, "отметка потери не снята — строка придёт снова"


def test_a_return_that_catches_up_has_no_lost_line(duo, monkeypatch):
    from tests.integration.test_reserve_jrn_follow_pair import _return_lagging
    _body, acts = _return_lagging(duo, monkeypatch)
    duo.b_to_a(duo.a_to_b(acts))                          # резерв дослал с диска
    lost = duo.a.rsv_get("rsv_out")["merge_report"]["lost"]
    assert not [t for t in lost if "до возврата" in t], lost


# ── A9: пересчёт сроков после перебазирования ───────────────────────────────

_PAST = "2026-01-02T00:00:00+03:00"


def _expired_world(duo, tail) -> dict:
    """Срок Ивана уже вышел (подтверждено резервом, истечение ещё не прошло); основа
    правит Ивана (`tail`) и падает до подтверждения; бот на резерве открылся и истёк
    Ивана (EXPIRY профилю и устройству); основа следует."""
    from tests.integration.test_reserve_jrn_tail_pair import _follow_after_crash
    ids = _base(duo)
    duo.a.db.update_client_fields(ids["ivan"], activation_status="active", period_end=_PAST)
    duo.tick()
    assert duo.a.db.jrn_meta()["acked"] == duo.a.db.jrn_last_i()
    tail(duo.a.db, ids)
    duo.open_guest(button=False)
    with duo.as_guest():
        duo.g.check_expiry()
        assert duo.g.db.get_client(ids["ivan"]).status == "expired", "условие: гость истёк Ивана"
    ids["gm"] = _follow_after_crash(duo)
    return ids


def _masks(db, cid: int) -> tuple[int, list[int]]:
    return int(db.get_client(cid).block_reason), [int(d.block_reason) for d in db.list_devices(cid)]


def test_an_extension_in_the_tail_over_an_expiry_at_the_holder_reactivates_the_subscription(duo):
    """Продление из хвоста (срок) легло поверх истечения у держателя (статус и биты) —
    колонки разные, спора нет; после переноса подписка снова активна и EXPIRY снят с
    профиля и устройств — у гостя и у основы, когда события вернутся."""
    from awgbot.core.blocks import ClientBlock, DeviceBlock
    ids = _expired_world(duo, lambda db, i: db.update_client_fields(i["ivan"], period_end=_FUTURE))
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    with duo.as_guest():
        cl = duo.g.db.get_client(ids["ivan"])
        cmask, dmasks = _masks(duo.g.db, ids["ivan"])
    assert cl.period_end == _FUTURE and cl.status == "active", f"продление перенесено, а подписка {cl.status}"
    assert not cmask & int(ClientBlock.EXPIRY) and dmasks and not any(m & int(DeviceBlock.EXPIRY) for m in dmasks), \
        f"EXPIRY остался после продления: профиль {cmask}, устройства {dmasks}"
    _guest_events_to_main(duo)
    assert duo.a.db.get_client(ids["ivan"]).status == "active", "у основы подписка осталась истёкшей"
    assert _diff(duo) == []


def test_a_tail_without_an_extension_keeps_an_expired_subscription_expired(duo):
    """Пересчёт — только по итогу: срок в прошлом — истечение держателя остаётся."""
    from awgbot.core.blocks import ClientBlock
    ids = _expired_world(duo, lambda db, i: db.update_client_fields(i["ivan"], name="Иван здесь"))
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    with duo.as_guest():
        cl = duo.g.db.get_client(ids["ivan"])
        cmask, _ = _masks(duo.g.db, ids["ivan"])
    assert cl.status == "expired" and cmask & int(ClientBlock.EXPIRY), f"истёкшая подписка ожила без продления: {cl.status}"


# ── A12: мой хвост в сводке — не «сделан на резерве»; С9 и продления ────────

def test_the_own_tail_rebased_by_the_holder_is_not_counted_as_made_on_the_reserve(duo):
    """Вера — моя правка перед аварией: перебазированная гостем, она возвращается его
    событиями. В сводке Т-РУ5б — не новый профиль и устройство «с резерва», а пир её
    устройства всё равно снова на интерфейсе."""
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    skip = duo.a.rsv_get("rsv_out").get("tail_skip")
    assert isinstance(skip, list) and len(skip) == 2 and skip[0] <= skip[1], f"диапазон перенесённого не пришёл: {skip}"
    notes = _return_to_done(duo)
    assert "Вера" in _names(duo.a.db), "перенесённая вставка не вернулась"
    head = notes[0].text
    assert head.startswith("↩️ Пользователи и бот снова здесь") and "без новых профилей и устройств" in head, \
        f"Т-РУ5б называет свой же хвост правками на резерве: {head}"
    assert "DUMMY-pub-vera" in duo.fake.peers, "пир перенесённого устройства не вернулся"


def test_the_guests_own_edits_after_my_tail_are_still_counted(duo):
    """Диапазон — только порождённое переносом: правка гостя после него в сводке есть."""
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
    _guest_events_to_main(duo)
    rep = duo.a.rsv_get("rsv_out")["jrn_report"]
    assert rep["clients"] == 1, f"правка гостя после хвоста не в сводке: {rep}"


def _following_world(duo) -> dict:
    ids = _base(duo)
    duo.a.db.update_client_fields(ids["ivan"], tg_id=7100)
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following()
    return ids


def test_extensions_count_only_a_growing_term_and_once_per_profile(duo):
    """«Продления» в Т-РУ5б — рост срока, по профилю раз: два продления Ивана — одно;
    сокращение срока Веры и правка без срока — не продления."""
    ids = _following_world(duo)
    with duo.as_guest():
        vera = duo.g.db.create_client("Вера", 1, _START, "2099-06-01T00:00:00+03:00", "INV-DUMMY-V")
        duo.g.guest_jrn_tick()
    _guest_events_to_main(duo)
    with duo.as_guest():
        duo.g.db.update_client_fields(ids["ivan"], period_end="2100-01-01T00:00:00+03:00")
        duo.g.db.update_client_fields(ids["ivan"], period_end="2100-06-01T00:00:00+03:00")
        duo.g.db.update_client_fields(vera, period_end="2098-01-01T00:00:00+03:00")
        duo.g.db.update_client_fields(ids["ivan"], name="Иван Петров")
    duo.clock.advance(25)
    _guest_events_to_main(duo)
    rep = duo.a.rsv_get("rsv_out")["jrn_report"]
    assert rep["extensions"] == 1, f"продления посчитаны не по росту срока и не по профилю: {rep}"


def test_c9_edits_of_the_reserve_go_to_the_report_with_the_check_lines(duo):
    """С9: правки бота на резерве после взятия эпохи — хвостом у основы, и в сводку
    Т-РУ5б, со строкой «проверь глазами» о смене Telegram (как при следовании)."""
    ids = _following_world(duo)
    _force_home(duo)
    with duo.as_guest():
        duo.g.db.update_client_fields(ids["ivan"], tg_id=7999)
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        msgs = duo.g.guest_jrn_tick()
    at_a = _to_main(duo, msgs)
    assert sends(at_a, "jack"), at_a
    rep = duo.a.rsv_get("rsv_out").get("jrn_report") or {}
    assert rep.get("clients") == 1, f"правки С9 не в сводке: {rep}"
    assert any("сменён Telegram-аккаунт" in t and "7100 → 7999" in t for t in rep.get("lost") or []), \
        f"нет строки «проверь глазами» о смене Telegram в С9: {rep.get('lost')}"


# ── A16: возврат без реплики на журнальном пути ─────────────────────────────

def _returning_without_a_guest(duo) -> None:
    """Возврат начат (одним шагом, как reserve_return_start), гость на stop молчит —
    бот резерва отдаёт пустой released."""
    duo.a.reserve_set_out("returning", kind="dead", gen=7, step="back_req")
    assert duo.a.reserve_following(), "условие: на back_req основа ещё следует"


def test_a_return_without_a_replica_on_the_journal_path_keeps_the_epoch_report(duo):
    """Всё подтверждённое уже применено следованием: Т-РУ5б — сводка эпохи (Ольга —
    новый профиль), без строки «изменения не вольются»."""
    from awgbot.domain.notices import reserve as rn
    _following_world(duo)
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
    _guest_events_to_main(duo)
    _returning_without_a_guest(duo)
    acts = duo.a.reserve_released_in(b"")
    assert ("take_tg",) in acts, acts
    rep = duo.a.rsv_get("rsv_out")["merge_report"]
    assert rep.get("clients") == 1, f"сводка эпохи выброшена при возврате без реплики: {rep}"
    assert rn.returned_without_replica("Beta") not in rep.get("lost", []), rep
    assert not [t for t in rep.get("lost", []) if "могли не дойти" in t], f"строка о недошедшем без повода: {rep}"
    assert "extended" not in rep and duo.a.rsv_get("rsv_out").get("jrn_report") is None, rep


def test_a_return_without_a_replica_says_the_last_guest_edits_may_be_missing(duo):
    """Гость присылал больше, чем применено (пачка с разрывом, досылки не было), и ушёл:
    строка Т-РУ5б «последние правки там могли не дойти» — один раз."""
    from awgbot.domain.notices import reserve_tail as rn
    _following_world(duo)
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        duo.g.guest_jrn_tick()                            # потерялось по дороге
        duo.g.db.create_client("Пётр", 1, _START, _FUTURE, "INV-DUMMY-P")
        msgs = duo.g.guest_jrn_tick()                     # только новое — после потерянного
    at_a = _to_main(duo, msgs)
    assert sends(at_a, "jneed") and "Пётр" not in _names(duo.a.db), f"условие: разрыв, не применено: {at_a}"
    _returning_without_a_guest(duo)
    duo.a.reserve_released_in(b"")
    lost = duo.a.rsv_get("rsv_out")["merge_report"]["lost"]
    assert lost.count(rn.returned_without_guest_tail("Beta")) == 1, lost


# ── A22: разрыв журнала при открытии гостя ──────────────────────────────────

def _drop_records(store, numbers: set[int]) -> None:
    from awgbot.util.rsvjrn import event
    recs = store.events()
    store.wipe()
    store.append_events([r for r in recs if event.head(r).n not in numbers])


def _gap_world(duo) -> dict:
    """У резерва в хранилище основы недостаёт записей Анны (порча): гость открылся по
    событиям до неё — без Анны и Бориса; основа их подтвердила давно."""
    ids = _base(duo)
    x0 = duo.a.db.jrn_last_i()
    duo.a.db.create_client("Анна", 1, _START, _FUTURE, "INV-DUMMY-A")
    xa = duo.a.db.jrn_last_i()
    duo.tick()
    duo.a.db.create_client("Борис", 1, _START, _FUTURE, "INV-DUMMY-B")
    duo.tick()
    assert int(duo.a.db.jrn_meta()["acked"]) == duo.a.db.jrn_last_i()
    _drop_records(duo.b_keep, set(range(x0 + 1, xa + 1)))
    res = duo.open_guest(button=False)
    assert res.gap == x0 + 1 and res.fork == x0, f"условие: открытие с разрывом на {x0 + 1}: {res}"
    ids.update(x0=x0, xa=xa, acked=int(duo.a.db.jrn_meta()["acked"]), res=res)
    return ids


def test_an_open_gap_is_told_to_the_reserve_admin_once_and_goes_to_the_keep_view(duo, monkeypatch):
    """Процесс открытия сказал о разрыве (jgap в open.status) — админу резерва один раз,
    основе — в st.jrn.keep."""
    from awgbot.infra import rsvzone
    duo.journal_on()
    duo.b.rsv_update("rsv_in", state="taking", kind="dead", gen=3)
    monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "clone_up", "jgap": 12, "jlost": 1})
    monkeypatch.setattr(rsvzone, "state", lambda *a: {})
    first = duo.b._tick_taking(duo.b._inn())              # такт «пользователи основы у меня», шаг taking
    told = [n.text for n in rm.notices(first)]
    name = duo.b.reserve_peer_name()
    assert len(told) == 1 and told[0].startswith(f"⚠️ База бота «{name}» собрана не по всему журналу"), told
    assert "не применено изменений: 1" in told[0] and not told[0].endswith("."), told[0]
    assert ("send", "up", {}) in first, first
    assert rm.notices(duo.b._tick_taking(duo.b._inn())) == [], "о разрыве сказано второй раз"
    assert duo.b.reserve_keep_view().get("gap") == 12, duo.b.reserve_keep_view()


def test_an_open_without_a_gap_tells_nothing(duo, monkeypatch):
    from awgbot.infra import rsvzone
    duo.journal_on()
    duo.b.rsv_update("rsv_in", state="taking", kind="dead", gen=3)
    monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "clone_up", "jgap": 0, "jlost": 0})
    monkeypatch.setattr(rsvzone, "state", lambda *a: {})
    acts = duo.b._tick_taking(duo.b._inn())
    assert ("send", "up", {}) in acts and rm.notices(acts) == [], acts
    assert "gap" not in duo.b.reserve_keep_view()


def test_the_open_gap_notice_counts_the_unread_records(duo, monkeypatch):
    """«не прочитано записей: N» — число записей, которые гость не взял (с первой
    недостающей по последнюю у резерва), а не номер первой недостающей."""
    from awgbot.domain.notices import reserve_tail as rn
    from awgbot.util.rsvjrn import opener
    from tools import rsvopen
    ids = _gap_world(duo)
    last = duo.b_keep.last()[1]
    monkeypatch.setattr(opener, "build", lambda *a, **k: ids["res"])
    jinfo = rsvopen._journal(duo.a.reserve_k(), {}, button=False)
    text = rn.open_gap("Alpha", jinfo["jgap"], jinfo["jlost"])
    unread = last - ids["x0"]
    assert f"не прочитано записей: {unread}" in text, \
        f"у резерва записи {ids['x0'] + 1}…{last} не взяты ({unread}), а в тексте: {text}"


def test_with_a_gap_the_primary_takes_the_guest_fork_and_tells_about_its_own_decisions(duo):
    """Гость открылся с разрывом: основа принимает его точку (раньше подтверждённого) —
    его правки после неё применяются, а о своих решениях, которых он не видел, — строка
    Т-РУ5б; без отметки разрыва точка раньше подтверждённого отвергается, как прежде."""
    from awgbot.domain.notices import reserve_tail as rn
    from tests.integration.test_reserve_jrn_tail_pair import _follow_after_crash
    ids = _gap_world(duo)
    duo.b.rsv_update("rsv_in", jgap=int(ids["res"].gap))
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
    _follow_after_crash(duo)
    m = duo.a.db.jrn_meta()
    assert int(m["applied"]) == ids["x0"], f"точка гостя с разрывом не принята: {m}"
    assert duo.a.rsv_get("rsv_out").get("jrn_lost_mine") == [ids["x0"] + 1, ids["acked"]]
    _guest_events_to_main(duo)
    assert "Ольга" in _names(duo.a.db), "правки гостя после его точки не применены"
    acts = duo.a._jrn_back_take(int(duo.gdb.jrn_meta()["epoch"]), duo.gdb.jrn_last_i())
    assert ("take_tg",) in acts
    lost = duo.a.rsv_get("rsv_out")["merge_report"]["lost"]
    want = rn.jrn_mine_lost("Beta", ids["x0"] + 1, ids["acked"])
    assert lost.count(want) == 1, lost
    assert duo.a.rsv_get("rsv_out").get("jrn_lost_mine") is None


def test_without_a_gap_mark_a_fork_before_the_acked_is_refused_as_before(duo):
    from tests.integration.test_reserve_jrn_tail_pair import _follow_after_crash
    ids = _gap_world(duo)
    _follow_after_crash(duo)
    m = duo.a.db.jrn_meta()
    assert int(m["applied"]) == ids["acked"], m
    assert duo.a.rsv_get("rsv_out").get("jrn_lost_mine") is None


# ── F4-6: эпоха взята без released — правки гостя одним путём С9 ────────────

def test_taking_the_epoch_from_a_live_guest_without_released_starts_c9_and_its_edits_are_rebased(duo):
    """Зависший вебхук снят (reserve_stuck_home без «вернуть без резерва»; линк мёртв
    дольше 30 минут — гость на резерве мог жить и писать): эпоха здесь, его правки после
    точки взятия — хвостом С9 при встрече (перенос при получении, jack), а не отказом
    jstale и не молчанием."""
    ids = _following_world(duo)
    duo.a.rsv_update("rsv_peer_state", down_since=timeutil.to_iso(timeutil.shift(timeutil.now(), -31 * 60)))
    guest_e = int(duo.a.db.jrn_meta()["epoch"])
    duo.a.reserve_stuck_home()
    duo.a.reserve_epoch_sync()
    m = duo.a.db.jrn_meta()
    assert (m["holder"], m["role"]) == ("self", "holder"), m
    c9 = duo.a.rsv_get("rsv_c9")
    assert (c9.get("e"), c9.get("F")) == (guest_e, m["fork_i"]), f"С9 не заведён при взятии эпохи без released: {c9}"
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        duo.g.db.update_client_fields(ids["ivan"], name="Иван на резерве")
        msgs = duo.g.guest_jrn_tick()
    at_a = _to_main(duo, msgs)
    assert [k for k, _ in sends(at_a)] == ["jack"], f"правки гостя не приняты хвостом С9: {at_a}"
    assert {"Ольга", "Иван на резерве"} <= _names(duo.a.db), _names(duo.a.db)
    assert int(duo.a.rsv_get("rsv_c9")["upto"]) == duo.gdb.jrn_last_i()


def test_taking_the_epoch_from_the_guest_twice_does_not_restart_c9(duo):
    """Повтор становления держателем (такт) не сбрасывает С9 и перенесённое."""
    _following_world(duo)
    duo.a.reserve_stuck_home()
    duo.a.reserve_epoch_sync()
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        msgs = duo.g.guest_jrn_tick()
    _to_main(duo, msgs)
    before = dict(duo.a.rsv_get("rsv_c9"))
    assert before.get("e"), "условие: С9 заведён"
    duo.a.reserve_epoch_sync()
    duo.a.reserve_epoch_check()
    assert duo.a.rsv_get("rsv_c9") == before, "повторный такт сбросил С9"


def test_a_holder_without_c9_still_answers_jstale_to_an_older_epoch(duo):
    """Без С9 (эпоха не от бота на резерве) события меньшей эпохи — jstale, как прежде."""
    _base(duo)
    duo.open_guest(button=False)
    with duo.as_guest():
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
        (kind, body), = [m for m in duo.g.guest_jrn_tick() if m[0] == "jev"]
    duo.a.db.jrn_pass(holder="self", role="holder", why="тест", epoch=int(body["e"]) + 3)
    assert duo.a.rsv_get("rsv_c9") == {}
    assert [k for k, _ in sends(duo.a.reserve_jrn_in("jev", checked(kind, body)))] == ["jstale"]


# ── тексты и кнопка ─────────────────────────────────────────────────────────

def test_force_home_with_the_journal_speaks_of_the_tail_not_of_a_replica(duo):
    """«Вернуть без резерва» при журнале: правки бота на резерве перенесутся хвостом,
    спорное останется здешним — без «всё вольётся»."""
    _following_world(duo)
    rm.automatic(duo.a)
    duo.a.db.set_state("last_server_ok", "0")
    duo.a.rsv_update("rsv_peer_state", session=False, down_since=timeutil.to_iso(timeutil.shift(timeutil.now(), -660)))
    duo.a.reserve_force_home()
    acts = duo.a.reserve_stuck_home()
    (text,) = [n.text for n in rm.notices(acts)]
    assert "перенесутся, когда он выйдет на связь; конфликтующие правки с «Beta» применены не будут" in text, text
    assert "вольётся" not in text and not text.endswith("."), text


def test_force_home_without_the_journal_keeps_the_merge_wording(mv):
    from tests.integration.test_reserve_jrn_tail_pair import _ago
    rm.automatic(mv.svc)
    rm.go_away(mv.svc, kind="dead")
    mv.svc.db.set_state("last_server_ok", "0")
    mv.svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(11))
    mv.svc.reserve_force_home()
    (text,) = [n.text for n in rm.notices(mv.svc.reserve_stuck_home())]
    assert "изменения с него применятся, когда он выйдет на связь" in text, text


def test_the_guest_return_button_waits_for_my_tail(duo):
    """Кнопка возврата у гостя (can_return в st) — только когда мой хвост перенесён:
    иначе возврат отказал бы «правки ещё переносятся»."""
    _crash(duo, tail=_rename_ivan, guest=_guest_rename_ivan)
    duo.a.rsv_update("rsv_out", can_return=True)
    assert duo.a.reserve_can_return(), "условие: по автомату вернуть можно"
    _jtail_parts(duo)
    assert duo.a.reserve_st_body()["can_return"] is False, "кнопка возврата при неперенесённом хвосте"
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    assert not duo.a.db.jrn_tail_rows()
    assert duo.a.reserve_st_body()["can_return"] is True, "хвост перенесён — кнопки возврата нет"


# ── откат пиров хвоста и точка из jstale ────────────────────────────────────

def test_undoing_the_tail_puts_the_peer_back_at_its_old_address_without_a_key_change(duo):
    """Хвост сменил адрес устройства (ключ тот же): откат в базе — и пир снова со старым
    адресом (прежде пир откатывался только при смене ключа и до возврата ходил с новым)."""
    state = {}

    def tail(db, ids):
        dev = [d for d in db.list_all_devices() if d.client_id == ids["ivan"]][0]
        state.update(pub=dev.public_key, old=dev.address)
        db.update_device_fields(dev.id, address="10.8.1.250")
        duo.fake.peers[dev.public_key] = ("DUMMY-psk", "10.8.1.250")
    _crash(duo, tail=tail, guest=lambda db, i: None)
    assert duo.fake.peers.get(state["pub"], ("", ""))[1] == state["old"], \
        f"пир откаченного устройства остался с адресом хвоста: {duo.fake.peers.get(state['pub'])}"


def test_a_jstale_fork_outside_my_numbers_is_replaced_by_the_acked(duo):
    """Точка гостя из st (тело st не под K) вне `acked ≤ F ≤ last + 1` — при jstale та же
    проверка, что при уходе в следование: применённое — подтверждённое."""
    _base(duo)
    acked = int(duo.a.db.jrn_meta()["acked"])
    keep = dict(duo.b.reserve_keep_view(), guest_e=5, guest_fork=acked + 500)
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": keep}, **{"in": "serving"})
    duo.a.reserve_jrn_in("jstale", {"line": duo.line, "e": int(duo.a.db.jrn_meta()["epoch"]) + 1})
    m = duo.a.db.jrn_meta()
    assert m["role"] == "follower" and int(m["applied"]) == acked, f"точка вне номеров принята: {m}"
