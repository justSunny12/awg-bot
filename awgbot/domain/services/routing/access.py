"""access.py — обвязка, доступ профилям и устройствам, личный список доменов."""

from __future__ import annotations

from typing import Optional
from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import routing
from awgbot.domain import routing as domain_routing
from awgbot.domain.services.types import Notification, RoutingAddResult, ServiceError
from .common import log


class RoutingAccessMixin:
    """Обвязка, доступ профилям и устройствам, личный список доменов."""
    # ── Условная маршрутизация ─────────────────
    # Российский IP для российских сервисов. Конфиги устройств не меняются:
    # режим — серверное состояние. Проекция состояния в инфраструктуру ровно
    # одна — членство адреса устройства в наборе ipset, поэтому переключение
    # стоит одну запись и обратимо без последствий для выданных ссылок.

    _RT_LINK_KEY = "routing_link_ok"
    _RT_STREAK_KEY = "routing_link_up_streak"
    _RT_UP_STREAK = 3                     # хороших замеров подряд: «стабильно жив»
    # «НЕДОСТУПЕН» — одно понятие на гашение, переключение и письмо админу:
    # скользящее окно последних замеров (failover.window_samples, 10),
    # в нём неуспешных не меньше, чем допускает порог доступности
    # (failover.min_availability, 50 % → 5 из 10), подряд или вразнобой.
    # Стрика «плохих подряд» нет вовсе: гашение и переключение всегда в одном
    # такте, а частичная недоступность видна так же, как полная.
    #
    # Цена названа и принята: пока окно набирает неудачи, помеченный трафик
    # уходит в тоннель, который никуда не ведёт, — до двух с половиной минут при
    # полном отказе. Одиночный плохой замер и так не шум: внутри него зонд
    # делает две попытки к двум целям с таймаутом 4 с.
    _RT_ANNOUNCED_KEY = "routing_link_announced"
    _RT_LISTS_NEXT_KEY = "routing_lists_next"       # момент следующего похода за списками (с джиттером)
    # Подсказка про бандл — не украшение. Обфускация линка симметрична: не сойдись
    # H1..H4/S1..S4 у сторон, хендшейка не будет вовсе. Отказ громкий (вот эта
    # самая тревога), но причина со стороны ВПС не видна, и без строки ниже её
    # ищут в аплинке и NAT, где её нет.
    def _txt_rt_bundle_hint(self, gw=None) -> str:
        where = "" if gw is not None else " (<code>awg-bot gw-bundle</code>)"
        return ("\n\n<i>Если проблема возникла сразу после обновления — перевыпусти конфигурацию шлюза"
                f"{where}: набор обфускации линка обязан "
                "совпадать, иначе хендшейк не проходит</i>")

    @staticmethod
    def _rt_effect_line() -> str:
        """Что именно почувствуют пользователи, пока маркировка снята.

        Отказ мягкий: через шлюз идут только российские сервисы, всё прочее и так
        шло мимо. Раньше, в упразднённой обратной модели, тот же отвал означал
        «у людей пропал интернет» — и текст был другой.
        """
        return ("РФ-доступ недоступен: российские сервисы временно "
                "открываются с зарубежного адреса и могут ругаться. Всё остальное и так "
                "шло мимо шлюза — на него это не влияет")

    def _txt_rt_gw_down(self, active=None, also=()) -> str:
        """also — резервные слоты, которые тоже лежат: их строка идёт сразу за
        первой фразой, до объяснения эффекта и подсказки про бандл."""
        who = f" {self._gw_link(active)}" if active is not None else ""
        tail = (" " + self._txt_rt_standby_also_down(also)) if also else ""
        return (f"🔴 Шлюз{who} не отвечает.{tail}\n"
                + self._rt_effect_line() + self._txt_rt_bundle_hint(active))

    def _txt_rt_gw_no_path(self, active=None, also=()) -> str:
        who = f" {self._gw_link(active)}" if active is not None else ""
        tail = (" " + self._txt_rt_standby_also_down(also)) if also else ""
        return (f"🔴 Шлюз{who} отвечает, но интернета за ним нет "
                f"— проверь аплинк и NAT на самом шлюзе.{tail}\n" + self._rt_effect_line())

    _RT_LINK_IF = "awglink"

    def routing_provisioned(self) -> bool:
        """Обвязка развёрнута? Признак — заданный интерфейс линка в конфиге."""
        return bool(settings.get("app.routing.gw_interface", config.ROUTING_GW_INTERFACE))

    def routing_provision(self) -> str:
        """Развернуть обвязку условной маршрутизации на ВПС и поднять линк.

        Раньше это были три команды в SSH из README: поставить dnsmasq,
        прогнать routing-host-setup.sh, прогнать routing-link-setup.sh, потом
        руками вписать интерфейс в app.yaml. Каждая — с ключами, которые легко
        перепутать, и ни одна не проверяла, что предыдущая отработала.

        Возвращает хвост вывода для показа админу. Бросает ServiceError, если
        какой-то шаг не отработал: полуразвёрнутая обвязка хуже отсутствующей —
        она выглядит рабочей.
        """
        import subprocess
        base = config.BASE_DIR / "install"
        out: list[str] = []

        def run(argv: list[str], what: str, timeout: int = 600) -> None:
            try:
                proc = subprocess.run(argv, capture_output=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as e:
                raise ServiceError(f"{what}: не запустилось ({e})")
            text = (proc.stdout + proc.stderr).decode(errors="replace").strip()
            out.append(text)
            if proc.returncode != 0:
                tail = "\n".join(text.splitlines()[-6:])
                raise ServiceError(f"{what} не отработал:\n{tail}")

        # dnsmasq: пакет dnsmasq-base даёт только бинарь (его тянут libvirt и
        # соседи), а обвязке нужен ЮНИТ — иначе routing-host-setup.sh честно
        # остановится на полпути.
        has_unit = subprocess.run(["systemctl", "list-unit-files", "dnsmasq.service"],
                                  capture_output=True)
        if has_unit.returncode != 0 or b"dnsmasq.service" not in has_unit.stdout:
            run(["apt-get", "install", "-y", "--no-install-recommends", "dnsmasq"],
                "установка dnsmasq")
        run(["sh", str(base / "routing-host-setup.sh"), "--apply"], "обвязка хоста")
        run(["sh", str(base / "routing-host-setup.sh"), "--install-unit"],
            "закрепление обвязки от ребута")
        run(["sh", str(base / "routing-link-setup.sh"), "--apply"], "линк до шлюза")
        settings.set_value("app.routing.gw_interface", self._RT_LINK_IF)
        # Включаем и саму функцию: разворачивать обвязку и оставить тумблер
        # выключенным значило бы спрятать «🛰 Назначить шлюз» — кнопку, на
        # которую отправляет итоговое сообщение. До назначения шлюза включённая
        # функция ничего не меняет: маркировать трафик некуда.
        settings.set_value("app.routing.enabled", True)
        routing.invalidate_self_check()
        log.info("условная маршрутизация: обвязка развёрнута, линк %s", self._RT_LINK_IF)
        return "\n".join(out[-1:])[-1500:]

    def routing_status(self) -> tuple[bool, str]:
        """(работоспособна ли фича, причина) — для preflight и админ-UI."""
        return routing.self_check()

    def routing_available(self) -> bool:
        """Функция работоспособна И включена админом.

        Два слоя намеренно разные по природе: инфраструктурный (self_check —
        есть ли чем маршрутизировать) холодный, а выключатель в настройках
        горячий. Первый отвечает «можно ли», второй — «нужно ли»."""
        return bool(settings.get_bool("app.routing.enabled", False)
                    and routing.available())

    def routing_grantable_clients(self) -> list:
        """Профили, которым можно выдать РФ-доступ, — для экрана настроек.

        Служебный профиль исключён (у него нет владельца), админский тоже:
        разрешение у него по умолчанию, и строка в списке предлагала бы выдать
        то, что и так есть."""
        return self.db.list_clients(exclude_tg=config.ADMIN_ID)

    def routing_allowed_for(self, client) -> bool:
        """Разрешён ли клиенту РФ-доступ.

        Админу — всегда: разрешение выдаёт он сам, и заставлять его сначала
        отмечать галочку себе бессмысленно. Остальным — по флагу, который админ
        ставит в их профиле.
        """
        if client is None:
            return False
        if client.tg_id and client.tg_id == config.ADMIN_ID:
            return True
        if client.routing_allowed:
            return True
        # держатель чужих устройств: разрешение — у их владельца
        for dev in self.db.list_held_devices(client.id):
            owner = self.db.get_client(dev.client_id)
            if owner is not None and (owner.routing_allowed or owner.tg_id == config.ADMIN_ID):
                return True
        return False

    def routing_client_visible(self, client) -> bool:
        """Показывать ли фичу клиенту вообще. Пока админ не выдал разрешение,
        она невидима: иначе каждый первый пойдёт спрашивать, что это за пункт."""
        return bool(self.routing_allowed_for(client) and self.routing_available())

    def routing_device_counts(self, client_id: int) -> tuple[int, int]:
        """(включено, всего) — и заголовок кнопки, и состояние профиля разом.
        По устройствам СУБЪЕКТА с разрешённым РФ-доступом: свои непереданные и
        удерживаемые."""
        return self.db.routing_device_counts(client_id, config.ADMIN_ID)

    def routing_devices(self, client_id: int) -> list:
        """Устройства субъекта для экрана переключателей: свои, затем
        удерживаемые чужие."""
        return self.db.list_routing_devices(client_id, config.ADMIN_ID)

    def routing_lent_out(self, client_id: int) -> list:
        """Свои устройства, переданные другим: в разделе видны без
        переключателя — управляет держатель."""
        return self.db.list_lent_out_devices(client_id)

    def routing_profile_on(self, client_id: int) -> bool:
        """Режим у профиля включён ⇔ включён хоть на одном устройстве.

        ВЫВОДИМ, а не храним. Отдельная колонка на профиле существовала и была
        убрана: она обязана была совпадать с флагами устройств, а свестись
        обратно при расхождении ей было негде.
        """
        return self.db.routing_device_counts(client_id, config.ADMIN_ID)[0] > 0

    def routing_health_for_client(self, client) -> Optional[bool]:
        """Показывать ли клиенту статусную строку и что в ней.

        None — не показывать вовсе: админ функцию этому профилю не разрешил, и
        рассказывать про механизм тому, кому он недоступен, — шум.

        Разрешил — строка есть ВСЕГДА, в том числе когда сам режим у человека
        выключен. Она отвечает на вопрос «а работает ли оно вообще», который
        иначе задаётся заходом в раздел; знать это полезно как раз перед
        включением. Значение берём из кэша монитора, чтобы открытие меню не
        порождало сетевых вызовов.
        """
        if not self.routing_client_visible(client):
            return None
        return self.db.get_state(self._RT_LINK_KEY) != "0"

    def set_routing_allowed(self, client_id: int, allowed: bool) -> list["Notification"]:
        """Разрешение админа — верхний слой флага. Возвращает уведомление
        владельцу (0 или 1).

        Выдача включает режим на ВСЕХ устройствах профиля сразу: человеку
        обещано «функция включена для всех твоих устройств», и включать их
        руками после выдачи приходилось админу. Отзыв флаги устройств НЕ
        трогает: он гасит эффект, а не разрушает настройку.
        """
        client = self.db.get_client(client_id)
        if client is None:
            return []
        changed = bool(client.routing_allowed) != bool(allowed)
        self.db.update_client_fields(client_id, routing_allowed=1 if allowed else 0)
        if allowed:
            self.db.set_owner_devices_routing(client_id, True)   # и переданные тоже
        self.reconcile_routing()
        if not changed:
            return []
        from awgbot.bot import texts                   # ленивый, как в соседних миксинах
        notes: list[Notification] = []
        if client.tg_id:
            notes.append(Notification(client.tg_id, texts.ROUTING_GRANTED_NOTICE if allowed
                                      else texts.ROUTING_REVOKED_NOTICE))
        # держателям переданных устройств — то же, с оговоркой, от кого
        seen: set[int] = set()
        for dev in self.db.list_lent_out_devices(client_id):
            if dev.holder_tg_id and dev.holder_tg_id not in seen:
                seen.add(dev.holder_tg_id)
                notes.append(Notification(
                    dev.holder_tg_id,
                    texts.routing_granted_holder_notice(client) if allowed
                    else texts.routing_revoked_holder_notice(client)))
        return notes

    def set_routing_allowed_many(self, client_ids, allowed: bool) -> list["Notification"]:
        """«Выбрать все»: флаг у всех, одна реконсиляция (и один перезапуск
        dnsmasq) вместо полной реконсиляции на каждый профиль — 15 профилей
        давали 15 рестартов и сброс DNS-кэша всем."""
        from awgbot.bot import texts                   # ленивый, как в соседних миксинах
        notes: list[Notification] = []
        changed_clients = []
        with self.db.transaction():
            for cid in client_ids:
                client = self.db.get_client(cid)
                if client is None or bool(client.routing_allowed) == bool(allowed):
                    continue
                self.db.update_client_fields(cid, routing_allowed=1 if allowed else 0)
                if allowed:
                    self.db.set_owner_devices_routing(cid, True)
                changed_clients.append(client)
        if changed_clients:
            self.reconcile_routing()
        for client in changed_clients:
            if client.tg_id:
                notes.append(Notification(client.tg_id, texts.ROUTING_GRANTED_NOTICE if allowed
                                          else texts.ROUTING_REVOKED_NOTICE))
            seen: set[int] = set()
            for dev in self.db.list_lent_out_devices(client.id):
                if dev.holder_tg_id and dev.holder_tg_id not in seen:
                    seen.add(dev.holder_tg_id)
                    notes.append(Notification(
                        dev.holder_tg_id,
                        texts.routing_granted_holder_notice(client) if allowed
                        else texts.routing_revoked_holder_notice(client)))
        return notes

    def set_routing_all(self, client_id: int, on: bool) -> int:
        """Массовое включение/выключение по всему профилю. Возвращает, сколько
        устройств изменилось.

        Досева при включении здесь нет. Он существовал для обратной модели, где
        набор означал «за границу»: без него сервис с закэшированным у клиента
        адресом уезжал на шлюз и получал отказ. В нынешней модели набор означает
        «домой», и промах даёт мягкий эффект — российский сервис просто
        продолжит ходить как ходил, пока кэш не истечёт. Резолвить ради этого
        шестьсот доменов по нажатию тумблера значило бы подвесить обработчик.
        """
        n = self.db.set_devices_routing(client_id, on)
        if n:
            self.reconcile_routing()
        return n

    def set_routing_device(self, device_id: int, on: bool) -> None:
        """Переключатель одного устройства. ПАРНЫЙ: в окне переезда человек
        видит и щёлкает двойника, а его реальный трафик до переимпорта идёт со
        СТАРОГО адреса. Тронь одну строку — и тумблер перестаёт делать что-либо:
        выключение не выключает, включение не включает, оба молча."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        if on and dev.is_gateway:
            return                                  # шлюзу РФ-доступ не нужен никогда (старая клавиатура)
        for peer in self._device_pair(dev):
            self.db.update_device_fields(peer.id, routing_on=1 if on else 0)
        self.reconcile_routing()

    def toggle_routing_device(self, device_id: int) -> Optional[bool]:
        """Инвертировать флаг устройства. Возвращает новое состояние, None —
        устройства нет (колбэк из старого сообщения в истории чата)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return None
        new_state = not bool(dev.routing_on)
        self.set_routing_device(device_id, new_state)
        return new_state


    # ── Личный список доменов ────────────────────────────────────────────────

    def routing_domains(self, client_id: int) -> list[str]:
        return self.db.list_routing_domains(client_id)

    def routing_add_domains(self, client_id: int, text: str) -> "RoutingAddResult":
        """Добавить домены пачкой. Возвращает разбор: что взято, что отброшено.

        Пользователь вставляет списком из мессенджера, и молча проглотить часть
        нельзя — он должен видеть причину по каждой строке, иначе решит, что
        кнопка сломана.
        """
        limit = int(settings.get("app.routing.user_domains_max", 100))
        accepted, rejected = domain_routing.parse_batch(
            text, denylist=config.routing_denylist())
        existing = set(self.db.list_routing_domains(client_id))
        free = max(0, limit - len(existing))
        added: list[str] = []
        over = 0
        for dom in accepted:
            if dom in existing:
                rejected.append((dom, "уже в списке"))
                continue
            if len(added) >= free:
                over += 1
                continue
            if self.db.add_routing_domain(client_id, dom):
                added.append(dom)
        if added:
            self.reconcile_routing()
            self._routing_preseed(client_id, added)
        return RoutingAddResult(added=added, rejected=rejected,
                                over_limit=over, limit=limit)

    _RT_PRESEED_MAX = 20                  # доменов за раз; дальше ждём резолва клиента

    def _routing_preseed(self, client_id: int, domains: list[str]) -> None:
        """Досеять набор адресами только что добавленных доменов.

        Без этого набор для домена пуст до первого DNS-запроса клиента, а его
        не будет, пока не истечёт кэш браузера, — со стороны выглядит как
        «добавил, но не работает; потом само заработало».

        Не критично: резолв может не удаться, набор всё равно наполнится по
        запросам клиента. Поэтому все ошибки глотаем и работу не срываем.
        """
        if not routing.available():
            return
        addrs: list[str] = []
        for dom in domains[:self._RT_PRESEED_MAX]:
            addrs += routing.resolve_a(dom)
        if not addrs:
            return
        try:
            routing.add_networks(routing.user_set(client_id), addrs)
        except routing.RoutingError as e:
            log.warning("routing_preseed: %s", e)

    def routing_remove_domain(self, client_id: int, domain: str) -> bool:
        removed = self.db.remove_routing_domain(client_id, domain)
        if removed:
            self.reconcile_routing()
        return removed

    def routing_clear_domains(self, client_id: int) -> int:
        n = self.db.clear_routing_domains(client_id)
        if n:
            self.reconcile_routing()
        return n
