"""
keys.py — часть пакета reserve (см. __init__): `K`, `M`, поколения и аварийный
код на стороне основы.

Ключ `rsv_keys`: `m` (hex) — главный ключ текущего поколения, `m_gen`; `k`
(hex) и `k_gen` — ключ пакета; `code_shown_at` — когда код показан;
`compromised` (`compromised_by` = share | code) — ключ текущего поколения
могла узнать вторая сторона без моего K: её слово `st.exposed` (точки ключа
кворума были у неё, код подошёл — и при сорванном открытии). Дома без хода —
новое поколение сразу, иначе — на любом пути домой (общий хвост
`reserve_keys_after_foreign_open`, он же — по своей отметке «`go` я не
отдавал»). `K` меняется после каждого возврата, `M` — когда пакет открывали
без моего K и при снятии или замене держателя точки.

Прямой «2 из n» нет: `M` всегда закрыт ящиком кода (прямая `Mc`: точка
второго сервера + аварийный код админа, quorum/keys) с сопряжения, а пока
кворум линии включён — ещё ящиком кворума (многочлен `Mq`, точки у второго
сервера и шлюзов). Смена поколения `M` обновляет оба ящика; прямая кода и
код меняются только по `code` (после переезда по коду, «🔄 Сменить код»).
"""
from __future__ import annotations

from awgbot.core import config
from awgbot.util import rsvkeys, timeutil
from awgbot.util.rsvpkg import seal

from ..gateway_link.common import log
from .state import _LOCK


class ReserveKeysMixin:

    def reserve_keys(self) -> dict:
        return self.rsv_get("rsv_keys")

    def reserve_keys_init(self) -> dict:
        """Первое поколение при сопряжении: `M`, `K`; ящик кода — сразу (quorum/keys)."""
        with _LOCK:
            data = {"m": rsvkeys.new_master().hex(), "m_gen": 1, "k": seal.new_k().hex(), "k_gen": 1,
                    "compromised": False, "created_at": timeutil.now_iso()}
            self.rsv_put("rsv_keys", data)
        if hasattr(self, "reserve_quorum_keys_init"):
            self.reserve_quorum_keys_init()
        return data

    def reserve_keys_ensure(self) -> None:
        """Ключи без ящика кода — один раз новое поколение `M` и ящик кода, код заново (Т-РУ14
        тактом): после восстановления из копии (`rsv_qkeys` в копию не идут) и как защитная
        ветка для состояния прежних выпусков (пар ниже 4.2.0 нет)."""
        if not self.reserve_paired() or not hasattr(self, "reserve_quorum_keys_on"):
            return
        keys = self.reserve_keys()
        if not keys.get("m") or self.reserve_quorum_keys_on():
            return
        self.reserve_quorum_keys_init()
        self.reserve_new_generation("ключи резерва 4.2.0: аварийный код на своей прямой", code=True)
        self.rsv_update("rsv_keys", code_notify_why="ящик аварийного кода создан заново — прежний код не действует")
        log.warning("резерв: ключи переведены на ящик кода — новое поколение M и новый код")

    def reserve_new_generation(self, reason: str, *, code: bool = True, notify: bool = True) -> dict:
        """Новое поколение `M` — новый `K`, оба ящика заново (quorum/keys); новая прямая
        кода и код — только с `code` (снятие шлюза и возврат после переезда по точкам
        код не меняют); `notify` — слать ли код тактом (у включения — своё слово)."""
        del notify
        with _LOCK:
            keys = self.reserve_keys()
            keys.pop("a", None)                                   # прямой «2 из n» больше нет
            keys.update(m=rsvkeys.new_master().hex(), m_gen=int(keys.get("m_gen") or 0) + 1, k=seal.new_k().hex(),
                        k_gen=int(keys.get("k_gen") or 0) + 1, compromised=False,
                        code_shown_at="", regenerated_at=timeutil.now_iso(), regenerated_why=reason)
            self.rsv_put("rsv_keys", keys)
        if hasattr(self, "reserve_quorum_after_m"):
            self.reserve_quorum_after_m(reason, code=code)   # многочлен заново (если есть), прямая кода — по `code`
        if hasattr(self, "reserve_pkg_mark"):
            self.reserve_pkg_mark()                           # пакет прежнего поколения у резерва — заменить сразу
        return keys

    def _reserve_after_gateway_change(self, why: str) -> None:
        """Снятие или замена шлюза: его точка многочлена могла остаться у чужого —
        новое поколение `M` и `K`; прямая кода со шлюзом не связана, код прежний."""
        if not self.reserve_paired():
            return
        try:
            self.reserve_new_generation(why, code=False)
            self.reserve_pkg_mark() if hasattr(self, "reserve_pkg_mark") else None
        except Exception as e:                            # noqa: BLE001
            log.warning("резерв: поколение ключа после смены шлюза не сменено: %s", e)

    def reserve_code_notify_pending(self) -> str:
        """Причина отложенной отправки нового кода админу; пусто — нечего слать."""
        return str(self.reserve_keys().get("code_notify_why") or "")

    def reserve_code_notify_clear(self) -> None:
        self.rsv_update("rsv_keys", code_notify_why="")

    def reserve_new_k(self) -> dict:
        """Новый ключ пакета после каждого возврата: резерв прежний знал. Журнал — снимком
        со сбросом: записи под прежним ключом новым не открыть."""
        with _LOCK:
            keys = self.reserve_keys()
            keys.update(k=seal.new_k().hex(), k_gen=int(keys.get("k_gen") or 0) + 1)
            self.rsv_put("rsv_keys", keys)
        if hasattr(self, "reserve_jrn_new_k"):
            self.reserve_jrn_new_k()
        return keys

    def reserve_mark_compromised(self, why: str, by: str = "share") -> None:
        """Ключ текущего поколения могла узнать вторая сторона: `by` — share (точки ключа
        кворума) или code (подошедший код — и прямая кода). Код не понижается до share."""
        keys = self.reserve_keys()
        if keys.get("compromised") and (str(keys.get("compromised_by") or "") == "code" or by != "code"):
            return
        self.rsv_update("rsv_keys", compromised=True, compromised_why=why, compromised_by=by)
        log.warning("резерв: ключ поколения %s могла узнать вторая сторона (%s) — %s", keys.get("m_gen"),
                    "по коду" if by == "code" else "по точкам", why)

    def reserve_compromised(self) -> bool:
        return bool(self.reserve_keys().get("compromised"))

    # ── пакет открывали без моего K ─────────────────────────────────────────
    def reserve_keys_after_foreign_open(self, out: dict, *, by_out: bool, why: str) -> list:
        """Общий хвост путей домой (возврат, «вернуть без резерва», зависший вебхук, остаток
        вебхука) и дома после сорванного открытия: пакет открывали без моего K — новое
        поколение `M` и `K` (оба ящика заново), по коду — и новая прямая кода с кодом (Т-РУ14
        тактом); токен DNS был у резерва — просьба сменить. Без моего K — по СВОЕЙ отметке
        (`by_out`: эпизод away и `go` я не отдавал) или по отметке `compromised` (слово резерва
        `st.exposed` о текущем поколении). Один раз на эпизод: новое поколение снимает отметку,
        а `exposed` прежнего поколения больше не совпадает. Возвращает уведомления."""
        from awgbot.domain.notices import reserve as rn
        from ..types import Notification
        keys = self.reserve_keys()
        kind = str(out.get("kind") or "")
        foreign = False
        if by_out:
            own_go = out.get("go_sent")
            if own_go is None:                                # отметки нет (состояние до этой линии) — по виду переезда
                own_go = kind in ("button", "rf")
            foreign = not own_go
        if not foreign and not keys.get("compromised"):
            return []
        # по точкам резерв узнал Mq и M — многочлен и M заново, код прежний; по коду — узнал Mc:
        # и прямая кода с кодом заново
        by_code = str(keys.get("compromised_by") or "") == "code" or kind == "code" or \
            (foreign and str(self.reserve_peer_state().get("in_by") or "") == "code")
        self.reserve_new_generation(why, code=by_code)
        if by_code:
            self.rsv_update("rsv_keys", code_notify_why=why)
        log.warning("резерв: пакет открывали без моего K — новое поколение ключа%s (%s)", " и новый код" if by_code else "", why)
        if self.reserve_dns_automatic():
            return [Notification(tg_id=int(config.ADMIN_ID), text=rn.dns_token_rotate(self.reserve_peer_name()))]
        return []

    def reserve_exposed_in(self, exposed) -> list:
        """Основа: `st.exposed` второго сервера — поколения моего `M`, которые он мог узнать без
        моего K ({share: m_gen} — точки ключа кворума у него, {code: m_gen} — код подошёл).
        Текущее поколение — отметка `compromised`; дома без хода и без моих пользователей у
        него — хвост сразу (срыв открытия после сборки ключа), иначе — на пути домой."""
        if not isinstance(exposed, dict):
            return []
        cur = int(self.reserve_keys().get("m_gen") or 0)
        hit = [by for by in ("code", "share") if cur and _int(exposed.get(by)) == cur]
        if hit:
            self.reserve_mark_compromised("второй сервер открывал пакет без моего K", by=hit[0])
        return self.reserve_compromised_settle()

    def reserve_compromised_settle(self) -> list:
        """Отметка `compromised` дома: пользователи здесь, хода нет, у резерва их нет — новое
        поколение сейчас (и новый `K` журналу — снимком); в ходе и в away — ждёт пути домой."""
        if not self.reserve_compromised():
            return []
        out = self.rsv_get("rsv_out")
        from .state import HOSTING
        if str(out.get("state") or "home") != "home" or out.get("await_merge") or \
                str(self.reserve_peer_state().get("inn") or "idle") in HOSTING:
            return []
        self.reserve_new_k()
        return self.reserve_keys_after_foreign_open(out, by_out=False, why="после попытки аварийного переезда")

    # ── сторона резерва: что я мог узнать без K основы ─────────────────────
    def reserve_exposed_mark(self, by: str) -> None:
        """Резерв: ключ основы мне известен мимо её K — точки ключа кворума у меня (share) или
        код подошёл (code: процесс собрал ключ). Отметка — по поколению `M` пакета, основе —
        полем `st.exposed` (её новое поколение делает отметку прежней)."""
        m_gen = int(self.rsv_get("rsv_pkg_in").get("m_gen") or 0)
        if not m_gen or by not in ("share", "code"):
            return
        ex = dict(self.rsv_get("rsv_in").get("exposed") or {})
        if _int(ex.get(by)) == m_gen:
            return
        ex[by] = m_gen
        self.rsv_update("rsv_in", exposed=ex)
        if hasattr(self, "reserve_st_sent_reset"):
            self.reserve_st_sent_reset()                      # st основе — сразу
        log.warning("резерв: ключ основы поколения %s мне известен без её K (%s) — основа сменит поколение", m_gen,
                    "точки ключа кворума" if by == "share" else "код подошёл")

    def reserve_exposed_st_part(self) -> dict:
        ex = dict(self.rsv_get("rsv_in").get("exposed") or {})
        return {by: _int(ex.get(by)) for by in ("share", "code") if _int(ex.get(by))}

    # ── код ───────────────────────────────────────────────────────────────────

    def reserve_code(self) -> str:
        """Аварийный код — точка x = 255 прямой `Mc` (quorum/keys), строкой для человека."""
        return self.reserve_quorum_code() if hasattr(self, "reserve_quorum_code") else ""

    def reserve_code_shown(self) -> None:
        self.rsv_update("rsv_keys", code_shown_at=timeutil.now_iso())

    def reserve_m(self) -> bytes:
        return bytes.fromhex(self.reserve_keys().get("m") or "")

    def reserve_k(self) -> bytes:
        if config.GUEST:                                      # бот основы на резерве: K — в его хранилище
            from awgbot.infra import rsvzone
            try:
                return bytes.fromhex(rsvzone.K_FILE.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return b""
        return bytes.fromhex(self.reserve_keys().get("k") or "")


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0
