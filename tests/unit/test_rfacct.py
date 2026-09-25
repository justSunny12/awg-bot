"""Учёт РФ-трафика на сервере (infra/rfacct):
отрисовка таблицы `awg_bot_acct`, дифф состава счётчиков и карт, разбор
`nft -j`, правила дельт, запуск `nft`. Ядро подменено: ни одного настоящего
вызова nft."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from awgbot.infra import rfacct
from awgbot.infra.rfacct import AcctState

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
T = rfacct.TABLE                                      # "inet awg_bot_acct"


def _rules(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("add rule ")]


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


# ── отрисовка статики ────────────────────────────────────────────────────────

def test_static_render_is_deterministic():
    """Хэш отрисовки решает, переписывать ли цепочку: плавающий текст
    перезаписывал бы её на каждом опросе."""
    a = rfacct.render_static(["awglink", "awglink2"], ["10.8.1.0/24", "10.9.1.0/24"])
    b = rfacct.render_static(["awglink", "awglink2"], ["10.8.1.0/24", "10.9.1.0/24"])
    assert a == b


def test_static_render_counts_both_client_subnets_including_migration():
    """Подсеть переезда — тоже клиенты: без неё РФ-трафик двойников в окне
    переезда прошёл бы мимо учёта."""
    text = rfacct.render_static(["awglink"], ["10.8.1.0/24", "10.9.1.5/24"])
    assert f"add element {T} clients4 {{ 10.8.1.0/24, 10.9.1.0/24 }}" in text, \
        "подсеть переезда (нормализованная) не попала в clients4"


def test_static_render_puts_every_link_into_all_four_rules():
    """Правила — по всем линкам: переключение активного слота не должно
    прерывать счёт."""
    rules = _rules(rfacct.render_static(["awglink", "awglink2"], ["10.8.1.0/24"]))
    assert len(rules) == 4, rules
    for r in rules:
        assert '{ "awglink", "awglink2" }' in r, f"не все линки в правиле: {r}"
    up = [r for r in rules if " oifname " in r]
    dn = [r for r in rules if " iifname " in r]
    assert len(up) == 2 and len(dn) == 2
    assert all("ip saddr @clients4 ip daddr != @private4" in r for r in up)
    assert all("ip daddr @clients4 ip saddr != @private4" in r for r in dn)
    assert any('counter name "rf_up"' in r for r in up) and any("map @rf_dev_up" in r for r in up)
    assert any('counter name "rf_dn"' in r for r in dn) and any("map @rf_dev_dn" in r for r in dn)


def test_static_render_has_no_verdicts_and_never_deletes_the_table():
    """Таблица без вердиктов и с policy accept: ошибка в ней не может ни
    отрезать, ни открыть трафик. `delete table` обнулил бы накопленные
    счётчики на каждой перезаписи."""
    text = rfacct.render_static(["awglink"], ["10.8.1.0/24"])
    assert "delete table" not in text
    assert "policy accept;" in text and "policy drop" not in text
    assert "priority filter + 10;" in text, "учёт после файервола — видит только пропущенное"
    for r in _rules(text):
        assert not re.search(r"\b(accept|drop|reject|return|jump|goto)\b", r), f"вердикт в правиле: {r}"
    assert f"flush chain {T} forward" in text


def test_static_render_without_links_or_subnets_has_no_rules():
    assert _rules(rfacct.render_static([], ["10.8.1.0/24"])) == []
    assert _rules(rfacct.render_static(["awglink"], [])) == []


def test_private_nets_match_the_gateway_setup_script():
    """Сторож: «частные» у учёта — тот же список, что PRIVATE_NETS обвязки
    шлюза. Разойдутся — одна сторона начнёт считать РФ-трафиком доступ к
    локальным подсетям (или наоборот терять его)."""
    script = (ROOT / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")
    line = next(ln for ln in script.splitlines() if ln.startswith("PRIVATE_NETS="))
    r = subprocess.run(["sh", "-c", f'{line}\nprintf "%s" "$PRIVATE_NETS"'],
                       capture_output=True, text=True, check=True)
    assert sorted(r.stdout.split()) == sorted(rfacct.PRIVATE_NETS), \
        "PRIVATE_NETS учёта разошёлся с install/routing-gw-setup.sh"
    text = rfacct.render_static(["awglink"], ["10.8.1.0/24"])
    elem = next(ln for ln in text.splitlines() if ln.startswith(f"add element {T} private4"))
    assert sorted(re.findall(r"[\d.]+/\d+", elem)) == sorted(rfacct.PRIVATE_NETS)


# ── дифф состава: счётчики и карты ───────────────────────────────────────────

def _state(devs: dict[int, str], bytes_: int = 0, rules: int = 4) -> AcctState:
    """Ядро, в котором уже есть счётчики и элементы карт этих устройств."""
    st = AcctState(handle=7, rules=rules,
                   counters={rfacct.COUNTER_UP: bytes_, rfacct.COUNTER_DN: bytes_})
    for dev_id, addr in devs.items():
        up, dn = rfacct.counter_names(dev_id)
        st.counters[up] = bytes_
        st.counters[dn] = bytes_
        st.up[addr] = up
        st.dn[addr] = dn
    return st


def test_new_device_gets_two_counters_and_an_element_in_each_map():
    text = rfacct.render_devices(_state({}), [(12, "10.8.1.5")])
    lines = _lines(text)
    assert f"add counter {T} d12_up" in lines and f"add counter {T} d12_dn" in lines
    assert f'add element {T} rf_dev_up {{ 10.8.1.5 : "d12_up" }}' in lines
    assert f'add element {T} rf_dev_dn {{ 10.8.1.5 : "d12_dn" }}' in lines
    assert lines.index(f"add counter {T} d12_up") < lines.index(f'add element {T} rf_dev_up {{ 10.8.1.5 : "d12_up" }}'), \
        "элемент карты раньше счётчика, на который он ссылается"
    assert not any(ln.startswith("delete ") for ln in lines)


def test_device_address_with_mask_is_normalized():
    text = rfacct.render_devices(_state({}), [(3, "10.8.1.9/32")])
    assert f'add element {T} rf_dev_up {{ 10.8.1.9 : "d3_up" }}' in text


def test_device_without_valid_address_gets_nothing():
    """Устройство без адреса — не в ядро: элемент карты «пустота → счётчик»
    nft отверг бы, и учёт встал бы целиком."""
    assert rfacct.render_devices(_state({}), [(3, ""), (4, "мусор")]) == ""


def test_gone_device_drops_elements_before_counters():
    """Карта ссылается на счётчик — удалить счётчик раньше элемента nft не даст,
    и транзакция откатится целиком. Итоговая пара rf_up/rf_dn не трогается:
    её удаление обнулило бы итог сервера."""
    lines = _lines(rfacct.render_devices(_state({12: "10.8.1.5"}), []))
    assert lines == [
        f"delete element {T} rf_dev_up {{ 10.8.1.5 }}",
        f"delete element {T} rf_dev_dn {{ 10.8.1.5 }}",
        f"delete counter {T} d12_up",
        f"delete counter {T} d12_dn",
    ]


def test_orphan_counter_without_map_element_is_removed():
    """След ушедшего устройства — только счётчик (элемента нет): его всё равно
    снимаем, иначе объекты копятся в ядре годами."""
    st = _state({})
    st.counters["d9_up"] = 5
    st.counters["d9_dn"] = 5
    assert _lines(rfacct.render_devices(st, [])) == [
        f"delete counter {T} d9_up", f"delete counter {T} d9_dn"]


def test_address_passed_from_removed_device_to_new_one_is_reinserted_once():
    """Аллокатор отдаёт адрес удалённого устройства новому. Элемент карты
    должен перейти к новому счётчику, а `delete element` на один адрес — ровно
    один: второй nft отвергнет (элемента уже нет), и не применится ничего."""
    lines = _lines(rfacct.render_devices(_state({12: "10.8.1.5"}), [(13, "10.8.1.5")]))
    for m in ("rf_dev_up", "rf_dev_dn"):
        dels = [ln for ln in lines if ln == f"delete element {T} {m} {{ 10.8.1.5 }}"]
        assert len(dels) == 1, f"delete element в {m}: {len(dels)} раз\n" + "\n".join(lines)
    assert f"delete counter {T} d12_up" in lines and f"delete counter {T} d12_dn" in lines
    add_up = f'add element {T} rf_dev_up {{ 10.8.1.5 : "d13_up" }}'
    add_dn = f'add element {T} rf_dev_dn {{ 10.8.1.5 : "d13_dn" }}'
    assert add_up in lines and add_dn in lines
    assert lines.index(f"delete element {T} rf_dev_up {{ 10.8.1.5 }}") < lines.index(add_up)
    assert lines.index(f"delete element {T} rf_dev_up {{ 10.8.1.5 }}") < lines.index(f"delete counter {T} d12_up")
    assert lines.index(f"add counter {T} d13_up") < lines.index(add_up)


def test_address_changed_on_the_same_device_is_reinserted_without_new_counters():
    """Смена адреса у живого устройства: элемент переезжает, счётчик тот же —
    накопленное не теряется."""
    lines = _lines(rfacct.render_devices(_state({12: "10.8.1.5"}), [(12, "10.8.1.7")]))
    assert lines == [
        f"delete element {T} rf_dev_up {{ 10.8.1.5 }}",
        f'add element {T} rf_dev_up {{ 10.8.1.7 : "d12_up" }}',
        f"delete element {T} rf_dev_dn {{ 10.8.1.5 }}",
        f'add element {T} rf_dev_dn {{ 10.8.1.7 : "d12_dn" }}',
    ]


def test_two_devices_swapping_addresses_delete_each_element_once():
    """Обмен адресами двух живых устройств: каждый адрес снимается один раз и
    вставляется с новым именем."""
    lines = _lines(rfacct.render_devices(_state({1: "10.8.1.2", 2: "10.8.1.3"}),
                                         [(1, "10.8.1.3"), (2, "10.8.1.2")]))
    dels = [ln for ln in lines if ln.startswith("delete element")]
    assert len(dels) == len(set(dels)) == 4, "\n".join(lines)
    assert f'add element {T} rf_dev_up {{ 10.8.1.3 : "d1_up" }}' in lines
    assert f'add element {T} rf_dev_up {{ 10.8.1.2 : "d2_up" }}' in lines
    assert not any(ln.startswith(("add counter", "delete counter")) for ln in lines)


def test_nothing_changed_gives_an_empty_script():
    assert rfacct.render_devices(_state({12: "10.8.1.5", 13: "10.8.1.6"}),
                                 [(12, "10.8.1.5"), (13, "10.8.1.6")]) == ""


def test_no_state_means_everything_is_created():
    """Таблицы нет (первый запуск, flush ruleset) — всё рождается заново."""
    text = rfacct.render_devices(None, [(1, "10.8.1.2")])
    assert f"add counter {T} d1_up" in text and "delete" not in text


# ── синхронизация: когда переписывается статика ──────────────────────────────

class _Nft:
    """Подставной nft: журнал скриптов; `-c` и применение можно уронить."""

    def __init__(self, check_rc=0, apply_rc=0, raise_=None):
        self.check_rc, self.apply_rc, self.raise_ = check_rc, apply_rc, raise_
        self.calls: list[tuple[list[str], str]] = []

    def run(self, argv, input=None, capture_output=True, timeout=None):
        if self.raise_ is not None:
            raise self.raise_
        args = argv[1:]
        self.calls.append((args, (input or b"").decode()))
        rc = self.check_rc if args[:1] == ["-c"] else self.apply_rc
        return subprocess.CompletedProcess(argv, rc, b"", b"Error: boom" if rc else b"")

    @property
    def applied(self) -> list[str]:
        return [s for a, s in self.calls if a == ["-f", "-"]]


@pytest.fixture()
def nft(monkeypatch):
    fake = _Nft()
    monkeypatch.setattr(rfacct.subprocess, "run", fake.run)
    monkeypatch.setattr(rfacct, "_static_applied", "")        # свежий процесс бота
    return fake


SUBS = ["10.8.1.0/24"]


def test_sync_without_links_does_not_touch_nft(nft):
    """Нет ни одного линка — учёта нет: ни таблицы, ни вызовов nft."""
    assert rfacct.sync(None, [], SUBS, [(1, "10.8.1.2")]) is False
    assert nft.calls == []


def test_sync_creates_table_when_absent_and_checks_before_applying(nft):
    assert rfacct.sync(None, ["awglink"], SUBS, [(1, "10.8.1.2")]) is True
    assert [a for a, _ in nft.calls] == [["-c", "-f", "-"], ["-f", "-"]], "без проверки nft -c"
    script = nft.applied[0]
    assert f"add table {T}" in script and "add rule" in script
    assert f"add counter {T} d1_up" in script


def test_sync_after_restart_rewrites_static_once_then_is_quiet(nft):
    """После старта бота — одна перезапись цепочки (хэш в памяти пуст),
    дальше при неизменном составе — ни одного вызова nft."""
    st = _state({1: "10.8.1.2"})
    assert rfacct.sync(st, ["awglink"], SUBS, [(1, "10.8.1.2")]) is True
    first = nft.applied[0]
    assert "flush chain" in first and "add counter inet awg_bot_acct d1_up" not in first, \
        "счётчик существующего устройства пересоздан"
    assert "delete" not in first
    n = len(nft.calls)
    assert rfacct.sync(st, ["awglink"], SUBS, [(1, "10.8.1.2")]) is False
    assert len(nft.calls) == n, "без изменений nft вызван снова"


def test_sync_link_change_rewrites_chain_but_keeps_counters(nft):
    """Добавили слот — цепочка переписывается с новым линком; счётчики
    устройств не удаляются и не пересоздаются (показания целы)."""
    st = _state({1: "10.8.1.2"})
    rfacct.sync(st, ["awglink"], SUBS, [(1, "10.8.1.2")])
    assert rfacct.sync(st, ["awglink", "awglink2"], SUBS, [(1, "10.8.1.2")]) is True
    script = nft.applied[-1]
    assert f"flush chain {T} forward" in script
    assert all('{ "awglink", "awglink2" }' in r for r in _rules(script)) and len(_rules(script)) == 4
    assert "delete" not in script
    assert "d1_up" not in script and "d1_dn" not in script


def test_sync_rewrites_static_when_rules_are_missing(nft):
    """Цепочку кто-то вычистил (правил не 4) — хэш тот же, но статика
    переписывается."""
    st = _state({1: "10.8.1.2"})
    rfacct.sync(st, ["awglink"], SUBS, [(1, "10.8.1.2")])
    broken = _state({1: "10.8.1.2"}, rules=0)
    assert rfacct.sync(broken, ["awglink"], SUBS, [(1, "10.8.1.2")]) is True
    assert len(_rules(nft.applied[-1])) == 4


def test_sync_only_devices_changed_sends_only_the_diff(nft):
    st = _state({1: "10.8.1.2"})
    rfacct.sync(st, ["awglink"], SUBS, [(1, "10.8.1.2")])
    rfacct.sync(st, ["awglink"], SUBS, [(1, "10.8.1.2"), (2, "10.8.1.3")])
    script = nft.applied[-1]
    assert "flush chain" not in script and "add table" not in script
    assert f"add counter {T} d2_up" in script


def test_sync_check_refusal_applies_nothing_and_raises(nft):
    """`nft -c` отверг — ничего не применяется, опрос получает ошибку для
    строки «учёт не идёт»; хэш не запоминается, следующий такт попробует снова."""
    nft.check_rc = 1
    with pytest.raises(rfacct.AcctError, match="nft -c"):
        rfacct.sync(None, ["awglink"], SUBS, [])
    assert nft.applied == []
    assert rfacct._static_applied == ""


def test_sync_apply_failure_raises_and_is_retried(nft):
    nft.apply_rc = 1
    with pytest.raises(rfacct.AcctError, match="nft -f"):
        rfacct.sync(None, ["awglink"], SUBS, [])
    nft.apply_rc = 0
    st = _state({})
    assert rfacct.sync(st, ["awglink"], SUBS, []) is True, "после отказа статика не переписана"


@pytest.mark.parametrize("exc, msg", [(FileNotFoundError(), "nft не найден — поставь пакет nftables"),
                                      (subprocess.TimeoutExpired("nft", 15), "таймаут"),
                                      (PermissionError("denied"), "denied")])
def test_nft_missing_or_hanging_becomes_acct_error(monkeypatch, exc, msg):
    fake = _Nft(raise_=exc)
    monkeypatch.setattr(rfacct.subprocess, "run", fake.run)
    monkeypatch.setattr(rfacct, "_static_applied", "")
    with pytest.raises(rfacct.AcctError, match=msg):
        rfacct.sync(None, ["awglink"], SUBS, [])
    with pytest.raises(rfacct.AcctError, match=msg):
        rfacct.read()


def test_apply_of_empty_script_does_not_call_nft(nft):
    rfacct.apply("  \n")
    assert nft.calls == []


# ── разбор nft -j ────────────────────────────────────────────────────────────

SAMPLE = {"nftables": [
    {"metainfo": {"version": "1.0.2", "release_name": "Lester Gooch", "json_schema_version": 1}},
    {"table": {"family": "inet", "name": "awg_bot_acct", "handle": 42}},
    {"set": {"family": "inet", "name": "clients4", "table": "awg_bot_acct", "type": "ipv4_addr",
             "handle": 2, "flags": ["interval"],
             "elem": [{"prefix": {"addr": "10.8.1.0", "len": 24}}]}},
    {"map": {"family": "inet", "name": "rf_dev_up", "table": "awg_bot_acct", "type": "ipv4_addr",
             "handle": 4, "map": "counter", "elem": [["10.8.1.5", "d12_up"], ["10.8.1.6", "d13_up"]]}},
    {"map": {"family": "inet", "name": "rf_dev_dn", "table": "awg_bot_acct", "type": "ipv4_addr",
             "handle": 5, "map": "counter", "elem": [["10.8.1.5", "d12_dn"], ["10.8.1.6", "d13_dn"]]}},
    {"counter": {"family": "inet", "name": "rf_up", "table": "awg_bot_acct", "handle": 6,
                 "packets": 10, "bytes": 1500}},
    {"counter": {"family": "inet", "name": "rf_dn", "table": "awg_bot_acct", "handle": 7,
                 "packets": 20, "bytes": 30000}},
    {"counter": {"family": "inet", "name": "d12_up", "table": "awg_bot_acct", "handle": 8,
                 "packets": 3, "bytes": 900}},
    {"counter": {"family": "inet", "name": "d12_dn", "table": "awg_bot_acct", "handle": 9,
                 "packets": 4, "bytes": 20000}},
    {"counter": {"family": "inet", "name": "d13_up", "table": "awg_bot_acct", "handle": 10,
                 "packets": 0, "bytes": 0}},
    {"counter": {"family": "inet", "name": "d13_dn", "table": "awg_bot_acct", "handle": 11,
                 "packets": 0, "bytes": 0}},
    {"chain": {"family": "inet", "table": "awg_bot_acct", "name": "forward", "handle": 12,
               "type": "filter", "hook": "forward", "prio": 10, "policy": "accept"}},
] + [{"rule": {"family": "inet", "table": "awg_bot_acct", "chain": "forward", "handle": 13 + i,
               "expr": []}} for i in range(4)] + [
    # чужая таблица с одноимёнными объектами — не наша
    {"table": {"family": "inet", "name": "awg_bot_guard", "handle": 3}},
    {"counter": {"family": "inet", "name": "rf_up", "table": "awg_bot_guard", "handle": 1,
                 "packets": 1, "bytes": 999999}},
]}


def test_parse_reads_handle_counters_map_elements_and_rules():
    st = rfacct.parse(SAMPLE)
    assert st is not None
    assert st.handle == 42, "handle — половина поколения; ошибка тут — скачок или потеря"
    assert st.counters == {"rf_up": 1500, "rf_dn": 30000, "d12_up": 900, "d12_dn": 20000,
                           "d13_up": 0, "d13_dn": 0}, "подмешан счётчик чужой таблицы или потерян свой"
    assert st.up == {"10.8.1.5": "d12_up", "10.8.1.6": "d13_up"}
    assert st.dn == {"10.8.1.5": "d12_dn", "10.8.1.6": "d13_dn"}
    assert st.rules == 4


def test_parsed_state_needs_no_further_sync():
    """Разобранное ядро с тем же составом — дифф пуст: разбор и отрисовка
    говорят на одном языке имён и адресов."""
    st = rfacct.parse(SAMPLE)
    assert rfacct.render_devices(st, [(12, "10.8.1.5"), (13, "10.8.1.6")]) == ""


def test_parse_without_our_table_is_none():
    doc = {"nftables": [{"metainfo": {"version": "1.0.2"}},
                        {"table": {"family": "inet", "name": "awg_bot_guard", "handle": 3}}]}
    assert rfacct.parse(doc) is None
    assert rfacct.parse({}) is None


def _read_with(monkeypatch, rc, out=b"", err=b""):
    def run(argv, input=None, capture_output=True, timeout=None):
        assert argv[:2] == ["nft", "-j"], argv
        return subprocess.CompletedProcess(argv, rc, out, err)
    monkeypatch.setattr(rfacct.subprocess, "run", run)
    return rfacct.read()


def test_read_parses_the_json_listing(monkeypatch):
    st = _read_with(monkeypatch, 0, json.dumps(SAMPLE).encode())
    assert st.handle == 42 and st.counters["d12_dn"] == 20000


def test_read_missing_table_is_none_not_an_error(monkeypatch):
    """Таблицы нет (первый запуск, flush ruleset) — это не «учёт не идёт»:
    синхронизация её создаст."""
    err = b"Error: No such file or directory\nlist table inet awg_bot_acct\n"
    assert _read_with(monkeypatch, 1, err=err) is None


def test_read_other_failure_and_garbage_raise(monkeypatch):
    with pytest.raises(rfacct.AcctError, match="nft list table"):
        _read_with(monkeypatch, 1, err=b"Error: Operation not permitted")
    with pytest.raises(rfacct.AcctError, match="nft -j"):
        _read_with(monkeypatch, 0, out=b"{not json")


# ── дельты ────────────────────────────────────────────────────────────

def test_delta_without_stored_generation_is_zero():
    """Первый запуск или старая БД из бэкапа: накопленное в ядре — не этого
    месяца, в дельту не идёт."""
    assert rfacct.delta("", "boot:42", None, 5000) == 0
    assert rfacct.delta("", "boot:42", 100, 5000) == 0


def test_delta_on_new_generation_is_the_whole_current_value():
    """Перезагрузка или flush ruleset: счётчики с нуля — всё текущее значение
    и есть прирост; разность с прежней базой дала бы скачок или минус."""
    assert rfacct.delta("boot:42", "boot2:42", 9000, 300) == 300
    assert rfacct.delta("boot:42", "boot:43", 100, 5000) == 5000, \
        "после пересоздания таблицы вычтена чужая база"


def test_delta_without_base_is_the_whole_current_value():
    """Новое устройство: счётчик родился нулём при синхронизации — всё, что
    на нём есть, накоплено после."""
    assert rfacct.delta("boot:42", "boot:42", None, 700) == 700


def test_delta_on_counter_drop_is_the_current_value():
    assert rfacct.delta("boot:42", "boot:42", 9000, 200) == 200


def test_delta_on_growth_is_the_difference():
    assert rfacct.delta("boot:42", "boot:42", 1000, 1500) == 500
    assert rfacct.delta("boot:42", "boot:42", 1500, 1500) == 0


def test_the_expected_rule_count_is_what_the_static_part_writes():
    """RULES — сколько правил опрос ждёт в цепочке: меньше или больше, чем
    пишет render_static, — и таблица переписывалась бы на каждом опросе, а
    `awg-bot doctor` вечно жёлтый «ждём N»."""
    assert len(_rules(rfacct.render_static(["awglink"], ["10.8.1.0/24"]))) == rfacct.RULES
    assert len(_rules(rfacct.render_static(["awglink", "awglink2"], ["10.8.1.0/24"]))) == rfacct.RULES


@pytest.mark.parametrize("name,want", [
    ("d12_up", True), ("d1_up", True),
    ("d12_dn", False), (rfacct.COUNTER_UP, False), (rfacct.COUNTER_DN, False), ("x12_up", False),
])
def test_a_device_counter_is_told_apart_by_name(name, want):
    """Счётчик устройства — пара d<ID>_up/d<ID>_dn, считаем по _up: итоговые
    rf_up/rf_dn устройствами не считаются (doctor не пишет «+2 устройства»)."""
    assert rfacct.is_device_counter(name) is want
    if want:
        assert name == rfacct.counter_names(int(name[1:-3]))[0]
