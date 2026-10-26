"""report.py — часть пакета migration (см. __init__): прогресс и обзор переезда по
замороженной когорте."""
from __future__ import annotations

from awgbot.core import config
from awgbot.infra import awg

from .common import MigrationProgress


class MigrationReportMixin:
    """Прогресс и обзор по когорте."""

    # ── прогресс ─────────────────────────────────────────────────────────────

    def migration_progress(self) -> MigrationProgress:
        """Готовность по замороженной когорте. Переехал ⇔ на двойнике был
        хендшейк; пользователь переехал ⇔ переехали ВСЕ его живые устройства («хотя бы
        одно» дало бы число, которое врёт)."""
        cohort = self.db.cohort_ids()
        if not cohort:
            return MigrationProgress()
        twins = self.db.twins_by_origin()
        by_id = {d.id: d for d in self.db.list_all_devices()}

        done_by_client: dict[int, list[bool]] = {}
        devices_done = 0
        for old_id in cohort:
            old = by_id.get(old_id)
            if old is None:
                continue                                  # удалено в окне — выбыло
            twin = by_id.get(twins.get(old_id, -1))
            moved = bool(twin and twin.last_handshake)
            devices_done += int(moved)
            done_by_client.setdefault(old.client_id, []).append(moved)

        return MigrationProgress(
            clients_done=sum(1 for flags in done_by_client.values() if all(flags)),
            clients_total=len(done_by_client),
            devices_done=devices_done,
            devices_total=sum(len(f) for f in done_by_client.values()),
        )

    def migration_pending(self) -> list[tuple[str, str]]:
        """Кто ещё не переехал: [(профиль, устройство), ...].

        Реальность выглядит как «11 из 12» в течение недели, и админу нужно
        видеть не только что кто-то отстал, но и кто именно.
        """
        cohort = self.db.cohort_ids()
        if not cohort:
            return []
        twins = self.db.twins_by_origin()
        by_id = {d.id: d for d in self.db.list_all_devices()}
        out: list[tuple[str, str]] = []
        for old_id in sorted(cohort):
            old = by_id.get(old_id)
            if old is None:
                continue
            twin = by_id.get(twins.get(old_id, -1))
            if twin and twin.last_handshake:
                continue
            client = self.db.get_client(old.client_id)
            out.append((client.name if client else "?", old.name))
        return out

    def migration_overview(self) -> dict:
        """Экран обзора переезда: что меняется (порт, подсеть, поколение ядра —
        только известное) и профили когорты — [(client, переехало, живых)]."""
        from awgbot.core import settings
        from awgbot.infra import awglock
        d = {"port": ("", ""), "subnet": ("", ""), "generation": ("", ""), "rows": []}
        new_if = config.MIGRATION_INTERFACE
        if new_if:
            try:
                new_port = int(awg.read_server_params(iface=new_if)["listen_port"])
                d["port"] = (int(config.SERVER_PORT or settings.get_int("app.network.server_port", 0)), new_port)
            except (awg.AwgError, KeyError, TypeError, ValueError):
                pass
        if config.MIGRATION_SUBNET_PREFIX:
            # подсеть — как в «🖥 Сервер AWG»: CIDR, а не префикс
            d["subnet"] = (f"{config.SUBNET_PREFIX}.0/24", f"{config.MIGRATION_SUBNET_PREFIX}.0/24")
        applied, target = awglock.applied_generation(), awglock.target_generation()
        if target > applied:
            d["generation"] = (applied, target)
        cohort = self.db.cohort_ids()
        if not cohort:
            return d
        twins = self.db.twins_by_origin()
        by_id = {x.id: x for x in self.db.list_all_devices()}
        per: dict[int, list[bool]] = {}
        for old_id in cohort:
            old = by_id.get(old_id)
            if old is None:
                continue
            twin = by_id.get(twins.get(old_id, -1))
            per.setdefault(old.client_id, []).append(bool(twin and twin.last_handshake))
        for cid, flags in per.items():
            client = self.db.get_client(cid)
            if client is not None:
                d["rows"].append((client, sum(flags), len(flags)))
        return d

    def migration_client_devices(self, client_id: int) -> list[tuple]:
        """Переезд одного профиля: [(старое устройство, переехало, unix-время
        последнего подключения двойника, иначе старой строки)]."""
        cohort = self.db.cohort_ids()
        twins = self.db.twins_by_origin()
        devices = [x for x in self.db.list_all_devices() if x.client_id == client_id]
        by_id = {x.id: x for x in devices}
        out = []
        for old in devices:
            if old.id not in cohort:
                continue
            twin = by_id.get(twins.get(old.id, -1))
            moved = bool(twin and twin.last_handshake)
            ts = (twin.last_handshake if moved else old.last_handshake) or 0
            out.append((old, moved, int(ts or 0)))
        return out

    def migration_client_progress(self, client_id: int) -> tuple[int, int, int]:
        """(переехало, живых в когорте, всего устройств) для одного профиля.

        Третье число считается по НОВОМУ интерфейсу — парами, а не строками: в
        окне каждое устройство задвоено, и наивный счёт показал бы «всего 6» на
        трёхустройственном профиле.
        """
        cohort = self.db.cohort_ids()
        twins = self.db.twins_by_origin()
        devices = [d for d in self.db.list_all_devices() if d.client_id == client_id]
        by_id = {d.id: d for d in devices}
        live_ids = [d.id for d in devices if d.id in cohort]
        done = 0
        for old_id in live_ids:
            twin_id = twins.get(old_id)
            twin = by_id.get(twin_id) if twin_id else None
            done += int(bool(twin and twin.last_handshake))
        # «Всего» — по ВИДИМЫМ строкам, а не по парам: непарная старая строка
        # (рождение двойника упало) видна человеку и обязана попасть в счёт.
        total = len(self.db.list_devices(client_id))
        return done, len(live_ids), total
