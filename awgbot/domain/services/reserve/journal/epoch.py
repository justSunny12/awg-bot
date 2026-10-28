"""
epoch.py — часть пакета journal (см. __init__): эпоха линии данных.

Держатель эпохи пишет решения; основа, чьи пользователи и бот у резерва, —
следует: база отказывает ей в записи решений (временные триггеры, util/rsvjrn),
задания решений в планировщике выключены. Отдельного голосования нет: роль
выводится из состояния автомата «мои пользователи» (`rsv_out`) и права
отвечать в Telegram (`rsv_poll`) — одним правилом (`_follow_wanted`), которое
сверяется при каждой смене состояния и на каждом такте:

  следование  финальная реплика собрана (шаг released передачи бота); пользователи
              и бот у резерва, а его гость не остановлен; возврат до реплики
              (back_req) и на время слияния (merge — пишет сырым соединением);
              дома, но Telegram отдал право резерву и резерв несёт пользователей
              (или молчит — тогда верим Telegram)
  держатель   всё остальное: дома с поллингом, реплика возврата пришла (гость
              остановлен), отказ слияния, «⚠️ Да, вернуть», срыв переезда, пары нет

Факт Telegram сам по себе роль не меняет: вебхук, оставшийся у резерва без
гостя (после возврата, срыва переезда, отказа слияния), — остаток, а не держатель.
"""
from __future__ import annotations

from ...gateway_link.common import log

_HOSTING = ("taking", "serving", "releasing")


def _earlier(a: dict, b: dict) -> bool:
    """Отметка a поставлена не позже b: по at_ts (доли секунды), без него — по since."""
    if a.get("at_ts") is not None and b.get("at_ts") is not None:
        return float(a["at_ts"]) <= float(b["at_ts"])
    return str(a.get("since") or "") <= str(b.get("since") or "")


class ReserveEpochMixin:

    def reserve_following(self) -> bool:
        """Основа следует за ботом на резерве: решения здесь не пишутся."""
        try:
            return bool(self.db.jrn_following())
        except Exception:                                     # noqa: BLE001 — база старше эпохи (тесты, CLI)
            return False

    def _follow_wanted(self) -> tuple[bool, str]:
        """(следовать ли, почему) — по состоянию пары, переезда и права отвечать."""
        if not self.reserve_paired():
            return False, "пары нет"
        out = self.rsv_get("rsv_out")
        state, step = str(out.get("state") or "home"), str(out.get("step") or "")
        if state == "leaving":
            return step == "released", ("передача бота" if step == "released" else "переезд ещё не передал бот")
        if state == "away":
            if out.get("guest_stopped"):
                return False, "бот на резерве остановлен"
            if out.get("await_merge"):
                return False, "вернуть без резерва"
            return True, "пользователи и бот у резерва"
        if state == "returning":
            if step == "merge" or (step == "back_req" and not out.get("guest_stopped")):
                return True, "возврат: ждём реплику и слияние"
            return False, "возврат: бот на резерве остановлен"
        # home: решает слово Telegram, сказанное уже дома (прежнее «away» — до возврата)
        poll = self.rsv_get("rsv_poll")
        if self.reserve_poll() != "away" or _earlier(poll, out):
            return False, "пользователи и Telegram здесь"
        peer = self.reserve_peer_state()
        fresh = str(peer.get("at") or "") >= str(out.get("since") or "")
        if self.reserve_online() and fresh and str(peer.get("inn") or "idle") not in _HOSTING:
            return False, "вебхук у резерва без гостя"
        return True, "Telegram: вебхук у резерва"

    def reserve_epoch_sync(self) -> None:
        """Роль — по правилу; переход — передача эпохи (эпоха + 1, synchronous = FULL)."""
        want, why = self._follow_wanted()
        if want == self.reserve_following():
            return
        if want:
            meta = self.db.jrn_pass(holder="peer", role="follower", why=why)
            if meta is not None:
                log.warning("резерв: эпоха %s у «%s» — здесь следование (%s)", meta["epoch"],
                            self.reserve_peer_name(), why)
        else:
            meta = self.db.jrn_pass(holder="self", role="holder", why=why)
            if meta is not None:
                log.warning("резерв: эпоха %s здесь (%s)", meta["epoch"], why)
                self.reserve_follow_clear()

    def reserve_epoch_check(self) -> None:
        """Такт: страховка — роль снова сверяется с состоянием."""
        self.reserve_epoch_sync()
