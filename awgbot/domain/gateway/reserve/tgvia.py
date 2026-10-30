"""
tgvia.py — часть пакета reserve агента (см. __init__): выход агента в Telegram
через линк к резерву `awgrsv`, когда аплинк молчит (концепт «кворум» §4.11),
и немедленный перерезолв аплинка по записи у авторитетного сервера (§4.12).

Обход — метка 0x53 на сокеты Bot API процесса агента (`reserve-gw-setup.sh
--tg on`): они уходят в `awgrsv`, зона второго сервера пересылает только
`tcp 443` на набор Telegram через свой veth `awggx`. Включить: раздел
`reserve` с флагом `tg` (основа кладёт его, когда у агента и у второго
сервера есть `rsvtg1`), рукопожатие аплинка старше HANDSHAKE_STALE, а
`awgrsv` — свежее. Выключить: рукопожатие аплинка свежее и проба через
аплинк ответила два раза подряд; во время обхода проба идёт раз в 60 с ±
40 % — у аплинка без keepalive иначе не было бы трафика. На каждом
переключении runtime пересоздаёт сессию Bot API (живые соединения остались
бы на прежнем пути) и после включения спрашивает `getMe`. Аплинка нет —
решать не по чему, обхода нет. Отметки — в разделе (`tg_via`, `tg_since`),
они переживают новый документ (`_KEEP_KEYS`).

Перерезолв сразу: повод — `state{serving, move}` резерва с номером больше
виденного (адрес — второй сервер) и слово возврата (раздел `reserve` с
`idle` и `move` не меньше номера резерва — адрес основы); адрес берётся из
записи у авторитетного сервера (`uplink.reresolve_now`), не из кэша
резолвера. Один раз на номер переезда; запасной путь раз в минуту по
рукопожатию — без изменений.
"""
from __future__ import annotations

import random
import time

from awgbot.core import config
from awgbot.util import timeutil

from ..base import log
from .store import _write

CHECK_EVERY_S = 15            # рукопожатия аплинка и awgrsv — не чаще
ASSERT_EVERY_S = 600          # пока обход включён — --tg on заново (cgroup юнита после перезапуска новый)
RERESOLVE_EVERY_S = 20        # повтор перерезолва по записи (± 40 %), пока запись не дала адрес
RERESOLVE_MAX_S = 600         # дольше не повторяем: запасной путь по рукопожатию
PROBE_EVERY_S = 60            # проба аплинка во время обхода (± 40 %)
OFF_STREAK = 2                # ответов пробы подряд при свежем рукопожатии — обход выключен


class ReserveTgViaMixin:

    # ── такт ────────────────────────────────────────────────────────────────
    def reserve_tgvia_tick(self, now: float | None = None) -> list[tuple]:
        """Действия: ("tg", "on"|"off") — обвязка и сессия Bot API; ("reresolve",
        {name, expect, why, move}) — Endpoint аплинка по записи; ("push",)."""
        now = time.monotonic() if now is None else now
        cur = self.reserve_section()
        if not cur:
            self.__dict__.pop("_tgvia", None)
            return []
        actions: list[tuple] = []
        actions += self._reresolve_tick(cur, now)
        st = self.__dict__.setdefault("_tgvia", {"at": 0.0, "probe_at": 0.0, "streak": 0, "assert_at": None})
        via = bool(cur.get("tg_via"))
        if via and (st.get("assert_at") is None or now - float(st["assert_at"]) >= ASSERT_EVERY_S):
            st["assert_at"] = now
            actions.append(("tg_assert", "on"))              # после перезапуска агента правило метки надо ставить заново
        if not cur.get("tg") or not self._tgvia_link_present():
            if via:
                actions += self._tgvia_off(cur, "раздел без флага tg или линка нет")
            return actions
        if now - float(st["at"]) < CHECK_EVERY_S:
            return actions
        st["at"] = now
        iface = self._tgvia_uplink_if()
        if not iface:
            if via:
                actions += self._tgvia_off(cur, "аплинка нет")
            return actions
        uplink_stale = self._tgvia_stale(iface)
        if not via:
            if uplink_stale and self._tgvia_fresh(self._tgvia_rsv_if()):
                actions += self._tgvia_on(cur, now)
            return actions
        # обход включён: проба аплинка раз в 60 с ± 40 %, два ответа подряд при свежем рукопожатии — выключить;
        # проба — к резолверу подсети пользователей через аплинк, куда бы он ни смотрел (дома или на клон)
        if now >= float(st["probe_at"]):
            st["probe_at"] = now + PROBE_EVERY_S * random.uniform(0.6, 1.4)
            ok = (not uplink_stale) and self._tgvia_probe(iface)
            st["streak"] = st["streak"] + 1 if ok else 0
            if st["streak"] >= OFF_STREAK:
                actions += self._tgvia_off(cur, "аплинк снова отвечает")
        return actions

    # ── переключения ────────────────────────────────────────────────────────
    def _tgvia_on(self, cur: dict, now: float) -> list[tuple]:
        from awgbot.domain.notices import reserve as rn
        since = timeutil.now_iso()
        cur.update(tg_via=True, tg_since=since)
        _write(cur, self._RSV_STORE)
        st = self.__dict__.setdefault("_tgvia", {})
        st.update(probe_at=now + PROBE_EVERY_S * random.uniform(0.6, 1.4), streak=0)
        primary, _since = self.reserve_primary_seen() if hasattr(self, "reserve_primary_seen") else ("", "")
        log.warning("обход: аплинк молчит с %s, Telegram — через awgrsv", since)
        note = self.reserve_note(cur, rn.gateway_tg_via(str(cur.get("name") or ""), str(cur.get("peer_name") or ""), since), silent=True)
        return [("tg", "on"), ("notify", [note]), ("push",)]

    def _tgvia_off(self, cur: dict, why: str) -> list[tuple]:
        cur.update(tg_via=False, tg_since="")
        _write(cur, self._RSV_STORE)
        st = self.__dict__.setdefault("_tgvia", {})
        st.update(streak=0)
        log.info("обход: %s — Telegram через аплинк", why)
        return [("tg", "off"), ("push",)]

    # ── факты ───────────────────────────────────────────────────────────────
    @staticmethod
    def _tgvia_uplink_if() -> str:
        from awgbot.infra import gwguard
        try:
            return config.GW_UPLINK_IF or gwguard.uplink_interface()
        except Exception:                                     # noqa: BLE001
            return ""

    @staticmethod
    def _tgvia_rsv_if() -> str:
        from awgbot.infra.gwguard import rsv as rsvlink
        return str(getattr(rsvlink, "RSV_IF", "awgrsv") or "awgrsv")

    @staticmethod
    def _tgvia_link_present() -> bool:
        from awgbot.infra.gwguard import rsv as rsvlink
        try:
            return bool(rsvlink.present())
        except Exception:                                     # noqa: BLE001
            return False

    @staticmethod
    def _tgvia_probe(iface: str) -> bool:
        """DNS-запрос к резолверу подсети пользователей через аплинк — любой ответ: аплинк
        жив (до основы дома или до её клона после переезда)."""
        from awgbot.infra import gwguard
        from .watch import _dns_probe
        try:
            subnet = str(gwguard.unit_env("CLIENT_SUBNET") or "").split("/")[0]
            parts = subnet.split(".")
            resolver = ".".join(parts[:3] + ["1"]) if len(parts) == 4 else ""
        except Exception:                                     # noqa: BLE001
            resolver = ""
        return bool(resolver) and _dns_probe(resolver, iface)

    # ── перерезолв сразу (§4.12): повтор, пока запись не дала адрес ─────────
    def _reresolve_tick(self, cur: dict, now: float) -> list[tuple]:
        pending = cur.get("reresolve_pending")
        if not isinstance(pending, dict) or not pending.get("expect"):
            return []
        st = self.__dict__.setdefault("_reresolve", {"next": 0.0, "since": None, "key": None})
        key = (str(pending.get("why") or ""), int(pending.get("move") or 0))
        if st["key"] != key:
            st.update(key=key, since=now, next=now)
        if now - float(st["since"]) > RERESOLVE_MAX_S:
            cur["reresolve_pending"] = None
            _write(cur, self._RSV_STORE)
            log.info("аплинк: перерезолв по записи (%s, move %s) не удался за %d с — запасной путь по рукопожатию",
                     key[0], key[1], RERESOLVE_MAX_S)
            return []
        if now < float(st["next"]):
            return []
        st["next"] = now + RERESOLVE_EVERY_S * random.uniform(0.6, 1.4)
        return [("reresolve", dict(pending))]

    def reserve_reresolve_done(self, pending: dict, result: str) -> None:
        """Исход `uplink.reresolve_now`: адрес встал или уже тот — повод закрыт."""
        if result not in ("set", "same", "skip:ip", "skip:no-uplink", "skip:no-endpoint", "skip:other-name"):
            return
        cur = self.reserve_section()
        cur_p = cur.get("reresolve_pending")
        if isinstance(cur_p, dict) and (str(cur_p.get("why")), int(cur_p.get("move") or 0)) == \
                (str(pending.get("why")), int(pending.get("move") or 0)):
            cur["reresolve_pending"] = None
            _write(cur, self._RSV_STORE)

    @staticmethod
    def _tgvia_stale(iface: str) -> bool:
        """Рукопожатие аплинка старше HANDSHAKE_STALE или его не было; `unknown` — не
        мёртв (разовый сбой команды не решает за аплинк)."""
        from awgbot.infra import gwguard
        from . import uplink
        state, age = gwguard.handshake_state(iface)
        if state == "ok":
            return age is None or age >= uplink.HANDSHAKE_STALE
        return state == "never"

    @staticmethod
    def _tgvia_fresh(iface: str) -> bool:
        from awgbot.infra import gwguard
        from . import uplink
        state, age = gwguard.handshake_state(iface)
        return state == "ok" and age is not None and age < uplink.HANDSHAKE_STALE

    def reserve_tgvia_view(self) -> dict:
        """Для панели: {via, since}; пусто — обхода нет."""
        cur = self.reserve_section()
        if not cur.get("tg_via"):
            return {}
        return {"via": True, "since": str(cur.get("tg_since") or "")}


__all__ = ["ReserveTgViaMixin"]
