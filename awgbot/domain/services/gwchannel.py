"""
gwchannel.py — сторона ВПС: приём того, что привёз канал линка (концепт
«канал линка», §3.2).

Здесь хранение, сверка и решение, что доставить шлюзу. Снимок рисуется на
экранах и сравнивается с тем, что ВПС сам же выдал в конфигурации; расхождение
определяет, какие настройки уйдут каналом (этап 2). Фиды локальной сети для
шлюзов ВПС качает здесь же (этап 3). В автомат переключения слотов снимок не
входит: данные приехали с чужой машины, и доверять им выбор пути было бы
странно.

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


def _cut(v: str) -> str:
    return v if len(v) <= 160 else v[:157] + "…"


def drift_lines(items: list, html: bool) -> list[str]:
    """Строки расхождения «что выдаёт сервер — что стоит на шлюзе» из структуры
    (ключ, человеческое имя, у сервера, на шлюзе). Режим без VPN — словами, а
    не 1/0; подсети и адреса — в <code>, когда строка идёт в HTML. Живёт в
    домене: уведомления собираются здесь же, а слой текстов только берёт."""
    import html as _html
    esc = (lambda s: _html.escape(str(s), quote=False)) if html else (lambda s: str(s))
    out = []
    for key, human, mine, theirs in items:
        if key == "LAN_MODE":
            fmt = lambda v: {"1": "включён", "0": "выключен"}.get(v, esc(v) if v else "—")   # noqa: E731
        elif html:
            fmt = lambda v: f"<code>{esc(v)}</code>" if v else "«—»"            # noqa: E731
        else:
            fmt = lambda v: f"«{v}»" if v else "«—»"                              # noqa: E731
        out.append(f"{esc(human)}: у сервера {fmt(mine)}, на шлюзе {fmt(theirs)}")
    return out


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
                              self._gwlink_sent_at(patch.get("ts")))
        return True

    @staticmethod
    def _gwlink_sent_at(ts) -> str:
        """Время отправки по часам малины, ISO. В сообщение канала оно приходит
        unix-числом из конверта — и это правильный источник для сверки часов:
        время отправки, а не время тика, когда снимок был снят (тот мог быть
        три минуты назад, и сверка врала бы про часы при идеальных часах)."""
        raw = str(ts or "").strip()
        if raw.isdigit():
            from datetime import datetime, timezone
            try:
                dt = datetime.fromtimestamp(int(raw), tz=timezone.utc).astimezone(timeutil.TZ)
            except (OverflowError, OSError, ValueError):
                return ""
            return timeutil.to_iso(dt)
        return raw[:40]

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
                    self._GWLINK_ERROR_KEY, self._GWLINK_ACK_KEY, self._GWLINK_LISTS_KEY,
                    self._GWLINK_SVC_KEY, self._GWLINK_SVC_AT_KEY, self._GWLINK_PEER_SVC_KEY,
                    self._GWLINK_APPLIED_KEY, self._GWLINK_BUNDLE_MSG_KEY,
                    self._GWLINK_INSTALL_WAIT_KEY):
            self.db.set_state(self._gwlink_key(key, slot_id), "")

    # ── новый шлюз: файл первого применения выдан, ждём его на связи ─────────
    _GWLINK_INSTALL_WAIT_KEY = "gwlink_install_wait"

    def gw_install_wait_set(self, slot_id: int) -> None:
        """Файл первого применения выдан: первый полный снимок слота — это
        «установщик отработал, агент стоит», админу об этом скажут."""
        self.db.set_state(self._gwlink_key(self._GWLINK_INSTALL_WAIT_KEY, slot_id),
                          timeutil.to_iso(timeutil.now()))

    _GWLINK_INSTALL_WAIT_TTL_S = 30 * 24 * 3600

    def gw_install_wait_take(self, slot_id: int) -> bool:
        """Снять ожидание, если было: True — уведомить админа (один раз).
        Просроченное (месяц: канал включили много позже установки) — снять молча."""
        key = self._gwlink_key(self._GWLINK_INSTALL_WAIT_KEY, slot_id)
        raw = self.db.get_state(key)
        if not raw:
            return False
        self.db.set_state(key, "")
        try:
            age = (timeutil.now() - timeutil.parse_iso(raw)).total_seconds()
        except ValueError:
            return True
        return age <= self._GWLINK_INSTALL_WAIT_TTL_S

    # ── итог применения конфигурации, пришедший каналом ──────────────────────
    _GWLINK_APPLIED_KEY = "gwlink_applied"       # ok|fail время отпечаток время-у-агента ошибка
    _GWLINK_BUNDLE_MSG_KEY = "gwlink_bundle_msg" # где в чате лежит файл конфигурации слота

    def gwlink_applied_in(self, slot_id: int, body: dict) -> dict:
        """Шлюз применил файл конфигурации из своего чата (или не смог) и сразу
        сказал об этом каналом: {ok, error, fp, at}. Тот же итог второй раз
        (агент не дождался подтверждения) — dup: показывать нечего, подтвердить надо."""
        ok = bool(body.get("ok"))
        err = " ".join(str(body.get("error") or "").split())[:300]
        fp = str(body.get("fp") or "")[:32]
        at = str(body.get("at") or "")[:40]
        key = self._gwlink_key(self._GWLINK_APPLIED_KEY, slot_id)
        prev = (self.db.get_state(key) or "").split(" ", 4)
        dup = bool(fp) and len(prev) >= 4 and prev[2] == fp and prev[3] == at
        if not dup:
            self.db.set_state(key, f"{'ok' if ok else 'fail'} {timeutil.to_iso(timeutil.now())} "
                                   f"{fp or '-'} {at or '-'} {err}".rstrip())
            log.info("канал линка: слот %s %s конфигурацию%s", slot_id,
                     "применил" if ok else "не применил", f": {err}" if err else "")
        return {"ok": ok, "error": err, "fp": fp, "at": at, "dup": dup}

    def gw_bundle_msg_set(self, slot_id: int, chat_id: int, file_id: int, instr_id: int | None,
                          fp: str = "", plain: bool = False) -> None:
        """Запомнить, где лежит выданный файл и его отпечаток: итог с шлюза
        уберёт файл, «В меню» тоже. Итог о другом файле этот не трогает.
        plain — файл первого применения: его убирает выход шлюза на связь."""
        self.db.set_state(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id),
                          json.dumps({"chat": int(chat_id), "file": int(file_id),
                                      "instr": int(instr_id) if instr_id else None,
                                      "fp": str(fp or "")[:32], "plain": bool(plain)}))

    @staticmethod
    def bundle_fingerprint(blob: bytes) -> str:
        """Отпечаток файла конфигурации, общий для обеих ролей: по нему агент и
        сервер понимают, что говорят об одном файле."""
        import hashlib
        return hashlib.sha256(blob).hexdigest()[:16]

    def gw_bundle_msg_get(self, slot_id: int) -> dict:
        try:
            data = json.loads(self.db.get_state(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id)) or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def gw_bundle_msg_clear(self, slot_id: int) -> None:
        self.db.set_state(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id), "")

    # ── сервисы соседних сетей (концепт «сервисы соседних сетей») ────────────
    _GWLINK_SVC_KEY = "gwlink_svc"           # список SMB-серверов сети слота (после чистки)
    _GWLINK_SVC_AT_KEY = "gwlink_svc_at"
    _GWLINK_PEER_SVC_KEY = "gwlink_peer_svc" # ответ слота на раздачу записей соседей
    _SVC_NEIGHBOUR_STALE_S = 24 * 3600       # сосед молчит дольше — его сервисы не раздаются

    def gwlink_services_in(self, slot_id: int, items) -> bool:
        """Список SMB-серверов сети слота от его агента: чистка по подсетям
        слота из БД (не из снимка), запись только при изменении. True — изменился."""
        from awgbot.domain import gwservices
        gw = self.db.gateway(slot_id)
        if gw is None:
            return False
        cleaned = gwservices.clean(items, gw.home_subnets, gwservices.MAX_OWN)
        raw = json.dumps(cleaned, ensure_ascii=False)
        key = self._gwlink_key(self._GWLINK_SVC_KEY, slot_id)
        self.db.set_state(self._gwlink_key(self._GWLINK_SVC_AT_KEY, slot_id),
                          timeutil.to_iso(timeutil.now()))
        if (self.db.get_state(key) or "[]") == raw:
            return False
        self.db.set_state(key, raw)
        log.info("канал линка: слот %s назвал SMB-серверы своей сети: %s", slot_id, len(cleaned))
        return True

    def gwlink_services(self, slot_id: int) -> list[dict]:
        try:
            data = json.loads(self.db.get_state(self._gwlink_key(self._GWLINK_SVC_KEY, slot_id)) or "[]")
        except json.JSONDecodeError:
            return []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    def _gwlink_seen_recent(self, slot_id: int, within_s: int) -> bool:
        if self.gwlink_session(slot_id):
            return True
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, slot_id)) or ""
        try:
            return bool(raw) and (timeutil.now() - timeutil.parse_iso(raw)).total_seconds() < within_s
        except ValueError:
            return False

    def _gwlink_peer_nets_applied(self, gw) -> tuple[list[str], list[str]]:
        """(что сервер выдаёт слоту по функции B, что из этого применено на
        шлюзе по снимку): до перевыпуска маршрута к соседу нет."""
        issued = self.gateway_peer_nets(gw.id)
        snap = self.gwlink_snapshot(gw.id)
        applied = str((snap.get("bundle") or {}).get("peer_home_nets") or "").split()
        return issued, [n for n in issued if n in applied]

    def gwlink_peer_services_for(self, gw) -> tuple[str, list[dict]]:
        """Что раздать слоту: сервисы других слотов с адресами в пересечении
        «выдано по B» ∩ «применено на шлюзе». Сосед молчит дольше суток — его
        сервисы не раздаются. (отпечаток, записи); пусто — («», [])."""
        from awgbot.domain import gwservices
        _issued, allowed = self._gwlink_peer_nets_applied(gw)
        items = self._gwlink_neighbour_services(gw, allowed)
        return gwservices.feed_hash(items), items

    def _gwlink_neighbour_services(self, gw, nets: list[str]) -> list[dict]:
        """Сервисы других слотов на связи (не молчащих дольше суток) с адресами
        в nets, после чистки."""
        from awgbot.domain import gwservices
        if not nets:
            return []
        out: list[dict] = []
        for other in self.db.gateways():
            if other.id == gw.id or not self._gwlink_seen_recent(other.id, self._SVC_NEIGHBOUR_STALE_S):
                continue
            out += self.gwlink_services(other.id)
        return gwservices.clean(out, nets, gwservices.MAX_PEER)

    def gwlink_peer_services_ack_in(self, slot_id: int, body: dict) -> None:
        ok = bool(body.get("ok"))
        digest = "".join(c for c in str(body.get("hash") or "") if c in "0123456789abcdef")[:64]
        err = " ".join(str(body.get("error") or "").split())[:200]
        try:
            n = int(body.get("n") or 0)
        except (TypeError, ValueError):
            n = 0
        self.db.set_state(self._gwlink_key(self._GWLINK_PEER_SVC_KEY, slot_id),
                          f"{'ok' if ok else 'fail'} {digest or '-'} {n} "
                          f"{timeutil.to_iso(timeutil.now())} {err}".strip())

    def gwlink_peer_services_ack(self, slot_id: int) -> dict:
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_PEER_SVC_KEY, slot_id)) or ""
        parts = raw.split(" ", 4)
        if len(parts) < 4:
            return {}
        return {"ok": parts[0] == "ok", "hash": "" if parts[1] == "-" else parts[1],
                "n": int(parts[2]) if parts[2].isdigit() else 0, "at": parts[3],
                "error": parts[4] if len(parts) > 4 else ""}

    def gwlink_services_card(self, gw) -> dict:
        """Строка карточки слота: сколько SMB у слота, сколько ему раздано и что
        с ними на шлюзе. Имена на экраны сервера не идут — только числа."""
        from awgbot.domain import gwservices
        own = self.gwlink_services(gw.id)
        digest, items = self.gwlink_peer_services_for(gw)
        ack = self.gwlink_peer_services_ack(gw.id)
        snap = self.gwlink_snapshot(gw.id)
        issued, applied = self._gwlink_peer_nets_applied(gw)
        # в счёт — сервисы по ВЫДАННЫМ подсетям: до перевыпуска они уже есть,
        # просто ждут, и строка обязана это сказать, а не «не найдены»
        peer = self._gwlink_neighbour_services(gw, issued) if issued else []
        if ack and ack.get("hash") == digest and (items or ack.get("hash")):
            state = "applied" if ack.get("ok") else "failed"
        elif issued and not applied:
            state = "reissue"
        elif snap and not gwservices.version_at_least(snap.get("agent_version", ""), (3, 1, 0)):
            state = "old_agent"
        elif ack and ack.get("hash") == digest:
            state = "applied" if ack.get("ok") else "failed"
        else:
            state = "pending"
        return {"enabled": bool(issued), "own": len(own), "peer": len(peer), "state": state,
                "error": (ack.get("error") or "") if ack else ""}

    # ── сверка выданного с установленным ─────────────────────────────────────

    def gwlink_config_drift(self, gw, with_keys: bool = False):
        """Чем установленное на шлюзе расходится с тем, что ВПС выдал.

        Сравниваем своим же набором значений, из которых собирается бандл, —
        второй схемы сериализации для этого не нужно. Снимка нет — расхождений
        не выдумываем: пустой список значит «сказать нечего», а не «всё сошлось».
        """
        items = self.gwlink_config_drift_items(gw)
        keys = [k for k, _h, _m, _t in items]
        out = drift_lines(items, html=False)
        return (out, keys) if with_keys else out

    def gwlink_config_drift_items(self, gw) -> list[tuple[str, str, str, str]]:
        """То же расхождение структурой: (ключ, человеческое имя, у сервера, на
        шлюзе) — тексты рисуют его по-своему (подсети в <code>, режим словами).
        До 64 значений с каждой стороны — значение обрезается, иначе экран с
        несколькими расхождениями перерос бы лимит сообщения."""
        from awgbot.util import gwlink
        snap = self.gwlink_snapshot(gw.id)
        got = snap.get("bundle") if isinstance(snap.get("bundle"), dict) else None
        if not got:
            return []
        want = self.gwlink_issued_env(gw)
        items = []
        for key in gwlink.BUNDLE_KEYS:
            mine = want[key]
            theirs = " ".join(str(got.get(gwlink.snap_field(key), "")).split())
            if mine != theirs:
                items.append((key, gwlink.KEY_HUMAN[key], _cut(mine), _cut(theirs)))
        return items

    # ── настройки по каналу (этап 2) ─────────────────────────────────────────
    _GWLINK_ACK_KEY = "gwlink_ack"

    def gwlink_issued_env(self, gw) -> dict:
        """Что ВПС выдал бы слоту сейчас — по всем ключам бандла, нормализовано.
        Те самые значения, из которых собирается бандл: второй схемы нет, и
        расходиться им не с чего."""
        from awgbot.util import gwlink
        env = dict(self._lan_env(gw))
        env["ADMIN_IPS"] = " ".join(self._gw_ssh_allow())
        return {k: " ".join(str(env.get(k, "")).split()) for k in gwlink.BUNDLE_KEYS}

    def gwlink_settings_want(self, gw) -> dict:
        """Желаемые настройки слота — только ключи, которые везёт канал."""
        from awgbot.util import gwlink
        env = self.gwlink_issued_env(gw)
        return {k: env[k] for k in gwlink.SETTINGS_KEYS}

    def gwlink_settings_due(self, gw) -> dict | None:
        """Что отправить шлюзу сейчас, или None — отправлять нечего.

        Решает РАСХОЖДЕНИЕ, а не событие на ВПС: снимок показывает, что реально
        стоит в юните обвязки, и пока оно не совпало с выдаваемым, есть что
        доставлять. Кнопку, поменявшую настройку, это ловит так же, как бандл,
        применённый руками из старого файла. Снимка нет — молчим: судить не по
        чему, а слать вслепую значит рестартить обвязку без нужды."""
        from awgbot.util import gwlink
        _lines, keys = self.gwlink_config_drift(gw, with_keys=True)
        if not any(k in gwlink.SETTINGS_KEYS for k in keys):
            return None
        return self.gwlink_settings_want(gw)

    def gwlink_ack_in(self, slot_id: int, body: dict) -> None:
        """Итог применения на шлюзе: `ok|fail время хэш|ошибка`."""
        ok = bool(body.get("ok"))
        err = " ".join(str(body.get("error") or "").split())[:200]
        self.db.set_state(self._gwlink_key(self._GWLINK_ACK_KEY, slot_id),
                          f"{'ok' if ok else 'fail'} {timeutil.to_iso(timeutil.now())} {err}".strip())
        if not ok:
            log.warning("канал линка: слот %s не применил настройки: %s", slot_id, err)

    def gwlink_ack(self, slot_id: int) -> dict:
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_ACK_KEY, slot_id)) or ""
        parts = raw.split(" ", 2)
        if len(parts) < 2:
            return {}
        return {"ok": parts[0] == "ok", "at": parts[1], "error": parts[2] if len(parts) > 2 else ""}

    # ── списки локальной сети по каналу (этап 3) ─────────────────────────────
    # Те же источники, что у скрипта списков на шлюзе (awg-lan-lists.sh): ВПС и
    # так ходит к этому репозиторию за своими списками, а адрес квартиры с этим
    # перестаёт ходить на GitHub и в Google каждые шесть часов.
    _LAN_ITDOG = "https://raw.githubusercontent.com/itdoginfo/allow-domains/main"
    _LAN_DOMAINS_URL = _LAN_ITDOG + "/Russia/inside-dnsmasq-ipset.lst"
    _LAN_SERVICES = ("telegram", "meta", "twitter", "cloudflare", "discord")
    _LAN_GOOG_URL = "https://www.gstatic.com/ipranges/goog.json"
    _LAN_FEEDS_AT_KEY = "gwlink_lan_feeds_at"
    _GWLINK_LISTS_KEY = "gwlink_lists"

    def _lan_feeds_path(self):
        from awgbot.core import config
        return config.DATA_DIR / "lan-feeds.json"

    def gwlink_lan_feeds(self) -> dict:
        """{'hash', 'domains', 'nets', 'fetched_at'} или пусто — фидов ещё нет."""
        try:
            data = json.loads(self._lan_feeds_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) and data.get("hash") else {}

    def gwlink_lan_feeds_digest(self) -> str:
        """Отпечаток текущих фидов без разбора всего файла на каждом такте:
        кеш по времени изменения, файл меняется раз в шесть часов."""
        path = self._lan_feeds_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        cached = self.__dict__.get("_lan_feeds_digest")
        if cached and cached[0] == mtime:
            return cached[1]
        digest = self.gwlink_lan_feeds().get("hash", "")
        self.__dict__["_lan_feeds_digest"] = (mtime, digest)
        return digest

    def gwlink_lan_feeds_update(self, force: bool = False) -> bool:
        """Скачать фиды локальной сети для шлюзов. True — фиды сменились.

        Только если хотя бы у одного слота включено «за шлюзом — без VPN»: без
        него фиды никому не нужны, и ходить за ними — лишняя сеть. Период — тот
        же, что у своих списков ВПС. Негодное (короткий фид доменов, пустые
        подсети) не заменяет прежнее: застывший список лучше пустого, а
        проверки формата на шлюзе повторятся всё равно.
        """
        import re
        import time as _time
        from awgbot.core import settings
        from awgbot.infra import routing
        if not any(g.lan_mode for g in self.db.gateways()):
            return False
        import random
        now = int(_time.time())
        every = int(settings.get("app.routing.lists_refresh_hours", 6)) * 3600
        # В state — момент СЛЕДУЮЩЕГО похода, с джиттером ±40 %: «прошло шесть
        # часов» внутри тика давало метроном с разбросом в минуты, а это семь
        # одних и тех же адресов по часам.
        nxt = self.db.get_state(self._LAN_FEEDS_AT_KEY) or ""
        if not force and nxt.isdigit() and now < int(nxt) and self.gwlink_lan_feeds():
            return False
        self.db.set_state(self._LAN_FEEDS_AT_KEY, str(now + int(every * random.uniform(0.6, 1.4))))
        body, err, _code = routing.fetch(self._LAN_DOMAINS_URL, timeout=60)
        if body is None or len(re.findall(r"(?m)^ipset=/", body)) < 10:
            log.warning("канал линка: фид доменов локальной сети не получен (%s)", err or "короткий")
            return False
        nets: list[str] = []
        for svc in self._LAN_SERVICES:
            text, err, _code = routing.fetch(f"{self._LAN_ITDOG}/Subnets/IPv4/{svc}.lst", timeout=60)
            if text is None:
                log.info("канал линка: подсети %s не получены (%s)", svc, err)
                continue
            nets += re.findall(r"(?m)^\d+\.\d+\.\d+\.\d+/\d+$", text)
        goog, err, _code = routing.fetch(self._LAN_GOOG_URL, timeout=60)
        if goog is not None:
            nets += re.findall(r'"ipv4Prefix"\s*:\s*"(\d+\.\d+\.\d+\.\d+/\d+)"', goog)
        if not nets:
            log.warning("канал линка: подсети локальной сети не получены ни из одного источника")
            return False
        nets_text = "\n".join(sorted(set(nets))) + "\n"
        from awgbot.util import gwlink
        digest = gwlink.feeds_hash(body, nets_text)
        if digest == self.gwlink_lan_feeds().get("hash"):
            return False
        path = self._lan_feeds_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hash": digest, "domains": body, "nets": nets_text,
                                   "fetched_at": timeutil.to_iso(timeutil.now())},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        log.info("канал линка: фиды локальной сети обновлены")
        return True

    def gwlink_lists_ack_in(self, slot_id: int, body: dict) -> None:
        ok = bool(body.get("ok"))
        digest = "".join(c for c in str(body.get("hash") or "") if c in "0123456789abcdef")[:64]
        err = " ".join(str(body.get("error") or "").split())[:200]
        self.db.set_state(self._gwlink_key(self._GWLINK_LISTS_KEY, slot_id),
                          f"{'ok' if ok else 'fail'} {digest or '-'} "
                          f"{timeutil.to_iso(timeutil.now())} {err}".strip())

    def gwlink_lists(self, slot_id: int) -> dict:
        """Что шлюз ответил на последнюю доставку фидов: ok, hash, at, error."""
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_LISTS_KEY, slot_id)) or ""
        parts = raw.split(" ", 3)
        if len(parts) < 3:
            return {}
        return {"ok": parts[0] == "ok", "hash": "" if parts[1] == "-" else parts[1],
                "at": parts[2], "error": parts[3] if len(parts) > 3 else ""}

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
        from awgbot.util import gwlink
        sess = self.gwlink_session(gw.id)
        fresh = handshake_age is not None and handshake_age <= 180
        snap = self.gwlink_snapshot(gw.id)
        seen = self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, gw.id)) or ""
        try:
            mine_gen = awglock.generation()
        except Exception:                                 # noqa: BLE001
            mine_gen = None
        drift, drift_keys = self.gwlink_config_drift(gw, with_keys=True) if snap else ([], [])
        return {
            "online": bool(sess) and fresh,
            "ever": bool(seen or snap),
            "seen": seen,
            "age": self.gwlink_snapshot_age(gw.id),
            "agent": snap.get("agent_version", ""),
            "awg_gen": snap.get("awg_generation"),
            "awg_gen_mine": mine_gen,
            "drift": drift,
            "drift_items": self.gwlink_config_drift_items(gw) if snap else [],
            # что из расхождения довезёт канал, а что — только файл (подсети
            # соседей живут и в конфиге линка): экран не должен обещать
            # доставку, которой не будет
            "drift_bundle": any(k in gwlink.BUNDLE_ONLY_KEYS for k in drift_keys),
            "drift_channel": any(k in gwlink.SETTINGS_KEYS for k in drift_keys),
            "has_snap": bool(snap),
            "egress_gw": snap.get("egress_ok") if snap else None,
            "link_contract": snap.get("link_contract", "") if snap else "",
            "plumbing_gen": snap.get("plumbing_gen", "") if snap else "",
            # Снимок есть, а блока установленной конфигурации нет (старый или
            # урезанный агент): пустая сверка тогда значит «сказать нечего», а
            # не «совпадает» — экран обязан это различать.
            "has_bundle": isinstance(snap.get("bundle"), dict) if snap else False,
            "clock_skew": self._gwlink_clock_skew(gw.id),
            "ack": self.gwlink_ack(gw.id),
            "lists": self.gwlink_lists(gw.id) if gw.lan_mode else {},
            "error": (self.db.get_state(self._gwlink_key(self._GWLINK_ERROR_KEY, gw.id)) or ""
                      ).partition(" ")[2],
            "peer_nets": snap.get("peer_nets") if snap else None,
        }

    def _gwlink_clock_skew(self, slot_id: int) -> int | None:
        """Насколько часы малины расходятся с часами ВПС, секунд: `ts` снимка по
        часам малины против времени приёма по нашим. Доставка внутри линка —
        доли секунды, так что разница и есть расхождение часов. None — нечем
        мерить. Канал от часов не зависит (повтор отсекают нонсы сессии), но
        часы нужны TLS, расписаниям и срокам — строка на экране говорит о них
        раньше, чем сломается что-то ещё."""
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
            res = self.gateway_claim(token)
        except (ValueError, ServiceError) as e:
            log.info("канал линка: claim слота %s не принят: %s", slot_id, e)
            return
        # Токен подписан ключом линка — значит, своего слота; но проверим явно:
        # сообщение из сессии слота 1 не должно метить устройство слота 2.
        got = res.get("gateway") if isinstance(res, dict) else None
        if got is not None and getattr(got, "id", slot_id) != slot_id:
            log.warning("канал линка: claim из сессии слота %s пометил слот %s", slot_id, got.id)
