"""
vote.py — часть пакета reserve агента (см. __init__): гостевой канал со
стороны шлюза — словарь `pairproto.GUEST_KINDS`: `hello{slot, caps, m_gen,
seen}` в начале сессии, `seen` при смене взгляда, `share` по `need` (только
если шлюз всё ещё не видит основу и доля того поколения), `pong` на `ping`;
входящие `view`, `need`, `state`, `ping`.

Шлюз не отдаёт долю, пока его линк к основе жив (и аплинк, если он смотрит
на основу), и без `need`: доля — только резерву, который сам не видит
основу и спросил.
"""
from __future__ import annotations

from awgbot.util import timeutil

from ..base import log

GW_CAPS: tuple[str, ...] = ("rsv1",)


class ReserveVoteMixin:
    def reserve_guest_hello_body(self) -> dict | None:
        """Тело hello гостевого канала; None — взгляда на основу ещё нет (после
        перезапуска агента свежая живость говорила бы «вижу» без единой пробы, а
        резерв взял бы это за голос)."""
        if not self.reserve_view_ready():
            return None
        cur = self.reserve_section()
        share = cur.get("share") or {}
        return {"slot": int(cur.get("slot") or 0), "caps": list(GW_CAPS), "m_gen": int(share.get("gen") or 0),
                "seen": self.reserve_seen_body().get("seen", "")}

    def reserve_guest_channel_key(self) -> bytes:
        """Ключ подписи гостевого канала — из PSK линка к резерву."""
        import re
        from awgbot.util import pairproto
        conf = str(self.reserve_section().get("conf") or "")
        m = re.search(r"(?m)^\s*PresharedKey\s*=\s*(\S+)", conf)
        return pairproto.guest_key(m.group(1)) if m else b""

    def reserve_guest_target(self) -> tuple[str, int]:
        """(адрес стороны зоны, порт гостевого канала)."""
        from awgbot.core import settings
        cur = self.reserve_section()
        port = int(cur.get("chan") or 0) or int(settings.get_int("app.gateway.guest_channel_port", 8787) or 8787)
        return str(cur.get("peer_addr") or ""), port

    def reserve_guest_in(self, kind: str, body: dict) -> list[tuple]:
        """Сообщение резерва по гостевому каналу → действия (как в watch)."""
        self.reserve_guest_word()
        if kind == "ping":
            return [("guest", "pong", {"n": str(body.get("n") or "")})]
        if kind == "pong":
            self.reserve_guest_pong_in(str(body.get("n") or ""))
            return []
        if kind == "state":
            actions = self.reserve_state_in(str(body.get("state") or ""), int(body.get("move") or 0),
                                            str(body.get("reason") or ""))
            return actions + [("push",)]                      # Т-РА3 даёт само открытие (reserve_forward_decide)
        if kind == "view":
            cur = self.reserve_section()
            cur["reserve_view"] = {"seen": str(body.get("seen") or ""), "in": str(body.get("in") or ""),
                                   "voters": list(body.get("voters") or [])[:16], "at": timeutil.now_iso()}
            from .store import _write
            _write(cur, self._RSV_STORE)
            return self._view_notice(cur)
        if kind == "need":
            return self._reserve_need_in(str(body.get("case") or ""), str(body.get("since") or ""), int(body.get("m_gen") or 0))
        return []

    def _moved_notice(self, actions: list[tuple]) -> list[tuple]:
        """Т-РА3: пересылка открылась по `serving` резерва — клиенты переехали.
        Вариант — по взглядам: сервер не отвечает (🔴, со звуком); «отсюда не
        отвечает, а резерв видит» (случай 3); этот шлюз сервер видит (случай 5,
        без звука); Н3 — этот шлюз не был на связи с резервом (без звука)."""
        if not any(a and a[0] == "open" for a in actions):
            return []
        from awgbot.domain.notices import reserve as rn
        cur = self.reserve_section()
        move = int(cur.get("reserve_move") or 0)
        if cur.get("moved_told") == move and move:
            return []                                         # этот переезд уже объявлен
        cur["moved_told"] = move
        view = cur.get("reserve_view") or {}
        primary, since = self.reserve_primary_seen()
        reserve_sees = not view.get("seen")
        rsv_since = str(cur.get("reserve_down_last") or self.__dict__.get("_rsv_reserve_since") or "")
        if primary == "down" and reserve_sees:
            case, at, sound = "rf", since, True
        elif primary == "down":
            case, at, sound = "dead", since, True
        elif not reserve_sees:
            others = [f"Шлюз {int(v.get('slot') or 0)}" for v in view.get("voters") or [] if isinstance(v, dict) and not v.get("see")]
            case, at, sound = "partial", str(view.get("seen") or ""), False
            text = rn.gateway_moved(str(cur.get("name") or ""), case, at, others={"unseen": others})
            cur["open_at"] = cur.get("open_at") or timeutil.now_iso()
            note = self.reserve_note(cur, text, silent=True, cond={"open": True})
            from .store import _write
            _write(cur, self._RSV_STORE)
            return [("notify", [note])]
        else:
            case, at, sound = "nogw", rsv_since or timeutil.now_iso(), False
        text = rn.gateway_moved(str(cur.get("name") or ""), case, at, record=str(cur.get("record") or ""),
                                ip=str(cur.get("peer_host") or ""))
        cur["open_at"] = cur.get("open_at") or timeutil.now_iso()
        # со звуком — и с почтой как запасным каналом (critical): Telegram агента идёт по аплинку,
        # который в этот момент может быть мёртв
        note = self.reserve_note(cur, text, sound=sound, silent=not sound, cond={"open": True}, critical=sound)
        from .store import _write
        _write(cur, self._RSV_STORE)
        return [("notify", [note])]

    def _view_notice(self, cur: dict) -> list[tuple]:
        """Т-РА4: по `view` резерва — случай 3 до переезда (резерв видит сервер,
        я — нет, все голосующие — нет) или случай 4 (кто-то видит); один раз на
        расхождение (момент «не вижу»)."""
        from awgbot.core import settings
        from awgbot.domain.notices import reserve as rn
        view = cur.get("reserve_view") or {}
        primary, since = self.reserve_primary_seen()
        if primary != "down" or view.get("seen") or not since:
            return []
        if cur.get("view_told") == since:
            return []
        slot = int(cur.get("slot") or 0)
        others_see = [f"Шлюз {int(v.get('slot') or 0)}" for v in view.get("voters") or []
                      if isinstance(v, dict) and v.get("see") and int(v.get("slot") or 0) != slot]
        name = str(cur.get("name") or "")
        if others_see:
            text = rn.gateway_case4(name, since, others_see)
        else:
            from datetime import timedelta
            try:
                until = timeutil.to_iso(timeutil.parse_iso(since) + timedelta(minutes=settings.get_int("app.gateway.rf_block_minutes", 15)))
            except ValueError:
                until = since
            text = rn.gateway_rf_warning(name, since, until)
        cur["view_told"] = since
        note = self.reserve_note(cur, text, silent=True, cond={"view_since": since})
        from .store import _write
        _write(cur, self._RSV_STORE)
        return [("notify", [note])]

    def _reserve_need_in(self, case: str, since: str, m_gen: int = 0) -> list[tuple]:
        """`need{случай}`: доля — только если основу не видим сейчас и поколение
        доли — то, которое просит резерв (прежняя доля — ошибка поколения)."""
        state, _ = self.reserve_primary_seen()
        cur = self.reserve_section()
        share = cur.get("share") or {}
        if state != "down":
            log.info("reserve: резерв просит долю (случай %s), а основа видна — отказ", case)
            return []
        if not share.get("y"):
            log.warning("reserve: резерв просит долю, а доли нет")
            return []
        if m_gen and int(share.get("gen") or 0) != int(m_gen):
            log.warning("reserve: резерв просит долю поколения %s, а у меня %s — отказ", m_gen, share.get("gen"))
            return []
        log.warning("reserve: отдаю долю ключа переезда резерву (случай %s, основа не отвечает с %s)", case, since)
        cur["share_given_at"] = timeutil.now_iso()
        from .store import _write
        _write(cur, self._RSV_STORE)
        return [("guest", "share", {"share": str(share.get("y")), "m_gen": int(share.get("gen") or 0)})]
