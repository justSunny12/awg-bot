"""
Свои списки, общие для всех шлюзов: правила слияния на ВПС, сверка файлов агента с базой ⊕ pending, правило применения канона и состояние для монитора.

Слияние — чистые функции `gwownlists`. Агент — настоящий `GatewayServices`
поверх временной БД; малина под ним — `_Host`: каталог dnsmasq.d с двумя
файлами своих списков, юнит обвязки и подставной `awg-lan-domain.sh` (list
читает файлы, sync пишет их и считается). Настоящий скрипт гоняется в
tests/unit/test_gw_lan_script.py и в интеграции.

Цена ошибки: канон, который затирает свежую правку, — домен «вернулся» или
пропал на всех шлюзах сразу; отсутствующий файл, принятый за удаление, —
пустые списки у всех квартир; правка, которую сервер счёл повтором, —
«ждут синхронизации» навсегда и отправка на каждом тике.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from awgbot.domain import gwownlists
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.util import timeutil

NOW = "2026-09-25T10:00:00+03:00"
VPS = "vps.example.net"
VPN_USER, RU_USER = "awg-gw-vpn-user.conf", "awg-gw-ru-user.conf"


def _merge(canon, events, upto=("", 0), run="r1", slot=1, deny=(VPS,)):
    return gwownlists.merge(canon, slot, run, events, list(upto), NOW, deny=deny)


def _kinds(canon) -> dict:
    return gwownlists.canon_items(canon)


# ── слияние на ВПС: правила слияния ─────────────────────────────────────────────

def test_a_new_domain_becomes_a_record_with_slot_and_time_and_bumps_ver():
    canon, upto, rej, changed = _merge({"gen": "g", "ver": 4, "items": {}}, [[1, "example.com", "vpn", False]], slot=2)
    assert changed and rej == []
    assert canon["items"] == {"example.com": ["vpn", 2, NOW]}, "запись канона — вид, слот, время ВПС"
    assert canon["ver"] == 5 and canon["gen"] == "g", "поколение не меняется, версия +1"
    assert upto == ["r1", 1]


def test_the_last_edit_that_arrived_wins_whatever_the_kind():
    canon = {"gen": "g", "ver": 1, "items": {"shop.ru": ["ru", 1, "t"]}}
    canon, _, _, changed = _merge(canon, [[1, "shop.ru", "vpn", False]], slot=2)
    assert changed and _kinds(canon) == {"shop.ru": "vpn"}, "обычная правка перебивает прежний вид"
    assert canon["items"]["shop.ru"][1] == 2, "автор записи — слот последней правки"
    assert canon["ver"] == 2


def test_the_same_kind_again_changes_nothing_and_keeps_ver():
    """Повтор того же вида — не изменение: иначе каждое нажатие «уже в
    списке» гнало бы новый канон на все шлюзы."""
    canon = {"gen": "g", "ver": 7, "items": {"shop.ru": ["ru", 1, "t"]}}
    canon, upto, _, changed = _merge(canon, [[1, "shop.ru", "ru", False]])
    assert not changed and canon["ver"] == 7
    assert canon["items"]["shop.ru"] == ["ru", 1, "t"], "запись переписана без изменения вида"
    assert upto == ["r1", 1], "событие разобрано — upto двигается и без изменения канона"


@pytest.mark.parametrize("have,event,expect,changed", [
    ("ru", "vpn", "ru", False),      # первое слияние: «напрямую» сильнее
    ("vpn", "ru", "ru", True),
    (None, "vpn", "vpn", True),
    ("vpn", "vpn", "vpn", False),
])
def test_an_init_edit_never_moves_direct_into_the_tunnel(have, event, expect, changed):
    """Первая синхронизация: спор вида решается в пользу «напрямую» — как в
    nft; ошибка в эту сторону видна сразу, в обратную — трафик молча в туннель."""
    items = {"shop.ru": [have, 1, "t"]} if have else {}
    canon, _, _, ch = _merge({"gen": "g", "ver": 1, "items": items}, [[1, "shop.ru", event, True]], slot=2)
    assert _kinds(canon) == {"shop.ru": expect}, f"init {event} поверх {have}"
    assert ch is changed and canon["ver"] == (2 if changed else 1)


def test_a_plain_tunnel_edit_does_move_a_direct_domain():
    canon = {"gen": "g", "ver": 1, "items": {"shop.ru": ["ru", 1, "t"]}}
    canon, *_ = _merge(canon, [[1, "shop.ru", "vpn", False]])
    assert _kinds(canon) == {"shop.ru": "vpn"}, "правило «ru сильнее» — только для init"


def test_delete_removes_the_record_or_does_nothing():
    canon = {"gen": "g", "ver": 3, "items": {"a.com": ["vpn", 1, "t"], "b.com": ["ru", 1, "t"]}}
    canon, _, _, changed = _merge(canon, [[1, "a.com", "del", False]])
    assert changed and _kinds(canon) == {"b.com": "ru"} and canon["ver"] == 4
    canon, _, _, changed = _merge(canon, [[2, "a.com", "del", False]])
    assert not changed and canon["ver"] == 4, "удаление отсутствующего — не изменение"


@pytest.mark.parametrize("bad", ["not a domain", "-x.com", "a..b.com", "пример.рф", "a.com\nvpn evil.com",
                                 "x" * 300 + ".com", ""])
def test_a_non_domain_is_refused_and_counted_as_processed(bad):
    canon, upto, rej, changed = _merge({"gen": "g", "ver": 1, "items": {}}, [[1, bad, "vpn", False]])
    assert not changed and _kinds(canon) == {}
    assert rej == [[bad[:64], "не домен"]], rej
    assert upto == ["r1", 1], "отвергнутое событие разобрано — повтор его не вернёт"


def test_a_kind_outside_the_list_is_refused():
    canon, _, rej, changed = _merge({"gen": "g", "ver": 1, "items": {}}, [[1, "a.com", "block", False]])
    assert not changed and rej == [["a.com", "не домен"]]


def test_the_domain_is_lowercased_before_everything():
    canon, _, rej, _ = _merge({"gen": "g", "ver": 1, "items": {}}, [[1, " Example.COM ", "VPN", False]])
    assert rej == [] and _kinds(canon) == {"example.com": "vpn"}


@pytest.mark.parametrize("d,denied", [(VPS, True), ("api." + VPS, True), ("VPS.Example.NET", True),
                                      ("x" + VPS, False), ("example.net", False)])
def test_the_server_host_and_its_subdomains_are_refused(d, denied):
    """Хост сервера в туннеле или «напрямую» — шлюз запирает себя: канал и
    аплинк идут на этот адрес. Соседний по суффиксу домен — не поддомен."""
    canon, _, rej, _ = _merge({"gen": "g", "ver": 1, "items": {}}, [[1, d, "ru", False]])
    if denied:
        assert rej == [[d.lower(), "хост сервера"]] and _kinds(canon) == {}, rej
    else:
        assert rej == [] and d.lower() in _kinds(canon)


def test_a_replay_of_the_same_run_is_skipped_and_a_new_run_starts_over():
    """Сессия оборвалась между own_ev и own_set — агент шлёт то же ещё раз.
    Разобранное не сливается второй раз: иначе домен, который после этого
    удалили на другом шлюзе, воскрес бы. Новая метка запуска — новый счёт."""
    canon = {"gen": "g", "ver": 1, "items": {}}
    canon, upto, _, _ = _merge(canon, [[1, "a.com", "vpn", False], [2, "b.com", "vpn", False]])
    assert upto == ["r1", 2]
    canon, _, _, _ = _merge(canon, [[1, "a.com", "del", False]], upto=("r2", 0), run="r2", slot=2)
    assert "a.com" not in _kinds(canon)
    ver = canon["ver"]
    canon, upto, _, changed = _merge(canon, [[1, "a.com", "vpn", False], [2, "b.com", "vpn", False]], upto=upto)
    assert not changed and canon["ver"] == ver and "a.com" not in _kinds(canon), "повтор воскресил удалённое"
    assert upto == ["r1", 2]
    # тот же run, номер дальше — новое
    canon, upto, _, changed = _merge(canon, [[2, "b.com", "ru", False], [3, "c.com", "vpn", False]], upto=upto)
    assert changed and _kinds(canon) == {"b.com": "vpn", "c.com": "vpn"}, "n=2 — повтор, n=3 — новое"
    assert upto == ["r1", 3]
    # перезапуск агента: новая метка с единицы
    canon, upto, _, changed = _merge(canon, [[1, "d.com", "ru", False]], upto=upto, run="r9")
    assert changed and "d.com" in _kinds(canon) and upto == ["r9", 1], "новая метка упёрлась в «уже было»"


def test_the_ceiling_refuses_new_domains_but_not_changes():
    items = {f"d{i}.com": ["vpn", 1, "t"] for i in range(gwownlists.MAX_DOMAINS)}
    canon = {"gen": "g", "ver": 1, "items": items}
    canon, _, rej, changed = _merge(canon, [[1, "new.com", "vpn", False], [2, "d1.com", "ru", False]])
    assert rej == [["new.com", "максимум 500 доменов"]], rej
    assert changed and _kinds(canon)["d1.com"] == "ru", "смена вида при полном каноне — не новый домен"
    assert len(canon["items"]) == 500
    canon, _, rej, _ = _merge(canon, [[3, "d2.com", "del", False], [4, "new.com", "vpn", False]])
    assert rej == [] and "new.com" in _kinds(canon), "место освободилось в том же пакете"


def test_ver_grows_once_per_changed_batch_and_not_at_all_otherwise():
    canon = {"gen": "g", "ver": 10, "items": {}}
    canon, *_ = _merge(canon, [[1, "a.com", "vpn", False], [2, "b.com", "ru", False], [3, "c.com", "vpn", False]])
    assert canon["ver"] == 11
    canon, *_ = _merge(canon, [[4, "a.com", "vpn", False], [5, "zz.com", "del", False], [6, "bad", "vpn", False]])
    assert canon["ver"] == 11, "пакет без изменений поднял версию — все шлюзы получат тот же канон заново"


def test_only_the_first_200_events_of_a_message_are_taken():
    events = [[i + 1, f"d{i}.com", "vpn", False] for i in range(gwownlists.MAX_EVENTS + 5)]
    canon, upto, _, _ = _merge({"gen": "g", "ver": 1, "items": {}}, events)
    assert len(canon["items"]) == 200 and upto == ["r1", 200]


def test_rejections_are_capped_and_junk_events_are_skipped():
    events = [[i + 1, f"bad_{i}", "vpn", False] for i in range(30)] + ["мусор", [None, "a.com", "vpn"], [99, "a.com"]]
    canon, upto, rej, changed = _merge({"gen": "g", "ver": 1, "items": {}}, events)
    assert len(rej) == gwownlists.REJ_KEEP and not changed and upto == ["r1", 30]


def test_merge_does_not_touch_the_canon_it_was_given():
    src = {"gen": "g", "ver": 1, "items": {"a.com": ["vpn", 1, "t"]}}
    before = json.dumps(src, sort_keys=True)
    _merge(src, [[1, "a.com", "del", False], [2, "b.com", "vpn", False]])
    assert json.dumps(src, sort_keys=True) == before, "слияние поменяло канон до записи в БД"


# ── чистка, отпечаток, разница ───────────────────────────────────────────────

def test_clean_keeps_domains_puts_both_kinds_to_direct_and_drops_junk():
    raw = [["a.com", "vpn"], ["shop.ru", "vpn"], ["shop.ru", "ru"], ["bank.ru", "ru"], ["bank.ru", "vpn"],
           ["bad_host", "vpn"], ["x.com", "block"], "мусор", ["Y.COM", "RU"], ["z.com\nvpn evil.com", "vpn"]]
    assert gwownlists.clean(raw) == {"a.com": "vpn", "shop.ru": "ru", "bank.ru": "ru", "y.com": "ru"}
    assert gwownlists.clean({"a.com": "vpn"}) == {"a.com": "vpn"}
    assert gwownlists.clean(None) == {}


def test_clean_caps_at_the_ceiling():
    many = [[f"d{i:04}.com", "vpn"] for i in range(gwownlists.MAX_DOMAINS + 10)]
    assert len(gwownlists.clean(many)) == gwownlists.MAX_DOMAINS


def test_the_digest_depends_on_content_and_upto_but_not_on_order():
    a = gwownlists.digest("g", 3, {"a.com": "vpn", "b.ru": "ru"}, ["r", 2])
    assert a == gwownlists.digest("g", 3, {"b.ru": "ru", "a.com": "vpn"}, ["r", 2])
    for other in (gwownlists.digest("g", 3, {"a.com": "vpn", "b.ru": "ru"}, ["r", 3]),
                  gwownlists.digest("g", 4, {"a.com": "vpn", "b.ru": "ru"}, ["r", 2]),
                  gwownlists.digest("h", 3, {"a.com": "vpn", "b.ru": "ru"}, ["r", 2]),
                  gwownlists.digest("g", 3, {"a.com": "ru", "b.ru": "ru"}, ["r", 2])):
        assert other != a
    assert len(a) == 64 and int(a, 16) >= 0


def test_diff_names_additions_removals_and_moves_in_alphabet_order():
    expected = {"a.com": "vpn", "b.com": "ru", "c.com": "vpn"}
    local = {"a.com": "vpn", "b.com": "vpn", "d.com": "ru"}
    assert gwownlists.diff(local, expected) == [("b.com", "vpn"), ("c.com", "del"), ("d.com", "ru")]
    assert gwownlists.diff(expected, dict(expected)) == []


def test_apply_events_goes_by_number_not_by_list_order():
    base = {"a.com": "vpn"}
    ev = [[3, "a.com", "del", False], [1, "a.com", "ru", False], [2, "b.com", "vpn", False]]
    assert gwownlists.apply_events(base, ev) == {"b.com": "vpn"}
    assert base == {"a.com": "vpn"}, "база изменена на месте"


def test_list_output_parses_and_render_writes_only_clean_lines():
    out = "vpn zeta.com\nru shop.ru\nvpn shop.ru\n  отступ\nvpn bad_host\nru Alpha.com\n"
    items = gwownlists.parse_list(out)
    assert items == {"zeta.com": "vpn", "shop.ru": "ru", "alpha.com": "ru"}
    assert gwownlists.render_sync(items) == "ru alpha.com\nru shop.ru\nvpn zeta.com\n"
    assert gwownlists.render_sync({"a.com\nvpn evil.com": "vpn", "b.com": "vpn"}) == "vpn b.com\n", \
        "перевод строки в домене с чужой машины дописал строку в файл для скрипта"
    assert gwownlists.render_sync({}) == ""
    assert gwownlists.counts({"a": "vpn", "b": "vpn", "c": "ru"}) == (2, 1)


def test_the_domain_rule_is_the_one_the_script_uses():
    """Правило домена на ВПС и в awg-lan-domain.sh — одно: общий список
    примеров из тестов скрипта даёт те же вердикты. Разойдись — домен,
    разосланный сервером, скрипт отвергал бы на каждом шлюзе."""
    from tests.unit.test_gw_lan_script import _DOMAIN_EXAMPLES
    wrong = [(d[:40], ok) for d, ok in _DOMAIN_EXAMPLES.items() if gwownlists.valid_domain(d) != ok]
    assert wrong == [], f"правило канона разошлось с примерами скрипта: {wrong}"


# ── агент: малина под ним ────────────────────────────────────────────────────

def _line(d: str, kind: str) -> str:
    return f"nftset=/{d}/inet#awg_home#lan_{'vpn' if kind == 'vpn' else 'ru'}4\n"


class _Host:
    """Малина: юнит обвязки (`env`), dnsmasq.d с двумя файлами своих списков,
    подставной awg-lan-domain.sh (`calls` — что вызывалось), метка sync в
    шапке скрипта (`has_sync`), реассерт обвязки (`reasserts`)."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.env = {"LAN_MODE": "1", "LINK_CHANNEL": "1"}
        self.dns_d = tmp_path / "dnsmasq.d"; self.dns_d.mkdir()
        self.new = tmp_path / "lib" / "own-lists.new"
        self.calls: list[str] = []
        self.timeouts: dict[str, int] = {}
        self.list_ok = True
        self.sync_ok, self.sync_err = True, "dnsmasq --test отверг списки — откатываю"
        self.has_sync = True
        self.reasserts = 0
        self.write()
        monkeypatch.setattr(gwguard, "DNSMASQ_D", str(self.dns_d))
        monkeypatch.setattr(gwguard, "OWN_LISTS_NEW", str(self.new))
        # файл для fill — рядом с файлом для sync, не в /var/lib хоста
        self.fill_new = tmp_path / "lib" / "own-fill.new"
        monkeypatch.setattr(gwguard, "OWN_FILL_NEW", str(self.fill_new))
        monkeypatch.setattr(gwguard, "unit_env", lambda k: self.env.get(k, ""))
        monkeypatch.setattr(gwguard, "lan_mode", lambda: self.env.get("LAN_MODE") == "1")
        monkeypatch.setattr(gwguard, "lan_domain_has_sync", lambda: self.has_sync)
        monkeypatch.setattr(gwguard, "run_lan_domain", self.run)
        monkeypatch.setattr(gwguard, "unit_state", lambda: {"ActiveState": "active"})
        monkeypatch.setattr(gwguard, "reassert", self._reassert)

    def _reassert(self, timeout=90):
        self.reasserts += 1
        return True, ""

    def write(self, vpn=(), ru=()) -> None:
        (self.dns_d / VPN_USER).write_text("".join(_line(d, "vpn") for d in vpn), encoding="utf-8")
        (self.dns_d / RU_USER).write_text("".join(_line(d, "ru") for d in ru), encoding="utf-8")

    def lists(self) -> dict:
        out = {}
        for name, kind in ((VPN_USER, "vpn"), (RU_USER, "ru")):
            f = self.dns_d / name
            for ln in (f.read_text().splitlines() if f.exists() else []):
                out[ln.split("/")[1]] = "ru" if out.get(ln.split("/")[1]) == "ru" else kind
        return out

    def run(self, cmd, domains, timeout=150):
        self.calls.append(cmd)
        self.timeouts[cmd] = timeout
        if cmd == "list":
            if not self.list_ok:
                return False, "awg-lan-domain.sh: нет прав"
            rows = []
            for name, kind in ((VPN_USER, "vpn"), (RU_USER, "ru")):
                rows += [f"{kind} {ln.split('/')[1]}" for ln in (self.dns_d / name).read_text().splitlines()]
            return True, "\n".join(rows)
        if cmd == "sync":
            if not self.sync_ok:
                return False, self.sync_err
            text = Path(domains[0]).read_text(encoding="utf-8")
            pairs = [ln.split(" ", 1) for ln in text.splitlines()]
            self.write([d for k, d in pairs if k == "vpn"], [d for k, d in pairs if k == "ru"])
            return True, "свои списки применены"
        if cmd == "fill":
            self.filled = Path(domains[0]).read_text(encoding="utf-8")
            return True, "набор lan_vpn4 пополнен: 1 домен"
        raise AssertionError(f"неожиданный вызов скрипта: {cmd}")


@pytest.fixture()
def host(tmp_path, monkeypatch):
    return _Host(tmp_path, monkeypatch)


@pytest.fixture()
def agent(tmp_path, monkeypatch, host):
    # метка запуска — на классе; каждый тест — свой «процесс»
    monkeypatch.setattr(GatewayServices, "_own_run", "")
    db = Database(tmp_path / "gw.db"); db.init_schema()
    a = GatewayServices(db)
    a._last_reassert = -1e9
    yield a
    db.close()


def _canon(items: dict, gen="g1", ver=1, upto=("", 0), rej=()) -> dict:
    return {"t": "own_set", "hash": gwownlists.digest(gen, ver, items, list(upto)), "gen": gen, "ver": ver,
            "items": sorted([d, k] for d, k in items.items()), "upto": list(upto), "rej": list(rej)}


def _synced(agent, host, items: dict, gen="g1", ver=1) -> None:
    """Агент уже применял канон `items`: файлы и база совпадают."""
    host.write()
    res = agent.apply_own_lists(_canon(items, gen, ver))
    assert res["ok"] is True, res
    assert host.lists() == items


def _pending(agent) -> list:
    return agent._own_pending_raw().get("ev") or []


# ── сверка ───────────────────────────────────────────────────────────────────

def test_sync_works_only_with_lan_mode_and_the_channel(agent, host):
    assert agent.own_active() is True
    host.env["LINK_CHANNEL"] = "0"
    assert agent.own_active() is False, "без канала списки только этого шлюза"
    host.env.update(LINK_CHANNEL="1", LAN_MODE="0")
    assert agent.own_active() is False


def test_an_edit_past_the_agent_becomes_one_event(agent, host):
    """`awg-bot lan add` на самой малине: тик находит правку и ставит её в
    очередь серверу — один раз, а не на каждом тике. Вторая сверка новых
    правок не находит (True только на новые — по нему кнопка дописывает хвост
    «синхронизируются»), а неотправленное видно отдельно — own_unsent."""
    _synced(agent, host, {"a.com": "vpn", "b.ru": "ru"})
    host.write(vpn=["a.com", "new.com"], ru=["b.ru"])
    assert agent.own_reconcile() is True
    assert [e[1:] for e in _pending(agent)] == [["new.com", "vpn", False]]
    assert agent.own_reconcile() is False, "та же правка второй раз названа новой"
    assert agent.own_unsent() is True, "правка ещё не отправлена — есть что слать"
    assert len(_pending(agent)) == 1, "та же правка встала в очередь второй раз"
    agent.own_pending_events()
    assert agent.own_unsent() is False, "отправленная правка снова «неотправленная» — маячок на каждом тике"


def test_removals_and_moves_become_del_and_the_new_kind(agent, host):
    _synced(agent, host, {"a.com": "vpn", "b.ru": "ru", "c.com": "vpn"})
    host.write(vpn=["b.ru"], ru=[])
    agent.own_reconcile()
    assert sorted(e[1:3] for e in _pending(agent)) == [["a.com", "del"], ["b.ru", "vpn"], ["c.com", "del"]]


def test_a_domain_in_both_files_counts_as_direct(agent, host):
    """Правка руками оставила домен в обоих файлах: nft решает «напрямую» —
    так же его видит и сверка."""
    _synced(agent, host, {"shop.ru": "vpn"})
    host.write(vpn=["shop.ru"], ru=["shop.ru"])
    agent.own_reconcile()
    assert [e[1:3] for e in _pending(agent)] == [["shop.ru", "ru"]]


def test_an_unchanged_fingerprint_does_not_run_the_script(agent, host):
    """Сверка на каждом тике монитора — два чтения файлов, без exec: скрипт
    зовётся, только когда файлы изменились."""
    _synced(agent, host, {"a.com": "vpn"})
    host.calls.clear()
    for _ in range(5):
        assert agent.own_reconcile() is False
    assert host.calls == [], f"сверка без изменений вызывала скрипт: {host.calls}"
    host.write(vpn=["a.com", "b.com"])
    agent.own_reconcile()
    assert host.calls == ["list"]


def test_a_missing_file_is_never_a_removal(agent, host):
    """Режим выключили или раздел не применился — файлов нет. Сверка молчит:
    иначе весь список ушёл бы удалениями и опустел на всех шлюзах."""
    _synced(agent, host, {"a.com": "vpn", "b.ru": "ru"})
    (host.dns_d / RU_USER).unlink()
    assert agent.own_local() is None
    assert agent.own_reconcile() is False and _pending(agent) == []
    (host.dns_d / VPN_USER).unlink()
    assert agent.own_reconcile() is False and _pending(agent) == []


def test_a_failing_listing_is_not_a_removal_either(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    host.write(vpn=["a.com", "b.com"])
    host.list_ok = False
    assert agent.own_reconcile() is False and _pending(agent) == [], "отказ скрипта list принят за пустой список"
    host.list_ok = True
    assert agent.own_reconcile() is True, "после отказа list правка потерялась: отпечаток запомнен раньше разбора"
    assert [e[1:3] for e in _pending(agent)] == [["b.com", "vpn"]]


def test_without_a_base_the_files_are_not_compared(agent, host):
    """Базы нет — шлюз ещё не синхронизировался: весь список уйдёт init
    по первому канону, а не обычными правками (они перебили бы «напрямую»)."""
    host.write(vpn=["a.com"], ru=["b.ru"])
    assert agent.own_reconcile() is False and _pending(agent) == []


def test_nothing_is_compared_when_sync_is_off(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    host.write(vpn=["b.com"])
    host.env["LINK_CHANNEL"] = ""
    host.calls.clear()
    assert agent.own_reconcile() is False and _pending(agent) == [] and host.calls == []


# ── нумерация и метка запуска ────────────────────────────────────────────────

def test_unsent_edits_are_renumbered_under_a_new_run(agent, host, monkeypatch):
    """Агент перезапустился с неотправленными правками: они уходят под новой
    меткой с единицы, init сохраняется — сервер не примет их за повтор."""
    _synced(agent, host, {"a.com": "vpn"})
    monkeypatch.setattr(GatewayServices, "_own_run", "run-a")
    host.write(vpn=["a.com", "b.com"], ru=["c.ru"])
    agent.own_reconcile()
    assert agent._own_pending_raw()["run"] == "run-a"
    monkeypatch.setattr(GatewayServices, "_own_run", "run-b")
    run, ev = agent.own_pending_events()
    assert run == "run-b" and [e[0] for e in ev] == [1, 2]
    assert [e[1:3] for e in ev] == [["b.com", "vpn"], ["c.ru", "ru"]]


def test_the_send_time_is_set_once_and_cleared_by_a_new_edit(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    assert agent.own_pending_events() == ("", []), "пустая очередь — нечего слать"
    host.write(vpn=["a.com", "b.com"])
    agent.own_reconcile()
    agent.own_pending_events()
    first = agent._own_pending_raw()["sent_at"]
    agent.own_pending_events()
    assert agent._own_pending_raw()["sent_at"] == first, "повторная отправка сдвинула время ожидания"
    host.write(vpn=["a.com", "b.com", "c.com"])
    agent.own_reconcile()
    assert "sent_at" not in agent._own_pending_raw(), "новая правка — ожидание заново"


def test_numbers_keep_growing_after_a_canon_is_applied(agent, host):
    """Главный путь после первой синхронизации: правки ушли, сервер их
    разобрал (upto = run/2), канон применён. Следующая правка того же запуска
    обязана пройти слияние на сервере — номер не может начаться заново с
    единицы, иначе сервер сочтёт её повтором и отбросит, а агент будет слать
    её на каждом тике и висеть «ждут синхронизации»."""
    _synced(agent, host, {})
    host.write(vpn=["a.com", "b.com"])
    agent.own_reconcile()
    run, ev = agent.own_pending_events()
    canon, upto, _, _ = gwownlists.merge({"gen": "g1", "ver": 1, "items": {}}, 1, run, ev, ["", 0], NOW)
    res = agent.apply_own_lists(_canon(gwownlists.canon_items(canon), "g1", canon["ver"], upto))
    assert res["ok"] is True and _pending(agent) == []
    # вторая правка того же запуска
    host.write(vpn=["a.com", "b.com", "c.com"])
    agent.own_reconcile()
    run2, ev2 = agent.own_pending_events()
    canon2, upto2, _, changed = gwownlists.merge(canon, 1, run2, ev2, upto, NOW)
    assert changed and "c.com" in gwownlists.canon_items(canon2), (
        f"сервер отбросил вторую правку как повтор: upto {upto}, событие {run2} {ev2}")


# ── применение канона: правило применения ──────────────────────────────────────────

def test_a_covered_canon_is_written_through_sync_and_becomes_the_base(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    host.calls.clear()
    res = agent.apply_own_lists(_canon({"a.com": "vpn", "b.ru": "ru"}, ver=2))
    assert res == {"ok": True, "hash": _canon({"a.com": "vpn", "b.ru": "ru"}, ver=2)["hash"], "n": 2, "error": ""}
    assert host.calls.count("sync") == 1, host.calls
    assert host.timeouts["sync"] > 120, \
        "sync ждёт блокировку фидов до 120 с — таймаут короче выдал бы «занято» за поломку"
    assert host.new.read_text() == gwownlists.render_sync({"a.com": "vpn", "b.ru": "ru"})
    assert host.lists() == {"a.com": "vpn", "b.ru": "ru"}
    base = agent.own_base()
    assert base["items"] == {"a.com": "vpn", "b.ru": "ru"} and base["ver"] == 2 and base["gen"] == "g1"
    assert agent.own_applied_hash() == res["hash"]
    # сразу после применения сверка пуста: применённое не возвращается правкой
    host.calls.clear()
    assert agent.own_reconcile() is False and host.calls == []


def test_the_same_lists_are_accepted_without_running_sync(agent, host):
    """Свой же канон вернулся (ответ на кнопку): файлы уже такие — ни exec,
    ни рестарта dnsmasq, но база и ответ серверу есть."""
    _synced(agent, host, {"a.com": "vpn"})
    host.write(vpn=["a.com"], ru=["b.ru"])
    agent.own_reconcile()
    run, ev = agent.own_pending_events()
    host.calls.clear()
    res = agent.apply_own_lists(_canon({"a.com": "vpn", "b.ru": "ru"}, ver=2, upto=(run, ev[-1][0])))
    assert res["ok"] is True and "sync" not in host.calls, host.calls
    assert _pending(agent) == [] and agent.own_base()["ver"] == 2


@pytest.mark.parametrize("upto", [("другой", 99), ("SAME", 0)])
def test_a_canon_that_misses_own_edits_is_skipped(agent, host, upto):
    """Канон пришёл раньше, чем сервер разобрал правку: применить — значит
    затереть свежую правку старым списком. Пропуск без ответа серверу."""
    _synced(agent, host, {"a.com": "vpn"})
    host.write(vpn=["a.com", "b.com"])
    agent.own_reconcile()
    run, _ = agent.own_pending_events()
    upto = (run if upto[0] == "SAME" else upto[0], upto[1])
    host.calls.clear()
    res = agent.apply_own_lists(_canon({"a.com": "vpn"}, ver=2, upto=upto))
    assert res.get("skipped") == "pending", res
    assert "sync" not in host.calls and host.lists() == {"a.com": "vpn", "b.com": "vpn"}, "свежую правку затёр старый канон"
    assert agent.own_base()["ver"] == 1 and len(_pending(agent)) == 1


def test_the_first_sync_sends_the_whole_list_as_init_once(agent, host):
    """Шлюз из 3.0/3.1 со своим списком, базы нет: канон не применяется, весь
    список уходит init один раз; повторный канон до разбора — снова пропуск
    без второй пачки; канон с upto — применение."""
    host.write(vpn=["a.com"], ru=["b.ru"])
    res = agent.apply_own_lists(_canon({}, ver=0))
    assert res.get("skipped") == "init", res
    assert host.lists() == {"a.com": "vpn", "b.ru": "ru"}, "пустой канон стёр список при первой синхронизации"
    ev = _pending(agent)
    assert [e[1:] for e in ev] == [["a.com", "vpn", True], ["b.ru", "ru", True]]
    run = agent._own_pending_raw()["run"]
    res = agent.apply_own_lists(_canon({"x.com": "vpn"}, ver=1))
    assert res.get("skipped") == "pending" and len(_pending(agent)) == 2, "init ушёл второй раз"
    res = agent.apply_own_lists(_canon({"a.com": "vpn", "b.ru": "ru", "x.com": "vpn"}, ver=2, upto=(run, 2)))
    assert res["ok"] is True and host.lists() == {"a.com": "vpn", "b.ru": "ru", "x.com": "vpn"}
    assert _pending(agent) == []


def test_an_empty_gateway_takes_the_canon_at_once(agent, host):
    """Новый шлюз без своих списков: слать нечего, канон применяется сразу."""
    res = agent.apply_own_lists(_canon({"a.com": "vpn"}, ver=3))
    assert res["ok"] is True and host.lists() == {"a.com": "vpn"} and _pending(agent) == []


@pytest.mark.parametrize("gen,ver", [("g2", 9), ("g1", 1)])
def test_a_reinstalled_or_restored_server_gets_the_list_as_init(agent, host, gen, ver):
    """Сервер переустановлен (другое поколение) или восстановлен из старой
    копии (версия меньше базы): его канон не затирает список шлюза — список
    уходит init, объединение решит сервер."""
    _synced(agent, host, {"a.com": "vpn", "b.ru": "ru"}, ver=5)
    host.calls.clear()
    res = agent.apply_own_lists(_canon({}, gen=gen, ver=ver))
    assert res.get("skipped") == "init", res
    assert "sync" not in host.calls and host.lists() == {"a.com": "vpn", "b.ru": "ru"}
    assert [e[1:] for e in _pending(agent)] == [["a.com", "vpn", True], ["b.ru", "ru", True]]


def test_a_newer_canon_of_the_same_server_is_applied(agent, host):
    _synced(agent, host, {"a.com": "vpn"}, ver=5)
    res = agent.apply_own_lists(_canon({}, ver=6))
    assert res["ok"] is True and host.lists() == {}, "удаление на другом шлюзе не дошло"


@pytest.mark.parametrize("gen,ver", [("g2", 1), ("g1", 1)])
def test_an_empty_gateway_takes_the_canon_of_a_new_or_restored_server_at_once(agent, host, gen, ver):
    """Шлюз с ПУСТЫМИ своими списками, база от прежнего сервера (другое
    поколение или версия меньше базы): слать init нечего — канон применяется
    сразу. Раньше такой шлюз отвечал «skipped: init» на каждый канон и не
    применял его никогда: после переустановки сервера списки не доходили."""
    _synced(agent, host, {"a.com": "vpn"}, ver=4)
    assert agent.apply_own_lists(_canon({}, ver=5))["ok"] is True
    assert host.lists() == {} and agent.own_base()["ver"] == 5
    res = agent.apply_own_lists(_canon({"x.com": "vpn"}, gen=gen, ver=ver))
    assert res["ok"] is True and not res.get("skipped"), f"пустой шлюз не принял канон: {res}"
    assert host.lists() == {"x.com": "vpn"}
    assert (agent.own_base()["gen"], agent.own_base()["ver"]) == (gen, ver), "база не стала новым каноном"
    assert _pending(agent) == [], "пустой список ушёл init"


def _dropping_sync(host, monkeypatch, drop: str) -> None:
    """sync, который (как настоящий скрипт с хостом Endpoint аплинка) молча
    не пишет домен `drop`."""
    orig = host.run

    def run(cmd, domains, timeout=150):
        res = orig(cmd, domains, timeout)
        if cmd == "sync" and res[0]:
            cur = host.lists(); cur.pop(drop, None)
            host.write([d for d, k in cur.items() if k == "vpn"], [d for d, k in cur.items() if k == "ru"])
        return res
    monkeypatch.setattr(gwguard, "run_lan_domain", run)


def test_the_base_is_what_the_script_actually_wrote(agent, host, monkeypatch):
    """Скрипт отбросил домен канона (хост Endpoint аплинка): база — то, что
    лежит в файлах, а не присланное. Иначе следующая сверка сочла бы
    отброшенный домен удалённым руками и отправила бы серверу «del» — домен
    пропал бы у всех шлюзов."""
    _synced(agent, host, {"a.com": "vpn"})
    _dropping_sync(host, monkeypatch, "uplink.example.org")
    res = agent.apply_own_lists(_canon({"a.com": "vpn", "uplink.example.org": "vpn"}, ver=2))
    assert res["ok"] is True, res
    assert agent.own_base()["items"] == {"a.com": "vpn"}, agent.own_base()
    assert agent.own_reconcile() is False and _pending(agent) == [], \
        f"отброшенный скриптом домен ушёл серверу правкой: {_pending(agent)}"


def test_the_base_is_empty_when_the_script_dropped_the_only_domain(agent, host, monkeypatch):
    """Тот же случай, когда отброшенный домен в каноне единственный: в файлах
    пусто — и база пуста; сверка правок не находит."""
    _synced(agent, host, {})
    _dropping_sync(host, monkeypatch, "uplink.example.org")
    res = agent.apply_own_lists(_canon({"uplink.example.org": "vpn"}, ver=2))
    assert res["ok"] is True and host.lists() == {}, res
    assert agent.own_base()["items"] == {}, f"база — присланное, а не записанное: {agent.own_base()}"
    assert agent.own_reconcile() is False and _pending(agent) == [], \
        f"отброшенный скриптом домен ушёл серверу правкой: {_pending(agent)}"


def test_an_unwritable_sync_file_refuses_with_a_reason(agent, host, monkeypatch, tmp_path):
    """Файл для sync не записался (каталог состояния — файл, диск полон): отказ
    с причиной, а не исключение в цикле канала; база и файлы прежние."""
    _synced(agent, host, {"a.com": "vpn"})
    blocker = tmp_path / "blocker"; blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(gwguard, "OWN_LISTS_NEW", str(blocker / "own-lists.new"))
    host.calls.clear()
    res = agent.apply_own_lists(_canon({"b.com": "vpn"}, ver=2))
    assert res["ok"] is False and res["error"].startswith("файл для sync не записан: "), res
    assert "sync" not in host.calls and host.lists() == {"a.com": "vpn"}
    assert agent.own_base()["ver"] == 1
    assert agent.own_status()[0]["state"] == "failed"


# ── fill: адреса новых «в туннель» — после sync, фоном ──────────────────────

def test_new_tunnel_domains_go_to_the_fill_file_and_nothing_else(agent, host):
    """sync больше не резолвит новые «в туннель»: их список для fill — ровно
    новые домены «в туннель», включая переехавшие из «напрямую». Прежние и
    «напрямую» в нём — лишний dig по сотне доменов на каждом каноне."""
    _synced(agent, host, {"old.com": "vpn", "moved.ru": "ru", "stay.ru": "ru"})
    res = agent.apply_own_lists(_canon({"old.com": "vpn", "moved.ru": "vpn", "stay.ru": "ru",
                                        "b.com": "vpn", "a.com": "vpn", "new.ru": "ru"}, ver=2))
    assert res["ok"] is True, res
    assert res.get("fill") == str(host.fill_new), f"пути для fill в итоге нет: {res}"
    assert host.fill_new.read_text() == "a.com\nb.com\nmoved.ru\n", host.fill_new.read_text()
    assert "fill" not in host.calls, "fill запущен прямо в применении канона — канал ждал бы dig"


def test_without_new_tunnel_domains_there_is_no_fill(agent, host):
    """Канон только убрал домены или добавил «напрямую» — fill не нужен."""
    _synced(agent, host, {"a.com": "vpn", "b.com": "vpn"})
    host.fill_new.unlink(missing_ok=True)
    res = agent.apply_own_lists(_canon({"a.com": "vpn", "c.ru": "ru"}, ver=2))
    assert res["ok"] is True and "fill" not in res, res
    assert not host.fill_new.exists(), "файл для fill записан без новых доменов"


def test_the_same_lists_give_no_fill(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    res = agent.apply_own_lists(_canon({"a.com": "vpn"}, ver=2))
    assert res["ok"] is True and "fill" not in res, res


def test_a_failed_sync_gives_no_fill(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    host.sync_ok = False
    res = agent.apply_own_lists(_canon({"a.com": "vpn", "b.com": "vpn"}, ver=2))
    assert res["ok"] is False and "fill" not in res, res


def test_a_domain_the_script_dropped_is_not_filled(agent, host, monkeypatch):
    """Хост Endpoint скрипт отбросил — в набор «в туннель» его адрес не
    попадает и через fill: иначе шлюз запер бы сам себя."""
    _synced(agent, host, {"a.com": "vpn"})
    _dropping_sync(host, monkeypatch, "uplink.example.org")
    res = agent.apply_own_lists(_canon({"a.com": "vpn", "uplink.example.org": "vpn", "b.com": "vpn"}, ver=2))
    assert res["ok"] is True, res
    assert host.fill_new.read_text() == "b.com\n", host.fill_new.read_text()


def test_an_unwritable_fill_file_keeps_the_canon_applied(agent, host, monkeypatch, tmp_path):
    """fill — лишь ускорение: не записался его файл — канон всё равно
    применён и подтверждён, без ключа fill."""
    _synced(agent, host, {"a.com": "vpn"})
    blocker = tmp_path / "blocker"; blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(gwguard, "OWN_FILL_NEW", str(blocker / "own-fill.new"))
    res = agent.apply_own_lists(_canon({"a.com": "vpn", "b.com": "vpn"}, ver=2))
    assert res["ok"] is True and "fill" not in res, res
    assert agent.own_base()["items"] == {"a.com": "vpn", "b.com": "vpn"}


def test_own_fill_runs_the_script_with_the_long_timeout(agent, host):
    """dig по каждому из сотен доменов идёт минутами: обычные 150 с оборвали
    бы fill на первой синхронизации."""
    _synced(agent, host, {})
    res = agent.apply_own_lists(_canon({"a.com": "vpn"}, ver=2))
    ok, tail = agent.own_fill(res["fill"])
    assert ok is True and "пополнен" in tail, tail
    assert host.filled == "a.com\n"
    assert host.timeouts["fill"] == gwguard.OWN_FILL_TIMEOUT >= 1800, host.timeouts


def test_lan_mode_off_refuses_and_keeps_the_base(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    host.env["LAN_MODE"] = "0"
    host.calls.clear()
    res = agent.apply_own_lists(_canon({}, ver=2))
    assert res["ok"] is False and "выключен" in res["error"] and host.calls == []
    assert agent.own_base()["ver"] == 1


def test_missing_files_refuse_the_canon(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    (host.dns_d / VPN_USER).unlink()
    res = agent.apply_own_lists(_canon({}, ver=2))
    assert res["ok"] is False and "раздел не применился" in res["error"], res
    assert agent.own_base()["ver"] == 1


def test_a_failed_sync_keeps_base_and_edits_and_says_why(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    host.sync_ok = False
    res = agent.apply_own_lists(_canon({"b.com": "vpn"}, ver=2))
    assert res == {"ok": False, "hash": _canon({"b.com": "vpn"}, ver=2)["hash"], "n": 0, "error": host.sync_err}
    assert agent.own_base()["ver"] == 1 and host.lists() == {"a.com": "vpn"}
    info, checks = agent.own_status()
    assert info["state"] == "failed" and checks[0].ok is False
    assert checks[0].detail == f"не применились: {host.sync_err}"
    host.sync_ok = True
    assert agent.apply_own_lists(_canon({"b.com": "vpn"}, ver=2))["ok"] is True
    assert agent.own_status()[0]["state"] == "synced", "ошибка пережила успешное применение"


def test_a_dirty_canon_is_cleaned_before_it_reaches_the_script(agent, host):
    """Скомпрометированный ВПС шлёт в канон строки с переводом строки и чужие
    виды: в файл для sync попадают только чистые «vpn d» / «ru d»."""
    body = _canon({}, ver=2)
    body["items"] = [["a.com", "vpn"], ["b.com\nru evil.com", "vpn"], ["c.com", "server=/x/1.1.1.1"],
                     ["d.com", "ru"], "мусор", ["d.com", "vpn"]]
    res = agent.apply_own_lists(body)
    assert res["ok"] is True
    assert host.new.read_text() == "vpn a.com\nru d.com\n", host.new.read_text()


def test_an_old_script_triggers_a_reassert_and_the_canon_waits(agent, host):
    """Агент обновился, обвязка ещё старая — скрипт не знает sync: канон
    откладывается, обвязка перевыставляется; на тике со свежим скриптом канон
    применяется сам, повтор — ничего."""
    host.has_sync = False
    res = agent.apply_own_lists(_canon({"a.com": "vpn"}, ver=2))
    assert res["ok"] is False and host.reasserts == 1
    assert "скрипт своих списков старого образца" in res["error"], res
    assert host.lists() == {} and "sync" not in host.calls
    assert agent.own_status()[0]["state"] == "old_script"
    assert agent.own_retry() is None, "скрипт всё ещё старый — применять нечем"
    host.has_sync = True
    res = agent.own_retry()
    assert res is not None and res["ok"] is True and host.lists() == {"a.com": "vpn"}
    assert agent.own_retry() is None, "отложенный канон применён второй раз"


def test_the_retry_itself_reasserts_until_the_script_learns_sync(agent, host, monkeypatch):
    """Первый реассерт не помог (юнит был занят, скрипт остался старым): тик
    сам пробует снова, когда троттлинг отпустит, и применяет отложенный канон,
    как только скрипт стал новым. Раньше повтор ждал, пока обвязку
    перевыставит кто-то другой, — канон висел до перезапуска агента."""
    host.has_sync = False
    agent.apply_own_lists(_canon({"a.com": "vpn"}, ver=2))
    assert host.reasserts == 1
    assert agent.own_retry() is None and host.reasserts == 1, "троттлинг реассерта не соблюдён"
    agent._last_reassert = -1e9                         # прошло 10 минут

    def reassert(timeout=90):
        host.reasserts += 1
        host.has_sync = True                            # юнит положил новый скрипт
        return True, ""
    monkeypatch.setattr(gwguard, "reassert", reassert)
    res = agent.own_retry()
    assert host.reasserts == 2, "тик не перевыставил обвязку сам"
    assert res is not None and res["ok"] is True and host.lists() == {"a.com": "vpn"}, res
    assert agent.own_retry() is None


def test_nothing_to_retry_is_none(agent, host):
    assert agent.own_retry() is None


@pytest.mark.parametrize("raw", ["{не json", "[1, 2]", '"строка"', "{}"])
def test_a_broken_deferred_canon_is_dropped_without_applying(agent, host, raw):
    """Отложенный канон испорчен (обрыв записи на SD, чужой формат): снять и
    ничего не делать, как битую очередь сервисов соседей. Прочитанный как
    пустой канон, он ушёл бы в apply_own_lists: у шлюза со списками — весь
    список серверу событиями init, у пустого — sync и база без поколения."""
    _synced(agent, host, {"a.com": "vpn", "b.ru": "ru"}, ver=5)
    base = agent.own_base()
    host.calls.clear()
    agent.db.set_state(GatewayServices._OWN_DEFER_KEY, raw)
    assert agent.own_retry() is None
    assert (agent.db.get_state(GatewayServices._OWN_DEFER_KEY) or "") == "", "битая запись осталась — разбор на каждом тике"
    assert "sync" not in host.calls, "из битой записи запущен sync"
    assert _pending(agent) == [], f"битая запись породила правки для сервера: {_pending(agent)}"
    assert agent.own_base() == base, "база переписана битой записью"
    assert host.lists() == {"a.com": "vpn", "b.ru": "ru"}


def test_a_broken_deferred_canon_on_an_empty_gateway_touches_nothing(agent, host):
    """Пустой шлюз: пустой канон применился бы сразу — sync пустого файла и
    база без поколения и хэша."""
    agent.db.set_state(GatewayServices._OWN_DEFER_KEY, "{не json")
    assert agent.own_retry() is None
    assert "sync" not in host.calls, "из битой записи запущен sync"
    assert agent.own_base() == {}, f"битая запись стала базой: {agent.own_base()}"


# ── состояние для монитора ────────────────────────────────────────────

def _checks(agent):
    info, checks = agent.own_status()
    assert all(c.group == "own" for c in checks), "проверки своих списков — своей группой, без уведомлений"
    assert len(checks) == 1, checks
    return info, checks[0]


def test_status_is_off_without_the_channel(agent, host):
    """Режим без VPN есть, канала нет — «off» с пометкой no_channel: экран
    списков скажет, что они только этого шлюза и почему."""
    host.env["LINK_CHANNEL"] = "0"
    assert agent.own_status() == ({"active": False, "state": "off", "no_channel": True}, [])


def test_status_is_off_without_lan_mode_and_says_nothing_about_the_channel(agent, host):
    """Режим выключен — про канал говорить нечего: no_channel ложно даже без канала."""
    host.env.update(LINK_CHANNEL="0", LAN_MODE="0")
    info, checks = agent.own_status()
    assert info["state"] == "off" and not info["no_channel"] and checks == [], info


def test_status_before_and_after_the_first_sync(agent, host):
    info, c = _checks(agent)
    assert info["state"] == "synced" and c.ok is True
    assert c.detail == "ждут первой синхронизации с сервером AWG"
    _synced(agent, host, {"a.com": "vpn"})
    _, c = _checks(agent)
    assert c.ok is True and c.detail == "синхронизированы с сервером AWG"


def test_status_says_why_edits_wait(agent, host):
    _synced(agent, host, {"a.com": "vpn"})
    host.write(vpn=["a.com", "b.com", "c.com"])
    agent.own_reconcile()
    agent.channel.online = False
    info, c = _checks(agent)
    assert info["state"] == "no_link" and c.ok is None
    assert c.detail == "ждут синхронизации (2 правки): нет связи с сервером AWG", c.detail
    agent.channel.online = True
    agent.own_pending_events()
    info, c = _checks(agent)
    assert info["state"] == "pending" and c.detail == "ждут синхронизации (2 правки): сервер AWG ещё не ответил"
    # сервер молчит 11 минут — старый основной бот не знает own_ev
    p = agent._own_pending_raw()
    p["sent_at"] = timeutil.to_iso(timeutil.now() - datetime.timedelta(minutes=11))
    agent._own_pending_set(p)
    info, c = _checks(agent)
    assert info["state"] == "stale_server" and c.ok is None
    assert c.detail == "сервер AWG не отвечает на правки 11 мин: обнови основной бот", c.detail


def test_status_names_what_the_server_refused_and_the_monitor_escapes_it(agent, host):
    """Отвергнутый домен пришёл с сервера — в мониторе он экранирован один раз
    (экранирует экран монитора, деталь — простой текст)."""
    from awgbot.bot import texts
    from awgbot.domain.gateway import GwStatus
    _synced(agent, host, {"a.com": "vpn"})
    agent.own_rejected_in([["<b>x</b>.com", "максимум 500 доменов"]])
    info, c = _checks(agent)
    assert info["state"] == "rejected" and c.ok is None
    assert c.detail == "сервер AWG не принял 1 домен: <b>x</b>.com — максимум 500 доменов", c.detail
    health = texts.gateway_health(GwStatus(checks=[c]))
    assert "⚪ свои списки — сервер AWG не принял 1 домен: &lt;b&gt;x&lt;/b&gt;.com — максимум 500 доменов" in health, health
    agent.own_rejected_in([["a.com", "не домен"], ["b.com", "не домен"]])
    assert _checks(agent)[1].detail.startswith("сервер AWG не принял 2 домена: a.com — не домен")


def test_waiting_lists_do_not_reach_the_panel_summary(agent, host):
    """«Ждут синхронизации» — штатное ожидание, не повод для «⚪ не проверено»
    в сводке панели; на экране монитора строка остаётся."""
    from awgbot.bot import texts
    from awgbot.domain.gateway import GwCheck, GwStatus
    _synced(agent, host, {"a.com": "vpn"})
    host.write(vpn=["a.com", "b.com"])
    agent.own_reconcile()
    agent.channel.online = False
    _, checks = agent.own_status()
    st = GwStatus(link_up=True, handshake_age=5.0, checks=[GwCheck("MASQUERADE", True)] + checks)
    assert "🌡 Монитор здоровья: ✅ проблем не выявлено" in texts.gateway_panel(st), texts.gateway_panel(st)
    assert "⚪ свои списки — ждут синхронизации (1 правка): нет связи с сервером AWG" in texts.gateway_health(st)


def test_status_of_an_old_script(agent, host):
    host.has_sync = False
    info, c = _checks(agent)
    assert info["state"] == "old_script" and c.ok is None
    assert c.detail == "скрипт старого образца, обвязка перевыставляется"
