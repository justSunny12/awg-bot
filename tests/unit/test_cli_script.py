"""
Проверки awg-bot.sh — управляющего скрипта установки.

Скрипт не покрыт обычными тестами: он запускает systemd и правит /etc, и гонять
его в CI негде. Но часть ошибок в нём — чисто текстовая и ловится чтением, а
цена у них высокая: команда доезжает до боевого сервера и падает там. Здесь
именно такие проверки — по исходнику, без запуска.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "awg-bot.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _python_invocations(text: str) -> list[str]:
    """Строки, запускающие интерпретатор из venv."""
    return [ln.strip() for ln in text.splitlines() if "venv/bin/python" in ln]


def test_python_is_run_from_the_install_dir(script):
    """Пакет awgbot лежит в $INSTALL_DIR, а не в site-packages, поэтому запуск
    из чужого каталога даёт ModuleNotFoundError уже на боевом сервере. Каждый
    вызов интерпретатора обязан либо делать cd, либо быть его частью.
    """
    lines = script.splitlines()
    bad = []
    for i, ln in enumerate(lines):
        if "venv/bin/python" not in ln or "-x " in ln or "[[" in ln:
            continue
        if ln.strip().startswith("ExecStart="):
            continue                    # у юнита каталог задаёт WorkingDirectory
        # cd может стоять на этой же строке или на предыдущей (перенос строки)
        window = " ".join(lines[max(0, i - 2):i + 1])
        if 'cd "$INSTALL_DIR"' not in window:
            bad.append(ln.strip())
    assert not bad, "запуск python вне $INSTALL_DIR:\n" + "\n".join(bad)
    # исключение для ExecStart законно ровно до тех пор, пока юнит задаёт каталог
    assert "WorkingDirectory=$INSTALL_DIR" in script


def test_conf_dir_env_var_is_spelled_the_way_config_reads_it(script):
    """Имя переменной должно совпадать с тем, что читает config.py. Опечатка не
    падает, а тихо уводит команду на дефолтный конфиг — то есть диагностика
    посмотрит не ту установку и соврёт, ничего не сообщив.
    """
    from awgbot.core import config          # noqa: F401  (нужен как источник имён)
    src = (Path(config.__file__)).read_text(encoding="utf-8")
    known = set(re.findall(r'_resolve_dir\("([A-Z_]+)"', src))
    assert known, "не нашли имён переменных в config.py — тест устарел"

    used = set(re.findall(r"\b(AWG[A-Z_]*_(?:CONF|DATA)_DIR)\b", script))
    unknown = used - known
    assert not unknown, (
        f"awg-bot.sh задаёт переменные, которых config.py не читает: {sorted(unknown)}. "
        f"Читаются: {sorted(known)}")


def test_every_dispatched_verb_has_a_handler(script):
    """Глагол в case без функции — «команда не найдена» у пользователя."""
    verbs = re.findall(r"^\s{4}([a-z][a-z-]*)\)\s+(cmd_[a-z_]+)", script, re.M)
    assert verbs, "не разобрали case-блок — тест устарел"
    for verb, fn in verbs:
        assert f"{fn}()" in script, f"глагол {verb!r} зовёт несуществующую {fn}()"


def test_routing_doctor_is_documented_in_usage(script):
    """Команду, о которой не написано в usage, никто не найдёт в момент отказа —
    а нужна она именно тогда."""
    assert "routing-doctor" in script.split("VERB=")[0], "нет в usage()"


# ── shell-скрипты: ссылки на несуществующие переменные ───────────────────────

_SHELL_ENV = {
    # приходят из окружения/оболочки, а не присваиваются в скрипте
    "PATH", "HOME", "PWD", "OLDPWD", "IFS", "LINENO", "SHELL", "USER", "TERM",
    "EUID", "UID", "HOSTNAME", "BASH_SOURCE", "FUNCNAME", "PIPESTATUS", "RANDOM",
    "LANG", "LC_ALL", "SUDO_USER", "TMPDIR", "EDITOR", "COLUMNS", "PS1",
}
_SCRIPTS = sorted((SCRIPT.parent / "install").glob("*.sh")) + [SCRIPT]


@pytest.mark.parametrize("path", _SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_reference_only_defined_variables(path: Path):
    """Ссылка на неприсвоенную переменную в sh не падает, а подставляет пустую
    строку: `iptables -s "" -o eth0` — это уже другая команда, и ломается она на
    боевом сервере. Ровно так уехала правка MASQUERADE для линк-подсети.
    """
    text = path.read_text(encoding="utf-8")
    # присваивание может стоять не в начале строки: `A=1; B=2` — обычный приём
    # Имя целиком и хотя бы одна заглавная: иначе регулярка откусывает префикс
    # от локальных вроде __verbose и «находит» переменную __.
    name = r"[A-Z_]*[A-Z][A-Z0-9_]*"
    tail = r"(?![A-Za-z0-9_])"
    assigned = set(re.findall(rf"(?<![-\w$])({name})=", text))
    assigned |= set(re.findall(rf"\bfor\s+({name})\s+in\b", text))
    assigned |= set(re.findall(rf"\bread\s+(?:-r\s+)?(?:-a\s+)?({name})", text))
    # ${VAR:-default} — осознанная необязательность, а не забытая переменная
    used = {m.group(1) for m in re.finditer(rf"\$\{{({name})\}}", text)}
    used |= set(re.findall(rf"\$({name}){tail}", text))
    dangling = sorted(used - assigned - _SHELL_ENV)
    assert not dangling, (
        f"{path.name}: используются неприсвоенные переменные {dangling} — "
        f"в sh это молча подставит пустую строку")


# ── проактивные сообщения админу должны убираться ────────────────────────────

def test_startup_messages_to_admin_are_dismissible():
    """Сообщения, которых админ не заказывал, обязаны иметь чем закрыться.

    В проекте это соглашение: notifier подставляет «Скрыть» сам. Но
    bot.send_message идёт мимо него — и предупреждения preflight приходили без
    единой кнопки и висели в чате навсегда.
    """
    main = (SCRIPT.parent / "awgbot" / "runtime" / "main.py").read_text(encoding="utf-8")
    calls = re.findall(r"bot\.send_message\((.*?)\)\n", main, re.S)
    naked = [c for c in calls if "reply_markup" not in c]
    assert not naked, (
        "прямая отправка админу без клавиатуры — такое сообщение нечем убрать:\n"
        + "\n".join(c.strip()[:90] for c in naked)
        + "\nИспользуй notifier.notify_one — он подставит «Скрыть» сам.")
