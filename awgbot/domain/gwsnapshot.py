"""
gwsnapshot.py — снимок состояния шлюза для канала.

Снимок НЕ описывает машину в локальной сети. Он отвечает на три вопроса, которые
задаёт сам ВПС и на которые сегодня отвечает догадкой:

  1. то ли стоит на шлюзе, что я выдал в конфигурации;
  2. это мой шлюз и что на нём за код;
  3. сработало ли то, что я включил кнопкой на ВПС.

Всё, что не отвечает ни на один из трёх, сюда не входит — даже если его легко
взять и оно красиво выглядит на экране. Вердикты, по которым решение принимает
сам агент (железо, монитор здоровья, SSH, локальная сеть, маршруты к Telegram),
остаются в его чате: второй источник правды о том же факте всегда отстаёт, и
человек будет чинить по устаревшей картинке.

ВТОРОЕ ПРАВИЛО, ИНВАРИАНТ: снимок собирается только из того, что уже лежит
локально. Поле, для которого нужен сетевой вызов, сюда не попадает — никакое и
никогда. Снимок снимается каждым тиком монитора; поле с походом наружу
превратило бы сборку в периодический запрос с локального адреса — ровно тот
маячок, ради избавления от которого переписывали keepalive линка и зонд
живости. Жертва правила уже есть: «доступна версия X» требует GitHub, поэтому
информации об обновлениях в снимке нет вовсе — у шлюза для них свой планировщик.

Отдельный модуль, а не поле в GwStatus: GwStatus растёт по своим причинам (он
рисует панель агента), и общая структура рано или поздно протащила бы в канал
поле «раз уж оно тут есть». Границу сторожит тест на закрытый список имён.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Закрытый список полей. Тест падает на появлении нового: без сторожа в снимок
# со временем натечёт всё, что «и так снимается».
FACT_FIELDS = ("bundle", "link_contract", "mark_status",
               "agent_version", "awg_generation", "agent_bot", "cfg")
# cfg — отпечатки разделов документа конфигурации, которые стоят на шлюзе
# (domain/gwcfg): по ним ВПС решает, что доставить; gen — поколение последнего
# применённого документа. Всё из локального: юнит, конфиги, БД агента.
# Поля блока bundle — те же ключи, что везёт бандл в юнит, одной таблицей в
# util/gwlink: разойдись списки, сверка на ВПС молча не видела бы расхождения.
from awgbot.util import gwlink as _gwlink  # noqa: E402
from awgbot.infra import bootid
BUNDLE_FIELDS = tuple(_gwlink.snap_field(k) for k in _gwlink.BUNDLE_KEYS)
# lan_addr — адрес шлюза в его локальной сети (интерфейс VPN-транзита): ВПС
# подставляет его в настройку роутера вместо плейсхолдера. Локальное чтение,
# как и всё остальное; агент без транзита шлёт пустую строку.
FIELDS = FACT_FIELDS + ("peer_nets", "egress_ok", "lan_addr", "rev", "ts", "boot_id", "rsv", "quo")
_LAN_ADDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

_CONTRACT_RE = re.compile(r"^#\s*awg-bot:\s*контракт линка\s+(\d+)", re.M)


def link_contract(conf_path: str) -> str:
    """Версия контракта линка — из комментария, который вписал бандл. Пусто —
    конфиг старого образца или нечитаем: это тоже ответ, и врать тут нечем."""
    try:
        with open(conf_path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return ""
    m = _CONTRACT_RE.search(head)
    return m.group(1) if m else ""


def boot_id() -> str:
    """Разный после перезагрузки: ВПС отличает «агент перезапустился» от
    «шлюз перезагрузился», не спрашивая аптайм."""
    return bootid.read_boot_id()[:64]


_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{1,32}")   # username Telegram — и только он идёт в ссылку
_CFG_SECTIONS = ("env", "link", "uplink", "mail", "token", "server", "reserve", "quorum")   # = gwcfg.SECTIONS, сверяет тест


def collect(*, mark_status: str, egress_ok: bool | None,
            peer_nets: tuple[bool, list[str]] | None, agent_bot: dict | None = None,
            cfg: dict | None = None, lan_addr: str = "") -> dict:
    """Собрать снимок. Всё чтение — локальное: юнит обвязки, конфиг линка,
    install/awg.lock, константа версии. Ни одного сетевого вызова.

    `bundle.*` читается ИЗ ЮНИТА, а не из памяти агента о применении: юнит —
    это то, с чем скрипт обвязки реально стартует при каждой загрузке. Агент
    может не помнить, как он применял бандл полгода назад; юнит помнит всегда.
    """
    from awgbot.core import config
    from awgbot.infra import awglock, gwguard
    bundle = {_gwlink.snap_field(k): " ".join(gwguard.unit_env(k).split()) for k in _gwlink.BUNDLE_KEYS}
    snap = {
        "bundle": bundle,
        "link_contract": link_contract(config.GW_LINK_CONF),
        "mark_status": mark_status or "",
        "agent_version": config.INSTALLED_VERSION,
        "awg_generation": awglock.generation(),
        # кто бот этого шлюза (username, имя) — сервер ведёт в его чат ссылкой;
        # без своего getMe (сеть на старте) поле пустое, а не выдуманное
        "agent_bot": {"username": str((agent_bot or {}).get("username") or ""),
                      "name": str((agent_bot or {}).get("name") or "")},
        "egress_ok": egress_ok,
        "lan_addr": str(lan_addr or "").strip(),
        # rev ставит клиент по месту в сессии, ts — конверт (время отправки):
        # их значения здесь всё равно перезаписались бы
        "rev": 1,
        "ts": "",
        "boot_id": boot_id(),
    }
    # поле всегда на месте — закрытый список полей не зависит от того, что
    # агент сумел снять; не передали — нули и пустые отпечатки
    cfg = cfg or {}
    snap["cfg"] = {"gen": int(cfg.get("gen") or 0), **{s: str(cfg.get(s) or "") for s in _CFG_SECTIONS}}
    # Единственный вердикт, который едет, и едет по исключению: он объясняет
    # отказ функции, включённой кнопкой на ВПС («доступ между подсетями»).
    # Пусто — функции на этом шлюзе нет, и поля в снимке просто не будет.
    # Везём структуру, а не готовую строку: текст рисует ВПС, и чужой строке
    # на его экране делать нечего.
    if peer_nets is not None:
        ok, missing = peer_nets
        snap["peer_nets"] = {"ok": bool(ok), "missing": list(missing)}
    return snap


def delta(prev: dict | None, cur: dict) -> dict:
    """Что изменилось между снимками. Пусто — значит по каналу не уходит
    ничего: это обычное состояние на дни, а не особый случай.

    `rev`, `ts` и `boot_id` в сравнение не идут: первые два меняются всегда,
    третий — только вместе с новой сессией, которая и так начинается с полного
    снимка.
    """
    if not prev:
        return {k: v for k, v in cur.items() if k not in ("rev", "ts")}
    out: dict = {}
    for key in cur:
        if key in ("rev", "ts", "boot_id"):
            continue
        if cur[key] != prev.get(key):
            out[key] = cur[key]
    # Исчезнувшее поле — тоже новость: функцию выключили, и вердикта больше нет.
    for key in prev:
        if key not in cur and key not in ("rev", "ts", "boot_id"):
            out[key] = None
    return out


_MAX_STR = 512
_MAX_LIST = 64


def _clean_str(val) -> str:
    return str(val)[:_MAX_STR] if isinstance(val, (str, int, float)) else ""


def sanitize(raw: dict) -> dict:
    """Привести принятое к известным полям и длинам.

    Снимок приходит СО ШЛЮЗА, а он может быть скомпрометирован: для ВПС это
    недоверенные данные для отрисовки, и ничего больше. Поэтому закрытый
    список полей, потолки длин, никакой интерпретации — лишнее отбрасывается
    молча, а не «на всякий случай сохраняется».
    """
    out: dict = {}
    src = raw if isinstance(raw, dict) else {}
    bundle = src.get("bundle")
    if isinstance(bundle, dict):
        out["bundle"] = {k: _clean_str(bundle.get(k, "")) for k in BUNDLE_FIELDS}
    for key in ("link_contract", "mark_status", "agent_version", "boot_id", "ts"):
        if key in src:
            out[key] = _clean_str(src[key])
    if "awg_generation" in src:
        try:
            out["awg_generation"] = int(src["awg_generation"])
        except (TypeError, ValueError):
            pass
    ab = src.get("agent_bot")
    if isinstance(ab, dict):
        # username уходит в адрес ссылки — только то, что Telegram и выдаёт
        username = _clean_str(ab.get("username", ""))
        out["agent_bot"] = {"username": username if _USERNAME_RE.fullmatch(username) else "",
                            "name": _clean_str(ab.get("name", ""))[:64]}
    if "egress_ok" in src:
        val = src["egress_ok"]
        out["egress_ok"] = bool(val) if isinstance(val, bool) else None
    if "lan_addr" in src:
        # адрес уходит в команды для роутера: только IPv4 как есть, иначе пусто
        addr = _clean_str(src["lan_addr"]).strip()
        out["lan_addr"] = addr if _LAN_ADDR_RE.match(addr) else ""
    pn = src.get("peer_nets")
    if isinstance(pn, dict):
        missing = pn.get("missing")
        out["peer_nets"] = {
            "ok": bool(pn.get("ok")),
            "missing": [_clean_str(x) for x in list(missing)[:_MAX_LIST]]
            if isinstance(missing, list) else [],
        }
    elif "peer_nets" in src and src["peer_nets"] is None:
        out["peer_nets"] = None
    if "rev" in src:
        try:
            out["rev"] = int(src["rev"])
        except (TypeError, ValueError):
            pass
    cfg = src.get("cfg")
    if isinstance(cfg, dict):
        # отпечатки — только hex по 16 знаков, поколение — целое: экран и сверка
        # не получат ничего, кроме того, что сами умеют сравнивать
        try:
            gen = int(cfg.get("gen") or 0)
        except (TypeError, ValueError):
            gen = 0
        out["cfg"] = {"gen": gen, **{s: _gwlink.clean_hex(cfg.get(s), 16) for s in _CFG_SECTIONS}}
    elif "cfg" in src and src["cfg"] is None:
        out["cfg"] = None
    rsv = src.get("rsv")
    if isinstance(rsv, dict):
        # резерв глазами шлюза: связь с резервом, доля, пересылка — только строки
        # из перечня и целые; экран сервера и готовность направления читают это
        seen = _clean_str(rsv.get("seen", ""))
        out["rsv"] = {"seen": seen if seen in ("ok", "suspect", "down", "absent") else "",
                      "since": _clean_str(rsv.get("since", ""))[:40],
                      "gen": int(rsv.get("gen") or 0) if str(rsv.get("gen") or "0").isdigit() else 0,
                      "open": bool(rsv.get("open")), "state": _clean_str(rsv.get("state", ""))[:16],
                      "primary": _clean_str(rsv.get("primary", ""))[:16]}
    elif "rsv" in src and src["rsv"] is None:
        out["rsv"] = None
    quo = src.get("quo")
    if isinstance(quo, dict):
        # кворум глазами шлюза: взгляд на основу, поколение состава, известная эпоха
        w = _clean_str(quo.get("w", ""))
        out["quo"] = {"w": w if w in ("see", "sus", "uns", "new", "held") else "",
                      "since": _clean_str(quo.get("since", ""))[:40],
                      "g": int(quo.get("g") or 0) if str(quo.get("g") or "0").isdigit() else 0,
                      "e": int(quo.get("e") or 0) if str(quo.get("e") or "0").isdigit() else 0}
    elif "quo" in src and src["quo"] is None:
        out["quo"] = None
    return out


def apply_delta(snap: dict, patch: dict) -> dict:
    """Наложить дельту на хранимый снимок. None в значении — поле исчезло."""
    out = dict(snap or {})
    for key, val in (patch or {}).items():
        if key in ("rev", "ts", "seq", "t"):
            continue
        if val is None:
            out.pop(key, None)
        else:
            out[key] = val
    return out
