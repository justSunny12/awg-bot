"""
traffic.py — опрос трафика, лимиты потребления, сроки и истечение,
месячные сбросы.
"""
from __future__ import annotations

import datetime
import logging

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.infra import awg, rfacct
from awgbot.core.blocks import DeviceBlock, ClientBlock
from awgbot.core.enums import SubStatus, ActivationStatus, PeriodKind, FriendStatus
from awgbot.domain.services.types import BYTES_PER_GB, Notification
from awgbot.domain.services.base import _e
from awgbot.domain.services.blocks import _friend_blocked_text


log = logging.getLogger("awgbot.services")


# Тексты уведомлений (сухие, без слов про оплату — ТЗ 6.5).
_MONTH_CUT_MINUTES = 30 * 24 * 60             # порог, который месяцу не показываем

_TXT_EXPIRED_CLIENT = "Срок действия подписки истёк. Доступ приостановлен."
_TXT_EXPIRING_CLIENT = "Внимание: подписка истекает через {label}."
_TXT_EXPIRING_ADMIN = "Клиент «{name}»: подписка истекает через {label}."
_TXT_EXPIRED_ADMIN = "Клиент «{name}»: подписка истекла, доступ приостановлен."

# ── Тексты месячного сброса лимитов ──────────────────────────────────────────

def _gb_limit(num_bytes: int) -> str:
    """Лимит в целых ГБ для уведомлений: «500 ГБ». 0 = безлимит (в перечислениях
    такие не показываем, но на всякий)."""
    return f"{int(round(num_bytes / BYTES_PER_GB))} ГБ"


def _reset_client_text(total_limit: int, device_lines: list[str]) -> str:
    """Профилю: сброс + доступный лимит профиля + список лимитных устройств.
    total_limit>0 — показываем строку профиля; device_lines — уже отфильтрованы
    (только лимитные)."""
    parts = ["Начался новый месяц — лимит расхода по твоему профилю сброшен 🙂"]
    if total_limit > 0:
        parts.append(f"Доступный лимит на текущий месяц: {_gb_limit(total_limit)}")
    if device_lines:
        parts.append("\nДоступные лимиты по устройствам:\n" + "\n".join(device_lines))
    return "\n".join(parts)


def _reset_friend_text(device_lines: list[str]) -> str:
    """Другу: сброс + список ЕГО лимитных устройств."""
    return ("Начался новый месяц — лимиты расхода по твоим устройствам сброшены 🙂\n"
            "Доступные лимиты на текущий месяц:\n" + "\n".join(device_lines))


# ── Тексты уведомлений о потреблении (ТЗ 7-8) ────────────────────────────────


def _dev_warn_text(name: str, pct: int) -> str:
    return (f"⚠️ Устройство «{_e(name)}»: израсходовано ~{pct}% месячного лимита "
            "потребления.")


def _dev_over_text(name: str, until: str) -> str:
    return (f"🔴 Устройство «{_e(name)}»: месячный лимит потребления исчерпан. "
            f"Доступ приостановлен до {until} или пока лимит не увеличат.")


def _friend_dev_over_host_text(name: str, until: str) -> str:
    return (f"🔴 Устройство «{_e(name)}» (передано другу): лимит потребления "
            f"исчерпан, доступ приостановлен до {until}.")


def _cli_warn_text(pct: int) -> str:
    return f"⚠️ Израсходовано ~{pct}% месячного лимита потребления по всем устройствам."


def _cli_bonus_text(bonus_gb: int, until: str) -> str:
    return (f"📈 Месячный лимит потребления исчерпан. Тебе добавлено "
            f"{bonus_gb} ГБ до конца месяца — это разово, больше в этом месяце "
            "квота не увеличится. Лимит обновится "
            f"{until}.")


def _cli_bonus_admin_text(name: str, bonus_gb: int) -> str:
    return (f"📈 Клиенту «{_e(name)}» исчерпан лимит — выдано {bonus_gb} ГБ "
            "до конца месяца (разово).")


def _cli_over_text(until: str) -> str:
    return (f"🔴 Дополнительная квота исчерпана. Доступ ко всем устройствам "
            f"приостановлен до {until}.")


def _cli_over_admin_text(name: str) -> str:
    return f"🔴 Клиент «{_e(name)}» исчерпал лимит и доп.квоту — доступ приостановлен."


def _admin_self_over_text() -> str:
    return "🔴 Твой месячный лимит потребления исчерпан (уведомление, доступ не тронут)."


class TrafficMixin:
    # ── Опрос трафика (поток 4) ──────────────────────────────────────────────

    def poll_traffic(self) -> list:
        """Каждые 5 мин: dump → дельты с обработкой отката счётчика → накопление.
        last_handshake обновляем только при наличии (не затираем пустым).

        ВСЯ обработка — одна транзакция: (а) целостность — упади бот между
        накоплением и записью базы дельт, при раздельных коммитах дельта
        посчиталась бы дважды; (б) один fsync вместо 3-4 на устройство.

        Опрашиваем ВСЕ задействованные интерфейсы. Счётчики и хендшейки живут в
        ядре по интерфейсам, и опрос одного не увидит пиров другого: во время
        переезда у непереехавших замерло бы потребление, лимиты перестали бы
        срабатывать, а недоучтённое потерялось бы вместе со старым интерфейсом.
        Нечитаемый интерфейс просто пропускаем — его пиры подождут следующего
        такта, а состав пиров всё равно забота сверки."""
        # Поздравления собираем и здесь: обычно первый хендшейк ловит частый тик
        # migration_watch, но опрос всё равно ходит по ВСЕМ интерфейсам, а тот —
        # только по интерфейсу переезда. Страховка на случай, когда двойник
        # почему-то оказался не там, куда мы смотрим часто.
        greetings: list["Notification"] = []
        migrating = self.migration_running()
        peers: dict[str, dict] = {}
        for raw in self._migration_ifaces():
            try:
                for p in awg.show_dump(awg.iface_of(raw)):
                    peers[p["public_key"]] = p
            except awg.AwgError as e:
                log.warning("poll_traffic: %s не опрошен: %s", awg.iface_of(raw), e)
        # учёт РФ-трафика (концепт «учёт РФ-трафика»): чтение таблицы счётчиков —
        # вне транзакции, накопление — внутри той же, что у awg
        rf_links = self.rf_links()
        rf_state, rf_err = None, ""
        if rf_links:
            try:
                rf_state = rfacct.read()
            except rfacct.AcctError as e:
                rf_err = str(e)
        # бесплатный побочный продукт: онлайн-счётчик для статусного блока
        # (dump уже в руках — не тратим отдельный exec в мониторе)
        polled_at = timeutil.now()
        online = sum(1 for p in peers.values()
                     if timeutil.handshake_is_online(p["last_handshake"], polled_at))
        with self.db.transaction():
            if self.db.get_state("online_count") != str(online):
                self.db.set_state("online_count", str(online))   # только при изменении
            # момент опроса: «онлайн» в списках и карточках считается на него,
            # а не на «сейчас» (см. online_ref) — иначе цифра в панели и список
            # по ссылке расходятся на возраст последнего опроса
            self.db.set_state("online_polled_at", str(int(polled_at.timestamp())))
            # Все сэмплы одним запросом, дельты и новые базы — двумя executemany:
            # 3M+1 операторов на тик превращаются в четыре. Строку сэмпла
            # переписываем только при изменении счётчиков — оффлайн-устройство
            # не должно генерить UPDATE каждые пять минут.
            samples = self.db.get_samples_all()
            deltas: list[tuple[int, int, int]] = []
            bases: list[tuple[int, int, int]] = []
            for dev in self.db.list_all_devices():
                p = peers.get(dev.public_key)
                if p is None:
                    continue                          # состав пиров — забота reconcile
                rx_now, tx_now = p["rx"], p["tx"]
                sample = samples.get(dev.id)
                if sample is None:
                    bases.append((dev.id, rx_now, tx_now))       # первая база
                else:
                    drx = rx_now - sample[0]
                    dtx = tx_now - sample[1]
                    if drx < 0:                       # счётчик упал (рестарт awg)
                        drx = rx_now
                    if dtx < 0:
                        dtx = tx_now
                    if drx or dtx:
                        deltas.append((dev.id, drx, dtx))
                        bases.append((dev.id, rx_now, tx_now))
                if p["last_handshake"] and p["last_handshake"] != dev.last_handshake:
                    # пишем только при изменении: оффлайн-устройство не должно
                    # генерить UPDATE тем же значением каждые 5 минут
                    if not dev.last_handshake and migrating:
                        # первый хендшейк в окне переезда — за него тянет жребий
                        # и частый тик migration_watch, поздравить должен один
                        if self.db.claim_first_handshake(dev.id, p["last_handshake"]):
                            note = self.migration_greeting(dev)
                            if note is not None:
                                greetings.append(note)
                    else:
                        self.db.update_device_fields(dev.id, last_handshake=p["last_handshake"])
            self.db.add_traffic_bulk(deltas)
            self.db.set_samples(bases)
            if rf_links and rf_state is not None:
                self._rf_accumulate(rf_state)
        if rf_links:
            # состав счётчиков — после коммита: сначала снять показания, потом
            # удалять счётчики ушедших устройств. Чтение отказало — состав не
            # известен, писать вслепую поверх живой таблицы не будем
            if not rf_err:
                try:
                    rfacct.sync(rf_state, rf_links, [n for n, _i in config.routing_client_subnets()],
                                self.db.rf_acct_devices())
                except rfacct.AcctError as e:
                    rf_err = str(e)
            self._rf_note_error(rf_err)
        else:
            self._rf_note_error("")          # шлюзов нет — и учёта нет, старая ошибка не висит
        return greetings

    # ── учёт РФ-трафика ──────────────────────────────────────────────────────
    _RF_GEN_KEY = "rf_acct_gen"
    _RF_ERROR_KEY = "rf_acct_error"
    _RF_MONTH_RX_KEY = "rf_month_rx"
    _RF_MONTH_TX_KEY = "rf_month_tx"
    _RF_TOTAL_SAMPLE_KEY = "rf_total_sample"

    def rf_links(self) -> list[str]:
        """Линки шлюзов из БД плюс интерфейс из конфига; пусто — учёта нет."""
        out: list[str] = []
        for g in self.db.gateways():
            if g.link_if and g.link_if not in out:
                out.append(g.link_if)
        base = config.ROUTING_GW_INTERFACE
        if base and base not in out:
            out.append(base)
        return out

    def _rf_accumulate(self, state) -> None:
        """Дельты по счётчикам nft → месяц (в транзакции опроса). Поколение —
        boot_id и handle таблицы: перезагрузка или flush ruleset обнуляют
        счётчики, и всё текущее значение идёт в дельту."""
        from awgbot.domain.gwsnapshot import boot_id
        gen = f"{boot_id()}:{int(state.handle)}"
        prev_gen = self.db.get_state(self._RF_GEN_KEY) or ""
        # итог сервера
        cur_up = int(state.counters.get(rfacct.COUNTER_UP, 0))
        cur_dn = int(state.counters.get(rfacct.COUNTER_DN, 0))
        raw = (self.db.get_state(self._RF_TOTAL_SAMPLE_KEY) or "").split()
        prev_up, prev_dn = (int(raw[0]), int(raw[1])) if len(raw) == 2 and all(x.isdigit() for x in raw) else (None, None)
        d_up = rfacct.delta(prev_gen, gen, prev_up, cur_up)
        d_dn = rfacct.delta(prev_gen, gen, prev_dn, cur_dn)
        if d_up or d_dn:
            self.db.set_state(self._RF_MONTH_RX_KEY, str(int(self.db.get_state(self._RF_MONTH_RX_KEY) or 0) + d_up))
            self.db.set_state(self._RF_MONTH_TX_KEY, str(int(self.db.get_state(self._RF_MONTH_TX_KEY) or 0) + d_dn))
        if (prev_up, prev_dn) != (cur_up, cur_dn):
            self.db.set_state(self._RF_TOTAL_SAMPLE_KEY, f"{cur_up} {cur_dn}")
        # устройства
        samples = self.db.rf_samples_all()
        deltas: list[tuple[int, int, int]] = []
        bases: list[tuple[int, int, int]] = []
        for dev_id, _addr in self.db.rf_acct_devices():
            up_name, dn_name = rfacct.counter_names(dev_id)
            if up_name not in state.counters and dn_name not in state.counters:
                continue                                  # счётчиков ещё нет — ждём синхронизации
            cu, cd = int(state.counters.get(up_name, 0)), int(state.counters.get(dn_name, 0))
            s = samples.get(dev_id)
            pu, pd = (s[0], s[1]) if s else (None, None)
            du, dd = rfacct.delta(prev_gen, gen, pu, cu), rfacct.delta(prev_gen, gen, pd, cd)
            if du or dd:
                deltas.append((dev_id, du, dd))
            if s is None or (pu, pd) != (cu, cd):
                bases.append((dev_id, cu, cd))
        self.db.rf_add_bulk(deltas)
        self.db.rf_set_samples(bases)
        if prev_gen != gen:
            self.db.set_state(self._RF_GEN_KEY, gen)

    def _rf_note_error(self, err: str) -> None:
        """Одна строка журнала на смену состояния, значение — в state для панели."""
        err = " ".join((err or "").split())[:200]
        prev = self.db.get_state(self._RF_ERROR_KEY) or ""
        if err == prev:
            return
        self.db.set_state(self._RF_ERROR_KEY, err)
        if err:
            log.warning("учёт РФ-трафика: не идёт — %s", err)
        else:
            log.info("учёт РФ-трафика: идёт")

    def rf_month_total(self) -> dict:
        """Итог РФ-трафика сервера за месяц: {rx, tx, error} — только из state."""
        return {"rx": int(self.db.get_state(self._RF_MONTH_RX_KEY) or 0),
                "tx": int(self.db.get_state(self._RF_MONTH_TX_KEY) or 0),
                "error": self.db.get_state(self._RF_ERROR_KEY) or ""}

    def rf_enabled(self) -> bool:
        """Функция РФ-доступа развёрнута и включена — условие показа строк РФ
        (главная, карточки, списки); самопроверка обвязки в него не входит:
        сломанная обвязка — ровно тот случай, когда видеть «0 ГБ» полезно."""
        return self.routing_provisioned() and settings.get_bool("app.routing.enabled", False)

    def rf_line_visible(self) -> bool:
        """Строка РФ-трафика на главной: функция включена — всегда; выключена —
        только пока в месяце есть РФ-трафик."""
        tot = self.rf_month_total()
        return tot["rx"] + tot["tx"] > 0 or self.rf_enabled()

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
              by_id = {d.id: d for d in devices}
              # id старых строк, у которых есть двойник, — их расход учитывается
              # в проходе по двойнику, отдельно не судим
              paired_old = {d.twin_of for d in devices if d.twin_of is not None}

              # шлюз условной маршрутизации в лимитах не участвует: ни своим,
              # ни в сумме профиля (его трафик — весь РФ-трафик клиентов)
              devices = [d for d in devices if not d.is_gateway]
              by_id = {d.id: d for d in devices}
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
                      if not (int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER)):
                          self._device_set_block(dev.id, DeviceBlock.TRAFFIC_USER, twins)
                      if over_marker not in sent:
                          is_friend_dev = (dev.friend_status == FriendStatus.ACTIVE
                                           and dev.friend_tg_id)
                          # хозяину: спец-текст с пометкой «друг», если устройство
                          # передано; другу — обычный текст про его устройство
                          host_text = (_friend_dev_over_host_text(dev.name, until)
                                       if is_friend_dev else _dev_over_text(dev.name, until))
                          notes.append(Notification(client.tg_id, host_text))
                          if is_friend_dev:
                              notes.append(Notification(
                                  dev.friend_tg_id, _dev_over_text(dev.name, until)))
                          self.db.add_traffic_notified(client.id, over_marker)
                  elif used >= dlim * warn_pct // 100:
                      if warn_marker not in sent:
                          notes.append(Notification(
                              client.tg_id, _dev_warn_text(dev.name, warn_pct)))
                          if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                              notes.append(Notification(
                                  dev.friend_tg_id, _dev_warn_text(dev.name, warn_pct)))
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
                  if not client.bonus_granted_month:
                      # первая доп.квота этого месяца
                      bonus = settings.get_int("limits.traffic_bonus_gb", 100) * BYTES_PER_GB
                      # аудит: снимок квоты до выдачи разовой доп.квоты
                      self.db.archive_quota(client.id, "bonus_granted")
                      self.db.update_client_fields(
                          client.id,
                          bonus_bytes=int(client.bonus_bytes) + bonus,
                          bonus_granted_month=1)
                      notes.append(Notification(
                          client.tg_id,
                          _cli_bonus_text(settings.get_int("limits.traffic_bonus_gb", 100), until)))
                      if settings.get_bool("notifications.client_events.bonus", True):
                          notes.append(Notification(
                              admin_id, _cli_bonus_admin_text(client.name, settings.get_int("limits.traffic_bonus_gb", 100))))
                      self.db.add_traffic_notified(client.id, "bonus")
                  else:
                      # доп.квота уже выдавалась и тоже исчерпана → блок всех устройств
                      # КАСКАДНЫМ битом (TRAFFIC_CLIENT), не собственным TRAFFIC:
                      # так поднятие лимита устройства не снимет блок «по клиенту».
                      if "cli_over" not in sent:
                          self._client_set_block(client.id, ClientBlock.TRAFFIC_CLIENT)
                          for dev in devices:
                              self._device_set_block(dev.id, DeviceBlock.TRAFFIC_CLIENT, twins)
                              if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                                  notes.append(Notification(
                                      dev.friend_tg_id, _dev_over_text(dev.name, until)))
                          notes.append(Notification(client.tg_id, _cli_over_text(until)))
                          if settings.get_bool("notifications.client_events.over_limit", True):
                              notes.append(Notification(
                                  admin_id, _cli_over_admin_text(client.name)))
                          self.db.add_traffic_notified(client.id, "cli_over")
              elif total >= effective * warn_pct // 100:
                  if "cli80" not in sent and not is_admin_client:
                      notes.append(Notification(client.tg_id, _cli_warn_text(warn_pct)))
                      self.db.add_traffic_notified(client.id, "cli80")

        return notes

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
                          notifications.append(Notification(client.tg_id, _TXT_EXPIRED_CLIENT))
                      notifications.append(Notification(
                          config.ADMIN_ID, _TXT_EXPIRED_ADMIN.format(name=client.name)))
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
                  # самый строгий = наименьший порог по времени (сам порог
                  # дальше не нужен — только его подпись)
                  _, tightest_label = min(crossed, key=lambda x: x[0])
                  if client.tg_id:
                      # кнопка отсрочки: только КЛИЕНТУ (не другу — друзья идут иным
                      # путём), только на ГОДОВОМ периоде и один раз за период.
                      grace_offer = (client.period_kind == PeriodKind.YEAR
                                     and not client.grace_used)
                      notifications.append(Notification(
                          client.tg_id, _TXT_EXPIRING_CLIENT.format(label=tightest_label),
                          grace_offer_client_id=client.id if grace_offer else 0))
                  notifications.append(Notification(
                      config.ADMIN_ID,
                      _TXT_EXPIRING_ADMIN.format(name=client.name, label=tightest_label)))
                  # помечаем ВСЕ пересечённые отправленными (включая пропущенные крупные)
                  for th_min, _ in crossed:
                      self.db.add_notified(client.id, th_min)
        return notifications

    # ── Сбросы ───────────────────────────────────────────────────────────────

    def reset_monthly_traffic(self) -> list["Notification"]:
        """1-е число: обнулить месячные счётчики + доп.квоту + трафик-метки, и
        снять причину TRAFFIC со всех клиентов и устройств (разблокировать, если
        не осталось других причин). Причину EXPIRY НЕ трогаем — подписка живёт
        своим циклом. Возвращает уведомления о сбросе (профилям и друзьям);
        безлимитные позиции не показываем, пустые уведомления не шлём."""
        # аудит-метрика: снимок потребления завершившегося месяца ПЕРЕД обнулением.
        # Метка месяца — предыдущий календарный (сброс идёт 1-го числа за прошлый).
        _now = timeutil.now()
        _prev_month = (_now.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
        notes: list[Notification] = []
        friend_devs: dict[int, list] = {}     # friend_tg → [device rows] для их уведомлений
        # Одной транзакцией: и один fsync вместо ~5N+4NM, и атомарность —
        # падение посередине не оставит половину клиентов сброшенной.
        with self.db.transaction():
          self.db.snapshot_monthly_traffic(_prev_month)
          rf = self.rf_month_total()
          self.db.snapshot_server_rf(_prev_month, rf["rx"], rf["tx"])
          self.db.set_state(self._RF_MONTH_RX_KEY, "0")
          self.db.set_state(self._RF_MONTH_TX_KEY, "0")
          self.db.reset_month_traffic_all()
          twins = self.db.twins_by_origin()
          for client in self.db.list_clients(include_service=False):
            self.db.update_client_fields(
                client.id, bonus_bytes=0, bonus_granted_month=0)
            self.db.reset_traffic_notified(client.id)
            if int(client.block_reason) & int(ClientBlock.TRAFFIC_CLIENT):
                self._client_clear_block(client.id, ClientBlock.TRAFFIC_CLIENT)
            own_lines = []                    # лимитные СВОИ (не переданные) устройства
            for dev in self.db.list_devices(client.id):
                # месячный сброс снимает ОБЕ трафик-причины (свою и каскад клиента)
                for _tbit in (DeviceBlock.TRAFFIC_USER, DeviceBlock.TRAFFIC_CLIENT):
                    if int(dev.block_reason) & int(_tbit):
                        self._device_clear_block(dev.id, _tbit, twins)
                lim = int(dev.traffic_limit)
                if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                    if lim > 0:               # друг увидит в своём уведомлении
                        friend_devs.setdefault(dev.friend_tg_id, []).append(dev)
                elif lim > 0:
                    own_lines.append(f"{dev.name} — {_gb_limit(lim)}")
            # профилю шлём, если есть что показать: лимит профиля ИЛИ лимитные устройства
            total_limit = int(client.traffic_limit)
            if client.tg_id and (total_limit > 0 or own_lines):
                notes.append(Notification(
                    client.tg_id, _reset_client_text(total_limit, own_lines)))
        # друзьям — по их лимитным устройствам
        for friend_tg, devs in friend_devs.items():
            lines = [f"{d.name} — {_gb_limit(int(d.traffic_limit))}" for d in devs]
            notes.append(Notification(friend_tg, _reset_friend_text(lines)))
        return notes
