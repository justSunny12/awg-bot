"""poll.py — опрос трафика и учёт РФ-трафика. Пиры-зонды кворума резерва
(блок util/probenet) в онлайн не считаются: они не устройства. Подсети
пользователей для правила зондов — строки Address конфига интерфейса, как у
сверки (один источник), правило — один раз на интерфейс."""
from __future__ import annotations

import logging

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import probenet, timeutil
from awgbot.infra import awg, rfacct
from awgbot.domain.services.types import Notification

log = logging.getLogger("awgbot.services")


class PollMixin:
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
            name = awg.iface_of(raw)
            try:
                dump = awg.show_dump(name)
            except awg.AwgError as e:
                log.warning("poll_traffic: %s не опрошен: %s", name, e)
                continue
            # правило зондов — по подсетям пользователей интерфейса (строки Address), один раз на интерфейс;
            # конфиг не прочитан — правило действует (адрес блока без подсети в нём — не устройство)
            try:
                rule = probenet.rule_on(probenet.user_nets(probenet.iface_addrs(awg.read_file(awg.conf_path(name)))))
            except awg.AwgError:
                rule = True
            for p in dump:
                p["probe"] = rule and probenet.in_block(str(p.get("allowed_ips") or ""))
                peers[p["public_key"]] = p
        # учёт РФ-трафика: чтение таблицы счётчиков —
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
        online = sum(1 for p in peers.values()                  # пиры-зонды кворума — не устройства
                     if timeutil.handshake_is_online(p["last_handshake"], polled_at) and not p.get("probe"))
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
