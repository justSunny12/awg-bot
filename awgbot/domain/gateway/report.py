"""report.py — что агент докладывает серверу каналом: первый выход, итог применения, роль слота, пометка."""

from __future__ import annotations

import json
from awgbot.util import timeutil
from awgbot.domain.gateway.base import log


class ReportMixin:
    """Что агент докладывает серверу каналом: первый выход, итог применения, роль слота, пометка."""
    # ── первый выход на связь после установки — серверу каналом ─────────────
    _FIRST_START_KEY = "agent_first_start_at"
    _INSTALLED_SENT_KEY = "gwlink_installed_sent"
    _INSTALLED_REPORT_WINDOW_S = 3600

    def first_start_note(self, fresh_db: bool) -> None:
        """Отметить первый запуск агента — только когда базы до этого запуска не
        было (её создал установщик). Обновлённый со старой версии агент отметки
        не имеет и не получает: иначе первый запуск после обновления сошёл бы за
        установку, и админ получил бы «успешно настроен» по каждому шлюзу.
        Повторные запуски отметку не двигают."""
        if fresh_db and not self.db.get_state(self._FIRST_START_KEY):
            self.db.set_state(self._FIRST_START_KEY, timeutil.to_iso(timeutil.now()))

    def installed_report_pending(self) -> bool:
        """Сказать ли серверу «установлен»: один раз, и только если агент
        впервые запустился меньше часа назад. Канала в этот час не было —
        уведомления не будет вовсе: «шлюз настроен» спустя дни только пугает."""
        if self.db.get_state(self._INSTALLED_SENT_KEY):
            return False
        try:
            age = (timeutil.now() - timeutil.parse_iso(self.db.get_state(self._FIRST_START_KEY) or "")).total_seconds()
        except ValueError:
            return False
        return age <= self._INSTALLED_REPORT_WINDOW_S

    def installed_report_done(self) -> None:
        self.db.set_state(self._INSTALLED_SENT_KEY, timeutil.to_iso(timeutil.now()))


    # ── итог применения файла из чата — серверу каналом ──────────────────────
    _APPLIED_PENDING_KEY = "gwlink_applied_pending"

    _APPLIED_PENDING_TTL_S = 24 * 3600

    def applied_pending_set(self, ok: bool, error: str, fp: str = "") -> None:
        """Итог ждёт отправки: канал после применения бандла обычно
        переподключается, а сказать серверу надо ровно один раз. fp — отпечаток
        применённого файла: сервер по нему узнаёт, о каком файле речь."""
        self.db.set_state(self._APPLIED_PENDING_KEY,
                          json.dumps({"ok": bool(ok), "error": " ".join((error or "").split())[:300],
                                      "fp": str(fp or "")[:32],
                                      "at": timeutil.to_iso(timeutil.now())}, ensure_ascii=False))

    def applied_pending_get(self) -> dict:
        """Итог в очереди; просроченный (сутки без канала) — как пустой: столько
        у файла на сервере никто не ждёт, а сервер без ответа его и не убрал бы."""
        data = self.db.get_state_json(self._APPLIED_PENDING_KEY, {})
        if "ok" not in data:
            return {}
        try:
            age = (timeutil.now() - timeutil.parse_iso(str(data.get("at") or ""))).total_seconds()
        except ValueError:
            return {}                                 # без времени — как просроченный
        return {} if age > self._APPLIED_PENDING_TTL_S else data

    def applied_pending_clear(self) -> None:
        self.db.set_state(self._APPLIED_PENDING_KEY, "")


    # ── роль слота и диагностика по каналу ──
    _LINK_ROLE_KEY = "gwlink_role"

    _LINK_STANDBY_KEY = "gwlink_standby"
    _LINK_SLOT_NAME_KEY = "gwlink_slot_name"

    def set_link_role(self, active: bool, standby=None, name=None) -> None:
        """Роль этого шлюза, как её видит сервер: несёт он трафик или в резерве;
        standby — есть ли другой шлюз (для честных хвостов про синхронизацию и
        РФ-доступ), name — имя слота на сервере (заголовок рецепта роутера).
        Сам агент узнать это не может — решает автомат переключения на ВПС.
        Старый сервер шлёт только active — тогда прочее не трогаем."""
        self.db.set_state(self._LINK_ROLE_KEY, "active" if active else "standby")
        if standby is not None:
            self.db.set_state(self._LINK_STANDBY_KEY, "1" if standby else "0")
        if name is not None:
            self.db.set_state(self._LINK_SLOT_NAME_KEY, str(name)[:80])

    def link_role(self) -> str:
        """active | standby | "" — сервер не сообщал (канала нет)."""
        return (self.db.get_state(self._LINK_ROLE_KEY) or "").strip()

    def link_standby_known(self):
        """True — другой шлюз есть, False — нет, None — сервер не говорил."""
        raw = self.db.get_state(self._LINK_STANDBY_KEY)
        return None if raw is None or raw == "" else raw == "1"

    def link_slot_name(self) -> str:
        return (self.db.get_state(self._LINK_SLOT_NAME_KEY) or "").strip()

    def carries_traffic(self) -> bool:
        """Шлюз сейчас несёт трафик клиентов: сервер назначил его активным и
        линк жив по последнему снимку. Только тогда предупреждаем, что
        РФ-доступ прервётся."""
        if self.link_role() != "active":
            return False
        st = self.cached_status(900)
        return bool(st is not None and self.link_ok(st))

    def _rf_note(self, text: str) -> str:
        """Хвост «РФ-доступ … не работает» к алерту — только если шлюз нёс
        трафик и резерва, на который сервер мог переключиться, нет."""
        if self.link_role() == "active" and self.link_standby_known() is not True:
            return text
        return ""


    # ── шлюзовое устройство: пометка в основном боте ─────────────────────────
    _GW_MARK_KEY = "gw_mark_status"           # unmarked | confirmed | foreign | unconfirmed | ?

    def gateway_mark_outcome(self) -> dict:
        """После применения бандла: он ли помеченный шлюз. Решение принял
        скрипт обвязки (файл статуса), здесь — перевод в действие: unmarked,
        foreign и unconfirmed означают «переслать claim основному боту»."""
        from awgbot.infra import gwguard
        st = gwguard.script_status()
        status = st.get("GW_STATUS", "?")
        iface, pub = gwguard.uplink_pubkey()
        self.db.set_state(self._GW_MARK_KEY, status)
        out = {"status": status, "uplink": iface, "pubkey": pub, "claim": None}
        if status in ("unmarked", "foreign", "unconfirmed") and pub:
            try:
                out["claim"] = self.gateway_claim_message(pub)
            except (OSError, ValueError) as e:
                log.warning("gateway: claim не собран: %s", e)
        return out
