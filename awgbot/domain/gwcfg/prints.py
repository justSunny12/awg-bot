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
    """Канонический текст почты: ящик и серверы по порядку; подсказка backup
    не входит — на шлюзе ей соответствует свой выбор канала, а не значение.
    Неполный ящик (нет логина, пароля или хоста) — заглушка, текст пуст."""
    if not mail:
        return ""
    if not (norm(mail.get("login")) and str(mail.get("password") or "") and norm(mail.get("imap_host"))
            and norm(mail.get("smtp_host"))):
        return ""
    return "".join(f"{k}={norm(mail.get(k, ''))}\n" for k in MAIL_KEYS)


def mail_print(mail: dict | None) -> str:
    return section_print("mail", mail_text(mail))


def backup_print(phrase: str) -> str:
    """Фраза шифрования — отпечаток самой фразы; пусто — шифрование не задано."""
    return section_print("backup", str(phrase or ""))


def backup_key_print(key_b64: str) -> str:
    """Шлюз на случайном ключе шифрования (прежняя схема): отпечаток ключа в
    своём домене — с серверной фразой не совпадает никогда, и это не заглушка."""
    return section_print("backup-key", str(key_b64 or ""))


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
