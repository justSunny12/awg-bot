"""
tgpool.py — часть пакета reserve (см. __init__): пул адресов Telegram для
порта вебхука бота основы на резерве.

пул = ядро ∪ выученное. Ядро — документированные диапазоны Bot API
(`nftguard.DEFAULT_TG_POOL`), всегда. Выученное — префикс анонса любой AS
Telegram (`reserve.tg_pool_asns`), с адреса из которого в `serving` стучались
в порт вебхука ≥ `tg_pool_learn_syn` раз за 10 мин (динамический набор
`tg_seen4` файервола хоста: SYN виден до отбрасывания), и который сейчас
анонсируется. Анонсы — RIPEstat `announced-prefixes`, раз в `tg_pool_hours`
± 40 %, только пока есть пара; источник недоступен — пул не меняется, кэш
анонсов живёт в файле. Файл `reserve/tg_pool.json`: `pool` (выученные
префиксы — их читает файервол), `learned` (с датой и AS), `announced`,
`fetched_at`, `error`, `told`. Выучивание — только строкой в журнал (Т-РУ26
снят); Т-РУ25 — анонсы не обновлялись дольше `tg_pool_stale_days` или часть
ядра больше не анонсируется (страница Bot API могла устареть).
"""
from __future__ import annotations

import ipaddress
import json
import os
import random
import time
from pathlib import Path

from awgbot.core import config, settings
from awgbot.util import timeutil

from ..types import Notification
from ..gateway_link.common import log

LEARNED_MIN_PREFIX = 12               # шире /12 не учим
FETCH_TIMEOUT = 10              # с — правило проекта: наружу не дольше 10 с


def pool_path() -> Path:
    return Path(config.DATA_DIR) / "reserve" / "tg_pool.json"


def _read() -> dict:
    try:
        data = json.loads(pool_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = pool_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def parse_ripestat(doc: dict) -> list[str]:
    """Префиксы IPv4 из ответа RIPEstat, анонсируемые сейчас (`endtime` последнего
    отрезка == `latest_time` ответа); мусор — прочь."""
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        return []
    latest = str(data.get("latest_time") or "")
    out: list[str] = []
    for item in data.get("prefixes") or []:
        if not isinstance(item, dict):
            continue
        try:
            net = ipaddress.ip_network(str(item.get("prefix") or ""), strict=False)
        except ValueError:
            continue
        if net.version != 4:
            continue
        tl = item.get("timelines") or []
        ends = [str(t.get("endtime") or "") for t in tl if isinstance(t, dict)]
        if latest and ends and max(ends) < latest:
            continue
        out.append(str(net))
    return sorted(set(out))


def fetch_announced(asn: int, url_tpl: str) -> tuple[list[str], str]:
    """(префиксы, ошибка) по одной AS."""
    import urllib.request
    url = str(url_tpl).replace("{asn}", str(int(asn)))
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:   # noqa: S310 — адрес из настроек
            raw = resp.read(4 * 1024 * 1024)
        return parse_ripestat(json.loads(raw.decode("utf-8", errors="replace"))), ""
    except Exception as e:                                # noqa: BLE001
        return [], f"{type(e).__name__}: {str(e)[:120]}"


def narrowest(addr: str, prefixes) -> str:
    """Самый узкий префикс из перечня, содержащий адрес; пусто — нет."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return ""
    best = None
    for p in prefixes:
        try:
            net = ipaddress.ip_network(p, strict=False)
        except ValueError:
            continue
        if ip in net and (best is None or net.prefixlen > best.prefixlen):
            best = net
    return str(best) if best is not None else ""


def core_covered(core, announced) -> list[str]:
    """Префиксы ядра, которые НЕ покрыты анонсами (вложенность, не пересечение)."""
    nets = []
    for p in announced:
        try:
            nets.append(ipaddress.ip_network(p, strict=False))
        except ValueError:
            pass
    lost = []
    for c in core:
        cn = ipaddress.ip_network(c, strict=False)
        # ядро покрыто, если каждый адрес ядра лежит в каком-то анонсе: проверяем
        # разбиением ядра на анонсы, вложенные в него, плюс анонсы шире ядра
        if any(cn.subnet_of(n) for n in nets):
            continue
        inner = [n for n in nets if n.subnet_of(cn)]
        covered = sum(n.num_addresses for n in ipaddress.collapse_addresses(inner))
        if covered < cn.num_addresses:
            lost.append(str(cn))
    return lost


class ReserveTgPoolMixin:
    def reserve_tg_pool(self) -> dict:
        return _read()

    def _tg_pool_cfg(self) -> dict:
        asns = settings.get("app.reserve.tg_pool_asns", [62041, 62014, 59930, 44907, 211157]) or []
        return {"asns": [int(a) for a in asns if str(a).isdigit() or isinstance(a, int)],
                "url": str(settings.get("app.reserve.tg_pool_url",
                                        "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS{asn}") or ""),
                "hours": max(1, settings.get_int("app.reserve.tg_pool_hours", 24)),
                "max": max(1, settings.get_int("app.reserve.tg_pool_max", 256)),
                "syn": max(1, settings.get_int("app.reserve.tg_pool_learn_syn", 3)),
                "stale_days": max(1, settings.get_int("app.reserve.tg_pool_stale_days", 3))}

    # ── анонсы ──────────────────────────────────────────────────────────────
    def reserve_tg_pool_due(self, now: float | None = None) -> bool:
        """Пора ли обновлять анонсы: раз в tg_pool_hours ± 40 %, только при паре."""
        if not self.reserve_paired():
            return False
        now = time.monotonic() if now is None else now
        flag = pool_path().with_name("tg_pool.refresh")        # `awg-bot reserve tgpool refresh`
        if flag.exists():
            try:
                flag.unlink()
            except OSError:
                pass
            return True
        nxt = self.__dict__.get("_tg_pool_next")
        if nxt is None:
            data = _read()
            # после старта — скоро, если кэша нет; иначе по возрасту кэша
            self.__dict__["_tg_pool_next"] = nxt = now + (60 if not data.get("announced") else 600) * random.uniform(0.6, 1.4)
        return now >= nxt

    def reserve_tg_pool_refresh_soon(self, delay_s: float = 5.0, *, why: str = "") -> None:
        """Внеплановое обновление анонсов: при переходе в serving (до вебхука) и
        при кандидате вне кэша — не чаще раза в 30 минут."""
        now = time.monotonic()
        if why == "unknown":
            last = float(self.__dict__.get("_tg_pool_unknown_at") or 0)
            if last and now - last < 1800:
                return
            self.__dict__["_tg_pool_unknown_at"] = now
        self.__dict__["_tg_pool_next"] = now + delay_s * random.uniform(0.6, 1.4)

    def reserve_tg_pool_tick(self, now: float | None = None) -> list[Notification]:
        """Обновить анонсы AS Telegram, проверить ядро, снять неанонсируемое из
        выученного; Т-РУ25 один раз на расхождение."""
        if not self.reserve_tg_pool_due(now):
            return []
        cfg = self._tg_pool_cfg()
        now = time.monotonic() if now is None else now
        self.__dict__["_tg_pool_next"] = now + cfg["hours"] * 3600 * random.uniform(0.6, 1.4)
        data = _read()
        announced: dict = dict(data.get("announced") or {})   # кэш по AS: неответившая AS остаётся с прежним
        errors = []
        fetched_any = False
        for asn in cfg["asns"]:
            prefixes, err = fetch_announced(asn, cfg["url"])
            if err:
                errors.append(f"AS{asn}: {err}")
                continue
            announced[str(asn)] = prefixes
            fetched_any = True
        notes: list[Notification] = []
        told = dict(data.get("told") or {})
        name = self.reserve_peer_name()
        data["error"] = "; ".join(errors)
        if fetched_any:
            data["announced"] = announced
            data["fetched_at"] = timeutil.now_iso()
            all_prefixes = sorted({p for ps in announced.values() for p in ps})
            lost = core_covered(self._core(), all_prefixes)
            kept = [x for x in data.get("learned") or [] if isinstance(x, dict) and str(x.get("prefix") or "") in all_prefixes]
            dropped = len(data.get("learned") or []) - len(kept)
            if dropped:
                log.info("пул Telegram: %d выученных префиксов больше не анонсируются — сняты", dropped)
            data["learned"] = kept
            data["pool"] = [x["prefix"] for x in kept]
            if lost and told.get("core") != ",".join(lost):
                told["core"] = ",".join(lost)
                notes.append(Notification(tg_id=int(config.ADMIN_ID), text=self._rn().tg_pool_core_lost(lost[0]), silent=True))
            elif not lost and told.get("core"):
                told["core"] = ""
            if told.get("stale"):
                told["stale"] = ""
                notes.append(Notification(tg_id=int(config.ADMIN_ID), text=self._rn().tg_pool_ok(), silent=True))
        else:
            # ни одна AS не ответила: пул прежний; застой считается от последнего удачного
            # обновления, а до первого — от первой неудачи (свежая пара — не «давно»)
            data.setdefault("first_fail_at", timeutil.now_iso())
            since = str(data.get("fetched_at") or data.get("first_fail_at") or "")
            try:
                age = timeutil.elapsed(timeutil.parse_iso(since))
            except ValueError:
                age = 0.0
            if age >= cfg["stale_days"] * 86400 and not told.get("stale"):
                told["stale"] = timeutil.now_iso()
                notes.append(Notification(tg_id=int(config.ADMIN_ID), text=self._rn().tg_pool_stale(str(data.get("fetched_at") or "")), silent=True))
            log.warning("пул Telegram: анонсы не обновлены (%s) — пул прежний", data["error"])
        if fetched_any:
            data.pop("first_fail_at", None)
        data["told"] = told
        _write(data)
        if notes:
            log.info("пул Telegram для «%s»: %d уведомлений", name, len(notes))
        return notes

    @staticmethod
    def _core() -> list[str]:
        from awgbot.infra import nftguard
        return list(nftguard.DEFAULT_TG_POOL)

    # ── обучение по факту ───────────────────────────────────────────────────
    def reserve_tg_learn_tick(self) -> list[tuple]:
        """Раз в минуту при serving: адреса с ≥ N SYN на порт вебхука вне пула,
        принадлежащие анонсу AS Telegram, — в пул префиксом анонса (журнал)."""
        if self.reserve_in() != "serving":
            return []
        from awgbot.infra import nftguard
        try:
            seen = nftguard.live_set_counters(nftguard.SET_TG_SEEN)
        except Exception as e:                                # noqa: BLE001
            log.info("пул Telegram: набор tg_seen4 не прочитан: %s", e)
            return []
        if not seen:
            return []
        cfg = self._tg_pool_cfg()
        data = _read()
        announced = data.get("announced") or {}
        by_asn = {asn: list(ps) for asn, ps in announced.items() if isinstance(ps, list)}
        pool = set(nftguard.tg_pool())
        learned = [x for x in data.get("learned") or [] if isinstance(x, dict)]
        added = 0
        for addr, hits in seen.items():
            if int(hits) < cfg["syn"]:
                continue
            if narrowest(addr, pool):
                continue                                      # уже в пуле (гонка с перерисовкой)
            best, best_asn = "", ""
            for asn, ps in by_asn.items():
                p = narrowest(addr, ps)
                if p and (not best or ipaddress.ip_network(p).prefixlen > ipaddress.ip_network(best).prefixlen):
                    best, best_asn = p, asn
            if not best:
                log.info("пул Telegram: %d SYN с %s — адрес вне анонсов AS Telegram, не учу", int(hits), addr)
                self.reserve_tg_pool_refresh_soon(60, why="unknown")   # анонс мог появиться после кэша
                continue
            if ipaddress.ip_network(best).prefixlen < LEARNED_MIN_PREFIX:
                log.info("пул Telegram: анонс %s шире /%d — не учу", best, LEARNED_MIN_PREFIX)
                continue
            if len(learned) >= cfg["max"]:
                log.warning("пул Telegram: потолок выученных (%d) — %s не добавлен", cfg["max"], best)
                break
            learned.append({"prefix": best, "asn": best_asn, "at": timeutil.now_iso(), "addr": addr})
            pool.add(best)
            added += 1
            log.warning("пул Telegram: выучена сеть %s (AS%s) по %d SYN с %s на порт вебхука", best, best_asn, int(hits), addr)
        if not added:
            return []
        data["learned"] = learned
        data["pool"] = [x["prefix"] for x in learned]
        _write(data)
        self._tg_pool_to_guest()
        return [("fw",)]

    @staticmethod
    def _tg_pool_to_guest() -> None:
        """Полный пул — в хранилище гостя: его слушатель вебхука подхватит новые
        префиксы по такту (файл есть только пока зона обслуживает)."""
        from awgbot.infra import nftguard, rsvzone
        if not rsvzone.TG_POOL_FILE.exists():
            return
        try:
            rsvzone.write_private(rsvzone.TG_POOL_FILE, json.dumps({"pool": list(nftguard.tg_pool())}))
        except OSError as e:
            log.info("пул Telegram: файл пула гостя не обновлён: %s", e)

    def reserve_tg_pool_view(self) -> dict:
        """Для экранов: ядро, число выученных, когда обновлялись анонсы, застой."""
        data = _read()
        cfg = self._tg_pool_cfg()
        since = str(data.get("fetched_at") or data.get("first_fail_at") or "")
        try:
            age = timeutil.elapsed(timeutil.parse_iso(since)) if since else 0.0
        except ValueError:
            age = 0.0
        return {"learned": len(data.get("learned") or []), "fetched_at": str(data.get("fetched_at") or ""),
                "stale": age >= cfg["stale_days"] * 86400, "error": str(data.get("error") or "")}

    def _rn(self):
        from awgbot.domain.notices import reserve as rn
        return rn
