"""expiry.py — сроки подписок, истекающие, блокировка по сроку."""
from __future__ import annotations

import logging

from awgbot.core import config
from awgbot.util import timeutil
from awgbot.core.blocks import DeviceBlock, ClientBlock
from awgbot.core.enums import SubStatus, PeriodKind, FriendStatus
from awgbot.domain.services.types import Notification
from awgbot.domain.services.blocks import _friend_blocked_text

from awgbot.domain.notices.traffic import (_MONTH_CUT_MINUTES, _TXT_EXPIRED_ADMIN, _TXT_EXPIRED_CLIENT,
                                            _TXT_EXPIRING_ADMIN, _TXT_EXPIRING_CLIENT,
                                            _TXT_EXPIRING_CLIENT_GRACE, _left_label)

log = logging.getLogger("awgbot.services")


class ExpiryMixin:
    # ── Проверка сроков + уведомления (поток из ТЗ 7) ────────────────────────

    def _block_client(self, client) -> list["Notification"]:
        """Блокирует все устройства клиента по причине EXPIRY (подписка истекла).
        Ставит бит и клиенту. Возвращает уведомления друзьям переданных (active)
        устройств — доступ приостановлен."""
        notes: list[Notification] = []
        self._client_set_block(client.id, ClientBlock.EXPIRY)
        twins = self.db.twins_by_origin()
        for dev in self.db.list_devices(client.id):
            self._device_set_block(dev.id, DeviceBlock.EXPIRY, twins)
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_blocked_text(dev.name)))
        return notes

    # ── истекающие подписки (панель админа) ──────────────────────────────────

    @staticmethod
    def expiring_limit_days(length_days: int) -> int | None:
        """Порог «истекает» по длине периода L (дней): L < 12 — всегда в списке
        (None); иначе остаток ≤ max(⌊L/12⌋, 7) дней. Год → 30, месяц → 7."""
        if length_days < 12:
            return None
        return max(length_days // 12, 7)

    def expiring_subscriptions(self) -> list[tuple]:
        """[(client, секунд до конца)] — активные с конечным периодом, попавшие
        под порог; ближайшие сверху. Это НЕ пороги уведомлений: у панели своё
        правило, у уведомлений своё."""
        now = timeutil.now()
        out = []
        for client in self.db.list_clients(include_service=False, active_finite_only=True):
            end = timeutil.parse_iso(client.period_end)
            start = timeutil.parse_iso(client.period_start) if client.period_start else None
            secs = timeutil.remaining_seconds(end, now)
            if secs <= 0:
                continue
            length_days = int((end - start).total_seconds() // 86400) if start else 0
            limit = self.expiring_limit_days(length_days)
            if limit is None or secs <= limit * 86400:
                out.append((client, secs))
        out.sort(key=lambda t: t[1])
        return out

    def check_expiry(self) -> list[Notification]:
        now = timeutil.now()
        notifications: list[Notification] = []
        with self.db.transaction():
          for client in self.db.list_clients(include_service=False, active_finite_only=True):
              end = timeutil.parse_iso(client.period_end)
              start = timeutil.parse_iso(client.period_start)
              secs = timeutil.remaining_seconds(end, now)
              period_len_min = timeutil.period_minutes(start, end)

              # истёк
              if secs <= 0:
                  if client.status != SubStatus.EXPIRED:
                      friend_notes = self._block_client(client)
                      self.db.update_client_fields(client.id, status=SubStatus.EXPIRED)
                      if client.tg_id:
                          notifications.append(Notification(client.tg_id, _TXT_EXPIRED_CLIENT,
                                                            action=("sub", 0)))
                      notifications.append(Notification(
                          config.ADMIN_ID, _TXT_EXPIRED_ADMIN.format(name=self.cl_link(client)),
                          action=("extend", client.id)))
                      notifications.extend(friend_notes)   # друзьям — доступ приостановлен
                  continue

              # пороги приближения (строго меньше длительности периода).
              # Если бот «проспал» несколько порогов, шлём ТОЛЬКО самый строгий
              # (ближайший к концу) из пересечённых, остальные молча помечаем —
              # иначе клиент получит простыню «30 дней»+«14»+«7»+«1» разом.
              already = client.notified_thresholds  # уже в объекте — без запроса
              mins_left = secs // 60
              # Месяцу порог «30 дней» не показываем никогда: 31-дневный месяц
              # получал «истекает через 30 дней» назавтра после активации.
              crossed = [
                  (th_min, label) for th_min, label in config.NOTIFY_THRESHOLDS_MINUTES
                  if th_min < period_len_min and mins_left <= th_min and th_min not in already
                  and not (client.period_kind == "month" and th_min >= _MONTH_CUT_MINUTES)
              ]
              if crossed:
                  # самый строгий = наименьший порог по времени; порог проспали
                  # (бот лежал, срок правили) — называем остаток, а не подпись
                  tightest_min, tightest_label = min(crossed, key=lambda x: x[0])
                  if mins_left < tightest_min:
                      tightest_label = _left_label(mins_left)
                  if client.tg_id:
                      # кнопка отсрочки: только КЛИЕНТУ (не другу — друзья идут иным
                      # путём), только на ГОДОВОМ периоде и один раз за период.
                      grace_offer = (client.period_kind == PeriodKind.YEAR
                                     and not client.grace_used)
                      if grace_offer:
                          from awgbot.core import settings
                          from awgbot.util.textfmt import _days
                          text = _TXT_EXPIRING_CLIENT_GRACE.format(
                              label=tightest_label, days=_days(settings.get_int("grace.grace_days", 14)))
                      else:
                          text = _TXT_EXPIRING_CLIENT.format(label=tightest_label)
                      notifications.append(Notification(
                          client.tg_id, text,
                          grace_offer_client_id=client.id if grace_offer else 0,
                          action=("sub", 0)))
                  notifications.append(Notification(
                      config.ADMIN_ID,
                      _TXT_EXPIRING_ADMIN.format(name=self.cl_link(client), label=tightest_label),
                      action=("extend", client.id)))
                  # помечаем ВСЕ пересечённые отправленными (включая пропущенные крупные)
                  for th_min, _ in crossed:
                      self.db.add_notified(client.id, th_min)
        return notifications
