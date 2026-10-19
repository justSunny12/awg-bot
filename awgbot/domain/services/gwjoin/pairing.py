"""pairing.py — часть пакета gwjoin (см. __init__): сессия сопряжения и код.

hello{join, eph, host} → join_eph{eph, slot_name, until}: у обеих сторон
один код (util/joinsas), он живёт, пока жива сессия. Сессий по токену — не
больше 10, живая — одна (новая вытесняет прежнюю, код меняется). Ввод кода —
только к заявке нажатой кнопки (Р20): формат не тот — переспрос, попытка не
сгорает; не совпал — попытка; третья — сопряжение закрыто сразу (Р24). Срок —
час от выпуска; вышел — интерфейс под токен падает сразу.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from awgbot.util import gwlink, joinsas, timeutil

from ..gateway_link.common import log
from .state import JOIN_TTL, MAX_ATTEMPTS, MAX_SESSIONS, _LOCK

STALE_LINKED_S = 24 * 3600     # линк на постоянных ключах, а конца сопряжения нет — запись снимается


def _older(iso, seconds: int) -> bool:
    try:
        return timeutil.elapsed(timeutil.parse_iso(str(iso or ""))) > seconds
    except ValueError:
        return False

_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,63}\Z")


def clean_host(value) -> str:
    """hostname из hello сопряжения (Р25): данные с чужой машины — не длиннее
    63 знаков, только A-Za-z0-9.-, иначе отброшено молча."""
    s = str(value or "")
    return s if _HOST_RE.match(s) else ""


class GwJoinPairingMixin:
    """Сессия сопряжения, код, попытки, срок, отмена."""

    # ── для слушателя ────────────────────────────────────────────────────────

    def gwjoin_addresses(self) -> list[tuple[int, str]]:
        """[(слот, адрес ВПС в /30)] живых сопряжений — слушателю канала."""
        out = []
        for st in self.gwjoin_all():
            if st.get("state") in ("waiting", "connected", "accepted") and not self._gwjoin_expired(st):
                vps = gwlink.link_hosts(st["cidr"])[0]
                if vps:
                    out.append((int(st["slot"]), vps))
        return out

    def gwjoin_for_addr(self, local_ip: str, peer_ip: str) -> dict:
        """Сопряжение, чей линк принял коннект (адрес ВПС и адрес шлюза в /30)."""
        for st in self.gwjoin_all():
            if st.get("state") not in ("waiting", "connected", "accepted"):
                continue
            vps, gw = gwlink.link_hosts(st["cidr"])
            if vps == local_ip and gw == peer_ip:
                return st
        return {}

    def gwjoin_hello(self, slot_id: int, msg: dict) -> dict:
        """hello сессии сопряжения. {"ok", "kind": тело ответа ("join_eph" |
        "join_ok" повторно после верного кода) , "body", "why": отказ (gone |
        expired | sessions | bad), "sid", "event": для сообщения подключения
        (connected | reconnected | two), "st"}."""
        from awgbot.infra import routing
        with _LOCK:
            st = self.gwjoin(slot_id)
            if not st or str(msg.get("join") or "") != st.get("id") \
                    or st.get("state") not in ("waiting", "connected", "accepted"):
                return {"ok": False, "why": "gone"}
            if self._gwjoin_expired(st):
                # закрывает слушатель: сначала отказ шлюзу, затем возврат (Р24)
                st.update(state="closing", why="expired")
                self._gwjoin_save(slot_id, st)
                return {"ok": False, "why": "expired", "st": st}
            if int(st.get("sessions") or 0) >= MAX_SESSIONS:
                return {"ok": False, "why": "sessions"}
            try:
                g_eph = base64.urlsafe_b64decode(str(msg.get("eph") or "") + "==")
            except (ValueError, TypeError):
                g_eph = b""
            if len(g_eph) != 32:
                return {"ok": False, "why": "bad"}
            if st.get("state") == "accepted" and not hmac.compare_digest(
                    str(st.get("eph") or ""), hashlib.sha256(g_eph).hexdigest()):
                # после верного кода — только тот шлюз, чей код ввели: его
                # эфемерный ключ (агент держит пару до join_ok); чужой hello
                # с тем же токеном не получит ни секретов, ни постоянных ключей
                return {"ok": False, "why": "gone"}
            st["sessions"] = int(st.get("sessions") or 0) + 1
            try:
                addr = routing.link_peer_endpoint(st["link_if"]) or ""
            except Exception:                             # noqa: BLE001
                addr = ""
            host = clean_host(msg.get("host"))
            was = list(st.get("endpoints") or [])
            if addr and addr not in was:
                st["endpoints"] = (was + [addr])[-4:]
            if addr:
                hosts = dict(st.get("hosts") or {})
                if host:
                    hosts[addr] = host
                st["hosts"] = hosts
            s_priv, s_pub = joinsas.new_pair()
            k = joinsas.shared(s_priv, g_eph)
            sid = secrets.token_hex(4)
            live = {"sid": sid, "kp": joinsas.pair_key(st["id"], k), "addr": addr,
                    "eph": hashlib.sha256(g_eph).hexdigest(),
                    "code": joinsas.code(st["id"], slot_id, g_eph, s_pub, k)}
            self._gwjoin_live_set(slot_id, live)
            event = "reconnected" if st.get("state") in ("connected", "accepted") or int(st["sessions"]) > 1 \
                else "connected"
            if len(st.get("endpoints") or []) > 1:
                event = "two"
            accepted = st.get("state") == "accepted"
            if not accepted:
                st["state"] = "connected"
            self._gwjoin_save(slot_id, st)
            body = {"eph": base64.urlsafe_b64encode(s_pub).decode().rstrip("="), "slot_name": st.get("name") or "",
                    "until": st.get("expires") or "", "slot": int(slot_id)}
            if accepted:
                body["accepted"] = True               # код уже верный — шлюз ждёт join_ok
        log.info("сопряжение: слот %s — сессия %s (%s)", slot_id, st["sessions"], event)
        return {"ok": True, "kind": "join_eph", "body": body, "sid": sid, "event": event, "st": st,
                "accepted": accepted}

    def gwjoin_session_closed(self, slot_id: int, sid: str) -> None:
        """Сессия сопряжения закрылась: её код больше ничего не значит."""
        with _LOCK:
            live = self._gwjoin_live(slot_id)
            if live.get("sid") == sid:
                self._gwjoin_live_set(slot_id, None)

    # ── ввод кода ────────────────────────────────────────────────────────────

    def gwjoin_code(self, slot_id: int, join_id: str, text: str) -> dict:
        """Код из чата к заявке нажатой кнопки (Р20). {"result": format |
        wait | gone | expired | wrong | refused | ok, "left": попыток, "st"}."""
        code = joinsas.normalize(text)
        if code is None:
            return {"result": "format"}
        with _LOCK:
            st = self.gwjoin(slot_id)
            if not st or st.get("id") != join_id or st.get("state") not in ("waiting", "connected"):
                return {"result": "gone", "st": st}
            if self._gwjoin_expired(st):
                # закрывает вызывающий, как такт срока: отказ шлюзу, затем возврат
                st.update(state="closing", why="expired")
                self._gwjoin_save(slot_id, st)
                return {"result": "expired", "st": st}
            live = self._gwjoin_live(slot_id)
            if not live.get("code"):
                return {"result": "wait", "st": st}
            if joinsas.same(code, live["code"]):
                self.gwjoin_accept(slot_id)
                return {"result": "ok", "st": self.gwjoin(slot_id), "sid": live.get("sid")}
            st["attempts"] = int(st.get("attempts") or 0) + 1
            left = MAX_ATTEMPTS - st["attempts"]
            if left <= 0:
                # закрыть — вызывающему: сначала join_refused шлюзу, затем
                # gwjoin_revert (интерфейс под токен падает — отказ уже не дошёл бы)
                st.update(state="closing", why="code")
                self._gwjoin_save(slot_id, st)
                log.info("сопряжение: слот %s — код не подтверждён трижды, закрываю", slot_id)
                return {"result": "refused", "left": 0, "st": st, "sid": live.get("sid")}
            self._gwjoin_save(slot_id, st)
            return {"result": "wrong", "left": left, "st": st, "sid": live.get("sid")}

    def gwjoin_wait(self, slot_id: int, join_id: str) -> str:
        """Нажатие «✏️ Ввести код»: ask — можно вводить; wait — шлюз ещё не
        подключился; gone — заявки нет."""
        st = self.gwjoin(slot_id)
        if not st or st.get("id") != join_id or st.get("state") not in ("waiting", "connected"):
            return "gone"
        if self._gwjoin_expired(st):
            return "gone"
        return "ask" if self._gwjoin_live(slot_id).get("code") else "wait"

    def gwjoin_set_message(self, slot_id: int, chat_id: int, message_id: int) -> None:
        with _LOCK:
            st = self.gwjoin(slot_id)
            if st:
                st["msg"] = [int(chat_id), int(message_id)]
                self._gwjoin_save(slot_id, st)

    # ── отмена и срок ───────────────────────────────────────────────────────

    def gwjoin_cancel(self, slot_id: int, join_id: str = "") -> dict:
        """«⬅️ Отменить» / «↩️ Отменить замену»: можно ли (до верного кода).
        Возвращает запись, помеченную «closing», или пусто; закрывает —
        вызывающий (join_refused шлюзу, затем gwjoin_revert — сразу, Р24)."""
        with _LOCK:
            st = self.gwjoin(slot_id)
            if not st or (join_id and st.get("id") != join_id) \
                    or st.get("state") not in ("waiting", "connected", "closing"):
                return {}
            st.update(state="closing", why="cancel")
            self._gwjoin_save(slot_id, st)
            return st

    def gwjoin_revert_told(self, slot_id: int, join_id: str) -> bool:
        """Возврат по такту не удался: сказать человеку — один раз на запись
        (такт повторяет каждую минуту, сообщение — нет). True — сказать сейчас."""
        with _LOCK:
            st = self.gwjoin(slot_id)
            if not st or st.get("id") != join_id or st.get("err_told"):
                return False
            st["err_told"] = 1
            self._gwjoin_save(slot_id, st)
            return True

    def gwjoin_tick(self) -> list[dict]:
        """Такт: сопряжения, у которых вышел срок, — помечены «closing» и
        возвращены (закрывает вызывающий: join_refused, затем gwjoin_revert —
        сразу; Т-П11 и правка сообщения)."""
        closed = []
        for st in self.gwjoin_all():
            if st.get("state") in ("waiting", "connected") and self._gwjoin_expired(st):
                with _LOCK:
                    cur = self.gwjoin(int(st["slot"]))
                    if cur.get("id") != st.get("id") or cur.get("state") != st.get("state"):
                        continue                          # код ввели / отменили, пока такт шёл (join-2)
                    cur.update(state="closing", why="expired")
                    self._gwjoin_save(int(st["slot"]), cur)
                closed.append({**st, "event": "expired"})
            elif st.get("state") == "closing":
                # прошлый такт пометил или возврат не удался — повтор; итог —
                # по причине закрытия (срок, отмена, код)
                closed.append({**st, "event": st.get("why") or "expired"})
            elif st.get("state") == "accepted" and _older(st.get("accepted_at") or st.get("expires"), JOIN_TTL):
                # код верный, а смена ключей так и не встала за час — откат, как
                # при отмене (quar-3): штамп и .join-prev не остаются навсегда
                with _LOCK:
                    cur = self.gwjoin(int(st["slot"]))
                    if cur.get("id") != st.get("id") or cur.get("state") != st.get("state"):
                        continue                          # код ввели / отменили, пока такт шёл (join-2)
                    cur.update(state="closing", why="stuck")
                    self._gwjoin_save(int(st["slot"]), cur)
                closed.append({**st, "event": "stuck"})
            elif st.get("state") == "linked" and _older(st.get("linked_at") or st.get("expires"), STALE_LINKED_S):
                # линк на постоянных ключах, а агент так и не доложил конец (quar-7):
                # запись снимается, человеку — одно уведомление
                with _LOCK:
                    cur = self.gwjoin(int(st["slot"]))
                    if cur.get("id") != st.get("id") or cur.get("state") != "linked":
                        continue                          # итог агента пришёл, пока такт шёл (join-2)
                    self._gwjoin_save(int(st["slot"]), {})
                closed.append({**st, "event": "stale"})
        return closed
