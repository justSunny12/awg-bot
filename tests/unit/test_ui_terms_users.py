"""Unit: «пользователь», а не «клиент» — во всех строках, которые читает
человек: экраны и всплывашки ботов, уведомления, отказы сервисов, сообщения
инструментов (awgbot/, tools/) и строки, которые скрипты поставки печатают в
терминал (say/log/warn/die/echo/printf/read -p/whiptail/dialog).

Человек, который подключается к VPN, — «пользователь» (решение 25–26.10.2026).
Цена ошибки — разнобой терминов: в одном экране «пользователи», в соседнем
«клиенты», и админ спрашивает, одно это или разное; хуже того — «клиент»
у нас ещё и программа (клиент AmneziaVPN), и смешение путает инструкции.

Не в счёт: комментарии, докстринги, «голые» строковые выражения, строки
журнала (log./logger.), SQL-комментарии схемы БД, строки Description= юнитов
systemd. «Клиент» как программа — законно (см. _PROGRAM). Остальные
исключения — по месту, с объяснением; исключение, которое больше не нужно,
— красное.
"""
from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

import pytest

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[2]
PY_FILES = [p for p in sorted((REPO / "awgbot").rglob("*.py")) + sorted((REPO / "tools").rglob("*.py"))
            if "__pycache__" not in p.parts]
# те же скрипты, что сторожит test_ui_terms: обёртка и скрипты поставки
SCRIPTS = [REPO / "awg-bot.sh"] + sorted((REPO / "install").glob("*.sh"))

# клиент/клиенты/клиентов/клиентам/клиентами/клиента/клиенту/клиентах/
# клиентом/клиенте/клиентск* — словом: «клиентура» и латиница не в счёт
WORD = re.compile(r"(?<![а-яё])клиент(?:ск[а-яё]*|ы|ов|ам|ами|а|у|ах|ом|е)?(?![а-яё])", re.I)

# «клиент» как программа — не человек: «клиент AmneziaVPN», «приложение-клиент»,
# «клиентское приложение», «клиент DNS»; такое вхождение законно везде
_PROGRAM = re.compile(
    r"клиент[а-яё]*\s+(?:AmneziaVPN|Amnezia|WireGuard|AmneziaWG|DNS|TCP|SSH)"
    r"|приложени[а-яё]*[-\s]клиент"
    r"|клиентск[а-яё]+\s+(?:приложени|программ)", re.I)

# (файл, кусок строки) → почему можно. Ключ — кусок самой строки, не номер
# строки файла: правка рядом не ломает исключение, а переписанная строка —
# ломает (и тест про мёртвые исключения это покажет).
ALLOWED = {
    # имя служебной группы в БД; до миграции 4.1.0, которая её переименует
    ("awgbot/infra/db/schema/selects.py", "Устройства без клиента"):
        "имя служебной записи в БД, до миграции 4.1.0",
    # прежняя текстовая метка блокировки случая 3 из состояния выпусков до
    # 4.0.2: на экран не выходит, только распознаётся, чтобы её снять
    ("awgbot/domain/services/reserve/rfblock.py", "мои клиенты не на этом сервере"):
        "прежняя метка состояния до 4.0.2, только распознаётся",
    # нарушение инварианта для разработчика: «служебный клиент» — сущность
    # схемы (clients.is_service), человек этого текста в боте не видит
    ("awgbot/infra/db/clients.py", "Служебный клиент не инициализирован"):
        "исключение-инвариант схемы, уходит в трассировку",
    # DNS-клиент устройства (программа), а не человек: отказ получает резолвер
    ("routing-host-setup.sh", "клиент получает отказ сразу"):
        "клиент DNS на устройстве — программа",
}

_LOG_NAMES = {"log", "logger", "_log", "logging", "_logger", "LOG"}


def _skip_lines(tree: ast.AST) -> set:
    """Номера строк, где строки не для человека: докстринги, «голые»
    строковые выражения, аргументы вызовов журнала."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            skip.update(range(node.lineno, node.end_lineno + 1))   # докстринги — тоже Expr
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            name = owner.id if isinstance(owner, ast.Name) else (
                owner.attr if isinstance(owner, ast.Attribute) else None)
            if name in _LOG_NAMES:
                skip.update(range(node.lineno, node.end_lineno + 1))
    return skip


def _strip_sql_comments(text: str) -> str:
    """SQL-комментарии в DDL схемы — комментарии кода, не текст для человека."""
    return "\n".join(line.split("--", 1)[0] for line in text.split("\n"))


def _py_strings(path: pathlib.Path):
    """(строка-токен, номер строки) строковых литералов файла, кроме
    пропускаемых; части f-строк (FSTRING_MIDDLE) — тоже литералы."""
    src = path.read_text(encoding="utf-8")
    skip = _skip_lines(ast.parse(src))
    kinds = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
    sql = "infra/db/" in path.as_posix()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in kinds and tok.start[0] not in skip:
            yield (_strip_sql_comments(tok.string) if sql else tok.string), tok.start[0]


_SAY = re.compile(r"^\s*(?:say|log|warn|die|ok|step|echo|printf|info|err|whiptail|dialog|read)\b")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')


def _sh_strings(path: pathlib.Path):
    """(строка в кавычках, номер) из строк скрипта, которые печатают человеку,
    и значения Description= юнитов из heredoc (их видно в `systemctl status`);
    хвостовой комментарий вне кавычек не в счёт."""
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("Description="):
            yield line.split("=", 1)[1], i
            continue
        if line.lstrip().startswith("#") or not _SAY.match(line):
            continue
        for m in _QUOTED.finditer(line):
            yield m.group(1) if m.group(1) is not None else m.group(2), i


def _bad_words(value: str) -> list:
    """Вхождения «клиента-человека»: все совпадения WORD, кроме программ."""
    programs = [m.span() for m in _PROGRAM.finditer(value)]
    return [m.group(0) for m in WORD.finditer(value)
            if not any(a <= m.start() < b for a, b in programs)]


def _hits():
    out = []
    for path in PY_FILES:
        rel = path.relative_to(REPO).as_posix()
        for value, line in _py_strings(path):
            for word in _bad_words(value):
                out.append((rel, line, word, value))
    for path in SCRIPTS:
        for value, line in _sh_strings(path):
            for word in _bad_words(value):
                out.append((path.name, line, word, value))
    return out


def _allowed(rel: str, value: str) -> bool:
    return any(rel == f and part in value for (f, part) in ALLOWED)


def test_people_who_connect_are_called_users_not_clients():
    """Одна строка с «клиентами» — и в интерфейсе снова два слова для одних
    и тех же людей. Нашлось — перепиши строку на «пользователь»; если это
    программа — формулировка должна это говорить («клиент AmneziaVPN»)."""
    bad = [f"{rel}:{line} «{word}»: {value.strip()[:100]!r}"
           for rel, line, word, value in _hits() if not _allowed(rel, value)]
    assert not bad, "«клиент» вместо «пользователь»:\n" + "\n".join(bad)


def test_every_client_exception_still_points_at_a_real_string():
    """Исключение, которому больше нечего прикрывать, — мёртвый пропуск:
    завтра под него молча подпадёт новая строка. Миграция 4.1.0 переименует
    служебную группу — строку из ALLOWED убрать вместе с ней."""
    hits = _hits()
    stale = [key for key in ALLOWED
             if not any(rel == key[0] and key[1] in value for rel, _, _, value in hits)]
    assert not stale, f"исключения без строк: {stale}"


def test_the_client_scan_sees_ui_strings_and_skips_code_text(tmp_path):
    """Сторож самого сканера: f-строки, всплывашки и обычные литералы — в
    скане; докстринги, голые строки и журнал — нет. Иначе зелёный прогон
    ничего не значит."""
    src = ('"""клиенты в докстринге модуля"""\n'
           'import logging\nlog = logging.getLogger(__name__)\n'
           'async def f(cb, n):\n'
           '    """клиентов в докстринге функции"""\n'
           '    "клиенту голая строка"\n'
           '    log.info("клиентам в журнале %s", n)\n'
           '    await cb.answer(f"{n} клиентов на сервере")\n'
           '    return "Мои клиенты"\n')
    tmp = tmp_path / "probe.py"
    tmp.write_text(src, encoding="utf-8")
    words = [w for v, _ in _py_strings(tmp) for w in _bad_words(v)]
    assert sorted(words) == ["клиентов", "клиенты"], (
        f"скан видит не то: {words} — ждём f-строку всплывашки и литерал, без докстрингов и журнала")


def test_the_client_scan_reads_terminal_lines_of_scripts(tmp_path):
    """Сторож скана скриптов: печать человеку и Description= юнитов — в
    скане, комментарии и хвостовые комментарии — нет. Description= видна
    админу в `systemctl status`, а живёт в heredoc без say/echo."""
    sh = tmp_path / "probe.sh"
    sh.write_text('# клиенты в комментарии\n'
                  'say "  перевыпуск для клиентов"\n'
                  'read -r -p "Сколько клиентов? " n\n'
                  'ok "готово"  # клиентам не видно\n'
                  'Description=awg-bot: изоляция клиентов\n'
                  'x="клиентов"\n', encoding="utf-8")
    found = sorted(n for v, n in _sh_strings(sh) if _bad_words(v))
    assert found == [2, 3, 5], f"строки скрипта со «клиентом» для человека: {found}, ждём 2, 3 и 5"
    assert any(p.name == "awg-bot.sh" for p in SCRIPTS) and any(p.parent.name == "install" for p in SCRIPTS)
    assert any(p.parent.name == "tools" for p in PY_FILES), "tools/ выпал из скана"


@pytest.mark.parametrize("text, bad", [
    ("Мои клиенты", ["клиенты"]),
    ("нет клиентов", ["клиентов"]),
    ("клиентская подсеть", ["клиентская"]),
    ("Клиенту выдан профиль", ["Клиенту"]),
    ("установите клиент AmneziaVPN", []),
    ("клиентское приложение на телефоне", []),
    ("приложение-клиент", []),
    ("клиентура", []),
    ("client_id", []),
])
def test_client_word_is_matched_as_a_word_and_programs_are_spared(text, bad):
    """«Клиент» — словом и во всех формах; программа (клиент AmneziaVPN,
    клиентское приложение) — законна, иначе сторож гонит правильные тексты."""
    assert _bad_words(text) == bad, f"{text!r}: {_bad_words(text)} вместо {bad}"
