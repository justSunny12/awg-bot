"""
rfblock.py — часть пакета reserve (см. __init__): случаи 3 и Н3 матрицы
(README §6i, «Случаи 3 и Н3») — обе стороны.

Резерв: демпфер «резерв видит основу, а ни один шлюз на связи — нет» держится
`rf_block_minutes` подряд (первый «вижу» — счёт заново; переход 3 ↔ Н3 счёт
не сбрасывает) → `ask{3 | nogw}` основе; `ask_no` — повтор по причине; нет
ответа `ask_seconds`: случай 3 — `need` шлюзу «не вижу» (переезд по долям,
вид rf), Н3 — повтор `ask`. Для Н3 — только если резерву есть чем дать
РФ-доступ (`rf_via_mine` и свой шлюз).

Основа: `view` — Т-РУ13 / Т-РУ21 / Т-РУ23 один раз на расхождение, строка
карточки; `ask` — согласие кодом (не админом): случай 3 — линки слотов
подняты, иначе `ask_no{link_down}` (Т-РУ22); Н3 — своих шлюзов не вижу, иначе
`ask_no{gw_seen}` (Т-РУ22, вариант Н3); занята — `ask_no{busy}`; согласна —
путь кнопки с видом rf | nogw.
"""
from __future__ import annotations

import time

from awgbot.core import config, settings
from awgbot.util import livestep, timeutil

from ..types import Notification
from ..gateway_link.common import log

REFUSE_RETRY_S = 15 * 60          # после link_down / gw_seen — спросить снова не раньше


# свои метки блокировки (код в состоянии → слова на экране); прежние тексты — из
# состояния, записанного выпуском до 4.0.2, снимаются той же веткой
_BLOCK_WORDS = {"zone_busy": "зона занята", "mine_away": "мои пользователи не на этом сервере"}
_OWN_BLOCKS = frozenset({*_BLOCK_WORDS, "зона занята", "мои клиенты не на этом сервере"})

class ReserveRfBlockMixin:
    # ── резерв: демпфер и просьбы ───────────────────────────────────────────
    def reserve_rf_tick(self) -> list[tuple]:
        view = self.rsv_get("rsv_view")
        case = str(view.get("case") or "")
        rf = dict(view.get("rf") or {})
        now = time.monotonic()
        if case not in ("3", "H3"):
            if rf:
                self.rsv_update("rsv_view", rf=None)
            return []
        if case == "H3" and not self._nogw_worth():
            if rf.get("since"):
                self.rsv_update("rsv_view", rf={"blocked": "nogw_off"})
            return []
        if not self.reserve_zone_free() or self.reserve_out() != "home":
            # переезд уже идёт (согласие или доли) либо мои клиенты не здесь — ни ask, ни need
            # метка — код, а не текст: она лежит в состоянии и сравнивается; текст — только при показе
            why = "zone_busy" if not self.reserve_zone_free() else "mine_away"
            if rf.get("blocked") != why:
                rf["blocked"] = why
                if not rf.get("since"):
                    rf.update(since=str(view.get("case_since") or timeutil.now_iso()), mono=now)
                self.rsv_update("rsv_view", rf=rf)
            return []
        if rf.get("blocked") in _OWN_BLOCKS:
            rf["blocked"] = ""                                # зона свободна — метка прочь, демпфер идёт своим счётом
            self.rsv_update("rsv_view", rf=rf)
        if not rf.get("since"):
            rf = {"since": str(view.get("case_since") or timeutil.now_iso()), "mono": now}
            self.rsv_update("rsv_view", rf=rf)
            log.warning("резерв: случай %s — ни один шлюз основы на связи не видит её; жду %d мин",
                        case, settings.get_int("app.reserve.rf_block_minutes", 15))
            return []
        held = livestep.mono_since(rf.get("mono"), now)
        if held == float("inf"):
            rf["mono"] = now                                  # отметка прежней загрузки — счёт заново
            self.rsv_update("rsv_view", rf=rf)
            return []
        if held < settings.get_int("app.reserve.rf_block_minutes", 15) * 60:
            return []
        ask_s = settings.get_int("app.reserve.ask_seconds", 60)
        asked = float(rf.get("ask_mono") or 0)
        if asked and not livestep.mono_due(asked, now, ask_s):
            return []
        if rf.get("ask_no"):
            wait = ask_s if rf["ask_no"] == "busy" else REFUSE_RETRY_S
            if not livestep.mono_due(rf.get("ask_no_mono"), now, wait):
                return []
        if asked and not rf.get("ask_no") and self.reserve_online():
            # основа не ответила: случай 3 — по долям, Н3 — просить снова
            if case == "3":
                log.warning("резерв: основа не ответила на просьбу за %d с — переезд по долям (rf)", ask_s)
                rf.update(ask_mono=now)
                self.rsv_update("rsv_view", rf=rf)
                return self._need_send(case, self._voters(), kind="rf")
        if not self.reserve_online():
            return []
        why = self.reserve_auto_block()
        if rf.get("blocked") != (why or ""):
            rf["blocked"] = why or ""
            self.rsv_update("rsv_view", rf=rf)
        if why:
            return []
        body = {"case": "3" if case == "3" else "nogw", "since": str(rf.get("since") or ""),
                "voters": [{"slot": s, "see": v == "see"} for s, v in self._voters().items()][:16]}   # имена основа знает сама
        rf.update(ask_mono=now, ask_at=timeutil.now_iso(), ask_no="", blocked="")
        self.rsv_update("rsv_view", rf=rf)
        log.warning("резерв: прошу основу о переезде (случай %s)", case)
        return [("send", "ask", body)]

    def _nogw_worth(self) -> bool:
        """Н3 — только если РФ-доступ клиентов основы есть чем дать: мои шлюзы
        разрешены и хотя бы один есть."""
        return settings.get_bool("app.reserve.rf_via_mine", True) and bool(self.db.gateways())

    def reserve_ask_no_in(self, reason: str) -> list[tuple]:
        view = self.rsv_get("rsv_view")
        rf = dict(view.get("rf") or {})
        rf.update(ask_no=str(reason or "")[:16], ask_no_mono=time.monotonic(), ask_no_at=timeutil.now_iso())
        self.rsv_update("rsv_view", rf=rf)
        log.info("резерв: основа отказала в переезде: %s", reason)
        return []

    def reserve_rf_line(self) -> str:
        """Строка карточки резерва о случаях 3 и Н3 (пусто — нечего сказать)."""
        view = self.rsv_get("rsv_view")
        case, rf = str(view.get("case") or ""), dict(view.get("rf") or {})
        name = self.reserve_peer_name()
        if case not in ("3", "H3") or not rf.get("since"):
            return ""
        at = self._clock(rf["since"])
        if rf.get("ask_no"):
            why = {"link_down": "у него не поднят линк шлюзов", "gw_seen": "он видит свои шлюзы",
                   "busy": "он занят"}.get(rf["ask_no"], rf["ask_no"])
            return f"⚠️ Просил «{name}» перевести пользователей — отказ: {why}"
        if rf.get("blocked"):
            why = _BLOCK_WORDS.get(str(rf["blocked"]), str(rf["blocked"]))
            return f"⚠️ Шлюзы «{name}» не видят его с {at} — переезд не идёт: {why}"
        if rf.get("ask_at"):
            return f"⏳ Просил «{name}» перевести пользователей — жду ответа"
        until = self._clock_plus(rf["since"], settings.get_int("app.reserve.rf_block_minutes", 15))
        if case == "3":
            return f"🟡 Шлюзы «{name}» не видят его с {at}, а этот сервер видит — если так останется до {until}, пользователи переедут сюда"
        return f"🟡 Шлюзы «{name}» не на связи с этим сервером с {at} — если так останется до {until}, пользователи переедут сюда ради РФ-доступа"

    @staticmethod
    def _clock(iso: str) -> str:
        try:
            return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
        except ValueError:
            return ""

    @staticmethod
    def _clock_plus(iso: str, minutes: int) -> str:
        from datetime import timedelta
        try:
            return timeutil.fmt_clock_ui(timeutil.parse_iso(iso) + timedelta(minutes=int(minutes)))
        except ValueError:
            return ""

    # ── основа: view и ask ──────────────────────────────────────────────────
    def reserve_view_in(self, case: str, since: str, voters: list) -> list[Notification]:
        """`view` резерва: случай, с какого времени, кто что видит — Т-РУ13 /
        Т-РУ21 / Т-РУ23 один раз на расхождение, строка карточки."""
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        names = {int(g["slot"]): str(g["name"]) for g in self._reserve_gateways_view()}   # резерв шлёт только слоты
        def _nm(v):
            try:
                return names.get(int(v.get("slot") or 0)) or f"Шлюз {v.get('slot')}"
            except (TypeError, ValueError):
                return f"Шлюз {v.get('slot')}"
        seen = [_nm(v) for v in voters or [] if isinstance(v, dict) and v.get("see")]
        unseen = [_nm(v) for v in voters or [] if isinstance(v, dict) and not v.get("see")]
        key = f"{case}|{since}"
        cur = self.rsv_get("rsv_view")
        self.rsv_update("rsv_view", peer_case=case, peer_since=since, peer_seen=seen, peer_unseen=unseen, peer_at=timeutil.now_iso())
        if cur.get("peer_told") == key:
            return []
        self.rsv_update("rsv_view", peer_told=key)
        until = self._plus_iso(since, settings.get_int("app.reserve.rf_block_minutes", 15))
        if case == "3":
            text = rn.rf_block_warning(name, unseen, since, until)
        elif case == "H3":
            text = rn.nogw_warning(name, since, until)
        elif case == "4":
            text = rn.case4_notice(name, unseen, seen, since)
        else:
            return []
        return [Notification(tg_id=int(config.ADMIN_ID), text=text)]

    @staticmethod
    def _plus_iso(iso: str, minutes: int) -> str:
        from datetime import timedelta
        try:
            return timeutil.to_iso(timeutil.parse_iso(iso) + timedelta(minutes=int(minutes)))
        except ValueError:
            return iso

    def reserve_ask_in(self, case: str, since: str, voters: list) -> list[tuple]:
        """`ask{3 | nogw}` резерва: согласие — её код. Отказ — `ask_no{причина}`
        и Т-РУ22; согласие — путь кнопки с видом rf | nogw."""
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        kind = "nogw" if case == "nogw" else "rf"
        refuse = ""
        if kind == "rf":
            down = self._slot_links_down()
            if down:
                refuse = "link_down"
                note = rn.ask_refused(name, "3", since, link_if=down[0])
        else:
            if self._sees_own_gateway():
                refuse = "gw_seen"
                note = rn.ask_refused(name, "nogw", since, reserve_ip=self.reserve_peer_ip())
        if not refuse:
            why = self.reserve_move_check()
            if why:
                refuse = "busy"
                log.info("резерв: просьба «%s» о переезде (%s) — занят: %s", name, case, why)
        if refuse:
            actions: list[tuple] = [("send", "ask_no", {"reason": refuse})]
            if refuse != "busy":
                told = self.rsv_get("rsv_view").get("ask_told")
                key = f"{refuse}|{since}"
                if told != key:
                    self.rsv_update("rsv_view", ask_told=key)
                    actions.append(("notify", [Notification(tg_id=int(config.ADMIN_ID), text=note)]))
                log.warning("резерв: просьба «%s» о переезде (%s) отклонена: %s", name, case, refuse)
            return actions
        log.warning("резерв: по просьбе «%s» перевожу клиентов (%s, с %s)", name, kind, since)
        self.reserve_move_start(kind=kind, dead_since=since)
        return [("live",)]

    def _slot_links_down(self) -> list[str]:
        """Имена линков слотов, которые на сервере не подняты (awg show не читается)."""
        import subprocess
        out = []
        for gw in self.db.gateways():
            name = str(getattr(gw, "link_if", "") or "")
            if not name:
                continue
            try:
                proc = subprocess.run(["awg", "show", name, "listen-port"], capture_output=True, timeout=5)
                if proc.returncode != 0:
                    out.append(name)
            except (OSError, subprocess.SubprocessError):
                out.append(name)
        return out

    def _sees_own_gateway(self) -> bool:
        """Хотя бы один свой шлюз на связи (сессия канала и свежий хендшейк)."""
        from awgbot.infra import routing
        from ..gwchannel.session import GWLINK_FRESH_HANDSHAKE_S
        for gw in self.db.gateways():
            if not (self.gwlink_session(gw.id) if hasattr(self, "gwlink_session") else None):
                continue
            try:
                age = routing.link_handshake_age(gw.link_if)
            except Exception:                                 # noqa: BLE001
                continue
            if age is not None and age <= GWLINK_FRESH_HANDSHAKE_S:
                return True
        return False

    def reserve_peer_case_line(self) -> str:
        """Строка карточки основы по последнему `view` резерва."""
        view = self.rsv_get("rsv_view")
        case = str(view.get("peer_case") or "")
        if not case or not view.get("peer_since"):
            return ""
        try:
            if timeutil.elapsed(timeutil.parse_iso(str(view.get("peer_at") or ""))) > 3600:
                return ""
        except ValueError:
            return ""
        name = self.reserve_peer_name()
        at = self._clock(str(view["peer_since"]))
        unseen = ", ".join(f"«{n}»" for n in view.get("peer_unseen") or [])
        if case == "3":
            return f"🚫 Шлюзы {unseen} не видят этот сервер с {at}, а «{name}» видит — похоже, сервер недоступен из РФ"
        if case == "H3":
            return f"⚠️ Шлюзы не на связи с «{name}» с {at}"
        if case == "4":
            return f"⚠️ {unseen} не видит этот сервер с {at}, а «{name}» видит — похоже, дело в сети шлюза"
        return ""
