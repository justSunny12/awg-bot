"""limits.py — лимиты потребления: предупреждения, бонус, блокировка."""
from __future__ import annotations

import logging

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.core.blocks import DeviceBlock, ClientBlock
from awgbot.core.enums import ActivationStatus, FriendStatus
from awgbot.domain.services.types import Notification

from . import bonus
from .texts import _cli_limit_off_text, _cli_limit_off_admin_text, _admin_dev_over_text, _admin_self_over_text, _cli_bonus_admin_text, _cli_bonus_text, _cli_over_admin_text, _cli_over_text, _cli_warn_text, _dev_over_text, _dev_warn_text, _friend_dev_over_host_text

log = logging.getLogger("awgbot.services")


class LimitsMixin:
    # ── Лимиты потребления (ТЗ 7-8) ──────────────────────────────────────────

    def check_traffic_limits(self) -> list["Notification"]:
        """После накопления дельт: проверка лимитов устройств и тоталов клиентов.
        Возвращает уведомления (превышения, пред-уведомления 80%, доп.квота).

        Всё в рамках календарного месяца (счётчики _month сбрасываются 1-го).
        Меряем против СУММЫ up+down. Блокировки — битом TRAFFIC (не трогая EXPIRY).
        """
        notes: list[Notification] = []
        warn_pct = settings.get_int("limits.traffic_warn_percent", 80)
        until = timeutil.first_of_next_month_str()
        admin_id = config.ADMIN_ID
        twins = self.db.twins_by_origin()             # один раз на проход, не на устройство

        with self.db.transaction():                   # один коммит вместо десятков
          for client in self.db.list_clients(include_service=False):
              if client.activation_status != ActivationStatus.ACTIVE:
                  continue
              # ОБЕ строки пары: в окне переезда потребление размазано по ним, и
              # лимит по одной дал бы человеку двойную квоту.
              devices = self.db.list_devices(client.id, all_rows=True)
              sent = client.traffic_notified          # уже в объекте — без запроса
              # шлюз условной маршрутизации в лимитах не участвует: ни своим,
              # ни в сумме профиля (его трафик — весь РФ-трафик клиентов)
              devices = [d for d in devices if not d.is_gateway]
              by_id = {d.id: d for d in devices}
              # id старых строк, у которых есть двойник, — их расход учитывается
              # в проходе по двойнику, отдельно не судим
              paired_old = {d.twin_of for d in devices if d.twin_of is not None}

              # ── лимиты устройств (независимо от клиентского) ──
              for dev in devices:
                  if dev.id in paired_old:
                      continue                  # учтён суммой у своего двойника
                  dlim = dev.traffic_limit
                  if dlim == 0:
                      continue
                  used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
                  mate = by_id.get(dev.twin_of) if dev.twin_of else None
                  if mate is not None:
                      # СУММА по паре против лимита пары (лимиты строк равны —
                      # сеттер парный). Считай каждую строку отдельно — и человек
                      # получает двойную квоту, у которой ни одна половина не
                      # дотягивает до порога.
                      used += int(mate.traffic_rx_month) + int(mate.traffic_tx_month)
                  over_marker = f"dev_over:{dev.id}"
                  warn_marker = f"dev80:{dev.id}"
                  if used >= dlim:
                      if client.tg_id == admin_id:
                          # устройства админа не блокируем — только справочно
                          if over_marker not in sent:
                              notes.append(Notification(admin_id, _admin_dev_over_text(self.dev_link(dev))))
                              self.db.add_traffic_notified(client.id, over_marker)
                          continue
                      if not (int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER)):
                          self._device_set_block(dev.id, DeviceBlock.TRAFFIC_USER, twins)
                      if over_marker not in sent:
                          is_friend_dev = (dev.friend_status == FriendStatus.ACTIVE
                                           and dev.friend_tg_id)
                          # хозяину: спец-текст с пометкой «друг», если устройство
                          # передано; другу — обычный текст про его устройство
                          host_text = (_friend_dev_over_host_text(self.dev_link(dev), until)
                                       if is_friend_dev else _dev_over_text(self.dev_link(dev), until))
                          notes.append(Notification(client.tg_id, host_text))
                          if is_friend_dev:
                              notes.append(Notification(
                                  dev.friend_tg_id, _dev_over_text(self.dev_link(dev), until)))
                          self.db.add_traffic_notified(client.id, over_marker)
                  elif used >= dlim * warn_pct // 100:
                      if warn_marker not in sent:
                          notes.append(Notification(
                              client.tg_id, _dev_warn_text(self.dev_link(dev), warn_pct)))
                          if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                              notes.append(Notification(
                                  dev.friend_tg_id, _dev_warn_text(self.dev_link(dev), warn_pct)))
                          self.db.add_traffic_notified(client.id, warn_marker)

              # ── тотал клиента ──
              climit = client.traffic_limit
              if climit == 0:
                  continue
              total = sum(int(d.traffic_rx_month) + int(d.traffic_tx_month)
                          for d in devices)
              effective = climit + int(client.bonus_bytes)
              is_admin_client = (client.tg_id == admin_id)

              if total >= effective:
                  # исчерпан текущий потолок (базовый или уже с доп.квотой)
                  if is_admin_client:
                      if "cli_over" not in sent:
                          notes.append(Notification(admin_id, _admin_self_over_text()))
                          self.db.add_traffic_notified(client.id, "cli_over")
                      continue
                  spec = bonus.current(settings)
                  # «cli_over» в этом месяце — профиль уже приостановлен (бонус был
                  # выключен): включённый позже бонус его не разблокировал бы, а
                  # «Добавлено N ГБ» пообещало бы трафик заблокированному
                  if not client.bonus_granted_month and spec.kind != "off" and "cli_over" not in sent:
                      # первая доп.квота этого месяца — по правилу «💳 Подписки → 📈 Бонус»
                      given = bonus.bytes_for(spec, climit)
                      # аудит: снимок квоты до выдачи разовой доп.квоты
                      self.db.archive_quota(client.id, "bonus_granted")
                      self.db.update_client_fields(
                          client.id,
                          bonus_bytes=int(client.bonus_bytes) + given,
                          bonus_granted_month=1)
                      amount = bonus.gb_text(given)
                      notes.append(Notification(client.tg_id, _cli_bonus_text(amount, until)))
                      if settings.get_bool("notifications.client_events.bonus", True):
                          notes.append(Notification(
                              admin_id, _cli_bonus_admin_text(self.cl_link(client), amount,
                                                              bonus.admin_note(spec, given, climit))))
                      self.db.add_traffic_notified(client.id, "bonus")
                      # «израсходовано ~80%» — уже от нового потолка: следующий тик
                      # иначе прислал бы его сразу за «Добавлено…»
                      self.db.add_traffic_notified(client.id, "cli80")
                  else:
                      # доп.квота уже выдавалась и тоже исчерпана (или бонус выключен) →
                      # блок всех устройств КАСКАДНЫМ битом (TRAFFIC_CLIENT), не
                      # собственным TRAFFIC: так поднятие лимита устройства не снимет
                      # блок «по клиенту».
                      if "cli_over" not in sent:
                          off = not client.bonus_granted_month    # бонуса не было: выключен
                          self._client_set_block(client.id, ClientBlock.TRAFFIC_CLIENT)
                          for dev in devices:
                              self._device_set_block(dev.id, DeviceBlock.TRAFFIC_CLIENT, twins)
                              if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                                  notes.append(Notification(
                                      dev.friend_tg_id, _dev_over_text(self.dev_link(dev), until)))
                          notes.append(Notification(client.tg_id,
                                                    _cli_limit_off_text(until) if off else _cli_over_text(until)))
                          if settings.get_bool("notifications.client_events.over_limit", True):
                              notes.append(Notification(
                                  admin_id, _cli_limit_off_admin_text(self.cl_link(client)) if off
                                  else _cli_over_admin_text(self.cl_link(client))))
                          self.db.add_traffic_notified(client.id, "cli_over")
              elif total >= effective * warn_pct // 100:
                  if "cli80" not in sent and not is_admin_client:
                      notes.append(Notification(client.tg_id, _cli_warn_text(warn_pct)))
                      self.db.add_traffic_notified(client.id, "cli80")

        return notes
