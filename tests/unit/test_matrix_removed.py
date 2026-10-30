"""Unit: сторож снятой матрицы взглядов резерва. Переезд о сервере решает
только кворум (кандидат, лидер, голосующие); прежний путь — случаи 1–6, Н3/Н4,
доли прямой у шлюзов по слову `need`, демпфер случая 3, взгляды `seen`/`view`
— снят целиком, вместе с ключом `reserve.quorum.act` и шимами «второй сервер
4.1.x».

Проверка — по AST кода `awgbot/` (комментарии и докстринги не в счёт),
словарям каналов `util/pairproto`, шаблону `conf/app.yaml`, модулю
уведомлений и строковым литералам интерфейса. Запретные фрагменты текста
собираются из частей, чтобы сторож не ловил сам себя.

Цена ошибки: вернувшийся кусок матрицы — второй путь переезда рядом с
кворумом, без большинства (два живых бота у одних пользователей); оставшийся
вид в словаре канала — старый агент снова шлёт `seen`/`share`, и резерв
что-то с ними делает; оставшийся ключ `act` — выключатель, который больше
ничего не выключает, а админ думает, что выключил; строка «переезды по
прежним правилам» — админ ждёт поведения, которого нет.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
import yaml

from awgbot.domain.notices import reserve as rn
from awgbot.util import pairproto as pp

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[2]
AWGBOT = REPO / "awgbot"
PY_FILES = [p for p in sorted(AWGBOT.rglob("*.py")) if "__pycache__" not in p.parts]
RESERVE_DIRS = (AWGBOT / "domain" / "services" / "reserve", AWGBOT / "domain" / "gateway" / "reserve")

# имена снятого — нигде в awgbot/
GONE_NAMES = frozenset({
    "MOVE_CASES", "RF_CASES", "reserve_rf_tick", "reserve_view_in", "_nogw_worth", "_sees_own_gateway",
    "_slot_links_down", "_liveness_views", "_quorum_rules", "reserve_quorum_act", "reserve_quorum_peer_quo1",
    "reserve_gw_views", "_voters_body", "_reserve_need_in", "_view_notice", "reserve_primary_seen", "_uplink_probe",
    # прежнее API окна кода по случаю и такт матрицы
    "_code_window", "_code_window_quiet", "reserve_vote_eval", "reserve_vote_tick", "_auto_move", "_need_send",
    "_share_in", "_hosting_extra", "reserve_rf_line", "reserve_seen_body", "reserve_gw_word", "reserve_gw_pong_in",
    "reserve_view_ready",
})
# общие слова — только в пакетах резерва (в обновлениях `decide` — свой параметр)
GONE_IN_RESERVE = frozenset({"decide", "_voters"})


def _names(tree: ast.AST):
    """(строка, имя) каждого определения и обращения; строковый второй аргумент
    hasattr/getattr/setattr — тоже обращение (так прячется вызов снятого)."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield n.lineno, n.name
        elif isinstance(n, ast.Name):
            yield n.lineno, n.id
        elif isinstance(n, ast.Attribute):
            yield n.lineno, n.attr
        elif isinstance(n, ast.alias):
            yield getattr(n, "lineno", 0), n.name.rsplit(".", 1)[-1]
            if n.asname:
                yield getattr(n, "lineno", 0), n.asname
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("hasattr", "getattr", "setattr")
              and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)):
            yield n.lineno, n.args[1].value


def _hits(files, banned) -> list[str]:
    out = []
    for p in files:
        for line, name in _names(ast.parse(p.read_text(encoding="utf-8"))):
            if name in banned:
                out.append(f"{p.relative_to(REPO)}:{line} {name}")
    return out


def test_the_collector_sees_names_at_all():
    """Сам сборщик жив: пустой обход дал бы «чисто» даром."""
    got = {name for _l, name in _names(ast.parse((AWGBOT / "domain" / "services" / "reserve" / "codewin.py")
                                                 .read_text(encoding="utf-8")))}
    assert {"reserve_code_window_set", "reserve_code_link_watch", "reserve_code_window_quiet"} <= got, sorted(got)[:20]


def test_no_name_of_the_matrix_is_left_in_the_code():
    hits = _hits(PY_FILES, GONE_NAMES)
    assert not hits, "в коде остались имена снятой матрицы:\n" + "\n".join(hits)


def test_no_generic_name_of_the_matrix_is_left_in_the_reserve_packages():
    files = [p for p in PY_FILES if any(d in p.parents for d in RESERVE_DIRS)]
    assert files, "пакеты резерва не найдены — сторож ничего не проверил"
    hits = _hits(files, GONE_IN_RESERVE)
    assert not hits, "в пакетах резерва остались decide/_voters:\n" + "\n".join(hits)


@pytest.mark.parametrize("module", ["rfblock", "vote"])
def test_the_matrix_modules_are_gone_from_the_server_reserve_package(module):
    path = AWGBOT / "domain" / "services" / "reserve" / f"{module}.py"
    assert not path.exists(), f"модуль матрицы {path.relative_to(REPO)} вернулся"
    imports = []
    for p in PY_FILES:
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.endswith(f"services.reserve.{module}"):
                imports.append(f"{p.relative_to(REPO)}:{n.lineno}")
            if isinstance(n, ast.ImportFrom) and n.level and n.module == module and "services/reserve" in str(p):
                imports.append(f"{p.relative_to(REPO)}:{n.lineno}")
    assert not imports, f"импорт снятого модуля {module}: {imports}"


# ── словари каналов ─────────────────────────────────────────────────────────

def test_the_pair_channel_has_no_view_and_st_has_no_voters():
    assert "view" not in pp.PAIR_KINDS, "вид `view` канала пары остался"
    assert "voters" not in pp.PAIR_KINDS["st"][1], "поле `voters` в `st` осталось"
    # kind (dead | rf, Р6) — вид переезда кворума, необязательный; полей матрицы нет
    assert set(pp.PAIR_KINDS["ask"][1]) == {"case", "since", "kind"}, f"поля ask: {sorted(pp.PAIR_KINDS['ask'][1])}"
    assert pp.OPTIONAL["ask"] == frozenset({"kind"})


@pytest.mark.parametrize("kind", ["seen", "view", "need", "share"])
def test_the_guest_channel_has_no_matrix_kind(kind):
    assert kind not in pp.GUEST_KINDS, f"вид `{kind}` гостевого канала остался"
    assert kind not in pp.OPTIONAL, f"необязательные поля вида `{kind}` остались"


def test_the_guest_hello_is_slot_caps_and_nonce_only():
    fields = set(pp.GUEST_KINDS["hello"][1])
    assert fields == {"slot", "caps", "nonce"}, f"поля hello гостевого канала: {sorted(fields)}"


@pytest.mark.parametrize("kind, body, sender", [
    ("seen", {"seen": ""}, pp.GATEWAY),
    ("share", {"share": "ab" * 16, "m_gen": 1}, pp.GATEWAY),
    ("need", {"case": "1", "since": ""}, pp.RESERVE),
    ("view", {"seen": "", "voters": [], "in": "idle", "pkg_gen": 1}, pp.RESERVE),
])
def test_a_matrix_word_from_an_older_side_breaks_the_guest_session(kind, body, sender):
    with pytest.raises(pp.ProtocolError):
        pp.check(pp.GUEST_PROTO, kind, dict(body), sender)


@pytest.mark.parametrize("field", ["seen", "m_gen"])
def test_matrix_fields_of_an_older_hello_do_not_get_past_the_dictionary(field):
    """Словарь отбрасывает поля, которых не знает: `seen`/`m_gen` прежнего агента
    до резерва не доходят — ни взгляда, ни поколения доли из hello он не берёт."""
    body = {"slot": 1, "caps": ["rsv1", "quo1"], "nonce": "", field: 1 if field == "m_gen" else ""}
    out = pp.check(pp.GUEST_PROTO, "hello", body, pp.GATEWAY)
    assert field not in out and out["slot"] == 1, out


def test_voters_of_an_older_st_do_not_get_past_the_dictionary():
    from awgbot.domain.services.reserve import compat
    st = {"ready": True, "name": "Beta", "version": "4.2.0", "caps": list(compat.CAPS), "gen": 0, "ports": [1],
          "self": "", "out": "home", "in": "idle", "pkg": {}, "admin_id": 1, "admin_opened": False, "gw": True}
    out = pp.check(pp.PAIR_PROTO, "st", {**st, "voters": [{"slot": 1}]}, pp.INVITER)
    assert out["name"] == "Beta" and "voters" not in out, out


# ── шаблон и уведомления ────────────────────────────────────────────────────

def test_the_app_template_has_no_quorum_act():
    app = yaml.safe_load((REPO / "conf" / "app.yaml").read_text(encoding="utf-8"))
    quorum = ((app.get("reserve") or {}).get("quorum") or {})
    assert quorum, "в шаблоне нет reserve.quorum — сторож ничего не проверил"
    assert "act" not in quorum, "ключ reserve.quorum.act остался в шаблоне"


@pytest.mark.parametrize("name", ["rf_block_warning", "nogw_warning", "case4_notice", "ask_refused",
                                  "gateway_rf_warning", "gateway_case4", "code_needed_reserve", "code_needed_primary"])
def test_the_matrix_notices_are_gone(name):
    assert not hasattr(rn, name), f"уведомление матрицы {name} осталось"


# ── строки интерфейса ───────────────────────────────────────────────────────

FRAGMENTS = [
    "переезды по " + "прежним правилам",
    "падение подтвердят " + "шлюзы",
    "похоже, дело в " + "сети шлюза",
    "ради РФ-доступа через " + "его шлюзы",
]


def _strings(tree: ast.AST):
    """Строковые литералы и куски f-строк (докстринги — нет)."""
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            yield n.lineno, n.value


def test_no_interface_string_promises_the_old_rules():
    hits = []
    for p in PY_FILES:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for line, text in _strings(tree):
            for frag in FRAGMENTS:
                if frag in text:
                    hits.append(f"{p.relative_to(REPO)}:{line} «{frag}»")
    assert not hits, "строки снятой матрицы в интерфейсе:\n" + "\n".join(hits)


def test_the_fragment_check_does_catch_a_split_string():
    """Сторож видит фрагмент и тогда, когда строка склеена из литералов (так
    пишутся длинные тексты карточки): AST сливает соседние литералы в один."""
    tree = ast.parse('x = ("…переезды по прежним "\n     "правилам")\n')
    assert any(FRAGMENTS[0] in text for _l, text in _strings(tree))
