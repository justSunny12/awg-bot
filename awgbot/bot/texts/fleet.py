"""fleet.py — тексты набора обновлений основного бота: раздел «⬆️ Обновления»
диалог, живое сообщение хода и его итоги, строки шлюза — те же в разделе и
в карточке слота.

Данные — из домена (domain/fleet: план, ход, состояние шлюза). Имена шлюзов
— ссылкой на карточку слота; даты — дд.мм; без точки в конце.
"""
from __future__ import annotations

from awgbot.bot import ui
from awgbot.domain.notices.backup import chat_ref, gw_ref
from awgbot.domain.notices.fleet import JOURNAL, SERVER_SILENT

from .fmt import _e
from .updates import _seen_date, _ver, changelog_details, update_skipped_lines

UPDATE_RUNNING_TOAST = "Обновление уже идёт"
UPDATE_LINKS_BUSY_TOAST = "Идёт смена ключей линка или подключение шлюза — применить обновление можно после завершения"


def _name(g: dict, user: str) -> str:
    return gw_ref(g.get("name") or "", g.get("label") or "", slot=int(g.get("slot") or 0), bot_username=user)


def _clock(iso: str) -> str:
    from awgbot.util import timeutil
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except ValueError:
        return "?"


def _since(g: dict) -> str:
    date = _seen_date(g.get("seen") or "")
    return f"не на связи с {date}" if date else "не на связи"


def _who(run: dict, *, without=()) -> tuple[str, bool]:
    """Кто обновлялся в ходе — по его шагам, без хостов из without: («сервер
    AWG и шлюз», множественное число). «Набор» — слово кода, не человека:
    в текстах — состав словами. Шагов нет — общая форма."""
    steps = run.get("steps") or []
    main = any(s[0] == "main" for s in steps) and "main" not in without
    gws = len({int(s[1]) for s in steps if s[0] == "gw" and int(s[1]) not in without})
    gw = "шлюз" if gws == 1 else "шлюзы" if gws else ""
    if main and gw:
        return f"сервер AWG и {gw}", True
    if main:
        return "сервер AWG", False
    if gw:
        return gw, gws > 1
    return "сервер AWG и шлюзы", True


def gw_no_upd(g: dict) -> bool:
    """Агент команду обновления не знает (ниже 3.4.0) — обновить его можно
    только из чата бота шлюза."""
    return bool(g.get("version") and not g.get("upd") and g.get("device"))


# ── строка шлюза по состоянию (раздел, карточка) ────────────────────────────

def gw_state_line(g: dict, *, release: str = "", run: dict | None = None) -> str:
    """Строка шлюза — что с агентом шлюза в наборе; пусто — нечего
    сказать (на версии выпуска набора). release — выпуск набора; run — ход."""
    st = g.get("upd_state") or {}
    ver = g.get("version") or ""
    want = g.get("want") or ""
    if gw_no_upd(g):
        return (f"⚠️ агент {_e(_ver(ver))} командой сервера AWG не обновляется — обнови его "
                f"в чате {chat_ref(g.get('agent_bot'))}: «⚙️ Настройки» → «⬆️ Обновления»")
    if ver and not g.get("release"):
        return "⚠️ Агент — сборка не из выпуска: по команде сервера AWG обновить не получится"
    state = st.get("state") or ""
    active = (run or {}).get("state") in ("running", "rolling_back") and st.get("run") == (run or {}).get("id")
    if active or not st.get("run"):
        if state in ("sent", "accepted"):
            return f"🟡 обновляется до {_e(_ver(st.get('target')))}"
        if state == "lost":
            return "🟡 пропал посреди обновления: доведётся, когда выйдет на связь"
        if state == "reverting":
            return f"↩️ возвращается на {_e(_ver(st.get('prev')))}"
    if not st.get("run") and st.get("final") and state in ("failed", "rolled_back", "refused"):
        return (f"⚠️ не обновился до {_e(_ver(st.get('target')))} после выхода на связь: "
                f"{_e(st.get('error') or state)}")
    if not st.get("run") and state in ("failed", "rolled_back", "refused") and st.get("next_at"):
        # догон не удался, повтор назначен — молчать нельзя: шлюз отстаёт от выпуска
        return (f"⚠️ не обновился до {_e(_ver(st.get('target')))}: {_e(st.get('error') or state)} — "
                f"повтор в {_e(_clock(str(st['next_at'])))}")
    if want and ver and _ver(ver) != _ver(want):
        from awgbot.infra import updates
        a, b = updates.parse_version(ver), updates.parse_version(want)
        if not g.get("online"):
            return f"🟡 {_since(g)}: обновится, когда выйдет на связь"
        if a is not None and b is not None and a > b:
            return f"🟡 Агент новее поставки сервера AWG — вернётся к {_e(_ver(want))}"
        return "🟡 ждёт очереди" if active else ""
    return ""


# ── раздел ───────────────────────────────────────────────────────────────────

def fleet_section(plan: dict, *, scan_failed: bool = False, skipped=(), bot_username: str = "",
                  tail: str = "") -> str:
    """Раздел: шапка — выпуск набора и статус; строки хостов; список изменений
    цели — под катом; tail — готовая строка (HTML) после него, в том же
    пределе длины сообщения."""
    release = _ver(plan.get("release") or plan.get("server") or "")
    tag = plan.get("tag") or ""
    run = plan.get("run") or {}
    user = bot_username
    state = run.get("state") or ""
    if state in ("running", "rolling_back"):
        head = ui.head("⬆️ Обновления", meta=[_e(release)])
    elif tag:
        head = ui.head("⬆️ Обновления", meta=[f"доступна {_e(_ver(tag))}"])
    elif scan_failed:
        head = ui.head("⬆️ Обновления", meta=[f"{_e(release)} {ui.st('bad', 'проверка не удалась')}"])
    else:
        head = ui.head("⬆️ Обновления", meta=[f"{_e(release)} {ui.st('ok', 'актуальна')}"])
    lines = [head]
    if state in ("running", "rolling_back"):
        lines.append(f"⏳ Идёт обновление до {_e(_ver(run.get('tag')))}")
    elif state == "rolled_back":
        who, many = _who(run)
        lines.append(f"⚠️ Обновление до {_e(_ver(run.get('tag')))} отменено — {who} "
                     f"{'вернулись' if many else 'вернулся'} на {_e(_ver(run.get('prev')))}: "
                     f"{_e(run.get('reason') or '')}".rstrip(": "))
    elif state == "rollback_failed":
        lines.append("⚠️ Откат не удался — пока не нажмёшь «🔁 Повторить», сервер AWG шлюзам команд не шлёт")
    server = _ver(plan.get("server") or "")
    want = plan.get("server_want")
    lines.append(f"🖥 Сервер AWG: {_e(server)}" + (f" → {_e(_ver(want))}" if want else ""))
    gws = plan.get("gateways") or []
    if tag and not plan.get("gw_changes") and gws:
        lines.append(f"🛰 Шлюзы: остаются на {_e(_ver(gws[0].get('want') or ''))} — в {_e(_ver(tag))} "
                     "правки только для сервера AWG")
    elif gws:
        notes = [gw_state_line(g, release=release, run=run) for g in gws]
        if not tag and not any(notes):
            # всё на выпуске набора — шлюзы одной строкой
            lines.append("🛰 " + " · ".join(
                f"{_name(g, user)}: {_e(_ver(g['version'])) if g.get('version') else 'версия неизвестна'}"
                for g in gws))
        for g, note in zip(gws, notes) if (tag or any(notes)) else ():
            ver = _e(_ver(g.get("version"))) if g.get("version") else "версия неизвестна"
            if tag and g.get("want") and g.get("version") and _ver(g["version"]) != _ver(g["want"]) \
                    and not note.startswith("⚠️"):
                ver += f" → {_e(_ver(g['want']))}"
            if note.startswith("⚠️ агент"):
                lines.append(f"⚠️ {_name(g, user)}: {note[len('⚠️ '):]}")
            else:
                lines.append(f"🛰 {_name(g, user)}: {ver}" + (f" · {note}" if note else ""))
    lines.extend(update_skipped_lines(skipped))
    budget = "\n".join(lines) + "\n" + (tail + "\n" if tail else "")
    body = changelog_details(plan.get("body") or "", header=budget, tag=tag) if tag else ""
    if body:
        lines.append(body)
    if tail:
        lines.append(tail)
    return "\n".join(lines)


# ── диалог ──────────────────────────────────────────────────────────────────

def fleet_confirm(plan: dict) -> str:
    """Диалог: порядок и атомарность; шлюзы не на связи — хвостом."""
    tag = _e(_ver(plan.get("tag") or ""))
    if plan.get("needs_gw"):
        need = _e(_ver(plan.get("needs_gw_ver") or plan.get("tag") or ""))
        text = (f"⬆️ Обновить до {tag}?\nСерверу AWG {tag} нужны агенты шлюзов не ниже {need} — сначала "
                "обновятся шлюзы (по одному, начиная с резервных), затем сервер AWG. Не удастся обновить "
                "один из шлюзов — все вернутся на прежнюю версию")
    else:
        text = (f"⬆️ Обновить до {tag}?\nСначала обновится сервер AWG, затем шлюзы по одному: сначала "
                "резервные, последним — активный. Линк шлюза на время его обновления ненадолго пропадёт. "
                "Не удастся обновить один из шлюзов — сервер и шлюзы вернутся на прежнюю версию")
    if any(not g.get("online") and g.get("version") and g.get("want") and _ver(g["version"]) != _ver(g["want"])
           for g in plan.get("gateways") or []):
        text += ". Шлюзы не на связи обновятся, когда выйдут на связь"
    return text


# ── живое сообщение хода ────────────────────────────────────────────────────

def _live_gw_line(g: dict, run: dict, user: str) -> str:
    st = g.get("upd_state") or {}
    name = _name(g, user)
    mine = st.get("run") == run.get("id")
    state = st.get("state") if mine else ""
    prev = _e(_ver(st.get("prev") or run.get("prev") or ""))
    if state in ("failed", "rolled_back", "refused"):
        return f"⚠️ {name} — обновление не удалось, вернул {prev}: {_e(st.get('error') or state)}"
    if state == "revert_failed":
        return f"⚠️ {name} — не вернулся на {prev}: {_e(st.get('error') or 'откат не прошёл')}"
    if run.get("state") == "rolling_back":
        if state == "reverting":
            return f"↩️ {name} — возвращается на {prev}"
        if state == "reverted":
            return f"↩️ {name} — вернул {prev}"
    if run.get("state") == "rolled_back" and state == "reverted":
        return f"↩️ {name} — вернул {prev}"
    if state == "done":
        return f"✅ {name} — {_e(_ver(st.get('target')))}"
    if state in ("sent", "accepted"):
        return f"🟡 {name} — обновляется"
    if state == "lost":
        return f"🟡 {name} — пропал посреди обновления: доведётся, когда выйдет на связь"
    want = g.get("want") or ""
    ver = g.get("version") or ""
    if not mine and run.get("state") in ("rolling_back", "rolled_back", "rollback_failed") \
            and g.get("participant"):
        return ""                                         # ход его не трогал — он на прежнем
    if not g.get("participant"):
        if not g.get("online") and want and ver and _ver(ver) != _ver(want):
            if run.get("state") in ("rolled_back", "rollback_failed"):
                return f"🟡 {name} — {_since(g)}, остался на {_e(_ver(ver))}"
            return f"🟡 {name} — {_since(g)}: обновится, когда выйдет на связь"
        return ""
    if want and ver and _ver(ver) != _ver(want) and run.get("state") == "running":
        return f"🟡 {name} — ждёт очереди"
    return f"✅ {name} — {_e(_ver(ver))}" if ver else ""


def _server_waits_line(run: dict, server: str) -> str:
    """Сервер ещё не откачен: его очередь — по шагам, которые осталось
    откатить (сервер откатывается на последнем своём шаге с конца)."""
    steps = (run.get("steps") or [])[:int(run.get("undo") or 0)]
    mains = [i for i, s in enumerate(steps) if s[0] == "main"]
    ver = _e(_ver(server))
    if not mains:
        return f"✅ Сервер AWG — {ver}"
    m = mains[-1]
    after = any(s[0] == "gw" for s in steps[m + 1:])
    before = any(s[0] == "gw" for s in steps[:m])
    if not after:
        return f"🟡 Сервер AWG — {ver}, вернётся следующим"
    if not before:
        return f"🟡 Сервер AWG — {ver}, вернётся последним"
    return f"🟡 Сервер AWG — {ver}, вернётся в свою очередь"


def _server_line(run: dict, server: str, plan: dict) -> str:
    steps = run.get("steps") or []
    did = any(s[0] == "main" for s in steps)
    state = run.get("state") or ""
    prev = _e(_ver(run.get("prev") or ""))
    if state == "rolling_back" and did and not run.get("main_undone"):
        return _server_waits_line(run, server)
    if state == "rolling_back" and (run.get("server") or {}).get("kind") == "revert":
        return f"↩️ Сервер AWG — возвращается на {prev}"
    if state in ("rolling_back", "rolled_back", "rollback_failed") and (did or run.get("failed_on") == "main"):
        if "main" in (run.get("revert_failed") or []):
            return f"⚠️ Сервер AWG — не вернулся на {prev}: {_e(run.get('reason_main') or 'откат не прошёл')}"
        if run.get("failed_on") == "main":
            return f"⚠️ Сервер AWG — обновление не удалось, вернул {prev}: {_e(run.get('reason') or SERVER_SILENT)}"
        return f"↩️ Сервер AWG — вернул {prev}"
    if state == "running" and not did and plan.get("server_want"):
        return "🟡 Сервер AWG — ждёт очереди"
    return f"✅ Сервер AWG — {_e(_ver(server))}"


def fleet_live(plan: dict, *, bot_username: str = "") -> str:
    """Живое сообщение: ход и откат, итог, набор откатился, откат не прошёл."""
    run = plan.get("run") or {}
    tag = _e(_ver(run.get("tag") or ""))
    prev = _e(_ver(run.get("prev") or ""))
    state = run.get("state") or ""
    gws = plan.get("gateways") or []
    user = bot_username
    if state == "done":
        names = ["сервер AWG"] if any(s[0] == "main" for s in run.get("steps") or []) else []
        steps_gw = {int(s[1]) for s in run.get("steps") or [] if s[0] == "gw"}
        names += [_name(g, user) for g in gws if g["slot"] in steps_gw]
        lines = [f"✅ <b>Обновлено до {tag}</b>" + (f": {', '.join(names)}" if names else "")]
        for g in gws:
            if g["slot"] not in steps_gw and not g.get("online") and g.get("want") and g.get("version") \
                    and _ver(g["version"]) != _ver(g["want"]):
                lines.append(f"🟡 {_name(g, user)} обновится, когда выйдет на связь")
        body = changelog_details(plan.get("body") or "", header="\n".join(lines) + "\n", tag=run.get("tag") or "")
        return "\n".join(lines + ([body] if body else []))
    if state == "rollback_failed":
        failed = [x for x in run.get("revert_failed") or []]
        bad = []
        for x in failed:
            if x == "main":
                bad.append(f"сервер AWG остался на {tag} — {_e(run.get('reason_main') or 'откат не прошёл')}")
            else:
                g = next((g for g in gws if g["slot"] == x), None)
                err = ((g or {}).get("upd_state") or {}).get("error") or "откат не прошёл"
                bad.append(f"{_name(g, user) if g else f'шлюз {x}'} остался на {tag} — {_e(err)}")
        rest, _many = _who(run, without=failed)           # кто вернулся: шаги хода без не вернувшихся
        back = f"{rest[0].upper()}{rest[1:]} — {prev}. " if len(failed) < len(run.get("steps") or []) else ""
        return (f"⚠️ <b>Откат не удался</b>: {'; '.join(bad)}\n"
                f"{back}Пока не нажмёшь «🔁 Повторить», сервер AWG шлюзам команд не шлёт; "
                f"журнал шлюза: {JOURNAL}")
    who, many = _who(run)
    if state == "rolled_back":
        lines = [f"⚠️ <b>Обновление до {tag} отменено</b> — {who} {'вернулись' if many else 'вернулся'} на {prev}"]
    elif state == "rolling_back":
        lines = [f"⏳ <b>Обновление до {tag} не удалось — возвращаю {who} на {prev}</b>"]
    else:
        lines = [f"⏳ <b>Обновление до {tag}</b>"]
    server_line = _server_line(run, plan.get("server") or "", plan)
    gw_lines = [ln for ln in (_live_gw_line(g, run, user) for g in gws) if ln]
    if state == "running":
        steps = run.get("steps") or []
        mains = [s for s in steps if s[0] == "main"]
        if mains and (_ver(mains[-1][1]) != _ver(plan.get("server") or "") or not plan.get("server_settled", True)):
            server_line = "🟡 Сервер AWG — обновляется"      # ступень ушла апдейтеру или новый процесс ещё не на связи
        lines += [server_line] + gw_lines
    else:
        lines += gw_lines + [server_line]
    return "\n".join(lines)
