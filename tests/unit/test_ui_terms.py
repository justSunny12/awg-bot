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
    ("texts/routing.py", "квартир", "Роутер квартиры заворачивает"):
        "экран «без VPN» шлюза — до этапа 3",
    ("texts/routing.py", "квартир", "трафик квартиры"):
        "подтверждение выключения «без VPN» — до этапа 3",
    ("texts/routing.py", "дом", "«дом 2»"):
        "подпись слота шлюза — до этапа 3",
    ("texts/gateway.py", "квартир", "Адреса для входа снаружи"):
        "экран SSH-доступа агента — до этапа 4",
    ("texts/gateway.py", "потребление", "📊 Потребление за месяц"):
        "панель агента — до этапа 4",
    ("texts/routing.py", "потребление", "Потребление: "):
        "карточка устройства-шлюза у админа — до этапа 3",
    ("texts/migration.py", "потребление", "история потребления"):
        "переезд профилей — до этапа 3",
    ("keyboards/settings.py", "потребление", "Превышение лимита потребления"):
        "уведомления в настройках админа — до этапа 3",
}


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
