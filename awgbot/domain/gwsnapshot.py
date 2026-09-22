"""
gwsnapshot.py — снимок состояния шлюза для канала (концепт «канал линка», §3.5).

Снимок НЕ описывает машину в квартире. Он отвечает на три вопроса, которые
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
превратило бы сборку в периодический запрос с адреса квартиры — ровно тот
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
FACT_FIELDS = ("bundle", "link_contract", "plumbing_gen", "mark_status",
               "agent_version", "awg_generation")
BUNDLE_FIELDS = ("lan_mode", "home_subnets", "resolver", "peer_home_nets", "admin_ips")
FIELDS = FACT_FIELDS + ("peer_nets", "egress_ok", "rev", "ts", "boot_id")

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


def plumbing_gen(info: dict | None) -> str:
    """Поколение обвязки: new — таблица с цепочкой ssh_in, old — таблица без
    неё, none — таблицы нет (обвязку не разворачивали или сняли)."""
    if not info:
        return "none"
    chains = info.get("chains") or ()
    return "new" if "ssh_in" in chains else "old"


def boot_id() -> str:
    """Разный после перезагрузки: ВПС отличает «агент перезапустился» от
    «малина перезагрузилась», не спрашивая аптайм."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
            return f.read().strip()[:64]
    except OSError:
        return ""


def collect(*, mark_status: str, egress_ok: bool | None, guard_info: dict | None,
            peer_nets: tuple[bool, list[str]] | None, rev: int = 1,
            ts: str = "") -> dict:
    """Собрать снимок. Всё чтение — локальное: юнит обвязки, конфиг линка,
    install/awg.lock, константа версии. Ни одного сетевого вызова (§3.5.0.1).

    `bundle.*` читается ИЗ ЮНИТА, а не из памяти агента о применении: юнит —
    это то, с чем скрипт обвязки реально стартует при каждой загрузке. Агент
    может не помнить, как он применял бандл полгода назад; юнит помнит всегда.
    """
    from awgbot.core import config
    from awgbot.infra import awglock, gwguard
    snap = {
        "bundle": {
            "lan_mode": gwguard.unit_env("LAN_MODE"),
            "home_subnets": gwguard.unit_env("HOME_SUBNETS"),
            "resolver": gwguard.unit_env("RESOLVER"),
            "peer_home_nets": gwguard.unit_env("PEER_HOME_NETS"),
            "admin_ips": " ".join(gwguard.unit_admin_ips()),
        },
        "link_contract": link_contract(config.GW_LINK_CONF),
        "plumbing_gen": plumbing_gen(guard_info),
        "mark_status": mark_status or "",
        "agent_version": config.INSTALLED_VERSION,
        "awg_generation": awglock.generation(),
        "egress_ok": egress_ok,
        "rev": int(rev),
        "ts": ts,
        "boot_id": boot_id(),
    }
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

    Снимок приходит С МАЛИНЫ, а она может быть скомпрометирована: для ВПС это
    недоверенные данные для отрисовки, и ничего больше. Поэтому закрытый
    список полей, потолки длин, никакой интерпретации — лишнее отбрасывается
    молча, а не «на всякий случай сохраняется».
    """
    out: dict = {}
    src = raw if isinstance(raw, dict) else {}
    bundle = src.get("bundle")
    if isinstance(bundle, dict):
        out["bundle"] = {k: _clean_str(bundle.get(k, "")) for k in BUNDLE_FIELDS}
    for key in ("link_contract", "plumbing_gen", "mark_status", "agent_version", "boot_id", "ts"):
        if key in src:
            out[key] = _clean_str(src[key])
    if "awg_generation" in src:
        try:
            out["awg_generation"] = int(src["awg_generation"])
        except (TypeError, ValueError):
            pass
    if "egress_ok" in src:
        val = src["egress_ok"]
        out["egress_ok"] = bool(val) if isinstance(val, bool) else None
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
