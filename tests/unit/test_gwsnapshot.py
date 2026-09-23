"""
Снимок состояния шлюза для канала (domain/gwsnapshot): закрытый список полей,
запрещённые имена, инвариант «в сеть не ходим», дельта, приём недоверенных
данных.

Снимок — не описание машины в квартире, а ответ на три вопроса, которые задаёт
сам ВПС. Два сторожа здесь важнее остальных тестов файла: список имён (без него
в снимок со временем натечёт всё, что «и так снимается») и запрет сетевых
вызовов (без него сборка снимка каждым тиком превратится в периодический поход
наружу с адреса квартиры — ровно тот маячок, ради избавления от которого
переписывали keepalive линка и зонд живости).
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.core import config
from awgbot.domain import gwsnapshot
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import awglock, gwguard
from awgbot.infra.db import Database

# Имена полей — ЯВНЫМ списком, а не импортом из модуля: сторож границы обязан
# падать от правки кода, а не переезжать вместе с ней.
FACTS = ("bundle", "link_contract", "plumbing_gen", "mark_status",
         "agent_version", "awg_generation")
BUNDLE = ("lan_mode", "home_subnets", "resolver", "peer_home_nets", "admin_ips")
SERVICE = ("rev", "ts", "boot_id")
VERDICTS = ("peer_nets", "egress_ok")

# Всё, что решает сам агент и о чём он говорит в своём чате. Появление любого
# из этих имён в снимке означает, что на ВПС завели второй источник правды.
FORBIDDEN = ("temp", "cpu", "ram", "disk", "throttled", "smart", "uptime_seconds",
             "egress_ms", "rx", "tx", "month_rx", "month_tx", "ssh", "checks",
             "alerts", "lan_pkts", "dns_pkts", "own_vpn", "own_ru", "update_tag")


class _Pi:
    """Малина под снимком: что стоит в юните обвязки, что в таблице, какой
    конфиг линка и какое поколение поставки. Ни одного настоящего чтения с
    хоста — всё берётся отсюда."""

    def __init__(self, tmp_path, monkeypatch):
        self.env = {"LAN_MODE": "1", "HOME_SUBNETS": "192.168.68.0/24",
                    "RESOLVER": "10.9.1.1", "PEER_HOME_NETS": "192.168.2.0/24"}
        self.admin_ips = ["10.9.1.2", "10.9.1.3"]
        self.chains = {"input", "tunnel_in", "forward", "ssh_in"}
        self.peer_nets4 = {"192.168.2.0/24"}
        self.generation = 1
        self.conf = tmp_path / "awglink.conf"
        self.conf.write_text("# awg-bot: контракт линка 1\n[Interface]\nPrivateKey = x\n",
                             encoding="utf-8")
        # ADMIN_IPS тоже из юнита: блок bundle собирается одним проходом по
        # gwlink.BUNDLE_KEYS, отдельного чтения списка админа больше нет
        monkeypatch.setattr(gwguard, "unit_env",
                            lambda k: " ".join(self.admin_ips) if k == "ADMIN_IPS"
                            else self.env.get(k, ""))
        monkeypatch.setattr(gwguard, "unit_admin_ips", lambda: list(self.admin_ips))
        monkeypatch.setattr(awglock, "generation", lambda: self.generation)
        monkeypatch.setattr(config, "GW_LINK_CONF", str(self.conf))
        monkeypatch.setattr(config, "INSTALLED_VERSION", "3.1.0")
        monkeypatch.setattr(gwsnapshot, "boot_id", lambda: "b" * 36)

    def info(self) -> dict:
        return {"sets": {"peer_nets4": set(self.peer_nets4)}, "chains": set(self.chains),
                "masq_ifaces": set(), "ssh_ports": {}}

    def snap(self, **kw) -> dict:
        missing = [n for n in self.env.get("PEER_HOME_NETS", "").split()
                   if n not in self.peer_nets4]
        peer = None if not self.env.get("PEER_HOME_NETS") else (not missing, missing)
        # rev и ts снимок не выбирает: их ставит клиент по месту в сессии и
        # конверт по времени отправки — здесь делаем то же поверх собранного
        stamp = {k: kw.pop(k) for k in ("rev", "ts") if k in kw}
        args = {"mark_status": "confirmed", "egress_ok": True, "guard_info": self.info(),
                "peer_nets": peer}
        args.update(kw)
        snap = gwsnapshot.collect(**args)
        snap.update(stamp)
        return snap


@pytest.fixture()
def pi(tmp_path, monkeypatch):
    return _Pi(tmp_path, monkeypatch)


def _keys(obj, out=None) -> set:
    """Все имена полей сериализации, включая вложенные."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


# ── закрытый список полей ────────────────────────────────────────────────────

def test_the_snapshot_carries_exactly_the_agreed_fields_and_nothing_else(pi):
    """Сторож границы. Тест обязан падать на появлении любого нового поля: ВПС
    узнаёт только то, по чему сам принимает решения, и каждое лишнее значение —
    это второй источник правды, который всегда отстаёт от чата агента."""
    snap = pi.snap()
    assert set(snap) == set(FACTS + VERDICTS + SERVICE), (
        "состав снимка разошёлся с утверждённым списком: "
        f"лишние {sorted(set(snap) - set(FACTS + VERDICTS + SERVICE))}, "
        f"пропали {sorted(set(FACTS + VERDICTS + SERVICE) - set(snap))}")
    assert set(snap["bundle"]) == set(BUNDLE), "блок «что применено из конфигурации» изменился"
    assert set(gwsnapshot.FIELDS) == set(FACTS + ("peer_nets", "egress_ok") + SERVICE)
    assert set(gwsnapshot.BUNDLE_FIELDS) == set(BUNDLE)


def test_no_forbidden_name_ever_appears_in_the_snapshot(pi):
    """Железо, монитор здоровья, SSH, внутренности локальной сети и всё про
    обновления агента на ВПС не едут: решения по ним принимает агент, и он же о
    них говорит в своём чате. Имя из этого списка в снимке — начало второго
    экрана, который врёт с задержкой в тик."""
    snap = pi.snap()
    found = sorted(_keys(snap) & set(FORBIDDEN))
    assert not found, f"в снимок протекло то, что остаётся на шлюзе: {found}"


def test_the_single_verdict_travels_and_disappears_with_its_function(pi):
    """Вердикт едет на ВПС по единственному исключению: он объясняет отказ
    функции, включённой кнопкой на ВПС. Нет функции — нет и поля: иначе экран
    рисовал бы «всё хорошо» там, где проверять нечего."""
    pi.peer_nets4 = set()
    snap = pi.snap()
    assert snap["peer_nets"] == {"ok": False, "missing": ["192.168.2.0/24"]}, (
        "вердикт обязан ехать структурой — текст для человека рисует ВПС")
    pi.env["PEER_HOME_NETS"] = ""
    assert "peer_nets" not in pi.snap(), "функции на шлюзе нет, а поле осталось"


def test_the_gateway_view_outside_is_a_bare_boolean(pi):
    """Миллисекунды зонда наружу в снимок не едут: они меняются каждым тиком и
    превратили бы дельту в метроном с периодом тика. Едет только «есть / нет /
    не смотрели»."""
    for value in (True, False, None):
        assert pi.snap(egress_ok=value)["egress_ok"] is value
    assert "egress_ms" not in pi.snap()


# ── инвариант: снимок не ходит в сеть ────────────────────────────────────────

def _no_network(monkeypatch):
    """Всё, чем можно выйти наружу, — мина: тронул, и тест красный."""
    import socket
    import urllib.request

    from awgbot.infra import updates

    def boom(*a, **k):
        raise AssertionError("сборка снимка пошла в сеть — инвариант канала нарушен")

    for mod, name in ((socket, "socket"), (socket, "getaddrinfo"),
                      (socket, "create_connection"), (urllib.request, "urlopen"),
                      (updates, "list_releases"), (updates, "next_release"),
                      (subprocess, "run"), (subprocess, "Popen")):
        monkeypatch.setattr(mod, name, boom)


def test_collecting_a_snapshot_touches_neither_the_network_nor_a_subprocess(pi, monkeypatch):
    """Инвариант этапа, а не оговорка в скобках. Снимок снимается каждым тиком
    монитора; поле, требующее запроса наружу, превратило бы отрисовку чужого
    экрана в периодический поход с адреса квартиры. Этот тест и должен
    сломаться у того, кто добавит поле «ну это же всего один запрос»."""
    _no_network(monkeypatch)
    snap = pi.snap()
    assert snap["agent_version"] == "3.1.0" and snap["awg_generation"] == 1, (
        "версия агента и поколение поставки читаются локально — это не про обновления")


def test_the_agent_snapshot_of_a_whole_service_stays_local_too(tmp_path, pi, monkeypatch):
    """Тот же инвариант на боевой точке входа: gw_snapshot() берёт то, что тик
    уже снял, и своего nft не зовёт. Ходит в сеть здесь — значит ходит на живой
    малине каждые три минуты."""
    db = Database(tmp_path / "gw.db")
    db.init_schema()
    svc = GatewayServices(db)
    svc._guard_info = pi.info()
    svc.db.set_state(svc._GW_MARK_KEY, "confirmed")
    _no_network(monkeypatch)
    snap = svc.gw_snapshot()
    assert snap["mark_status"] == "confirmed"
    assert snap["egress_ok"] is None and snap["ts"] == "", (
        "тика ещё не было — снимок честно говорит «не знаем», а не идёт мерить сам")
    db.close()


def test_a_new_release_on_github_does_not_change_the_snapshot_by_a_byte(pi, monkeypatch):
    """Информации об обновлениях в снимке нет вовсе. Иначе релиз — событие,
    никак не связанное ни с квартирой, ни с сервером, — породил бы дельту у всех
    слотов почти одновременно, а синхронный всплеск на нескольких линках сам по
    себе рисунок."""
    from awgbot.infra import updates
    before = pi.snap()
    monkeypatch.setattr(updates, "list_releases",
                        lambda: [type("R", (), {"tag": "v9.9.9"})()], raising=False)
    assert gwsnapshot.delta(before, pi.snap()) == {}, "снимок услышал чужой календарь"


# ── дельта ───────────────────────────────────────────────────────────────────

def test_delta_of_the_first_snapshot_is_everything_but_the_counters(pi):
    """Первое подключение отправляет полный снимок; `rev` и `ts` в сравнение не
    идут — они меняются всегда и сами по себе не новость."""
    d = gwsnapshot.delta(None, pi.snap())
    assert "rev" not in d and "ts" not in d and d["bundle"] == pi.snap()["bundle"]


def test_delta_is_empty_when_only_rev_and_ts_moved(pi):
    """Обычное состояние канала на дни: тик прошёл, номер и метка времени
    другие, а сказать нечего — и не уходит ничего."""
    first = pi.snap(rev=1, ts="2026-09-22T20:00:00+03:00")
    second = pi.snap(rev=2, ts="2026-09-22T20:03:00+03:00")
    assert gwsnapshot.delta(first, second) == {}


@pytest.mark.parametrize("change, field", [
    (lambda pi: pi.env.__setitem__("HOME_SUBNETS", "192.168.1.0/24"), "bundle"),
    (lambda pi: pi.env.__setitem__("LAN_MODE", "0"), "bundle"),
    (lambda pi: pi.admin_ips.append("10.9.1.9"), "bundle"),
    (lambda pi: pi.chains.discard("ssh_in"), "plumbing_gen"),
    (lambda pi: pi.__setattr__("generation", 2), "awg_generation"),
    (lambda pi: pi.conf.write_text("# awg-bot: контракт линка 2\n", encoding="utf-8"), "link_contract"),
])
def test_delta_catches_a_changed_fact(pi, change, field):
    """Ровно эти события и должны будить канал: перевыпуск конфигурации,
    обновление поставки, смена обвязки. Прозеванное изменение означает, что ВПС
    неделями показывает старое как настоящее."""
    before = pi.snap()
    change(pi)
    d = gwsnapshot.delta(before, pi.snap())
    assert set(d) == {field}, f"ожидали новость про {field}, а пришло {sorted(d)}"


def test_delta_catches_the_agent_version_and_the_mark(pi, monkeypatch):
    """Версия агента отвечает на вопрос «что стоит на той стороне» — можно ли
    предлагать человеку то, чего старый агент не поймёт. Пометка отвечает на
    «мой ли это шлюз»."""
    before = pi.snap()
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.2.0")
    assert gwsnapshot.delta(before, pi.snap()) == {"agent_version": "3.2.0"}
    before = pi.snap()
    assert gwsnapshot.delta(before, pi.snap(mark_status="foreign")) == {"mark_status": "foreign"}


def test_delta_carries_the_verdict_when_peer_nets_break_and_its_loss_when_the_function_goes(pi):
    """Человек включил доступ между подсетями тумблером на ВПС и ждёт
    результата там же. Отвалившийся набор на шлюзе — единственная причина, по
    которой вердикт агента вообще едет на сервер."""
    before = pi.snap()
    pi.peer_nets4 = set()
    assert gwsnapshot.delta(before, pi.snap()) == {
        "peer_nets": {"ok": False, "missing": ["192.168.2.0/24"]}}
    before = pi.snap()
    pi.env["PEER_HOME_NETS"] = ""          # функцию выключили перевыпуском конфигурации
    d = gwsnapshot.delta(before, pi.snap())
    assert d["peer_nets"] is None, (
        "исчезнувшее поле — тоже новость: функцию выключили, и вердикта больше нет")
    assert d["bundle"]["peer_home_nets"] == "" and set(d) == {"peer_nets", "bundle"}


def test_delta_speaks_on_the_egress_verdict_but_not_on_its_measurements(pi):
    """Зонд наружу меряется каждым тиком: вердикт меняется редко, а числа —
    всегда. Едет только вердикт."""
    before = pi.snap(egress_ok=True)
    assert gwsnapshot.delta(before, pi.snap(egress_ok=True)) == {}
    assert gwsnapshot.delta(before, pi.snap(egress_ok=False)) == {"egress_ok": False}


def test_a_reboot_alone_is_not_news_for_the_delta(pi, monkeypatch):
    """boot_id едет в снимке, но в сравнение не идёт: после перезагрузки агент
    всё равно подключается заново и начинает с полного снимка, а дельта «сменился
    boot_id» была бы шумом."""
    before = pi.snap()
    monkeypatch.setattr(gwsnapshot, "boot_id", lambda: "c" * 36)
    assert gwsnapshot.delta(before, pi.snap()) == {}


def test_three_deltas_add_up_to_the_full_snapshot(pi):
    """Наложение дельт обязано давать ровно то же, что полный снимок в конце:
    иначе карточка слота показывала бы смесь старого и нового, и человек чинил
    бы несуществующее расхождение."""
    stored = pi.snap(rev=1)
    steps = [lambda: pi.env.__setitem__("HOME_SUBNETS", "192.168.1.0/24"),
             lambda: pi.__setattr__("generation", 2),
             lambda: pi.peer_nets4.clear()]
    prev = stored
    for rev, step in enumerate(steps, start=2):
        step()
        cur = pi.snap(rev=rev)
        stored = gwsnapshot.apply_delta(stored, gwsnapshot.delta(prev, cur))
        prev = cur
    full = pi.snap(rev=1)
    assert {k: v for k, v in stored.items() if k not in ("rev", "ts")} == \
           {k: v for k, v in full.items() if k not in ("rev", "ts")}


def test_apply_delta_removes_a_field_it_was_told_to_drop(pi):
    """None в дельте — «поля больше нет», а не «значение None»: спутать их
    значит оставить на экране вердикт выключенной функции."""
    stored = pi.snap()
    out = gwsnapshot.apply_delta(stored, {"peer_nets": None, "rev": 7, "ts": "потом"})
    assert "peer_nets" not in out
    assert out["ts"] == stored["ts"], "служебные поля дельты не затирают снимок"


# ── чтения с хоста ───────────────────────────────────────────────────────────

def test_link_contract_is_read_from_the_comment_the_bundle_wrote(tmp_path):
    """Контракт линка штампует бандл комментарием. Нечитаемый или старый конфиг
    — пустая строка: это тоже ответ, и врать тут нечем."""
    good = tmp_path / "ok.conf"
    good.write_text("[Interface]\n# awg-bot: контракт линка 3\n", encoding="utf-8")
    assert gwsnapshot.link_contract(str(good)) == "3"
    old = tmp_path / "old.conf"
    old.write_text("[Interface]\nPrivateKey = x\n", encoding="utf-8")
    assert gwsnapshot.link_contract(str(old)) == ""
    assert gwsnapshot.link_contract(str(tmp_path / "нет.conf")) == ""


def test_plumbing_generation_tells_new_old_and_missing():
    """Поколение обвязки — ответ на «старого образца или нового»: сегодня это
    видно только в чате агента, а решение о перевыпуске принимает человек на
    ВПС."""
    assert gwsnapshot.plumbing_gen({"chains": {"input", "ssh_in"}}) == "new"
    assert gwsnapshot.plumbing_gen({"chains": {"input", "tunnel_in"}}) == "old"
    assert gwsnapshot.plumbing_gen(None) == "none"
    assert gwsnapshot.plumbing_gen({}) == "none"


# ── приём недоверенных данных ────────────────────────────────────────────────

def test_sanitize_cuts_monsters_and_drops_everything_it_does_not_know():
    """Снимок приходит с малины, а она может быть скомпрометирована: для ВПС это
    недоверенные данные для отрисовки, и ничего больше. Поле на мегабайт не
    должно ни лечь в БД, ни доехать до экрана."""
    raw = {"bundle": {"lan_mode": "1", "home_subnets": "Я" * 5000, "чужое": "x"},
           "agent_version": "3.1.0", "awg_generation": "2", "egress_ok": True,
           "link_contract": 1, "plumbing_gen": "new", "mark_status": "confirmed",
           "boot_id": "b" * 36, "ts": "2026-09-22T20:00:00+03:00", "rev": "4",
           "peer_nets": {"ok": True, "missing": ["10.0.0.0/8"] * 500},
           "temp": 51.2, "ssh": {"port": 2222}, "checks": [1, 2, 3], "exec": "rm -rf /"}
    out = gwsnapshot.sanitize(raw)
    assert set(out) == set(FACTS + VERDICTS + ("boot_id", "ts", "rev"))
    assert set(out["bundle"]) == set(BUNDLE), "неизвестные ключи внутри bundle отброшены"
    assert len(out["bundle"]["home_subnets"]) == 512, "длина значения не ограничена"
    assert out["bundle"]["peer_home_nets"] == "", "недосланное поле стало пустым, а не пропало"
    assert len(out["peer_nets"]["missing"]) == 64, "список в вердикте не ограничен"
    assert out["awg_generation"] == 2 and out["rev"] == 4, "числа приводятся к числам"
    assert not set(out) & set(FORBIDDEN)


def test_sanitize_survives_lies_about_types_without_raising():
    """Битое или враждебное значение не имеет права уронить бота: хуже отказа
    отрисовать карточку только отказ отвечать вовсе."""
    for raw in ({"bundle": "не словарь", "awg_generation": "поколение",
                 "egress_ok": "да", "peer_nets": [1, 2], "rev": None},
                {"peer_nets": {"ok": "x", "missing": "не список"}},
                {}, {"bundle": None}):
        out = gwsnapshot.sanitize(raw)
        assert isinstance(out, dict)
    out = gwsnapshot.sanitize({"bundle": "не словарь", "awg_generation": "поколение",
                               "egress_ok": "да", "peer_nets": [1, 2]})
    assert "bundle" not in out and "awg_generation" not in out
    assert out["egress_ok"] is None, "«да» строкой — это не булево, а «не знаем»"


def test_html_in_a_value_is_kept_verbatim_for_the_screen_to_escape():
    """Экранирование — забота текстов (они это делают через _e). Снимок хранит
    то, что прислали, иначе разметка ломалась бы дважды."""
    out = gwsnapshot.sanitize({"bundle": {"home_subnets": "<b>10.0.0.0/8</b>"}})
    assert out["bundle"]["home_subnets"] == "<b>10.0.0.0/8</b>"


def test_an_unknown_field_from_a_newer_agent_is_simply_ignored():
    """Совместимость в обе стороны: новый агент, старый ВПС — незнакомое поле
    отбрасывается молча, сессия живёт. Иначе выпуск новой версии агента ронял бы
    канал у тех, кто ещё не обновил сервер."""
    out = gwsnapshot.sanitize({"agent_version": "9.9.9", "будущее_поле": {"a": 1},
                               "bundle": {"lan_mode": "1"}})
    assert out["agent_version"] == "9.9.9" and "будущее_поле" not in out


def test_an_old_agent_that_sends_half_the_fields_is_still_accepted():
    """Старый агент, новый ВПС: часть полей он не пришлёт вовсе. Снимок должен
    сохраниться таким, какой есть, — «шлюз ещё не сообщал» честнее пустого
    экрана."""
    out = gwsnapshot.sanitize({"agent_version": "3.0.0", "ts": "2026-09-22T20:00:00+03:00"})
    assert out == {"agent_version": "3.0.0", "ts": "2026-09-22T20:00:00+03:00"}
