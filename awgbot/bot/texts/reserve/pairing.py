"""pairing.py — сопряжение серверов: приглашение (живое сообщение и хвосты),
приглашение строки, диалог принятия, код у принявшего, ввод кода у
пригласившего, итог."""
from __future__ import annotations

from awgbot.util import timeutil

from ..fmt import _e
from ..routing.join import until_text


def _n(name: str) -> str:
    return f"«{_e(name)}»"


# ── Т-Р2: приглашение (пригласивший) ─────────────────────────────────────────

def invite_message(st: dict, token: str) -> str:
    until = until_text(st.get("expires") or "")
    head = ("🛟 <b>Приглашение в резерв</b>\n\n"
            "Перешли это сообщение админу второго сервера AWG — он вставит строку ниже в своём боте: "
            "«⚙️ Настройки» → «🛟 Резерв» → «📥 Принять приглашение»\n\n"
            f"<code>{_e(token)}</code>\n\n"
            "Его бот покажет код подтверждения — админ продиктует его тебе; введи код здесь кнопкой "
            f"«✏️ Ввести код». Приглашение действует час — до {_e(until)}")
    tail = invite_tail(st)
    return head + ("\n\n" + tail if tail else "")


def invite_tail(st: dict) -> str:
    ends = list(st.get("endpoints") or [])
    state = st.get("state") or ""
    if len(ends) > 1:
        return (f"⚠️ К приглашению подключались с двух адресов: <code>{_e(ends[-2])}</code> и "
                f"<code>{_e(ends[-1])}</code>. Если один из них не резерва — отмени приглашение и выпусти новое")
    if state in ("connected", "accepted") and ends:
        if int(st.get("sessions") or 0) > 1:
            return f"🟢 Резерв подключился заново с адреса <code>{_e(ends[-1])}</code> — код сменился, попроси новый"
        return f"🟢 Резерв подключился с адреса <code>{_e(ends[-1])}</code> — попроси у его админа код и введи его"
    return ""


INVITE_CANCELLED = "ℹ️ Приглашение отменено — строка больше не действует"
INVITE_STALE = "ℹ️ Не актуально: выпущено новое приглашение"


def invite_expired(st: dict) -> str:
    return f"⚠️ Приглашение не использовано за час, до {_e(until_text(st.get('expires') or ''))} — строка больше не действует"


# ── Т-Р3, Т-Р5, Т-Р6: принявший ──────────────────────────────────────────────

ACCEPT_PROMPT = ("📥 <b>Приглашение в резерв</b>\n\n"
                 "Пришли строку <code>AWGP1.…</code> от админа другого сервера AWG — сообщение удалю сразу")


def accept_dialog(peer: str, host: str, version: str, *, rf_via_mine: bool, has_gateway: bool,
                  warn: list[str], ask_version: bool) -> str:
    lines = [f"🛟 <b>Стать резервом для {_n(peer)}?</b>", "",
             f"Сервер {_n(peer)} · <code>{_e(host)}</code> · v{_e(version)}", "",
             f"Если {_n(peer)} упадёт, его клиенты и его бот будут работать на этом сервере, пока он не вернётся. "
             "Его данные хранятся здесь зашифрованными: ключ появится, только когда "
             f"{_n(peer)} не отвечает тебе и хотя бы одному его шлюзу, либо когда {_n(peer)} сам его отдаст — по кнопке "
             "своего админа или когда ты его видишь, а его шлюзы — нет. Этот сервер даёт им только VPN и место для "
             f"их бота: управлять клиентами {_n(peer)} ты не сможешь, их данные живут в памяти бота {_n(peer)} и "
             "снимаются при возврате. Как у любого оператора VPN, их трафик в это время идёт через твой сервер", "",
             f"Твои клиенты и бот так же переедут к {_n(peer)}, если упадёт этот сервер"]
    if has_gateway:
        lines += ["", ("✅" if rf_via_mine else "☑️") + f" РФ-доступ клиентов {_n(peer)} через мои шлюзы, если ни одного "
                  "его шлюза нет на связи — исходящий адрес будет адресом твоего шлюза"]
    for w in warn:
        lines += ["", "⚠️ " + _e(w)]
    if ask_version:
        lines += ["", f"⚠️ {_n(peer)} на v{_e(version)}, здесь версия старше: его клиентов этот сервер принять не сможет, "
                  f"пока не обновится, — твоих {_n(peer)} примет. Подключиться всё равно?"]
    lines += ["", f"Твой бот покажет код — продиктуй его админу {_n(peer)}"]
    return "\n".join(lines)


def connecting(host: str) -> str:
    return f"🟡 Подключаюсь к <code>{_e(host)}</code>…"


def no_answer(host: str, port: int) -> str:
    return (f"⚠️ Второй сервер не отвечает на <code>{_e(host)}</code>:<code>{port}</code> — проверь, что приглашение "
            "свежее и порт открыт у его провайдера")


def code_message(peer: str, code: str) -> str:
    from awgbot.util import joinsas
    return (f"🔑 <b>Код для {_n(peer)}: {_e(joinsas.show(code))}</b>\n\n"
            f"Продиктуй его админу {_n(peer)} — он введёт код в своём боте. Если связь прервётся, код сменится")


# ── Т-Р7: ввод кода (пригласивший) ───────────────────────────────────────────

CODE_PROMPT = ("🔑 <b>Код от резерва</b>\n\n"
               "Пришли 6 знаков, которые показал бот резерва, например <code>K7F-2QX</code>; регистр, пробелы и "
               "дефис не важны")
CODE_WAIT = "Резерв ещё не подключился — код появится у него после «🛟 Подключиться»"
CODE_FORMAT = "⚠️ Код — 6 букв и цифр, например K7F-2QX. Попробуй ещё раз"


def code_wrong(left: int) -> str:
    return f"⚠️ Код не совпал — сверь с админом резерва и пришли ещё раз. Осталось попыток: {int(left)}"


CODE_REFUSED = ("⛔ Код не совпал трижды — приглашение закрыто. Если код был верный, выпусти новое: "
                "«🛟 Резерв» → «📤 Пригласить»")
CODE_GONE = "ℹ️ Приглашения больше нет"


# ── Т-Р8: итог ───────────────────────────────────────────────────────────────

def linked(peer: str, bot_username: str) -> str:
    from .notices import linked as _linked
    return _linked(peer, bot_username)


def fmt_when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except ValueError:
        return ""
