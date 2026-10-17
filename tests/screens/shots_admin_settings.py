"""Снимки админа: настройки — уведомления, сервер AWG, резолвер, SSH-доступ, подписки, сервис."""
from __future__ import annotations

from awgbot.bot.callbacks import CancelCB, SetCB, UpdateCB

from tests.screens.base import Shot, in_zone
from tests.screens.shots_admin_common import _email_on, _fw, _people, _raise, _restarts, _tag, _upd_found


# кнопки «С/До» есть только при включённых тихих часах
_QUIET_ON = {"quiet_hours.quiet_hours_enabled": True}
_FW_ALLOW = {"allow": ["203.0.113.5", "198.51.100.0/24"], "raw_allow": ["203.0.113.5", "198.51.100.0/24"]}


def _srv(host: str = "vpn.example.org", dns: str = "10.8.1.1", **over):
    """«🖥 Сервер AWG» из настроек снимка; ядро AWG и поколение — константы
    (на хосте их читает modinfo), over — поверх (расхождение порта, переезд)."""
    def build(services):
        from awgbot.core import settings
        who = _people(services)
        d1, _, d2 = dns.partition(",")
        settings.set_value("app.network.server_host", host)
        settings.set_value("app.client_config.server_name", "AWG-SRV")
        settings.set_value("app.client_config.dns1", d1.strip())
        settings.set_value("app.client_config.dns2", (d2 or d1).strip())
        real = services.server_screen
        services.server_screen = lambda: {**real(), "kernel": "3.1.20260812", "generation": 1, **over}
        return who
    return build


def _backup_files(services):
    """«Сделать сейчас» в чат: файл копии — во временном каталоге."""
    import tempfile
    import os
    who = _people(services)
    d = tempfile.mkdtemp(prefix="awg-shot-")
    path = os.path.join(d, "awg-bot-backup.tgz.enc")
    with open(path, "wb") as f:
        f.write(b"DUMMY")
    services.make_backup = lambda: [path]
    return who


def _email_alias(services):
    """Почта подключена, письма с кодом — на алиас, а не на сам ящик."""
    from awgbot.core import settings
    who = _email_on(services)
    settings.set_value("email.resume_address", "codes@example.org")
    return who


SETTINGS = [
    # ── 1.6 настройки ───────────────────────────────────────────────────────
    Shot("adm.set.root", role="admin", press=[SetCB(sec="root")], data=_people, title="корень настроек"),
    Shot("adm.set.notify", role="admin", press=[SetCB(sec="notify")], data=_people, title="уведомления"),
    Shot("adm.set.notify.off", role="admin", press=[SetCB(sec="notify")], data=_people,
         conf={"quiet_hours.quiet_hours_enabled": False, "resource_alerts.enabled": False},
         title="уведомления: тихие часы и алерты выключены"),
    Shot("adm.set.notify.mail_off", role="admin",
         press=[SetCB(sec="notify", act="toggle", key="notifications.email_fallback")], data=_people,
         title="аварии на e-mail без ящика"),
    Shot("adm.set.notify.edit", role="admin",
         press=[SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start")], data=_people,
         conf=_QUIET_ON, title="начало тихих часов: приглашение"),
    Shot("adm.set.notify.edit.done", role="admin",
         press=[SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start")], text="22",
         data=_people, conf=_QUIET_ON, title="начало тихих часов: итог"),
    Shot("adm.set.notify.edit.cancel", role="admin",
         press=[SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start"),
                CancelCB(kind="set_notify")], data=_people, conf=_QUIET_ON, title="ввод: «✖️ Отмена» — раздел"),
    Shot("adm.set.ncl", role="admin", press=[SetCB(sec="ncl")], data=_people, title="события профилей"),
    Shot("adm.set.srv", role="admin", press=[SetCB(sec="srv")], data=_srv(), title="сервер AWG"),
    Shot("adm.set.srv.ip", role="admin", press=[SetCB(sec="srv")],
         data=_srv(host="203.0.113.10", dns="1.1.1.1, 8.8.8.8", kernel=""),
         title="сервер без домена, DNS публичный"),
    Shot("adm.set.srv.blocked", role="admin", press=[SetCB(sec="srv")],
         data=_srv(migration_blocked="переезд уже идёт", port=45871, port_conf=43125),
         title="сервер: порт расходится, переезд недоступен"),
    Shot("adm.set.srv.host", role="admin",
         press=[SetCB(sec="srv", act="edit", key="app.network.server_host")], data=_srv(),
         title="домен: приглашение"),
    Shot("adm.set.srv.host.done", role="admin",
         press=[SetCB(sec="srv", act="edit", key="app.network.server_host")], text="vpn2.example.org",
         data=_srv(), title="домен: итог"),
    Shot("adm.set.srv.dns", role="admin",
         press=[SetCB(sec="srv", act="edit", key="app.client_config.dns1")], data=_srv(),
         title="DNS: приглашение"),
    Shot("adm.set.srv.dns.done", role="admin",
         press=[SetCB(sec="srv", act="edit", key="app.client_config.dns1")], text="1.1.1.1, 8.8.8.8",
         data=_srv(), title="DNS: итог"),
    Shot("adm.set.dns", role="admin", press=[SetCB(sec="dns")], data=_srv(dns="1.1.1.1, 8.8.8.8"),
         title="свой DNS-резолвер"),
    Shot("adm.set.dns.later", role="admin", press=[SetCB(sec="dns", act="do", key="later")],
         data=_srv(dns="1.1.1.1, 1.0.0.1"),
         title="резолвер «📥 При переезде» — всплывашка и «🖥 Сервер AWG»"),
    Shot("adm.set.dns.never", role="admin", press=[SetCB(sec="dns", act="do", key="never")],
         data=_srv(dns="1.1.1.1, 1.0.0.1"),
         title="резолвер «☑️ Не нужно» — всплывашка и «🖥 Сервер AWG»"),
    Shot("adm.set.dns.now", role="admin", press=[SetCB(sec="dns", act="do", key="now")], data=_people,
         title="резолвер — переехать сейчас"),
    Shot("adm.set.mig_prep", role="admin", press=[SetCB(sec="mig_prep")], data=_people,
         title="смена порта или подсети"),
    Shot("adm.set.mig_prep.port", role="admin", press=[SetCB(sec="mig_prep", act="edit", key="port")],
         data=_people, title="свой порт: приглашение"),
    Shot("adm.set.mig_prep.port.done", role="admin", press=[SetCB(sec="mig_prep", act="edit", key="port")],
         text="51820", data=_people, title="свой порт: экран с портом"),
    Shot("adm.set.fw", role="admin", press=[SetCB(sec="fw")], data=_fw(), title="SSH-доступ: адресов нет"),
    Shot("adm.set.fw.allow", role="admin", press=[SetCB(sec="fw")], data=_fw(**_FW_ALLOW),
         title="SSH-доступ: адреса есть, фильтр выключен"),
    Shot("adm.set.fw.on", role="admin", press=[SetCB(sec="fw")],
         data=_fw(enabled=True, present=True, **_FW_ALLOW), title="SSH-доступ: фильтр включён"),
    Shot("adm.set.fw.add", role="admin", press=[SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")],
         data=_fw(), title="адрес: приглашение"),
    Shot("adm.set.fw.add.done", role="admin",
         press=[SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")], text="203.0.113.5",
         data=_fw(), title="адрес добавлен"),
    Shot("adm.set.fw.del", role="admin",
         press=[SetCB(sec="fw", act="do", key="del", val=f"0.{_tag('203.0.113.5')}")],
         data=_fw(**_FW_ALLOW), title="адрес убран"),
    Shot("adm.set.fw.on.do", role="admin", press=[SetCB(sec="fw", act="do", key="on")],
         data=_fw(**_FW_ALLOW), title="фильтр включён"),
    Shot("adm.set.fw.port", role="admin", press=[SetCB(sec="fw", act="edit", key="port")], data=_fw(),
         title="порт SSH: приглашение"),
    Shot("adm.set.fw.port.owner", role="admin", press=[SetCB(sec="fw", act="edit", key="port")],
         data=_fw(owner="cloud-init", owner_where="/etc/ssh/sshd_config.d/50-cloud-init.conf",
                  owner_detail="# generated by cloud-init, do not edit",
                  owner_files=["/etc/ssh/sshd_config.d/50-cloud-init.conf"]),
         title="порт SSH: конфигом владеет другая программа"),
    Shot("adm.set.fw.port.same", role="admin", press=[SetCB(sec="fw", act="edit", key="port")], text="22",
         data=_fw(), title="порт SSH: тот же"),
    Shot("adm.set.fw.port.busy", role="admin", press=[SetCB(sec="fw", act="edit", key="port")], text="8080",
         data=_fw(), title="порт SSH: занят"),
    Shot("adm.set.fw.port.done", role="admin", press=[SetCB(sec="fw", act="edit", key="port")], text="2222",
         data=_fw(), title="порт SSH: изменён"),
    Shot("adm.set.fw.port.retry", role="admin", press=[SetCB(sec="fw", act="do", key="port_retry")],
         data=_fw(), title="финишер: другой порт"),
    Shot("adm.set.email", role="admin", press=[SetCB(sec="email")], data=_people, title="e-mail: не подключён"),
    Shot("adm.set.email.on", role="admin", press=[SetCB(sec="email")], data=_email_on,
         title="e-mail: подключён"),
    Shot("adm.set.email.setup", role="admin", press=[SetCB(sec="email", act="do", key="setup")],
         data=_people, title="мастер почты: адрес"),
    Shot("adm.set.email.setup.known", role="admin", press=[SetCB(sec="email", act="do", key="setup")],
         text="admin@gmail.com", data=_people, title="мастер почты: известный провайдер — пароль"),
    Shot("adm.set.email.setup.unknown", role="admin", press=[SetCB(sec="email", act="do", key="setup")],
         text="admin@example.org", data=_people, title="мастер почты: свой сервер — IMAP"),
    Shot("adm.set.email.change", role="admin", press=[SetCB(sec="email", act="do", key="setup")],
         data=_email_on, title="сменить ящик: адрес"),
    Shot("adm.set.email.check", role="admin", press=[SetCB(sec="email", act="do", key="check")],
         data=_email_on, title="проверка соединения"),
    Shot("adm.set.email.test", role="admin", press=[SetCB(sec="email", act="do", key="test")],
         data=_email_on, title="тестовое письмо"),
    Shot("adm.set.email.forget", role="admin", press=[SetCB(sec="email", act="do", key="forget")],
         data=_email_on, title="отключить почту?"),
    Shot("adm.set.email.forget.yes", role="admin", press=[SetCB(sec="email", act="do", key="forget!")],
         data=_email_on, title="почта отключена"),
    Shot("adm.set.email.resume_addr", role="admin",
         press=[SetCB(sec="email", act="edit", key="email.resume_address")], data=_email_on,
         title="адрес для кода: приглашение"),
    Shot("adm.set.email.resume_addr.done", role="admin",
         press=[SetCB(sec="email", act="edit", key="email.resume_address")], text="codes@example.org",
         data=_email_on, title="адрес для кода: итог"),
    Shot("adm.set.email.resume_addr.self", role="admin",
         press=[SetCB(sec="email", act="edit", key="email.resume_address"),
                SetCB(sec="email", act="do", key="resume_self")],
         data=_email_alias,
         title="адрес для кода: кнопка «Сам ящик» — алиас снят"),
    Shot("adm.set.email.resume_addr.self.same", role="admin",
         press=[SetCB(sec="email", act="edit", key="email.resume_address"),
                SetCB(sec="email", act="do", key="resume_self")],
         data=_email_on, title="адрес для кода: «Сам ящик», когда код и так идёт на ящик"),
    Shot("adm.set.subs", role="admin", press=[SetCB(sec="subs")], data=_people, title="подписки"),
    Shot("adm.set.subs.edit", role="admin", press=[SetCB(sec="subs", act="edit", key="grace.grace_days")],
         data=_people, title="отсрочка: приглашение"),
    Shot("adm.set.subs.edit.done", role="admin",
         press=[SetCB(sec="subs", act="edit", key="grace.grace_days")], text="7", data=_people,
         title="отсрочка: итог"),
    Shot("adm.set.subs.bonus_off", role="admin", press=[SetCB(sec="subs")], data=_people,
         conf={"limits.traffic_bonus_gb": 0}, title="подписки: бонус выключен"),
    Shot("adm.set.subs.bonus_pct", role="admin", press=[SetCB(sec="subs")], data=_people,
         conf={"limits.traffic_bonus_gb": "10%"}, title="подписки: бонус 10% лимита"),
    Shot("adm.set.subs.bonus_pct_cap", role="admin", press=[SetCB(sec="subs")], data=_people,
         conf={"limits.traffic_bonus_gb": "10% 50"}, title="подписки: бонус 10% лимита, не более 50 ГБ"),
    Shot("adm.set.subs.bonus", role="admin",
         press=[SetCB(sec="subs", act="edit", key="limits.traffic_bonus_gb")], data=_people,
         title="бонус при исчерпании: приглашение"),
    Shot("adm.set.subs.bonus.bad", role="admin",
         press=[SetCB(sec="subs", act="edit", key="limits.traffic_bonus_gb")], text="10 ГБ", data=_people,
         title="бонус при исчерпании: непонятный ввод — переспрос"),
    Shot("adm.set.subs.bonus.done", role="admin",
         press=[SetCB(sec="subs", act="edit", key="limits.traffic_bonus_gb")], text="10% 50", data=_people,
         title="бонус при исчерпании: итог"),
    Shot("adm.set.mon", role="admin", press=[SetCB(sec="mon")], data=_people, title="мониторинг"),
    Shot("adm.set.mon.edit.done", role="admin",
         press=[SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes")], text="5", data=_people,
         title="частота опроса: итог"),
    Shot("adm.set.backup", role="admin", press=[SetCB(sec="backup")], data=_people, title="бэкапы"),
    Shot("adm.set.backup.off", role="admin", press=[SetCB(sec="backup")], data=_people,
         conf={"app.scheduler.backup_enabled": False}, title="бэкапы выключены"),
    Shot("adm.set.backup.email", role="admin", press=[SetCB(sec="backup")], data=_people,
         conf={"app.scheduler.backup_channel": "email"}, title="бэкапы на e-mail"),
    Shot("adm.set.backup.channel", role="admin",
         press=[SetCB(sec="backup", act="cycle", key="app.scheduler.backup_channel")], data=_people,
         title="канал на e-mail без ящика"),
    Shot("adm.set.backup.when", role="admin", press=[SetCB(sec="backup", act="edit", key="backup_when")],
         data=_people, title="день и час: приглашение"),
    Shot("adm.set.backup.when.done", role="admin",
         press=[SetCB(sec="backup", act="edit", key="backup_when")], text="5 3", data=_people,
         title="день и час: итог"),
    Shot("adm.set.backup.now", role="admin", press=[SetCB(sec="backup", act="do", key="now")],
         data=_backup_files, title="копия сейчас — файлом в чат"),
    Shot("adm.set.backup.enc", role="admin", press=[SetCB(sec="backup", act="do", key="enc")], data=_people,
         title="шифрование"),
    Shot("adm.set.backup.enc_set", role="admin", press=[SetCB(sec="backup", act="do", key="enc_set")],
         data=_people, title="фраза: первый ввод"),
    Shot("adm.set.backup.enc_set.first", role="admin", press=[SetCB(sec="backup", act="do", key="enc_set")],
         text="DUMMY-passphrase-1234", data=_people, title="фраза: повтор"),
    Shot("adm.set.svc", role="admin", press=[SetCB(sec="svc")], data=_people, title="сервис"),
    Shot("adm.set.svc.zone_conf", role="admin", press=[SetCB(sec="svc")],
         data=in_zone(_people, "Europe/Moscow", source="conf", host="Europe/London"),
         title="сервис: пояс задан ключом timezone и отличается от пояса сервера"),
    Shot("adm.set.svc.zone_bad", role="admin", press=[SetCB(sec="svc")],
         data=in_zone(_people, "Europe/London", source="conf_bad", bad="Europe/Moskva"),
         title="сервис: ключ timezone не распознан — открытая строка ⚠️, пояс сервера"),
    Shot("adm.set.svc.awg", role="admin", press=[SetCB(sec="svc", act="do", key="awg")], data=_restarts,
         title="перезапустить AWG?"),
    Shot("adm.set.svc.awg.yes", role="admin", press=[SetCB(sec="svc", act="do", key="awg!")],
         data=_restarts, title="AWG перезапущен"),
    Shot("adm.set.svc.bot", role="admin", press=[SetCB(sec="svc", act="do", key="bot")], data=_restarts,
         title="перезапустить бота?"),
    Shot("adm.set.svc.bot.yes", role="admin", press=[SetCB(sec="svc", act="do", key="bot!")],
         data=_restarts, title="бот перезапускается"),
    Shot("adm.set.upd", role="admin", press=[SetCB(sec="upd")], data=_people, title="обновления: актуальна"),
    Shot("adm.set.upd.found", role="admin", press=[SetCB(sec="upd")], data=_upd_found,
         title="обновления: доступна новая"),
    Shot("adm.set.upd.notify", role="admin", press=[SetCB(sec="upd", act="toggle", key="notify")],
         data=_upd_found, title="уведомления об обновлениях выключены"),
    Shot("adm.set.upd.cycle", role="admin", press=[SetCB(sec="upd", act="cycle", key="updates.poll_schedule")],
         data=_people, title="расписание проверки"),
    Shot("adm.upd.install", role="admin", press=[UpdateCB(action="install", tag="v1.3.0")], data=_upd_found,
         title="обновить: ожидание"),
    Shot("adm.upd.mute", role="admin", press=[UpdateCB(action="mute")], data=_people,
         title="«Не уведомлять» на уведомлении"),
    Shot("adm.upd.menu", role="admin", press=[UpdateCB(action="menu")], data=_people,
         title="«В меню» на итоге обновления"),
]


# ── шаг 5: настройки — ветки из e2e ─────────────────────────────────────────
BRANCHES = [
    Shot("adm.set.notify.quiet", role="admin", press=[SetCB(sec="notify")], data=_people,
         conf={"quiet_hours.quiet_hours_enabled": True, "quiet_hours.quiet_hours_start": 22,
               "quiet_hours.quiet_hours_end": 6, "resource_alerts.thresholds_percent.cpu": 90},
         title="уведомления: тихие часы включены — границы в тексте и «С»/«До», свой порог CPU"),
    Shot("adm.set.mig_prep.port.bad", role="admin", press=[SetCB(sec="mig_prep", act="edit", key="port")],
         text="abc", data=_people, title="свой порт: буквы — не порт"),
]


def _fw_with(extra: dict | None = None, **over):
    """SSH-доступ (_FwHost) с подменами сверх него: extra — {метод сервиса:
    функция} (ответы CLI-таймера, отказы nft и sshd)."""
    def build(services):
        who = _fw(**over)(services)
        for k, v in (extra or {}).items():
            setattr(services, k, v)
        return who
    return build


_DNS_PUBLIC = {"mode": "public", "dns1": "1.1.1.1", "dns2": "1.0.0.1", "target": "10.8.1.1"}


def _dns_blocked_by(reason: str):
    """Свой резолвер не решён, переезд сейчас невозможен: reason — ответ
    migration_blocked_reason (идёт другой переезд или второй интерфейс уже
    поднят, а переезд не начат)."""
    def build(services):
        who = _srv(dns="1.1.1.1, 1.0.0.1")(services)
        services.private_dns_info = lambda: {**_DNS_PUBLIC, "decision": ""}
        services.migration_blocked_reason = lambda: reason
        return who
    return build


_dns_blocked = _dns_blocked_by("переезд уже идёт")


def _mig_prepare(ok: bool):
    """Подготовка переезда: второй интерфейс поднят (awg1, порт 443) или
    отказ хоста; перезапуск бота — только по кнопке, здесь не зовётся."""
    def build(services):
        who = _people(services)
        services.migration_prepare = ((lambda port=None: {"iface": "awg1", "subnet": "10.9.1.0/24",
                                                          "port": str(port or 443)})
                                      if ok else _raise("runtime", "порт 443 занят"))
        services.restart_bot = _raise("runtime", "перезапуск без кнопки")
        return who
    return build


_PORT = [("press", SetCB(sec="fw", act="edit", key="port"))]


SSH = [
    Shot("adm.set.fw.on.empty", role="admin", press=[SetCB(sec="fw")], data=_fw(enabled=True, present=True),
         title="SSH-доступ: фильтр включён, адресов нет — выключить можно"),
    Shot("adm.set.fw.confirm", role="admin", press=[SetCB(sec="fw", act="do", key="confirm")],
         data=_fw_with({"firewall_confirm": lambda: True}, enabled=True, present=True, rollback=True,
                       **_FW_ALLOW),
         title="кнопка таймера CLI: «оставить» — таймер снят"),
    Shot("adm.set.fw.rollback", role="admin", press=[SetCB(sec="fw", act="do", key="rollback")],
         data=_fw_with({"firewall_confirm": lambda: True}, enabled=True, present=True, rollback=True,
                       **_FW_ALLOW),
         title="кнопка таймера CLI: «откатить сейчас» — SSH снова открыт"),
    Shot("adm.set.fw.add.bad", role="admin",
         press=[SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")], text="мусор",
         data=_fw_with({"firewall_allow_add": _raise("service", "«мусор» не адрес, не подсеть и не имя")}),
         title="адрес: не адрес — отказ, ввод открыт"),
    Shot("adm.set.fw.del.stale", role="admin", press=[SetCB(sec="fw", act="do", key="del", val="5")],
         data=_fw(**_FW_ALLOW), title="убрать адрес: список изменился — номер вне списка"),
    Shot("adm.set.fw.unknown", role="admin", press=[SetCB(sec="fw", act="do", key="чего-то-нет")],
         data=_fw(), title="неизвестное действие раздела — кнопка устарела"),
    Shot("adm.set.fw.on.fail", role="admin", press=[SetCB(sec="fw", act="do", key="on")],
         data=_fw_with({"firewall_enable": _raise("runtime", "nft: Operation not permitted")}, **_FW_ALLOW),
         title="включить фильтр: отказ nft — «Не вышло», раздел перерисован"),
    Shot("adm.set.fw.owner", role="admin", press=[SetCB(sec="fw")],
         data=_fw(owner="generator", owner_detail="managed by ansible", owner_files=["/etc/ssh/sshd_config"]),
         title="SSH-доступ: конфигом sshd владеет другой процесс"),
    Shot("adm.set.fw.drift", role="admin", press=[SetCB(sec="fw")],
         data=_fw(enabled=True, present=True, listening=2222, drift=True, **_FW_ALLOW),
         title="SSH-доступ: sshd слушает не тот порт, что держит фильтр"),
    Shot("adm.set.fw.drift.off", role="admin", press=[SetCB(sec="fw")], data=_fw(listening=2222, drift=True),
         title="SSH-доступ: фильтр выключен, sshd слушает не тот порт, что в настройках"),
    Shot("adm.set.fw.firewalld", role="admin", press=[SetCB(sec="fw")], data=_fw(firewalld=True),
         title="SSH-доступ: firewalld активен"),
    Shot("adm.set.fw.port.busy.unknown", role="admin", steps=_PORT + [("text", "8443")],
         data=_fw_with({"ssh_port_busy": lambda port: "?"}),
         title="порт SSH занят, имя процесса не видно"),
    Shot("adm.set.fw.port.range", role="admin", steps=_PORT + [("text", "70000")], data=_fw(),
         title="порт SSH вне 1–65535 — переспрос"),
    Shot("adm.set.fw.port.refused", role="admin", steps=_PORT + [("text", "2222")],
         data=_fw_with({"ssh_port_change": _raise("service", "sshd -t: Bad configuration option")}),
         title="порт SSH: sshd отказал — первой строкой раздела"),
    Shot("adm.set.fw.port.back", role="admin", press=[SetCB(sec="fw", act="do", key="port_back")],
         data=_fw(), title="финишер порта: «Назад» — раздел"),
    Shot("adm.set.srv.pending", role="admin", press=[SetCB(sec="srv")],
         data=_srv(dns="1.1.1.1, 1.0.0.1", private_dns={**_DNS_PUBLIC, "decision": "pending"}),
         title="сервер: DNS публичный, при переезде станет свой"),
    Shot("adm.set.dns.blocked", role="admin", press=[SetCB(sec="dns")], data=_dns_blocked,
         title="свой резолвер при невозможном переезде — без «Переехать сейчас», причина строкой"),
    Shot("adm.set.dns.now.blocked", role="admin", press=[SetCB(sec="dns", act="do", key="now")],
         data=_dns_blocked, title="«Переехать сейчас», когда идёт переезд — объяснение"),
    Shot("adm.set.dns.prepared", role="admin", press=[SetCB(sec="dns")],
         data=_dns_blocked_by("второй интерфейс уже поднят — начни переезд в «🔧 Сервис» или отмени его"),
         title="свой резолвер: интерфейс поднят, переезд не начат — настоящая причина"),
    Shot("adm.set.mig_prep.go", role="admin", press=[SetCB(sec="mig_prep", act="do", key="go", val="443")],
         data=_mig_prepare(True), title="подготовка переезда: интерфейс поднят, перезапуск по кнопке"),
    Shot("adm.set.mig_prep.go.fail", role="admin", press=[SetCB(sec="mig_prep", act="do", key="go")],
         data=_mig_prepare(False), title="подготовка переезда: отказ — ничего не тронуто"),
]
