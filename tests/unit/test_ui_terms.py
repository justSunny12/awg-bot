"""Unit: термины в текстах интерфейса — по литералам texts/*, keyboards/* и
guides.py (строки, которые видит человек; докстринги и комментарии не в
счёт).

Правила: «локальная сеть», а не «дом/домашняя/квартира»; VPN, а не «ВПН»;
у клиента и гостя — «Трафик», а не «Потребление». Цена ошибки — разнобой
терминов в одном боте: человек не понимает, одно это или разное, и
спрашивает.

Исключения — по месту, с объяснением. Экраны администратора и агента шлюза
переделываются следующими этапами; их строки — в исключениях до своего
этапа, список пустеет вместе с ними.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2] / "awgbot" / "bot"
FILES = (sorted((ROOT / "texts").glob("*.py")) + sorted((ROOT / "keyboards").glob("*.py"))
         + [ROOT / "guides.py"])

TERMS = {
    "дом": re.compile(r"(?<![а-яё])дом(?![а-яё])", re.I),
    "домашн": re.compile(r"домашн", re.I),
    "квартир": re.compile(r"квартир", re.I),
    "ВПН": re.compile(r"впн", re.I),
    "потребление": re.compile(r"потреблени", re.I),
}

# (файл, термин, кусок строки) → почему пока можно
ALLOWED = {
    ("guides.py", "дом", "проспект Абая, дом 8"):
        "адрес в форме Apple ID — это адрес, а не термин",
    ("texts/routing.py", "потребление", "Потребление: "):
        "карточка устройства-шлюза у админа (без слота) — до переделки карточки устройства",
}

# Прежние имена функций и разделов в текстах, которые видит человек:
# «VPN-транзит» вместо «За шлюзом — без VPN» / «Локальная сеть без VPN»,
# «Связь подсетей» вместо «Доступ между подсетями», «🛡 SSH-доступ» вместо
# «Доступ по SSH», «🔧 Восстановить» вместо «Мастер восстановления», «🩺
# Здоровье» вместо «Монитор здоровья»; «Обслуживания» нет ни у одной роли.
# Обе роли переименованы целиком — исключений нет.
OLD_NAMES = {
    "без VPN": re.compile(r"без VPN", re.I),
    "За шлюзом": re.compile(r"за шлюзом", re.I),
    "между подсетями": re.compile(r"между подсетями", re.I),
    "Доступ по SSH": re.compile(r"доступ по ssh", re.I),
    "Мастер восстановления": re.compile(r"мастер восстановления", re.I),
    "Монитор здоровья": re.compile(r"монитор здоровья", re.I),
    "Обслуживание": re.compile(r"обслуживани", re.I),
}
OLD_NAMES_ALLOWED: dict = {}
# детали проверок здоровья агента собираются в домене — человек читает их на
# экране «🩺 Здоровье» так же, как тексты бота
MAIN_FILES = (sorted((ROOT / "texts").glob("*.py")) + sorted((ROOT / "keyboards").glob("*.py"))
              + sorted((ROOT / "handlers").rglob("*.py"))
              + [ROOT.parent / "domain" / n for n in ("gateway.py", "gwssh.py", "gwchecks.py")])


def _literals(path: pathlib.Path):
    """(строка, номер) всех строковых литералов файла, кроме докстрингов;
    части f-строк — тоже литералы."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.value, node.lineno


def _hits():
    out = []
    for path in FILES:
        rel = path.relative_to(ROOT).as_posix()
        for value, line in _literals(path):
            for term, rx in TERMS.items():
                if rx.search(value):
                    out.append((rel, term, value, line))
    return out


def _allowed(rel, term, value):
    return any(rel == f and term == t and part in value for (f, t, part) in ALLOWED)


def test_ui_literals_use_the_agreed_terms():
    bad = [f"{rel}:{line} «{term}»: {value[:90]!r}"
           for rel, term, value, line in _hits() if not _allowed(rel, term, value)]
    assert not bad, "термины не по канону:\n" + "\n".join(bad)


def test_every_exception_still_points_at_a_real_string():
    """Исключение, которому больше нечего прикрывать, — мёртвый пропуск:
    завтра под него молча подпадёт новая строка. Переделали экран — убери
    строку из ALLOWED."""
    hits = _hits()
    stale = [key for key in ALLOWED
             if not any(rel == key[0] and term == key[1] and key[2] in value
                        for rel, term, value, _ in hits)]
    assert not stale, f"исключения без строк: {stale}"


def test_the_scan_actually_sees_literals(tmp_path):
    """Сторож самого теста: сканер видит f-строки и обычные литералы, а
    докстринги пропускает — иначе зелёный прогон ничего не значит."""
    src = ('"""квартира в докстринге"""\n'
           'def f(x):\n    """домашняя сеть"""\n    return f"ВПН {x}"\n'
           'Y = "Потребление"\n')
    tmp = tmp_path / "probe.py"
    tmp.write_text(src, encoding="utf-8")
    values = [v for v, _ in _literals(tmp)]
    assert "Потребление" in values and any("ВПН" in v for v in values)
    assert not any("квартира" in v or "домашняя" in v for v in values), "докстринги попали в скан"


@pytest.mark.parametrize("word, hit", [("дом 8", True), ("Дом", True), ("домен", False),
                                       ("домашняя", False), ("по дому", False)])
def test_the_word_dom_is_matched_as_a_word(word, hit):
    """«дом» — словом: «домен» в текстах про DNS — не нарушение."""
    assert bool(TERMS["дом"].search(word)) is hit


def _old_name_hits():
    out = []
    for path in MAIN_FILES:
        rel = path.relative_to(ROOT.parent).as_posix()
        for value, line in _literals(path):
            for term, rx in OLD_NAMES.items():
                if rx.search(value):
                    out.append((rel, term, value, line))
    return out


def test_main_bot_texts_call_the_gateway_functions_by_their_new_names():
    """«VPN-транзит» и «Связь подсетей» — везде, где их видит админ: в
    карточке, диалогах, всплывашках и отказах. Одна строка со старым именем —
    и человек ищет в интерфейсе функцию, которой там больше нет."""
    bad = [f"{rel}:{line} «{term}»: {value[:90]!r}" for rel, term, value, line in _old_name_hits()
           if not any(rel == f and term == t and part in value for (f, t, part) in OLD_NAMES_ALLOWED)]
    assert not bad, "старые имена функций:\n" + "\n".join(bad)


def test_the_old_name_scan_reaches_the_agent_domain_strings(tmp_path):
    """Сторож: домен агента в скане — там детали проверок здоровья вида
    «(🔧 Восстановить)», которые человек читает на экране."""
    assert any(p.name == "gwssh.py" and p.parent.name == "domain" for p in MAIN_FILES)
    assert all(p.is_file() for p in MAIN_FILES), [p for p in MAIN_FILES if not p.is_file()]
    tmp = tmp_path / "probe.py"
    tmp.write_text('X = "реассерт не прошёл (🔧 Мастер восстановления)"\n', encoding="utf-8")
    assert any(OLD_NAMES["Мастер восстановления"].search(v) for v, _ in _literals(tmp))


def test_the_sshd_config_head_points_at_the_real_buttons():
    """Шапка, которую бот пишет в sshd_config хоста, говорит человеку, где
    менять порт: «раздел → кнопка». Указывает на старые названия — человек
    на хосте ищет в боте раздел и кнопку, которых нет."""
    from awgbot.bot import keyboards as kb
    from awgbot.infra import sshd
    labels = {b.text for row in kb.gateway_settings_kb().inline_keyboard for b in row}
    labels |= {b.text for row in kb.gateway_ssh_kb({"new_plumbing": True}).inline_keyboard for b in row}
    head = sshd.OUR_HEAD.splitlines()[0]
    assert "«🛡 SSH-доступ»" in head and "«🅿️ Порт»" in head, head
    assert {"🛡 SSH-доступ", "🅿️ Порт"} <= labels, labels


def test_every_old_name_exception_still_points_at_a_real_string():
    hits = _old_name_hits()
    stale = [key for key in OLD_NAMES_ALLOWED
             if not any(rel == key[0] and term == key[1] and key[2] in value for rel, term, value, _ in hits)]
    assert not stale, f"исключения без строк: {stale}"


def test_the_old_name_scan_sees_handlers_and_fstrings(tmp_path):
    """Сторож: всплывашки живут в обработчиках f-строками — сканер обязан их
    видеть, иначе зелёный прогон ничего не значит."""
    tmp = tmp_path / "probe.py"
    tmp.write_text('async def h(cb, on):\n    await cb.answer(f"Доступ между подсетями {on}")\n',
                   encoding="utf-8")
    assert any(OLD_NAMES["между подсетями"].search(v) for v, _ in _literals(tmp))
    assert any(p.name == "settings.py" and p.parent.name == "handlers" for p in MAIN_FILES)
