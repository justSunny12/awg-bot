"""peeradmin.py — экран роли админа второго сервера (Т-РН1) и приглашение
кода (Т-РН2, Т-РН3). С точки зрения бота «Beta», в который пишет админ
«Alpha»."""
from __future__ import annotations

from awgbot.util import timeutil

from ..fmt import _e


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except ValueError:
        return ""


def screen(d: dict) -> str:
    me, peer = str(d.get("me") or "этот сервер"), str((d.get("peer") or {}).get("name") or "твой сервер")
    lines = [f"🛟 <b>Резерв {_n(me)} для {_n(peer)}</b> · v{_e(str(d.get('version') or ''))}", "",
             f"Ты — админ {_n(peer)}. Здесь видно, как {_n(me)} страхует твой сервер; управлять {_n(me)} отсюда нельзя", ""]
    ok, why = d.get("direction_in") or (False, "")
    if ok:
        lines.append("✅ Твои клиенты переедут сюда, если сервер упадёт")
    elif not d.get("my_ready"):
        lines.append(f"☑️ {_n(me)} не готов быть резервом")
    else:
        lines.append("⚠️ Твои клиенты сюда не переедут: " + _e(why))
    lines += ["", f"<b>Сервисы {_n(me)}</b>",
              "🟢 бот и AWG" if d.get("awg_ok") else "🔴 AWG не работает"]
    if d.get("link_online"):
        lines.append(f"🟢 линк с {_n(peer)}")
    else:
        since = d.get("down_since") or ""
        lines.append(f"🔴 линк с {_n(peer)}: нет связи" + (f" с {_e(_when(since))}" if since else ""))
    if d.get("has_gateway"):
        lines.append(("🟢 РФ-доступ через шлюзы " + _n(me) + ": разрешён") if d.get("rf_via_mine")
                     else f"☑️ РФ-доступ через шлюзы {_n(me)}: выключен его админом")
    lines += ["", f"<b>Как {_n(me)} видит {_n(peer)}</b>"]
    seen = d.get("seen") or "ok"
    if seen == "down":
        lines.append(f"🔴 сервер не отвечает с {_e(_when(d.get('down_since') or ''))}")
    elif seen == "suspect":
        lines.append("🟡 сервер молчит — проверяю")
    else:
        lines.append("🟢 сервер на связи")
    inn = d.get("inn") or "idle"
    if inn in ("taking", "serving"):
        lines += ["", f"📥 Твои клиенты и бот здесь с {_e(_when(d.get('in_since') or ''))}"]
    if d.get("pkg_at"):
        lines += ["", f"📦 Данные для переезда — от {_e(_when(d['pkg_at']))}"]
    else:
        lines += ["", "📦 Данных для переезда ещё нет — они придут с твоего сервера"]
    tr = d.get("traffic") or {}
    if tr.get("in"):
        lines.append(f"📊 Твои клиенты через {_n(me)} за {_e(tr.get('month_label') or 'месяц')}: {_e(tr['in'])}")
    return "\n".join(lines)


STRANGER = "Этот бот — не твой сервер AWG. Если тебе нужен экран резерва, попроси админа второго сервера подключить пару"
