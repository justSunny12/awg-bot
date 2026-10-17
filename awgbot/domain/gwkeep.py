"""
gwkeep.py — копия настроек шлюза на сервере AWG: закрытый список того, что
знает только шлюз, общий для обеих ролей.

Сервер знает о шлюзе почти всё — ключи линка и аплинка, токен бота, строки
юнита, свои списки, почту с сервера. Не знает четырёх вещей: настроек агента
в conf (уведомления, тихие часы, пороги, мониторинг, расписание обновлений),
доступа по SSH в firewall.env, своей почты шлюза с памяткой «оставить своё»
и месячного счёта трафика линка. Агент присылает их сообщением `keep`
(возможность канала keep1), сервер хранит блок в server_state gwkeep_<слот>
(а значит, и в бэкапе) и шлёт обратно новой установке того же устройства
(молча), новому устройству в слоте и после восстановления сервера (вопросом).

Модуль общий, как gwsnapshot.py и gwownlists.py: закрытый список ключей,
пределы и отпечаток обязаны совпадать на обоих концах. Чужой ключ, не тот тип,
длиннее предела — отбрасываются молча при приёме на обеих сторонах. Ни одного
сетевого вызова и чтения с хоста здесь нет — чистые функции.

Блок: {"v", "hash", "at", "items": {"conf", "fw", "mail"}, "traffic"}.
`traffic` — факт, а не настройка: в отпечаток не входит.
"""
from __future__ import annotations

import hashlib
import json
import re

V = 1
CAP = "keep1"

# ключи разделов агента: «🔔 Уведомления», «🩺 Мониторинг» (часть шлюза), «⬆️ Обновления»
CONF_KEYS = (
    "quiet_hours.quiet_hours_enabled", "quiet_hours.quiet_hours_start", "quiet_hours.quiet_hours_end",
    "resource_alerts.enabled", "resource_alerts.thresholds_percent.cpu",
    "resource_alerts.thresholds_percent.ram", "resource_alerts.thresholds_percent.disk",
    "notifications.email_fallback", "app.gateway.temp_alert_c",
    "app.monitoring.alert_streak", "app.gateway.monitor_minutes", "app.gateway.handshake_max_age",
    "app.gateway.link_alert_loud",
    "updates.poll_schedule",
)
# firewall.env без SSH_PORT: порт — факт устройства (под OMV его задаёт OMV);
# без SSH_ALLOW_RESOLVED: адреса имён из SSH_ALLOW — факт резолвера, их пересчитывает
# сам шлюз (в отпечатке давали бы ложные вопросы, в применении — устаревшие адреса)
FW_KEYS = ("ADMIN_IPS_EXTRA", "SSH_FILTER", "SSH_ALLOW")
MAIL_KEYS = ("login", "password", "imap_host", "imap_port", "smtp_host", "smtp_port", "keep")

MAX_STR = 256
MAX_FW = 2048
_FW_RE = re.compile(r"[A-Za-z0-9 .:/_-]*")
_MONTH_RE = re.compile(r"\d{4}-\d{2}")

# группы для человека: что именно отличается (вопрос в чате бота шлюза, итог)
GROUPS = (
    ("уведомления", ("notifications.email_fallback",)),
    ("тихие часы", ("quiet_hours.quiet_hours_enabled", "quiet_hours.quiet_hours_start",
                    "quiet_hours.quiet_hours_end")),
    ("пороги нагрузки", ("resource_alerts.enabled", "resource_alerts.thresholds_percent.cpu",
                         "resource_alerts.thresholds_percent.ram", "resource_alerts.thresholds_percent.disk",
                         "app.gateway.temp_alert_c")),
    ("мониторинг", ("app.monitoring.alert_streak", "app.gateway.monitor_minutes",
                    "app.gateway.handshake_max_age", "app.gateway.link_alert_loud")),
    ("расписание обновлений", ("updates.poll_schedule",)),
    ("доступ по SSH из туннеля", ("fw:ADMIN_IPS_EXTRA",)),
    ("фильтр SSH снаружи", ("fw:SSH_FILTER",)),
    ("адреса SSH", ("fw:SSH_ALLOW",)),
    ("своя почта", ("mail",)),
)


def _value(v):
    """Значение настройки: bool, int или короткая строка; иное — None."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if -10**9 < v < 10**9 else None
    if isinstance(v, str) and len(v) <= MAX_STR:
        return v
    return None


def clean(items) -> dict:
    """Закрытый список: чужое отброшено. {"conf": {…}, "fw": {…}, "mail": {…}|None}."""
    items = items if isinstance(items, dict) else {}
    conf_in = items.get("conf") if isinstance(items.get("conf"), dict) else {}
    conf = {}
    for k in CONF_KEYS:
        if k in conf_in:
            v = _value(conf_in[k])
            if v is not None:
                conf[k] = v
    fw_in = items.get("fw") if isinstance(items.get("fw"), dict) else {}
    fw = {}
    for k in FW_KEYS:
        v = fw_in.get(k)
        if isinstance(v, str) and len(v) <= MAX_FW and _FW_RE.fullmatch(v):
            fw[k] = " ".join(v.split())
    mail = None
    m = items.get("mail")
    if isinstance(m, dict):
        mail = {}
        for k in MAIL_KEYS:
            v = m.get(k)
            if k.endswith("_port"):
                if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 65536:
                    mail[k] = v
            elif isinstance(v, str) and len(v) <= MAX_STR:
                mail[k] = v
        if not (mail.get("login") and mail.get("password") and mail.get("imap_host") and mail.get("smtp_host")):
            mail = None
    return {"conf": conf, "fw": fw, "mail": mail}


def print_(items) -> str:
    """Отпечаток настроек — 16 hex; порядок ключей не важен, traffic не входит."""
    raw = json.dumps(clean(items), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def traffic_clean(t) -> dict | None:
    """{"month": "ГГГГ-ММ", "rx", "tx"} или None."""
    if not isinstance(t, dict) or not isinstance(t.get("month"), str) or not _MONTH_RE.fullmatch(t["month"]):
        return None
    try:
        rx, tx = int(t.get("rx") or 0), int(t.get("tx") or 0)
    except (TypeError, ValueError):
        return None
    if rx < 0 or tx < 0:
        return None
    return {"month": t["month"], "rx": rx, "tx": tx}


def traffic_merge(acc: dict, traffic, month: str) -> dict:
    """Счёт линка новой установки продолжает цифры блока того же месяца:
    rx/tx складываются, last_* — свои (счётчики нового интерфейса). Блок
    другого месяца — не переносится."""
    t = traffic_clean(traffic)
    acc = dict(acc or {})
    if t is None or t["month"] != month:
        return acc
    if str(acc.get("month") or "") != month:
        acc = {"month": month, "rx": 0, "tx": 0, "last_rx": acc.get("last_rx", 0), "last_tx": acc.get("last_tx", 0)}
    acc["rx"] = int(acc.get("rx") or 0) + t["rx"]
    acc["tx"] = int(acc.get("tx") or 0) + t["tx"]
    return acc


def _flat(items) -> dict:
    c = clean(items)
    out = dict(c["conf"])
    out.update({f"fw:{k}": v for k, v in c["fw"].items()})
    out["mail"] = c["mail"]
    return out


def differs(a, b) -> list[str]:
    """Группы, в которых b изменил бы a (ключи, которых в b нет, не в счёт —
    их применение не трогает), — в порядке GROUPS."""
    fa, fb = _flat(a), _flat(b)
    return [name for name, keys in GROUPS if any(k in fb and fa.get(k) != fb[k] for k in keys)]


def block(items, *, at: str, traffic=None) -> dict:
    """Блок, который агент шлёт серверу."""
    out = {"v": V, "hash": print_(items), "at": str(at)[:40], "items": clean(items)}
    t = traffic_clean(traffic)
    if t is not None:
        out["traffic"] = t
    return out
