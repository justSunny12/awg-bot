"""
gwchannel.py — сторона ВПС: приём того, что привёз канал линка (концепт
«канал линка», §3.2).

Здесь только хранение и сверка. Решений по снимку не принимается ни одного:
он рисуется на экранах и сравнивается с тем, что ВПС сам же выдал в
конфигурации, — и всё. В автомат переключения слотов снимок не входит: данные
приехали с чужой машины, и доверять им выбор пути было бы странно.

Два времени, а не одно, — намеренно. Возраст снимка считается от времени
ПРИЁМА по часам ВПС; `ts` малины хранится справочно. Машина без RTC, поднявшаяся
раньше сети, нарисовала бы «состояние два часа назад» у совершенно свежего
снимка или, при ошибке в другую сторону, «минус три минуты».
"""
from __future__ import annotations

import json
import logging

from awgbot.util import timeutil
from awgbot.domain import gwsnapshot
from awgbot.domain.services.types import ServiceError

log = logging.getLogger(__name__)


class GwChannelMixin:
    _GWLINK_SESSION_KEY = "gwlink_session"
    _GWLINK_SEEN_KEY = "gwlink_seen"
    _GWLINK_SNAP_KEY = "gwlink_snap"
    _GWLINK_SNAP_REV_KEY = "gwlink_snap_rev"
    _GWLINK_SNAP_AT_KEY = "gwlink_snap_at"
    _GWLINK_SNAP_TS_KEY = "gwlink_snap_ts"

    def _gwlink_key(self, key: str, slot_id: int) -> str:
        return f"{key}_{int(slot_id)}"

    # ── сессия ───────────────────────────────────────────────────────────────

    def gwlink_session_opened(self, slot_id: int, agent: str, proto: int) -> None:
        now = timeutil.to_iso(timeutil.now())
        self.db.set_state(self._gwlink_key(self._GWLINK_SESSION_KEY, slot_id),
                          f"since={now} agent={agent} proto={int(proto)}")
        self.db.set_state(self._gwlink_key(self._GWLINK_SEEN_KEY, slot_id), now)
        self.db.set_state(self._gwlink_key(self._GWLINK_ERROR_KEY, slot_id), "")
        log.info("канал линка: слот %s на связи (агент %s, proto %s)", slot_id, agent, proto)

    def gwlink_session_closed(self, slot_id: int) -> None:
        self.db.set_state(self._gwlink_key(self._GWLINK_SESSION_KEY, slot_id), "")
        self.db.set_state(self._gwlink_key(self._GWLINK_SEEN_KEY, slot_id),
                          timeutil.to_iso(timeutil.now()))
        # Нумерация снимка живёт в сессии: следующая начнётся с полного.
        self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_REV_KEY, slot_id), "")

    _GWLINK_ERROR_KEY = "gwlink_error"

    def gwlink_sessions_reset(self) -> None:
        """Старт процесса: ни одной живой сессии быть не может — сокеты умерли
        вместе с прежним процессом. Без сброса строка сессии пережила бы
        рестарт, и карточка зажигала бы «на связи» у шлюза, который ещё не
        переподключился, — а при выключенном канале или лежащем линке врала бы
        бесконечно. Время «последний раз» не трогаем: оно правдиво."""
        for gw in self.db.gateways():
            self.db.set_state(self._gwlink_key(self._GWLINK_SESSION_KEY, gw.id), "")
            self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_REV_KEY, gw.id), "")

    def gwlink_note_error(self, slot_id: int, reason: str) -> None:
        """Почему сервер оборвал сессию. Без этого «нет связи» оставалось бы без
        причины — а самая коварная из них, разошедшиеся часы, иначе не видна
        вовсе: каждое сообщение отвергается окном времени, и канал просто молчит."""
        self.db.set_state(self._gwlink_key(self._GWLINK_ERROR_KEY, slot_id),
                          f"{timeutil.to_iso(timeutil.now())} {reason[:200]}")

    def gwlink_session(self, slot_id: int) -> dict:
        """{'since', 'agent', 'proto'} или пусто — не на связи."""
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_SESSION_KEY, slot_id)) or ""
        out = {}
        for part in raw.split():
            name, _, val = part.partition("=")
            if name in ("since", "agent", "proto"):
                out[name] = val
        return out

    # ── снимок ───────────────────────────────────────────────────────────────

    def gwlink_snapshot_in(self, slot_id: int, patch: dict, rev: int, full: bool) -> bool:
        """Принять `snap` или `delta`. False — разрыв нумерации: вызывающий
        попросит полный снимок и начнёт счёт заново.

        Дельта накладывается на хранимый снимок, а не заменяет его: по каналу
        едет только разница, и хранить её отдельно значило бы держать два
        источника одной картинки.
        """
        rev_key = self._gwlink_key(self._GWLINK_SNAP_REV_KEY, slot_id)
        have = int(self.db.get_state(rev_key) or 0)
        if full:
            snap = gwsnapshot.sanitize(patch)
        else:
            if rev != have + 1 or not have:
                log.info("канал линка: слот %s — разрыв нумерации (было %s, пришло %s)",
                         slot_id, have, rev)
                return False
            stored = self.gwlink_snapshot(slot_id)
            if not stored:
                return False
            snap = gwsnapshot.apply_delta(stored, gwsnapshot.sanitize(patch))
        now = timeutil.now()
        with self.db.transaction():
            self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_KEY, slot_id),
                              json.dumps(snap, ensure_ascii=False))
            self.db.set_state(rev_key, str(int(rev)))
            self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_AT_KEY, slot_id),
                              timeutil.to_iso(now))
            self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_TS_KEY, slot_id),
                              str(patch.get("ts") or ""))
        return True

    def gwlink_snapshot(self, slot_id: int) -> dict:
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_SNAP_KEY, slot_id)) or ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def gwlink_snapshot_age(self, slot_id: int) -> int | None:
        """Секунд с приёма ПО ЧАСАМ ВПС; None — снимка не было."""
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_SNAP_AT_KEY, slot_id)) or ""
        if not raw:
            return None
        try:
            return max(0, int((timeutil.now() - timeutil.parse_iso(raw)).total_seconds()))
        except ValueError:
            return None

    def gwlink_forget(self, slot_id: int) -> None:
        """Снятие слота: ключи канала уходят вместе с ключами бандла."""
        for key in (self._GWLINK_SESSION_KEY, self._GWLINK_SEEN_KEY, self._GWLINK_SNAP_KEY,
                    self._GWLINK_SNAP_REV_KEY, self._GWLINK_SNAP_AT_KEY, self._GWLINK_SNAP_TS_KEY,
                    self._GWLINK_ERROR_KEY):
            self.db.set_state(self._gwlink_key(key, slot_id), "")

    # ── сверка выданного с установленным ─────────────────────────────────────

    def gwlink_config_drift(self, gw) -> list[str]:
        """Чем установленное на шлюзе расходится с тем, что ВПС выдал.

        Сравниваем своим же набором значений, из которых собирается бандл, —
        второй схемы сериализации для этого не нужно. Снимка нет — расхождений
        не выдумываем: пустой список значит «сказать нечего», а не «всё сошлось».
        """
        snap = self.gwlink_snapshot(gw.id)
        got = snap.get("bundle") if isinstance(snap.get("bundle"), dict) else None
        if not got:
            return []
        want = dict(self._lan_env(gw))
        want["ADMIN_IPS"] = " ".join(self._gw_ssh_allow())
        pairs = (("lan_mode", "LAN_MODE", "режим «за шлюзом — без VPN»"),
                 ("home_subnets", "HOME_SUBNETS", "локальные подсети"),
                 ("resolver", "RESOLVER", "резолвер"),
                 ("peer_home_nets", "PEER_HOME_NETS", "подсети за другими шлюзами"),
                 ("admin_ips", "ADMIN_IPS", "устройства админа"))
        out = []
        for field, env, human in pairs:
            mine = " ".join(str(want.get(env, "")).split())
            theirs = " ".join(str(got.get(field, "")).split())
            if mine != theirs:
                out.append(f"{human}: у сервера «{mine or '—'}», на шлюзе «{theirs or '—'}»")
        return out

    # ── карточка слота ───────────────────────────────────────────────────────

    def gwlink_card(self, gw, handshake_age) -> dict:
        """Что карточка слота знает о канале: связь, код на той стороне, сверка
        конфигурации, взгляд шлюза на выход наружу. Без единого exec и без
        запросов к шлюзу — только то, что уже лежит в state.

        «На связи» — конъюнкция: сессия открыта И хендшейк линка свежий.
        Heartbeat'а в канале нет намеренно, поэтому полуоткрытая сессия (у
        малины выдернули питание, RST не дошёл) перестаёт врать не позже, чем
        протухнет хендшейк.
        """
        from awgbot.infra import awglock
        sess = self.gwlink_session(gw.id)
        fresh = handshake_age is not None and handshake_age <= 180
        snap = self.gwlink_snapshot(gw.id)
        seen = self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, gw.id)) or ""
        try:
            mine_gen = awglock.generation()
        except Exception:                                 # noqa: BLE001
            mine_gen = None
        return {
            "online": bool(sess) and fresh,
            "ever": bool(seen or snap),
            "since": sess.get("since", ""),
            "seen": seen,
            "age": self.gwlink_snapshot_age(gw.id),
            "agent": snap.get("agent_version", ""),
            "awg_gen": snap.get("awg_generation"),
            "awg_gen_mine": mine_gen,
            "drift": self.gwlink_config_drift(gw) if snap else [],
            "has_snap": bool(snap),
            "egress_gw": snap.get("egress_ok") if snap else None,
            "link_contract": snap.get("link_contract", "") if snap else "",
            "plumbing_gen": snap.get("plumbing_gen", "") if snap else "",
            # Снимок есть, а блока установленной конфигурации нет (старый или
            # урезанный агент): пустая сверка тогда значит «сказать нечего», а
            # не «совпадает» — экран обязан это различать.
            "has_bundle": isinstance(snap.get("bundle"), dict) if snap else False,
            "clock_skew": self._gwlink_clock_skew(gw.id),
            "error": (self.db.get_state(self._gwlink_key(self._GWLINK_ERROR_KEY, gw.id)) or ""
                      ).partition(" ")[2],
            "peer_nets": snap.get("peer_nets") if snap else None,
        }

    def _gwlink_clock_skew(self, slot_id: int) -> int | None:
        """Насколько часы малины расходятся с часами ВПС, секунд: `ts` снимка по
        часам малины против времени приёма по нашим. Доставка внутри линка —
        доли секунды, так что разница и есть расхождение часов. None — нечем
        мерить. Канал отвергает сообщения вне окна ±5 минут; строка на экране
        нужна раньше, чем он замолчит без видимой причины."""
        ts = self.db.get_state(self._gwlink_key(self._GWLINK_SNAP_TS_KEY, slot_id)) or ""
        at = self.db.get_state(self._gwlink_key(self._GWLINK_SNAP_AT_KEY, slot_id)) or ""
        if not ts or not at:
            return None
        try:
            return int((timeutil.parse_iso(ts) - timeutil.parse_iso(at)).total_seconds())
        except ValueError:
            return None

    # ── claim по каналу ──────────────────────────────────────────────────────

    def gwlink_claim_in(self, slot_id: int, token: str) -> None:
        """Агент прислал токен пометки своим каналом. Разбор — общий с ручной
        пересылкой, включая список нонсов: канал лишь избавляет человека от
        копирования сообщения между чатами."""
        try:
            self.gateway_claim(token)
        except (ValueError, ServiceError) as e:
            log.info("канал линка: claim слота %s не принят: %s", slot_id, e)
