"""prints.py — канонический текст разделов и отпечатки документа."""
from __future__ import annotations

import hashlib

from awgbot.util import wgconf
from awgbot.util.wgconf import norm

from .doc import ENV_KEYS, MAIL_KEYS

PRINT_LEN = 16
_DOMAIN = "awgbot-cfg-v1"


def section_print(section: str, text: str) -> str:
    """Отпечаток раздела: sha256(домен + раздел + канонический текст), первые
    16 знаков; пустой текст (заглушка: ящик не задан, шифрование не задано) —
    пустой отпечаток. Простой хэш, не HMAC ключом канала: все значения сервер и
    так знает, а отпечаток не должен меняться при смене ключей линка."""
    if not text:
        return ""
    return hashlib.sha256(f"{_DOMAIN}\n{section}\n{text}".encode()).hexdigest()[:PRINT_LEN]


def doc_print(prints: dict) -> str:
    """Отпечаток документа — от отпечатков разделов по порядку (пустой — как пусто)."""
    from .doc import SECTIONS
    text = "\n".join(f"{s}={prints.get(s, '') or ''}" for s in SECTIONS)
    return hashlib.sha256(f"{_DOMAIN}\ndoc\n{text}".encode()).hexdigest()[:PRINT_LEN]


def env_text(env: dict) -> str:
    """Канонический текст env: ИМЯ=значение в порядке таблицы ключей, значения нормализованы."""
    return "".join(f"{k}={norm(env.get(k, ''))}\n" for k in ENV_KEYS)


def env_print(env: dict) -> str:
    return section_print("env", env_text(env))


def mail_text(mail: dict | None) -> str:
    """Канонический текст почты: ящик и серверы по порядку.
    Неполный ящик (нет логина, пароля или хоста) — заглушка, текст пуст."""
    if not mail:
        return ""
    if not (norm(mail.get("login")) and str(mail.get("password") or "") and norm(mail.get("imap_host"))
            and norm(mail.get("smtp_host"))):
        return ""
    return "".join(f"{k}={norm(mail.get(k, ''))}\n" for k in MAIL_KEYS)


def mail_print(mail: dict | None) -> str:
    return section_print("mail", mail_text(mail))


def server_print(name: str) -> str:
    return section_print("server", norm(name))


def token_print(token: str) -> str:
    return section_print("token", str(token or "").strip())


def link_print(conf_text: str) -> str:
    """Отпечаток конфига линка: канонический текст без штампа и комментариев;
    конфиг не разбирается — пустой отпечаток (на шлюзе — «стоит не наш»)."""
    if not str(conf_text or "").strip():
        return ""
    try:
        return section_print("link", wgconf.canonical(wgconf.parse(conf_text), "link"))
    except wgconf.WgConfError:
        return ""


def key_fp(*pubkeys: str) -> str:
    """Отпечаток пары ключей для человека: первые 8 знаков sha256 от
    публичных ключей по порядку, двумя группами — «ab12 cd34». Одинаково
    считают ВПС (строка карточки) и шлюз (приглашение): сверка глазами.
    Ключа нет — пусто."""
    if not all(pubkeys):
        return ""
    h = hashlib.sha256("\n".join(pubkeys).encode()).hexdigest()[:8]
    return f"{h[:4]} {h[4:]}"


def uplink_print(conf_text: str, pubkey: str = "", prev_pubkey: str = "") -> str:
    """Отпечаток аплинка: канонический конфиг (без PostUp/PostDown политики,
    их дописывает скрипт на шлюзе) и ключи признания."""
    try:
        canon = (wgconf.canonical(wgconf.drop_keys(wgconf.parse(conf_text), ("PostUp", "PostDown")), "uplink")
                 if conf_text else "")
    except wgconf.WgConfError:
        return ""
    if not canon:
        return ""
    return section_print("uplink", f"{canon}pubkey={norm(pubkey)}\nprev_pubkey={norm(prev_pubkey)}\n")


def quorum_print(sec: dict | None) -> str:
    """Отпечаток раздела quorum: линия, поколение, состав, порог, порт, канонические конфиги
    зондов, точка (номер и поколение), эпоха. Раздела нет — пусто."""
    if not sec or not sec.get("l"):
        return ""
    probes = sec.get("probes") or {}
    canon = []
    for key in ("h", "r"):
        try:
            canon.append(f"{key}:" + (wgconf.canonical(wgconf.parse(str(probes.get(key) or "")), "rsvlink") if probes.get(key) else ""))
        except wgconf.WgConfError:
            return ""
    point = sec.get("point") or {}
    text = (f"l={norm(sec.get('l'))}\ng={int(sec.get('g') or 0)}\nmembers={','.join(str(m) for m in (sec.get('members') or []))}\n"
            f"t={int(sec.get('t') or 0)}\nport={int(sec.get('port') or 0)}\n" + "\n".join(canon)
            + f"\npoint={int(point.get('x') or 0)}:{norm(point.get('y'))}:{int(point.get('q_gen') or 0)}\ne={int(sec.get('e') or 0)}\n")
    return section_print("quorum", text)


def reserve_print(sec: dict | None) -> str:
    """Отпечаток раздела reserve: канонический конфиг линка к резерву, доля
    (точка и поколение), состояние пересылки с номером переезда, адрес основы.
    Раздела нет — пусто."""
    if not sec:
        return ""
    link = sec.get("link") or {}
    try:
        canon = wgconf.canonical(wgconf.parse(str(link.get("conf") or "")), "rsvlink") if link.get("conf") else ""
    except wgconf.WgConfError:
        return ""
    share = sec.get("share") or {}
    text = (f"{canon}self={norm(sec.get('self'))}\nstate={norm(sec.get('state'))}\nmove={int(sec.get('move') or 0)}\n"
            f"share={int(share.get('x') or 0)}:{norm(share.get('y'))}:{int(share.get('gen') or 0)}\n"
            f"chan={int(sec.get('chan') or 0)}\n"
            # адрес основы и имя записи — опора факта записи и текстов: их смена тянет документ
            f"peer_host={norm(sec.get('peer_host'))}\nrecord={norm(sec.get('record'))}\n"
            f"name={norm(sec.get('name'))}\nslot={int(sec.get('slot') or 0)}\n")
    return section_print("reserve", text)

