"""Форма уведомлений — сообщений, которые бот шлёт сам (Notification и
тексты пакета domain/notices): без точки в конце (решение 14.10.2026 на
«🔴 Резолвер пользователей не отвечает (…).»). Сторож статический: хвост второго
аргумента Notification(...) и return'ов domain/notices/*.py — последняя
строковая константа выражения (f-строка, конкатенация, тернарник); хвост не
литерал — не судим.

Цена ошибки: правило держится только на памяти — следующая строка с точкой
проходит все тесты и уходит в чат вразнобой с остальными.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2] / "awgbot"

# сокращения, которыми фраза может кончаться — точка часть слова, не конца фразы
ABBREVIATIONS = (" дн.", " мин.", " сек.", " ч.")


def _tail(node) -> list[str]:
    """Возможные хвосты текста выражения; пусто — хвост не литерал."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return _tail(node.values[-1]) if node.values else []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _tail(node.right)
    if isinstance(node, ast.IfExp):
        return _tail(node.body) + _tail(node.orelse)
    return []


def _notice_tails(tree: ast.AST, *, returns: bool) -> list[tuple[int, str]]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "Notification":
            arg = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "text"), None)
            if arg is not None:
                out += [(n.lineno, t) for t in _tail(arg)]
        elif returns and isinstance(n, ast.Return) and n.value is not None:
            out += [(n.lineno, t) for t in _tail(n.value)]
    return out


def _scan(root: pathlib.Path) -> tuple[list[tuple[str, int, str]], int]:
    bad, seen = [], 0
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, text in _notice_tails(tree, returns=path.parent.name == "notices"):
            seen += 1
            t = text.rstrip()
            if t.endswith(".") and not t.endswith(ABBREVIATIONS):
                bad.append((str(path.relative_to(root.parent)), line, t[-50:]))
    return bad, seen


def test_notifications_do_not_end_with_a_period():
    bad, seen = _scan(ROOT)
    assert seen >= 40, f"сторож видит слишком мало хвостов ({seen}) — разбор сломан"
    assert not bad, "\n".join(f"{f}:{ln}: …{t}" for f, ln, t in bad)


def test_the_scan_sees_fstrings_concatenation_and_ternaries(tmp_path):
    (tmp_path / "x.py").write_text(
        'Notification(1, "ок.")\n'
        'Notification(1, f"{a} готово.")\n'
        'Notification(1, "a" + "б.")\n'
        'Notification(1, "a" + ("б" if x else "в."))\n'
        'Notification(1, text="чисто")\n'
        'Notification(1, "дальше " + name)\n'            # хвост не литерал — не судим
        'Notification(1, "взято 3 дн.")\n', encoding="utf-8")
    (tmp_path / "notices").mkdir()
    (tmp_path / "notices" / "n.py").write_text('def f():\n    return "итог."\n', encoding="utf-8")
    bad, seen = _scan(tmp_path)
    assert sorted((f.rsplit("/", 1)[-1], ln) for f, ln, _ in bad) == [("n.py", 2), ("x.py", 1), ("x.py", 2), ("x.py", 3), ("x.py", 4)], bad
    assert seen == 8
