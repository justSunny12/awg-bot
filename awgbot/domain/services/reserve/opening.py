"""
opening.py — часть пакета reserve (см. __init__): предусловия и запуск процесса
открытия зоны на резерве без ключа основы — по точкам ящика кворума или по
аварийному коду (кнопкой — outbound/inbound, с `go` основы). Общие предусловия
любого открытия — `reserve_open_block`; автоматический переезд сверх них ждёт
`min_interval_minutes` после состоявшегося автоматического переезда и
FAIL_DAMPER_S после срыва открытия.
"""
from __future__ import annotations

import time

from awgbot.core import config, settings
from awgbot.util import rsvkeys, timeutil

from ..types import Notification
from ..gateway_link.common import log
from .inbound_keep import KEEP_RESET
from .state import _LOCK

NEED_RETRY_S = 30                 # точка не пришла — qneed следующему «не вижу»
CODE_TRIES = 3                    # попыток кода в час
FAIL_DAMPER_S = 120               # после срыва открытия — пауза перед новой автоматической попыткой
CODE_WINDOW_S = 3600


class ReserveOpeningMixin:
    def reserve_open_block(self, *, need_box: bool = True) -> str:
        """Общие предусловия открытия зоны (кнопка, точки, код): зона свободна,
        мои пользователи на этом сервере, готовность включена, направление «к себе» готово,
        пакет на месте (и ящик кода его поколения `M` — для открытия без K основы).
        Пусто — можно; иначе причина."""
        if not self.reserve_zone_free():
            return "зона занята"
        if self.reserve_out() != "home":
            return "мои пользователи не на этом сервере"
        if not settings.get_bool("app.reserve.ready", False):
            return "готовность выключена"
        ok, why = self.reserve_directions()["in"]
        if not ok:
            return why
        pkg_in = self.rsv_get("rsv_pkg_in")
        if not pkg_in.get("gen"):
            return "данных для переезда нет"
        if need_box:
            peer = self.reserve_quorum_peer_keys() if hasattr(self, "reserve_quorum_peer_keys") else {}
            if not peer.get("cbox") or not rsvkeys.same_m(peer, pkg_in):
                return "данных для переезда нет"              # ящика кода текущего поколения ключа нет
        if need_box and pkg_in.get("has_dns") is False:
            # запись меняет процесс открытия на резерве — без конверта DNS (провайдер «вручную») ему нечем;
            # карточка ставит строку под «автоматический аварийный переезд невозможен: …», уведомление — хвост
            return f"DNS-запись у «{self.reserve_peer_name()}» настраивается вручную"
        return ""

    def reserve_auto_block(self) -> str:
        """Пусто — автоматический переезд (по кворуму) возможен; иначе причина (строка карточки)."""
        why = self.reserve_open_block()
        if why:
            return why
        inn = self.rsv_get("rsv_in")
        free = str(inn.get("state") or "idle") in ("idle", "relay")   # relay (хвост прошлого возврата) — зона свободна, паузы те же
        fail_at = str(inn.get("fail_at") or "")
        if fail_at and free:
            try:
                if timeutil.elapsed(timeutil.parse_iso(fail_at)) < FAIL_DAMPER_S:
                    return "после отказа открытия — пауза две минуты"
            except ValueError:
                pass
        last = str(inn.get("since") or "")
        if last and free and inn.get("ended_auto"):
            try:
                mins = settings.get_int("app.reserve.min_interval_minutes", 30)
                if timeutil.elapsed(timeutil.parse_iso(last)) < mins * 60:
                    return f"между автоматическими переездами не меньше {mins} мин"
            except ValueError:
                pass
        return ""

    def reserve_open_by_points(self, secret: dict, kind: str, case: str) -> list[tuple]:
        """Процесс открытия по точкам или по коду: как `go` основы, но без K;
        вид — dead | rf | code; итог админу — после захвата Telegram гостем. Точки ключа
        кворума уже у меня — ключ основы мне известен, запущу я процесс или нет: отметка
        основе (`st.exposed`), её поколение сменится."""
        if secret.get("qpoints") and hasattr(self, "reserve_exposed_mark"):
            self.reserve_exposed_mark("share")
        why = self.reserve_auto_block() if kind != "code" else self._code_block()
        if why:
            log.warning("резерв: процесс открытия (%s) не запущен: %s", kind, why)
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID),
                                             text=_fail_text(self.reserve_peer_name(), f"{why} — аварийный переезд невозможен"))])] \
                + self._gw_refusal_word(why)
        pkg_in = self.rsv_get("rsv_pkg_in")
        self.reserve_zone_shell_ensure()
        payload = self._open_payload(kind)
        payload.update(secret)
        with _LOCK:
            if not self.reserve_zone_free():                  # проверка и запись — под одним замком
                return []
            self._relay_yield()
            from awgbot.infra import rsvzone
            rsvzone.open_status_write("launching")
            self.reserve_set_in("taking", kind=kind, gen=int(pkg_in.get("gen") or 0), why=kind, case=case, extra="",   # why — гостю причина
                                by="code" if "code" in secret else "share",
                                open_started=timeutil.now_iso(), deadline=time.monotonic() + self._open_wait_s(),
                                up_sent=False, tg_taken=False, tg_take_forced=False, tg_wait="", **KEEP_RESET,
                                ended_auto="code" not in secret,   # по коду — не автоматический
                                dead_since=str(self.rsv_get("rsv_view").get("code", {}).get("since") or ""))
            self._in_step("open")
        try:
            self._rsvopen_launch(payload)
        except Exception as e:                                # noqa: BLE001
            # срыв открытия: отметка автоматического переезда прочь, пауза — короткий демпфер (fail_at)
            self.reserve_set_in("idle", step="", reason=str(e), ended_auto=False, fail_at=timeutil.now_iso())
            self._relay_restore()                             # пересылка хвоста, уступившая открытию, — обратно
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), text=_fail_text(self.reserve_peer_name(), str(e)))])] \
                + self._gw_refusal_word(str(e))
        # вход — только в памяти бота, не в базе: запись сменит процесс, а клон потом пропадёт —
        # перезапуск тем же входом (inbound_keep); ключ основы мне и так известен (st.exposed)
        self.__dict__["_rsvopen_payload"] = payload
        log.warning("резерв: процесс открытия по %s запущен (%s, пакет %s)", "коду" if kind == "code" else "точкам",
                    case, pkg_in.get("gen"))
        return [("fw",)] + self.reserve_gw_state_actions()

    def _gw_refusal_word(self, why: str) -> list[tuple]:
        """Отказ запуска после отданных точек — шлюзам `state{idle, reason}`: у отдавшего точку
        это Т-РА5 (переезд не состоялся); `qrel` следом снимает отметку молча, и без этого
        слова срыв терялся."""
        if not hasattr(self, "reserve_gw_state_actions"):
            return []
        return self.reserve_gw_state_actions(force=True, reason=why)

    def _open_wait_s(self) -> int:
        from .inbound import OPEN_WAIT_S
        return OPEN_WAIT_S


def _fail_text(primary: str, why: str) -> str:
    from awgbot.domain.notices import reserve as rn
    return rn.hosting_failed(primary, why)
