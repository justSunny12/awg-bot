"""doc.py — разделы документа, ключи env, сборка и разбор документа и ответа."""
from __future__ import annotations

import re

from awgbot.util import gwlink
from awgbot.util.wgconf import norm


class CfgError(ValueError):
    """Документ или раздел не принят: причина — словами для ответа и уведомления."""


CAP = "cfg1"                                   # возможность агента: принимает документ cfg и отвечает cfg_ack
LINK_CONTRACT = "1"                            # штамп «контракт линка N» — тот же, что LINK_CONTRACT скрипта линка (сверяет тест)

# Разделы — в порядке применения на агенте: имя сервера, env+link одной
# транзакцией, почта, токен, аплинк. Фразы бэкапов в документе нет: у шлюза
# своих копий нет, его настройки — в бэкапе сервера AWG.
SECTIONS = ("server", "env", "link", "mail", "token", "uplink")

# Ключи раздела env — те же имена, что у строк Environment= юнита обвязки.
ENV_KEYS = ("CLIENT_SUBNET", "ADMIN_IPS", "LAN_MODE", "LAN_SUBNETS", "RESOLVER",
            "PEER_LAN_NETS", "LINK_CHANNEL_PORT")
ENV_REQUIRED = ENV_KEYS

MAIL_KEYS = ("login", "password", "imap_host", "imap_port", "smtp_host", "smtp_port")

# Человеческие имена — для уведомлений обеих сторон («применено: …», «не применилось: …»).
HUMAN = {
    **gwlink.KEY_HUMAN,
    "CLIENT_SUBNET": "подсеть клиентов",
    "LINK_CHANNEL_PORT": "порт канала конфигурации",
    "env": "настройки обвязки",
    "link": "конфиг линка",
    "uplink": "конфиг аплинка",
    "mail": "почта",
    "token": "токен бота",
    "server": "имя сервера",
}


def human(what: str) -> str:
    return HUMAN.get(what, what)


_HEX_RE = re.compile(r"^[0-9a-f]{1,64}$")
_MAX_CONF = 16 * 1024
_MAX_STR = 512


def _s(val, limit: int = _MAX_STR) -> str:
    return str(val)[:limit] if isinstance(val, (str, int, float)) and not isinstance(val, bool) else ""


def _hex(val) -> str:
    s = _s(val, 64)
    return s if _HEX_RE.match(s) else ""


def _int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        return default


# ── документ cfg ─────────────────────────────────────────────────────────────

def build(gen: int, digest: str, sections: dict) -> dict:
    """Тело сообщения `cfg`: поколение, отпечаток документа, разделы (None —
    у сервера раздела нет, он не едет: своё на шлюзе не трогается)."""
    body: dict = {"gen": int(gen), "hash": str(digest)}
    for name in SECTIONS:
        if sections.get(name) is not None:
            body[name] = sections[name]
    return body


def parse(msg: dict) -> dict:
    """Разобрать принятый `cfg` до известных разделов и типов. Данные с другой
    машины — только известные ключи, строки с потолком длины; неизвестный раздел
    или ключ пропускается (новый сервер, старый агент). Возвращает
    {"gen", "hash", "sections": {раздел: {...}}} — проверка значений отдельно (check)."""
    src = msg if isinstance(msg, dict) else {}
    out: dict = {"gen": _int(src.get("gen")), "hash": _hex(src.get("hash")), "sections": {}}
    env = src.get("env")
    if isinstance(env, dict):
        out["sections"]["env"] = {k: norm(env.get(k)) for k in ENV_KEYS if k in env}
    link = src.get("link")
    if isinstance(link, dict):
        out["sections"]["link"] = {"conf": _s(link.get("conf"), _MAX_CONF)}
    uplink = src.get("uplink")
    if isinstance(uplink, dict):
        out["sections"]["uplink"] = {"conf": _s(uplink.get("conf"), _MAX_CONF),
                                     "pubkey": norm(uplink.get("pubkey")),
                                     "prev_pubkey": norm(uplink.get("prev_pubkey"))}
    mail = src.get("mail")
    if isinstance(mail, dict):
        out["sections"]["mail"] = {
            **{k: _s(mail.get(k)) for k in ("login", "password", "imap_host", "smtp_host")},
            **{k: _int(mail.get(k)) for k in ("imap_port", "smtp_port")},
        }
    token = src.get("token")
    if isinstance(token, dict):
        out["sections"]["token"] = {"value": norm(token.get("value"))}
    server = src.get("server")
    if isinstance(server, dict):
        out["sections"]["server"] = {"name": norm(server.get("name"))}
    return out


# ── ответ cfg_ack ────────────────────────────────────────────────────────────

def ack_build(gen: int, digest: str, *, applied=(), wait=None, declined=None, failed=None,
              retry: bool = False) -> dict:
    """Тело `cfg_ack`: что применено, что ждёт человека (раздел → отпечаток
    серверного значения), что отклонено (так же), что не применилось и почему;
    retry — временный отказ (юнит обвязки ещё работает), сервер повторит."""
    body: dict = {"gen": int(gen), "hash": str(digest), "applied": [s for s in SECTIONS if s in set(applied)],
                  "wait": dict(wait or {}), "declined": dict(declined or {}), "failed": dict(failed or {})}
    if retry:
        body["retry"] = True
    return body


def ack_parse(msg: dict) -> dict:
    """Разобрать принятый `cfg_ack`: только известные разделы, отпечатки — hex,
    причины — строки с потолком; всё прочее отбрасывается."""
    src = msg if isinstance(msg, dict) else {}
    out: dict = {"gen": _int(src.get("gen")), "hash": _hex(src.get("hash")), "applied": [],
                 "wait": {}, "declined": {}, "failed": {}, "retry": bool(src.get("retry"))}
    applied = src.get("applied")
    if isinstance(applied, list):
        out["applied"] = [s for s in SECTIONS if s in applied]
    for field in ("wait", "declined"):
        raw = src.get(field)
        if isinstance(raw, dict):
            out[field] = {s: _hex(raw[s]) for s in SECTIONS if s in raw}
    failed = src.get("failed")
    if isinstance(failed, dict):
        out["failed"] = {s: " ".join(_s(failed[s], 300).split()) for s in (*SECTIONS, "*") if s in failed}
    return out
