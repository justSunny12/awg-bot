"""Снимки агента шлюза: SSH-доступ (W23–W26)."""
from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import CancelCB, GwCB, PageCB

from tests.screens.shots_gateway_common import _ssh
from tests.screens.shots_gateway_agent import _p, _shot, _t


def _ssh_del(allow, entry: str) -> GwCB:
    i = allow.index(entry)
    return GwCB(action="ssh_del!", val=f"{i}.{kb.entry_tag(entry)}")


_ALLOW2 = ["203.0.113.7", "home.example.net"]
_ALLOW_MANY = [f"203.0.113.{n}" for n in range(1, 13)]


_ADDR_15 = [f"198.51.100.{n}" for n in range(1, 16)]
_OWNER_LATE = {"owner": "omv", "owner_port": 2222, "port": 2222, "ports": [2222], "conf_ports": [2222]}


SSH = [
    # ── W23–W26 SSH-доступ ──────────────────────────────────────────────────
    _shot("gw.ssh", press=[GwCB(action="ssh")], title="фильтр снаружи выключен, адресов нет"),
    _shot("gw.ssh.filter", press=[GwCB(action="ssh")],
          ssh=_ssh(filter=True, allow=list(_ALLOW2), peer_nets=["192.168.2.0/24"]),
          title="фильтр включён, два адреса, подсети других шлюзов связаны"),
    _shot("gw.ssh.old_plumbing", press=[GwCB(action="ssh")], ssh=_ssh(new_plumbing=False),
          title="обвязка старого образца — тумблера нет"),
    _shot("gw.ssh.filter.empty", press=[GwCB(action="ssh")], ssh=_ssh(filter=True, allow=[]),
          title="фильтр включён, список пуст — снаружи закрыт всем"),
    _shot("gw.ssh.omv", press=[GwCB(action="ssh")],
          ssh=_ssh(owner="omv", owner_port=2222, conf_ports=[2222], ports=[22, 2200], omv_rules=3, ufw=True,
                   filter=True, allow=["203.0.113.7", "dyn.example.net"], unresolved=["dyn.example.net"],
                   held=["203.0.113.99"]),
          title="порт под OMV и все предупреждения"),
    _shot("gw.ssh.omv.same", press=[GwCB(action="ssh")],
          ssh=_ssh(port=2222, env_port=2222, owner="omv", owner_port=2222, ports=[2222], conf_ports=[2222],
                   table_ports={"tunnel_in": 2222}, filter=True, allow=["203.0.113.7"], omv_rules=3),
          title="порт под OMV, в OMV и у sshd совпадает — предупреждения о порте нет"),
    _shot("gw.ssh.conf_differs", press=[GwCB(action="ssh")], ssh=_ssh(conf_ports=[2222]),
          title="конфиг sshd разошёлся с тем, что слушает сервис"),
    _shot("gw.ssh.unresolved", press=[GwCB(action="ssh")],
          ssh=_ssh(filter=True, allow=["home2.dyn.example"], unresolved=["home2.dyn.example"]),
          title="имя не резолвится и прошлого адреса нет"),
    _shot("gw.ssh.no_lan", press=[GwCB(action="ssh")], ssh=_ssh(lan=[]),
          title="подсеть шлюза неизвестна — строка локальной сети без адреса"),
    _shot("gw.ssh.peers.many", press=[GwCB(action="ssh")],
          ssh=_ssh(peer_nets=["10.0.0.0/8<b>", "10.2.0.0/16", "10.3.0.0/16", "10.4.0.0/16"]),
          title="подсети соседей — первые три, экранированы"),
    _shot("gw.ssh.generator", press=[GwCB(action="ssh")],
          ssh=_ssh(owner="generator", owner_detail="managed by <cloud-init>",
                   owner_files=["/etc/ssh/sshd_config"]),
          title="порт держит другой процесс — его пометка в конфиге"),
    _shot("gw.ssh.sshd_down", press=[GwCB(action="ssh")], ssh=_ssh(sshd_down=True, ports=[]),
          title="sshd не запущен"),
    _shot("gw.ssh.many", press=[GwCB(action="ssh")], ssh=_ssh(filter=True, allow=list(_ALLOW_MANY)),
          title="12 адресов — листание"),
    _shot("gw.ssh.many.page2", press=[GwCB(action="ssh"),
                                      PageCB(screen="gwssh", ref=0, page=1, back=GwCB(action="ssh").pack())],
          ssh=_ssh(filter=True, allow=list(_ALLOW_MANY)),
          title="листание: вторая страница — номера кнопок по полному списку"),
    _shot("gw.ssh.on", press=[GwCB(action="ssh"), GwCB(action="ssh_on!")], ssh=_ssh(allow=list(_ALLOW2)),
          title="включить фильтр — сразу, предупреждение alert"),
    _shot("gw.ssh.on.refused", press=[GwCB(action="ssh"), GwCB(action="ssh_on!")], ssh=_ssh(allow=list(_ALLOW2)),
          filter_error="Обвязка шлюза старого образца: перевыпусти конфигурацию шлюза с сервера AWG",
          title="включить фильтр на старой обвязке — отказ alert, раздел как был"),
    _shot("gw.ssh.off", press=[GwCB(action="ssh"), GwCB(action="ssh_off!")],
          ssh=_ssh(filter=True, allow=list(_ALLOW2)), title="выключить фильтр — alert"),
    _shot("gw.ssh.del", press=[GwCB(action="ssh"), _ssh_del(_ALLOW2, "home.example.net")],
          ssh=_ssh(filter=True, allow=list(_ALLOW2)), title="«➖ адрес» — сразу, всплывашка"),
    _shot("gw.ssh.del.stale", press=[GwCB(action="ssh"), GwCB(action="ssh_del!", val="5.deadbeef")],
          ssh=_ssh(allow=list(_ALLOW2)), title="список изменился — переспрос"),
    _shot("gw.ssh.port.ask", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], title="приглашение: порт"),
    _shot("gw.ssh.port.done", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="2222",
          title="порт изменён — итог первой строкой раздела"),
    _shot("gw.ssh.port.same", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="22",
          title="тот же порт — финишер"),
    _shot("gw.ssh.port.busy", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="8080",
          busy="nginx", title="порт занят — финишер"),
    _shot("gw.ssh.port.bad", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="70000",
          title="не порт — переспрос"),
    _shot("gw.ssh.port.error", press=[GwCB(action="ssh"), GwCB(action="ssh_port")], text="2222",
          port_error="sshd не поднялся на 2222 — вернул 22", title="смена не прошла"),
    _shot("gw.ssh.port.owner", press=[GwCB(action="ssh"), GwCB(action="ssh_port")],
          ssh=_ssh(owner="omv", owner_port=22), title="порт под OMV — отказ сразу по кнопке"),
    _shot("gw.ssh.port.owner_late", press=[GwCB(action="ssh_port")], text="2200", ssh_after=(1, _OWNER_LATE),
          title="OMV взял конфиг между приглашением и вводом — отказ экраном, ввод закрыт"),
    _shot("gw.ssh.port.owner_late.generator", press=[GwCB(action="ssh_port")], text="2200",
          ssh_after=(1, {"owner": "generator", "owner_detail": "Ansible managed: do not edit",
                         "owner_files": ["/etc/ssh/sshd_config.d/10-ansible.conf"]}),
          title="другой генератор взял конфиг между приглашением и вводом — его строка в отказе"),
    _shot("gw.ssh.port.race", press=[GwCB(action="ssh_port")], text="2200", port_refusal=True,
          title="OMV переписал конфиг уже во время смены — отказ сервиса тем же экраном"),
    _shot("gw.ssh.port.retry", steps=[_p(GwCB(action="ssh")), _p(GwCB(action="ssh_port")), _t("22"),
                                      _p(GwCB(action="ssh_port_retry"))],
          title="финишер после ввода → «✏️ Другой порт»: финишер со «Скрыть», приглашение новым"),
    _shot("gw.ssh.port.back", steps=[_p(GwCB(action="ssh")), _p(GwCB(action="ssh_port")), _t("8080"),
                                     _p(GwCB(action="ssh_port_back"))], busy="nginx",
          title="финишер после ввода → «⬅️ Назад»: финишер со «Скрыть», раздел новым"),
    _shot("gw.ssh.add.ask", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], title="приглашение: адреса"),
    _shot("gw.ssh.add.done", press=[GwCB(action="ssh"), GwCB(action="ssh_add")],
          text="198.51.100.4 home.example.net", ssh=_ssh(filter=True), title="добавлено"),
    _shot("gw.ssh.add.many", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], text=" ".join(_ADDR_15),
          ssh=_ssh(filter=True), title="15 адресов — итог перечисляет двенадцать и «и ещё 3»"),
    _shot("gw.ssh.add.merged", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], text="203.0.113.0/24",
          ssh=_ssh(allow=["203.0.113.7", "203.0.113.9", "home.example.net"]),
          title="подсеть поглотила адреса"),
    _shot("gw.ssh.add.already", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], text="203.0.113.7",
          ssh=_ssh(allow=list(_ALLOW2)), title="всё уже в списке"),
    _shot("gw.ssh.add.bad", press=[GwCB(action="ssh"), GwCB(action="ssh_add")], text="2001:db8::1",
          title="IPv6 — переспрос"),
    _shot("gw.ssh.add.cancel", press=[GwCB(action="ssh"), GwCB(action="ssh_add"), CancelCB(kind="set_ssh")],
          title="«✖️ Отмена» — раздел на месте"),
]
