"""
gwcfg/ — документ конфигурации шлюза, который ездит каналом ВПС ↔ шлюз (части:
doc — разделы, ключи env, сопоставление имён документа и юнита, сборка и
разбор документа и ответа; check — проверки значений по разделам, конфиги —
через util/wgconf; prints — канонический текст разделов, отпечатки, поколение).

Общий модуль обеих ролей, как gwservices и gwownlists: формат и проверки
обязаны совпадать на концах. Чистые функции — ни одного вызова на хосте.

Сервер — источник желаемого, шлюз — источник фактов: сервер каждый раз
вычисляет документ из тех же источников, из которых собирается файл первого
применения, шлюз отвечает отпечатками того, что у него стоит (снимок), и
доставка идёт по расхождению. Данные — не код: каждое значение проверяется
строго, конфиги WireGuard — белым списком; не прошло одно — отвергается весь
документ.
"""
from __future__ import annotations

from .doc import (SECTIONS, ENV_KEYS, ENV_REQUIRED, UNIT_NAMES, MAIL_KEYS, MAIL_HINT, HUMAN, CAP, LINK_CONTRACT,
                  unit_name, doc_name, human, build, parse, ack_build, ack_parse, CfgError)
from .check import (check_env, check_mail, check_backup, check_token, check_server, check_link,
                    check_uplink, check_doc, check_link_rekey, merge_link_keys, MIN_PHRASE)
from .prints import (env_text, mail_text, section_print, doc_print, env_print, mail_print,
                     backup_print, backup_key_print, server_print, token_print, link_print,
                     uplink_print, key_fp, PRINT_LEN)

__all__ = [
    "SECTIONS", "ENV_KEYS", "ENV_REQUIRED", "UNIT_NAMES", "MAIL_KEYS", "MAIL_HINT", "HUMAN", "CAP", "LINK_CONTRACT",
    "unit_name", "doc_name", "human", "build", "parse", "ack_build", "ack_parse", "CfgError",
    "check_env", "check_mail", "check_backup", "check_token", "check_server", "check_link",
    "check_uplink", "check_doc", "check_link_rekey", "merge_link_keys", "MIN_PHRASE",
    "env_text", "mail_text", "section_print", "doc_print", "env_print", "mail_print",
    "backup_print", "backup_key_print", "server_print", "token_print", "link_print",
    "uplink_print", "key_fp", "PRINT_LEN",
]
