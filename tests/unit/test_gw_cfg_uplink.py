"""
Раздел uplink документа конфигурации: на агенте (domain/gateway/cfg/uplink.py
— решение и наблюдение «аплинк мёртв», uplinktxn.py — транзакция под замком
обвязки, восстановление и тик; порядок в apply.py; ответ человека в
mailphrase.cfg_answer) и на ВПС (domain/services/gwchannel/cfg.py:
gwcfg_uplink, gwcfg_old_uplink, gwcfg_ack_take, отпечаток в карточке слота).

Аплинк — единственная дорога агента в Telegram. Цена ошибки: новый конфиг,
поставленный молча, может отрезать агента от чата; не поднявшийся и не
откатившийся — тоже, и шлюз чинят руками; ключи признания, опередившие или
отставшие от конфига, — обвязка опускает линк как у чужого устройства;
прогон юнита на загрузке, вклинившийся в транзакцию, — юнит со старыми
ключами; отпечаток, посчитанный со строками политики, — вечная доставка;
вопрос второй раз на то же значение — спам в чате агента; аварийное правило,
сработавшее на первом же замере, — аплинк меняется без человека из-за
разового сбоя; переезд профилей без аплинка двойника — агент немеет в финале.

Хост — `_Host` из теста документа (юнит, конфиг линка, /etc/awg-gw) плюс
аплинк: конфиг awg0 во временном каталоге и скрипт обвязки в режимах
--uplink / --uplink-commit / --uplink-revert, который ведёт себя как
настоящий (копия прежнего — `awg0.conf.before`, ставит файл со строками
политики, возвращает прежний байт в байт). Хендшейк аплинка — по
`uplink_age` (секунды; None — пир не отвечал ни разу; "absent" — интерфейса
нет; "unknown" — `awg show` не ответил). Замок обвязки — настоящий flock на
файле во временном каталоге. Ключи — настоящие X25519 на лету.
"""
from __future__ import annotations

import fcntl
import json
import os
import time

import pytest

from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway import base as gwbase
from awgbot.domain.gateway.cfg import uplink as upmod
from awgbot.domain.gateway.cfg import uplinktxn as txnmod
from awgbot.infra import gwguard
from awgbot.util import wgconf
from tests.unit import test_gw_cfg_apply as _apply
from tests.unit.test_gw_cfg_apply import _Clock, _doc
from tests.unit.test_gwlink_rekey import keypair

pytestmark = pytest.mark.unit

conf_dir = _apply.conf_dir
svc = _apply.svc

POLICY = ('PostUp = ip rule list | grep -q "fwmark 0x1 lookup 100" || ip rule add fwmark 0x1 lookup 100\n'
          "PostUp = ip route replace default dev %i table 100\n"
          "PostUp = sysctl -qw net.ipv4.conf.%i.rp_filter=2\n"
          "PostDown = ip route del default dev %i table 100 2>/dev/null || true\n")

EMERGENCY_SAME = ("⚠️ Аплинк молчал 10 мин — применил новый конфиг с сервера AWG без подтверждения: "
                  "ключ устройства тот же")
EMERGENCY_TWIN = ("⚠️ Аплинк молчал 10 мин — применил новый конфиг с сервера AWG без подтверждения: "
                  "сервер AWG выдал его этому шлюзу")
REVERTED = "⚠️ Новый конфиг аплинка не поднялся — вернул прежний"
RECOVERED = "⚠️ Применение конфига аплинка прервал перезапуск агента — вернул прежний"


def uplink_conf(priv: str, srv_pub: str, endpoint: str = "vps.example.org:43125") -> str:
    """Конфиг аплинка так, как его шлёт сервер: клиентский, без DNS, Table = off."""
    return (f"[Interface]\nPrivateKey = {priv}\nAddress = 10.8.1.5/32\nTable = off\nJc = 4\nJmin = 40\nJmax = 70\n\n"
            f"[Peer]\nPublicKey = {srv_pub}\nPresharedKey = DUMMY-UPLINK-PSK=\nEndpoint = {endpoint}\n"
            "AllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n")


def rendered(text: str) -> str:
    """Как ставит скрипт обвязки: строки политики сразу за Table = off."""
    return text.replace("Table = off\n", "Table = off\n" + POLICY)


class _Host(_apply._Host):
    """Шлюз с аплинком awg0.

    `uplink_age` — секунд с хендшейка аплинка (None — не было; "absent",
    "unknown" — см. шапку); `comes_up` — даст ли аплинк хендшейк после нового
    конфига; `refuse` — скрипт отвергнет конфиг проверкой (ничего не тронув);
    `uplink_rc` — юнит аплинка с новым конфигом не стартует (скрипт сам
    возвращает прежний и убирает копию); `revert_rc` — прежний не поднимется
    при --uplink-revert. `uplink_runs` — что подали в --uplink, `finishes` —
    commit/revert по порядку, `held` — держал ли кто-то замок обвязки в
    момент каждого вызова скрипта, `lock_env` — AWG_GW_LOCK_HELD каждого вызова."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.conf_dir = tmp_path / "amneziawg"; self.conf_dir.mkdir()
        self.lock = tmp_path / "awg-gw-apply.lock"
        monkeypatch.setattr(config, "GW_CONF_DIR", str(self.conf_dir))
        monkeypatch.setattr(config, "GW_UPLINK_IF", "awg0")
        monkeypatch.setattr(gwguard, "STATUS_FILE", str(tmp_path / "gateway.status"))
        monkeypatch.setattr(gwguard, "APPLY_LOCK_PATH", str(self.lock))
        self.dev_priv, self.dev_pub = keypair()
        _, self.srv_pub = keypair()
        self.server_text = uplink_conf(self.dev_priv, self.srv_pub)
        self.uplink = self.conf_dir / "awg0.conf"
        self.before = self.conf_dir / "awg0.conf.before"
        self.uplink.write_text(rendered(self.server_text), encoding="utf-8")
        self.uplink_bytes = self.uplink.read_bytes()
        # юнит нового образца: со строкой прогона юнита (её ставит скрипт и агент)
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            "GATEWAY_PUBKEY=AAAAPUBKEY=", f"GATEWAY_PUBKEY={self.dev_pub}").replace(
            "EnvironmentFile=", "Environment=AWG_GW_UNIT_RUN=1\nEnvironmentFile=", 1), encoding="utf-8")
        self.unit_before = self.path.read_text(encoding="utf-8")
        self.uplink_age: int | str | None = 5
        self.comes_up = True
        self.refuse = False
        self.uplink_rc = 0
        self.revert_rc = 0
        self.uplink_runs: list[tuple[str, str, str]] = []    # (имя файла, текст, UPLINK_IF)
        self.finishes: list[str] = []
        self.held: list[bool] = []
        self.lock_env: list[str] = []

    def _lock_is_held(self) -> bool:
        fd = os.open(str(self.lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def _script(self, argv, kw):
        self.calls.append(argv)
        self.held.append(self._lock_is_held())
        self.lock_env.append((kw.get("env") or {}).get("AWG_GW_LOCK_HELD", ""))

    def _run(self, argv, **kw):
        argv = list(argv)
        if len(argv) >= 3 and argv[1] == "--uplink":
            self._script(argv, kw)
            src = argv[2]
            text = open(src, encoding="utf-8").read()
            self.uplink_runs.append((src.rsplit("/", 1)[-1], text, (kw.get("env") or {}).get("UPLINK_IF", "")))
            if self.refuse:
                # как настоящий: причина строкой ОШИБКА, итог — «uplink: refused» последней строкой
                return _apply._cp(1, ("  ОШИБКА: в конфиге аплинка нет Table = off — не установлен\n"
                                      "uplink: refused\n").encode())
            if self.uplink_rc:
                return _apply._cp(1, "  ОШИБКА: аплинк с новым конфигом не поднялся — откатываю на прежний\n".encode())
            new = rendered(text)
            if self.uplink.read_text(encoding="utf-8") == new:
                return _apply._cp(0, "uplink: unchanged\n".encode())
            self.before.write_bytes(self.uplink.read_bytes())
            self.uplink.write_text(new, encoding="utf-8")
            self.uplink_age = 1 if self.comes_up else None
            return _apply._cp(0, "uplink: installed\n".encode())
        if len(argv) >= 2 and argv[1] in ("--uplink-commit", "--uplink-revert"):
            self._script(argv, kw)
            if argv[1] == "--uplink-commit":
                self.finishes.append("commit")
                self.before.unlink(missing_ok=True)
                return _apply._cp(0, "uplink: committed\n".encode())
            self.finishes.append("revert")
            if not self.before.exists():
                return _apply._cp(0, "uplink: unchanged\n".encode())
            self.uplink.write_bytes(self.before.read_bytes())
            self.before.unlink()
            if self.revert_rc:
                return _apply._cp(1, "ОШИБКА: аплинк на прежнем конфиге не поднялся\n".encode())
            self.uplink_age = 5
            return _apply._cp(0, "uplink: reverted\n".encode())
        if argv[:3] == ["awg", "show", "awg0"] and argv[-1] == "latest-handshakes":
            self.calls.append(argv)
            if self.uplink_age in ("absent", "unknown"):
                return _apply._cp(1, b"", b"Unable to access interface\n")
            stamp = 0 if self.uplink_age is None else int(time.time()) - self.uplink_age
            return _apply._cp(0, f"DUMMYPUB=\t{stamp}\n".encode())
        if argv[:2] == ["ip", "-j"] and "awg0" in argv:
            self.calls.append(argv)
            return _apply._cp(0, b'[{"ifname": "awg0"}]' if self.uplink_age == "unknown" else b"[]")
        return super()._run(argv, **kw)

    def unit_keys(self) -> tuple[str, str]:
        return gwguard.unit_env("GATEWAY_PUBKEY"), gwguard.unit_env("GATEWAY_PREV_PUBKEY")

    def txn_files(self) -> list[str]:
        """Файлы транзакции и ожидания с ключом устройства, что остались в /etc/awg-gw."""
        return sorted(n for n in ("uplink.next", "uplink.txn", "uplink.pending") if (self.etc / n).exists())

    def hold_lock(self) -> int:
        """Чужой прогон обвязки держит замок (вернуть — os.close)."""
        fd = os.open(str(self.lock), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd


@pytest.fixture()
def host(tmp_path, monkeypatch):
    return _Host(tmp_path, monkeypatch)


def _value(host, *, text: str | None = None, pubkey: str | None = None, prev: str = "") -> dict:
    return {"conf": host.server_text if text is None else text,
            "pubkey": host.dev_pub if pubkey is None else pubkey, "prev_pubkey": prev}


def _moved(host, endpoint: str = "vps2.example.org:43125") -> str:
    """Тот же ключ устройства, другой Endpoint (правка обфускации, адреса ВПС)."""
    return uplink_conf(host.dev_priv, host.srv_pub, endpoint=endpoint)


def _installed_as(host, text: str) -> str:
    return rendered(wgconf.render(wgconf.parse(text), "uplink"))


def _dead_for(svc, clock, seconds: float) -> None:
    """Агент наблюдает мёртвый аплинк `seconds` секунд монотонного времени:
    первый замер начинает окно, второй — через `seconds`."""
    svc.cfg_uplink_observe()
    clock.t += seconds


# ── то же и ключи признания ──────────────────────────────────────────────────

def test_the_standing_uplink_counts_as_the_same_despite_the_policy_lines(svc, host):
    """Стоящий конфиг несёт PostUp политики, которые дописал скрипт; сервер
    шлёт без них. Отпечаток обязан совпасть — иначе документ ехал бы на
    каждом снимке, а агент спрашивал бы о нём без конца."""
    assert svc.cfg_fingerprints()["uplink"] == gwcfg.uplink_print(host.server_text, host.dev_pub, ""), (
        "отпечаток снимка не совпал с серверным для того же аплинка")
    res = svc.apply_cfg(_doc(uplink=_value(host)))
    assert "uplink" in res["ack"]["applied"] and res["ask"] == [] and res["note"] == "", res
    assert host.uplink_runs == [] and host.text() == host.unit_before


def test_only_new_recognition_keys_are_written_to_the_unit_silently(svc, host):
    """Конфиг тот же, сменился прежний ключ признания (окно переезда): стоящий
    аплинк ими признаётся — строки юнита переписываются молча, аплинк не
    трогается; повтор того же документа — ничего."""
    _, other = keypair()
    res = svc.apply_cfg(_doc(uplink=_value(host, prev=other)))
    assert host.unit_keys() == (host.dev_pub, other), host.text()
    assert host.uplink_runs == [] and host.finishes == [], "аплинк переподнят ради ключей признания"
    assert "uplink" in res["ack"]["applied"] and "uplink" in res["changed"] and res["ask"] == []
    assert res["note"].startswith("⚙️ Сервер AWG прислал конфигурацию шлюза — применено:"), res["note"]
    again = svc.apply_cfg(_doc(uplink=_value(host, prev=other)))
    assert again["note"] == "" and again["changed"] == [], again


def test_recognition_keys_wait_for_a_running_unit_pass_and_do_not_overtake_it(svc, host, monkeypatch):
    """Ключи признания пишутся под замком обвязки: прогон юнита, держащий
    замок дольше 5 минут, — отказ с причиной, юнит не тронут (иначе прогон
    записал бы юнит поверх со старыми ключами)."""
    _Clock(monkeypatch)
    _, other = keypair()
    fd = host.hold_lock()
    try:
        res = svc.cfg_uplink_keys_only(_value(host, prev=other))
    finally:
        os.close(fd)
    assert res["ok"] is False and res["busy"] is True and res["error"].startswith("юнит не переписан"), res
    assert host.text() == host.unit_before, "ключи признания записаны в обход замка обвязки"


def test_busy_recognition_keys_ask_the_server_to_repeat(svc, host, monkeypatch):
    """Документ с новыми ключами признания пришёл, пока обвязку держит
    прогон юнита: раздел не «отвергнут», а ответ с retry — сервер повторит
    тот же документ, а не будет ждать нового расхождения снимка."""
    _Clock(monkeypatch)
    _, other = keypair()
    fd = host.hold_lock()
    try:
        res = svc.apply_cfg(_doc(uplink=_value(host, prev=other)))
    finally:
        os.close(fd)
    assert res["ack"].get("retry") is True and "uplink" in res["ack"]["failed"], res["ack"]
    assert host.text() == host.unit_before


# ── вопрос человеку и ожидающий конфиг ───────────────────────────────────────

def test_a_new_uplink_waits_for_the_human_and_touches_nothing(svc, host):
    """Новый конфиг при живом аплинке — приглашение: серверу wait с
    отпечатком, человеку — вопрос первым в очереди, ни юнит, ни аплинк не
    тронуты; тот же документ снова — второго вопроса нет."""
    value = _value(host, text=_moved(host))
    res = svc.apply_cfg(_doc(uplink=value))
    server = gwcfg.uplink_print(value["conf"], value["pubkey"], "")
    assert res["ack"]["wait"] == {"uplink": server} and res["ask"] == ["uplink"], res["ack"]
    assert host.uplink_runs == [] and host.text() == host.unit_before
    assert host.uplink.read_bytes() == host.uplink_bytes
    assert svc.cfg_questions() == ["uplink"]
    q = svc.cfg_question("uplink")
    assert q == {"section": "uplink", "fp": gwcfg.key_fp(host.dev_pub, host.srv_pub), "device": host.dev_pub[:4]}, q
    again = svc.apply_cfg(_doc(uplink=value))
    assert again["ask"] == [] and again["ack"]["wait"] == {"uplink": server}, "вопрос задан второй раз"


def test_the_waiting_uplink_keeps_its_private_key_in_a_root_only_file_not_in_the_db(svc, host):
    """Пока вопрос висит, конфиг с приватным ключом устройства лежит в
    /etc/awg-gw/uplink.pending (0600), а в базе — только ключи признания:
    копия базы в бэкапе не должна уносить ключ аплинка. Снятый вопрос убирает
    и файл."""
    value = _value(host, text=_moved(host))
    svc.apply_cfg(_doc(uplink=value))
    pending = host.etc / "uplink.pending"
    assert pending.read_text(encoding="utf-8") == value["conf"], "конфиг ожидающего аплинка не в файле"
    assert pending.stat().st_mode & 0o777 == 0o600, oct(pending.stat().st_mode)
    raw = svc.db.get_state(svc._CFG_PENDING_KEY) or ""
    assert host.dev_priv not in raw and "conf" not in json.loads(raw)["uplink"], raw
    assert svc.cfg_pending()["uplink"]["conf"] == value["conf"], "ожидающий конфиг не собран из файла"
    svc.cfg_release("uplink")
    assert not pending.exists(), "снятый вопрос оставил ключ устройства на диске"


def test_the_uplink_question_comes_before_mail_and_after_the_link_keys(svc, host):
    """Очередь: ключи линка (переход уже идёт), затем аплинк, затем почта и
    фраза — аплинк держит связь агента с чатом."""
    priv, _ = keypair()
    _, vpub = keypair()
    from tests.unit.test_wgconf_allowed import LINK
    new_link = LINK.replace("DUMMY-LINK-PRIVATE-KEY=", priv).replace("DUMMY-VPS-PUBLIC-KEY=", vpub)
    assert svc.rekey_next_in({"id": "a1b2c3d4e5f6", "conf": new_link})["ask"] is True
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    assert svc.cfg_questions() == ["link", "uplink"], svc.cfg_questions()


def test_a_hand_edit_of_the_uplink_withdraws_the_question(svc, host):
    """Аплинк поправили руками, пока вопрос висел, — вопрос потерял смысл и
    снимается без памятки."""
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    host.uplink.write_text(rendered(_moved(host, "vps3.example.org:43125")), encoding="utf-8")
    assert svc.cfg_questions() == [] and svc.cfg_keep() == {}


def test_keeping_the_own_uplink_is_remembered_for_this_server_value(svc, host):
    """«Оставить своё»: серверу declined с отпечатком; тот же документ —
    declined без вопроса; новое серверное значение — новый вопрос."""
    value = _value(host, text=_moved(host))
    svc.apply_cfg(_doc(uplink=value))
    ans = svc.cfg_answer("uplink", False)
    ack = svc.cfg_answer_ack(ans)
    server = gwcfg.uplink_print(value["conf"], value["pubkey"], "")
    assert ack["declined"] == {"uplink": server} and ack["applied"] == [], ack
    assert host.txn_files() == [], "после отказа ключ устройства остался на диске"
    res = svc.apply_cfg(_doc(uplink=value))
    assert res["ack"]["declined"] == {"uplink": server} and res["ask"] == [] and svc.cfg_questions() == []
    newer = _value(host, text=_moved(host, "vps4.example.org:43125"))
    assert svc.apply_cfg(_doc(uplink=newer))["ask"] == ["uplink"]
    assert host.uplink_runs == []


# ── «Применить»: транзакция с пробой ─────────────────────────────────────────

def test_accepting_installs_the_uplink_and_the_keys_together(svc, host, monkeypatch):
    """«Применить»: конфиг ставит скрипт (--uplink с UPLINK_IF), ключи
    признания — в юнит той же транзакцией, аплинк дал хендшейк — копия
    прежнего убрана (--uplink-commit); вопрос снят, серверу — applied, файлов
    транзакции нет."""
    _Clock(monkeypatch)
    _, prev = keypair()
    value = _value(host, text=_moved(host), prev=prev)
    svc.apply_cfg(_doc(uplink=value))
    ans = svc.cfg_answer("uplink", True)
    assert ans["ok"] and ans["accepted"] and ans["note"] == {"state": "ok"} and not ans.get("failed"), ans
    assert svc.cfg_answer_ack(ans)["applied"] == ["uplink"]
    assert [(n, i) for n, _, i in host.uplink_runs] == [("uplink.next", "awg0")], host.uplink_runs
    assert host.finishes == ["commit"], host.finishes
    assert host.uplink.read_text(encoding="utf-8") == _installed_as(host, _moved(host))
    assert not host.before.exists(), "копия прежнего аплинка осталась после удачной пробы"
    assert host.unit_keys() == (host.dev_pub, prev)
    assert svc.cfg_questions() == [] and svc.cfg_wait() == {} and svc.cfg_pending() == {}
    assert host.txn_files() == [], "файлы транзакции с ключом устройства остались"


def test_the_whole_transaction_runs_under_the_setup_lock(svc, host, monkeypatch):
    """Каждый вызов скрипта в транзакции идёт, пока агент держит замок
    обвязки, и с AWG_GW_LOCK_HELD=1 — иначе скрипт ждал бы сам себя, а прогон
    юнита на загрузке мог бы вклиниться между ключами признания и конфигом.
    После транзакции замок свободен."""
    _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    svc.cfg_answer("uplink", True)
    assert host.held == [True, True], f"скрипт звали без замка обвязки: {host.held}"
    assert host.lock_env == ["1", "1"], host.lock_env
    assert host._lock_is_held() is False, "замок обвязки остался взятым после транзакции"


def test_an_uplink_without_a_handshake_in_a_minute_is_rolled_back(svc, host, monkeypatch):
    """Новый конфиг встал, но хендшейка за 60 с нет: прежний конфиг — назад
    режимом --uplink-revert байт в байт (из копии, без второй проверки),
    юнит — прежним текстом; серверу — failed с причиной, вопрос снят."""
    clock = _Clock(monkeypatch)
    _, prev = keypair()
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host), prev=prev)))
    host.comes_up = False
    ans = svc.cfg_answer("uplink", True)
    assert ans["failed"] == f"аплинк не поднялся за {txnmod.UPLINK_PROBE_SECONDS} с", ans
    assert ans["note"]["how"] == "reverted", ans["note"]
    assert [n for n, _, _ in host.uplink_runs] == ["uplink.next"] and host.finishes == ["revert"], (
        host.uplink_runs, host.finishes)
    assert host.uplink.read_bytes() == host.uplink_bytes, "на месте остался не поднявшийся аплинк"
    assert not host.before.exists()
    assert host.text() == host.unit_before, "ключи признания не вернулись"
    assert clock.slept >= txnmod.UPLINK_PROBE_SECONDS - 3, f"пробовали {clock.slept} с"
    assert svc.cfg_answer_ack(ans)["failed"] == {"uplink": ans["failed"]}
    assert host.txn_files() == []


def test_a_previous_uplink_that_does_not_come_back_is_reported_as_such(svc, host, monkeypatch):
    """Новый не поднялся, и прежний не поднялся тоже: итог — revert_failed,
    человеку — не «вернул прежний», а что прежний не вернулся."""
    _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    host.comes_up = False
    host.revert_rc = 1
    res = svc.cfg_uplink_apply(svc.cfg_pending()["uplink"])
    assert res["state"] == "revert_failed" and res["ok"] is False, res
    note = svc.cfg_uplink_fail_note(res)
    assert note.startswith("⚠️ Новый конфиг аплинка не поднялся, и прежний не вернулся:"), note
    assert host.text() == host.unit_before, "юнит не вернули, хотя аплинк не встал"


def test_a_unit_that_fails_to_start_is_reverted_by_the_script_itself(svc, host, monkeypatch):
    """Юнит аплинка с новым конфигом не стартовал — скрипт сам поставил
    прежний и убрал копию; агент зовёт --uplink-revert (он ответит
    «unchanged»), возвращает юнит; итог — reverted, причина — последняя строка."""
    _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host), prev=keypair()[1])))
    host.uplink_rc = 1
    ans = svc.cfg_answer("uplink", True)
    assert "откатываю на прежний" in ans["failed"] and ans["note"]["how"] == "reverted", ans
    assert len(host.uplink_runs) == 1 and host.finishes == ["revert"], host.finishes
    assert host.text() == host.unit_before and host.uplink.read_bytes() == host.uplink_bytes


def test_a_script_check_refusal_touches_nothing_and_is_not_a_revert(svc, host, monkeypatch):
    """Скрипт отверг конфиг проверкой (итог «uplink: refused»): на диске
    ничего не менялось — итог refused, возвращать нечего (--uplink-revert не
    зовётся), юнит — прежним; причина человеку и серверу — строка ОШИБКА, а
    не служебный итог."""
    _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host), prev=keypair()[1])))
    host.refuse = True
    ans = svc.cfg_answer("uplink", True)
    assert ans["note"]["how"] == "refused", ans
    assert "нет Table = off" in ans["failed"], f"причина отказа потеряна: {ans['failed']!r}"
    assert host.finishes == [], "звали откат там, где скрипт ничего не ставил"
    assert host.text() == host.unit_before and host.uplink.read_bytes() == host.uplink_bytes
    assert host.txn_files() == []


def test_a_write_failure_touches_neither_the_unit_nor_the_uplink(svc, host, monkeypatch):
    """Диск не принял конфиг транзакции — юнит прежний, скрипт не звали."""
    _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host), prev=keypair()[1])))
    value = svc.cfg_pending()["uplink"]

    def full(path, text):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(svc, "_cfg_write_private", full)
    res = svc.cfg_uplink_apply(value)
    assert res["state"] == "refused" and res["error"].startswith("конфиг аплинка не записан"), res
    assert host.uplink_runs == [] and host.finishes == [] and host.text() == host.unit_before


def test_without_a_known_uplink_nothing_is_installed(svc, host, monkeypatch):
    """Аплинк этого устройства не найден (нет в настройках, в статусе
    скрипта, среди интерфейсов) — ставить некуда: отказ с причиной."""
    monkeypatch.setattr(config, "GW_UPLINK_IF", "")
    res = svc.cfg_uplink_apply(_value(host, text=_moved(host)))
    assert res == {"ok": False, "error": "аплинк этого устройства не найден", "state": "refused"}, res
    assert host.uplink_runs == [] and host.text() == host.unit_before


def test_an_agent_busy_with_another_apply_answers_busy_at_once(svc, host, monkeypatch):
    """Агент применяет env+линк (до четверти часа) — «Применить» не ждёт его
    замка: сразу busy, ничего не тронуто, человек не смотрит на «⏳» минутами."""
    clock = _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    with gwbase._APPLY_LOCK:
        res = svc.cfg_uplink_apply(svc.cfg_pending()["uplink"])
    assert res["state"] == "busy" and res["ok"] is False, res
    assert clock.slept == 0, f"ждали замка агента {clock.slept} с"
    assert host.uplink_runs == [] and host.text() == host.unit_before


def test_a_busy_setup_leaves_the_question_and_touches_nothing(svc, host, monkeypatch):
    """Обвязку держит другой прогон дольше 5 минут: «Применить» не трогает ни
    юнит, ни аплинк, ответ — busy без отказа серверу, вопрос висит —
    человек нажмёт ещё раз."""
    _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    fd = host.hold_lock()
    try:
        ans = svc.cfg_answer("uplink", True)
    finally:
        os.close(fd)
    assert ans.get("busy") is True and not ans.get("failed"), ans
    assert svc.cfg_questions() == ["uplink"], "занятая обвязка сняла вопрос"
    assert (host.etc / "uplink.pending").exists(), "занятая обвязка выбросила ожидающий конфиг"
    assert host.uplink_runs == [] and host.text() == host.unit_before


# ── перезапуск агента посреди транзакции ─────────────────────────────────────

class _Killed(BaseException):
    """Агент убит посреди транзакции (systemctl restart, OOM)."""


def test_a_restart_in_the_middle_of_the_transaction_puts_the_old_uplink_back(svc, host, monkeypatch, tmp_path):
    """Агента убили после --uplink, до пробы: на диске новый конфиг, копия
    прежнего, новые ключи признания и журнал. Новый процесс на ближайшем тике
    видит журнал без идущей транзакции — прежний конфиг и прежний юнит
    назад, файлы транзакции прочь, человеку — одна строка; второй тик молчит."""
    _Clock(monkeypatch)
    _, prev = keypair()
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host), prev=prev)))
    real = gwguard.uplink_apply

    def install_then_die(path, iface, *a, **kw):
        real(path, iface, *a, **kw)
        raise _Killed()
    monkeypatch.setattr(gwguard, "uplink_apply", install_then_die)
    with pytest.raises(_Killed):
        svc.cfg_answer("uplink", True)
    monkeypatch.setattr(gwguard, "uplink_apply", real)
    assert host.unit_keys() == (host.dev_pub, prev) and (host.etc / "uplink.txn").exists(), "сценарий не тот"
    fresh = GatewayServices(svc.db)
    tick = fresh.cfg_uplink_tick()
    assert tick["notes"] == [RECOVERED], tick
    assert host.text() == host.unit_before, "после перезапуска остались новые ключи признания"
    assert host.uplink.read_bytes() == host.uplink_bytes and not host.before.exists()
    assert sorted(n for n in ("uplink.next", "uplink.txn") if (host.etc / n).exists()) == []
    assert fresh.cfg_uplink_tick()["notes"] == [], "о прерванной транзакции сказано дважды"
    assert fresh.cfg_questions() == ["uplink"], "вопрос пропал — человеку не на что ответить снова"


def test_a_document_after_an_interrupted_transaction_is_compared_with_the_restored_uplink(
        svc, host, monkeypatch):
    """Документ пришёл раньше тика после перезапуска: прерванная транзакция
    откатывается ДО сравнения — иначе полуновый аплинк на диске сравнился бы
    с серверным как «то же», и вопрос пропал бы при прежнем аплинке."""
    _Clock(monkeypatch)
    value = _value(host, text=_moved(host))
    svc.apply_cfg(_doc(uplink=value))
    real = gwguard.uplink_apply

    def install_then_die(path, iface, *a, **kw):
        real(path, iface, *a, **kw)
        raise _Killed()
    monkeypatch.setattr(gwguard, "uplink_apply", install_then_die)
    with pytest.raises(_Killed):
        svc.cfg_answer("uplink", True)
    monkeypatch.setattr(gwguard, "uplink_apply", real)
    fresh = GatewayServices(svc.db)
    res = fresh.apply_cfg(_doc(uplink=value))
    assert RECOVERED in res["note"], res["note"]
    assert host.uplink.read_bytes() == host.uplink_bytes and host.text() == host.unit_before
    assert res["ack"]["wait"] == {"uplink": gwcfg.uplink_print(value["conf"], value["pubkey"], "")}, (
        "полуновый аплинк сравнился как «то же»: " + str(res["ack"]))


def test_a_running_transaction_is_not_mistaken_for_an_interrupted_one(svc, host):
    """Журнал есть, но транзакция идёт в этом процессе (замок агента взят) —
    восстановление молчит и ничего не трогает."""
    (host.etc).mkdir(parents=True, exist_ok=True)
    (host.etc / "uplink.txn").write_text(json.dumps({"iface": "awg0", "unit": "чужой текст", "at": 0}),
                                          encoding="utf-8")
    with gwbase._APPLY_LOCK:
        assert svc.cfg_uplink_recover() == ""
    assert (host.etc / "uplink.txn").exists() and host.text() == host.unit_before


# ── «аплинк мёртв» — только по наблюдению ────────────────────────────────────

def test_the_first_dead_reading_is_not_an_emergency(svc, host, monkeypatch):
    """Первый же замер без хендшейка — не авария: окно только начинается.
    Документ, пришедший в эту секунду, — приглашение, а не молча."""
    _Clock(monkeypatch)
    host.uplink_age = None
    res = svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    assert res["ask"] == ["uplink"] and host.uplink_runs == [], res


@pytest.mark.parametrize("dead", [None, "absent", upmod.UPLINK_ALIVE_SECONDS + 1])
def test_ten_minutes_of_observed_silence_make_the_uplink_dead(svc, host, monkeypatch, dead):
    """Пир не отвечал ни разу, интерфейса нет, хендшейк старше 3 минут —
    всё это «без хендшейка»; мёртв аплинк только через 600 с монотонного
    времени подряд, не раньше."""
    clock = _Clock(monkeypatch)
    host.uplink_age = dead
    assert svc.cfg_uplink_observe() is False
    clock.t += upmod.UPLINK_DEAD_SECONDS - 1
    assert svc.cfg_uplink_observe() is False, "мёртв раньше 10 минут"
    clock.t += 1
    assert svc.cfg_uplink_observe() is True


def test_a_fresh_handshake_resets_the_window(svc, host, monkeypatch):
    clock = _Clock(monkeypatch)
    host.uplink_age = None
    svc.cfg_uplink_observe()
    clock.t += 500
    host.uplink_age = 10
    assert svc.cfg_uplink_observe() is False
    host.uplink_age = None
    clock.t += 200
    assert svc.cfg_uplink_observe() is False, "окно не началось заново после живого замера"


def test_a_failing_awg_show_neither_starts_nor_breaks_the_window(svc, host, monkeypatch):
    """`awg show` не ответил (интерфейс есть) — «не знаю»: сбой команды под
    нагрузкой не начинает окно (иначе 10 минут сбоев = замена аплинка без
    человека) и не обрывает уже идущее."""
    clock = _Clock(monkeypatch)
    host.uplink_age = "unknown"
    svc.cfg_uplink_observe()
    clock.t += upmod.UPLINK_DEAD_SECONDS * 2
    assert svc.cfg_uplink_observe() is False, "сбой awg show засчитан как мёртвый аплинк"
    host.uplink_age = None
    svc.cfg_uplink_observe()
    clock.t += 300
    host.uplink_age = "unknown"
    assert svc.cfg_uplink_observe() is False
    clock.t += 300
    assert svc.cfg_uplink_observe() is True, "сбой awg show оборвал идущее окно"


# ── аварийное правило ────────────────────────────────────────────────────────

def test_a_dead_uplink_takes_the_new_conf_without_asking_and_says_so(svc, host, monkeypatch):
    """Аплинк молчит дольше 10 минут по наблюдению — подтвердить в чате
    нечем: конфиг того же устройства ставится без вопроса, с той же пробой,
    и человеку уходит объяснение."""
    clock = _Clock(monkeypatch)
    host.uplink_age = None
    _dead_for(svc, clock, upmod.UPLINK_DEAD_SECONDS)
    _, prev = keypair()
    res = svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host), prev=prev)))
    assert "uplink" in res["ack"]["applied"] and res["ask"] == [] and not res["ack"]["wait"], res["ack"]
    assert res["note"] == EMERGENCY_SAME, res["note"]
    assert host.unit_keys() == (host.dev_pub, prev) and [n for n, _, _ in host.uplink_runs] == ["uplink.next"]
    assert host.finishes == ["commit"]


def test_an_uplink_alive_within_ten_minutes_still_asks(svc, host, monkeypatch):
    """Девять минут без хендшейка — ещё не авария: приглашение, а не молча."""
    clock = _Clock(monkeypatch)
    host.uplink_age = None
    _dead_for(svc, clock, upmod.UPLINK_DEAD_SECONDS - 60)
    res = svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    assert res["ask"] == ["uplink"] and host.uplink_runs == []


def test_an_emergency_conf_that_does_not_come_up_keeps_the_question(svc, host, monkeypatch):
    """Аварийный конфиг тоже под пробой: не поднялся — прежний назад, юнит
    прежний, человеку — «вернул прежний»; раздел не «отвергнут», а ждёт
    вопросом (причина может быть временной) — тик повторит попытку."""
    clock = _Clock(monkeypatch)
    host.uplink_age = None
    _dead_for(svc, clock, upmod.UPLINK_DEAD_SECONDS)
    host.comes_up = False
    value = _value(host, text=_moved(host))
    res = svc.apply_cfg(_doc(uplink=value))
    assert "uplink" not in res["ack"]["failed"] and "uplink" not in res["ack"]["applied"], res["ack"]
    assert res["ack"]["wait"] == {"uplink": gwcfg.uplink_print(value["conf"], value["pubkey"], "")}, res["ack"]
    assert REVERTED in res["note"], res["note"]
    assert host.finishes == ["revert"] and host.text() == host.unit_before
    assert host.uplink.read_bytes() == host.uplink_bytes
    assert svc.cfg_questions() == ["uplink"], "неудачная аварийная попытка сняла вопрос"


def test_a_declined_value_is_not_installed_even_when_the_uplink_is_dead(svc, host, monkeypatch):
    """Человек оставил своё — этот же серверный конфиг аварийно не ставится,
    сколько бы аплинк ни молчал."""
    clock = _Clock(monkeypatch)
    value = _value(host, text=_moved(host))
    svc.apply_cfg(_doc(uplink=value))
    svc.cfg_answer("uplink", False)
    host.uplink_age = None
    _dead_for(svc, clock, upmod.UPLINK_DEAD_SECONDS)
    res = svc.apply_cfg(_doc(uplink=value))
    assert "uplink" in res["ack"]["declined"] and host.uplink_runs == [], res["ack"]
    assert svc.cfg_uplink_tick() == {"answer": None, "notes": []}
    assert host.uplink_runs == []


# ── аварийное правило на тике ────────────────────────────────────────────────

def test_the_tick_installs_a_waiting_conf_once_the_uplink_has_been_dead_ten_minutes(svc, host, monkeypatch):
    """Финал переезда роняет аплинк и тут же рождает документ: при его
    приходе «10 минут» не набежали — вопрос. Тик через 10 минут молчания
    ставит его сам: ответ серверу — applied того документа, человеку —
    объяснение, вопрос и ожидающий файл сняты."""
    clock = _Clock(monkeypatch)
    value = _value(host, text=_moved(host))
    first = svc.apply_cfg(_doc(gen=9, uplink=value))
    assert first["ask"] == ["uplink"]
    host.uplink_age = None
    assert svc.cfg_uplink_tick() == {"answer": None, "notes": []}, "аварийно на первом же мёртвом замере"
    clock.t += upmod.UPLINK_DEAD_SECONDS
    tick = svc.cfg_uplink_tick()
    assert tick["notes"] == [EMERGENCY_SAME], tick
    ans = tick["answer"]
    assert ans and ans["accepted"] and ans["section"] == "uplink" and ans["gen"] == 9, ans
    assert svc.cfg_answer_ack(ans)["applied"] == ["uplink"]
    assert host.uplink.read_text(encoding="utf-8") == _installed_as(host, value["conf"])
    assert svc.cfg_questions() == [] and host.txn_files() == []
    assert svc.cfg_uplink_tick() == {"answer": None, "notes": []}, "второй тик снова что-то сделал"


def test_the_emergency_note_names_a_twin_device_when_the_key_changed(svc, host, monkeypatch):
    """Конфиг другого устройства (двойник в переезде) — пояснение, что его
    выдал этому шлюзу сервер, а не «ключ тот же»."""
    clock = _Clock(monkeypatch)
    twin_priv, twin_pub = keypair()
    value = {"conf": uplink_conf(twin_priv, host.srv_pub), "pubkey": twin_pub, "prev_pubkey": host.dev_pub}
    svc.apply_cfg(_doc(uplink=value))
    host.uplink_age = None
    svc.cfg_uplink_tick()
    clock.t += upmod.UPLINK_DEAD_SECONDS
    assert svc.cfg_uplink_tick()["notes"] == [EMERGENCY_TWIN]


def test_a_failed_emergency_attempt_on_the_tick_waits_ten_minutes_before_the_next(svc, host, monkeypatch):
    """Аварийная попытка не удалась — заметка человеку, вопрос висит; на
    следующих тиках раньше 10 минут — ни попытки, ни заметки; через 10 минут
    — снова."""
    clock = _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    host.uplink_age = None
    svc.cfg_uplink_tick()
    clock.t += upmod.UPLINK_DEAD_SECONDS
    host.comes_up = False
    tick = svc.cfg_uplink_tick()
    attempt = clock.t                                      # отсчёт — от конца неудачной попытки (с пробой)
    assert tick["answer"] is None and tick["notes"] == [REVERTED], tick
    assert svc.cfg_questions() == ["uplink"]
    runs = len(host.uplink_runs)
    host.uplink_age = None                                 # прежний «вернулся», но так и молчит
    clock.t = attempt + txnmod.EMERGENCY_RETRY_SECONDS - 10
    assert svc.cfg_uplink_tick() == {"answer": None, "notes": []}
    assert len(host.uplink_runs) == runs, "повтор аварийной попытки раньше 10 минут"
    clock.t += 10
    host.comes_up = True
    tick = svc.cfg_uplink_tick()
    assert tick["notes"] == [EMERGENCY_SAME] and len(host.uplink_runs) == runs + 1, tick


def test_a_busy_setup_on_the_tick_is_silent_and_retried_on_the_next_tick(svc, host, monkeypatch):
    """Тик не ждёт замка обвязки: занята — ни попытки, ни заметки, и 10
    минут до следующей попытки не отсчитываются — следующий же тик пробует."""
    clock = _Clock(monkeypatch)
    svc.apply_cfg(_doc(uplink=_value(host, text=_moved(host))))
    host.uplink_age = None
    svc.cfg_uplink_tick()
    clock.t += upmod.UPLINK_DEAD_SECONDS
    fd = host.hold_lock()
    try:
        assert svc.cfg_uplink_tick() == {"answer": None, "notes": []}
    finally:
        os.close(fd)
    assert host.uplink_runs == [] and host.text() == host.unit_before
    assert clock.slept < 5, f"тик ждал замка {clock.slept} с"
    clock.t += 10
    assert svc.cfg_uplink_tick()["notes"] == [EMERGENCY_SAME], "после занятой обвязки попытка ждёт 10 минут"


def test_the_tick_marks_an_old_unit_for_the_comparison(svc, host):
    """Юнит старого образца (без строки AWG_GW_UNIT_RUN=1) получает её на
    первом же такте агента — иначе сверка прогона юнита с диском не
    сработает до первого полного прогона после обновления; есть — не трогается."""
    host.path.write_text(host.text().replace("Environment=AWG_GW_UNIT_RUN=1\n", ""), encoding="utf-8")
    assert "AWG_GW_UNIT_RUN" not in host.text(), "модель: юнит уже размечен"
    svc.cfg_uplink_tick()
    assert gwguard.unit_env("AWG_GW_UNIT_RUN") == "1", host.text()
    marked = host.text()
    svc.cfg_uplink_tick()
    assert host.text() == marked


def test_the_tick_without_a_question_does_nothing_even_when_the_uplink_is_dead(svc, host, monkeypatch):
    clock = _Clock(monkeypatch)
    host.uplink_age = None
    svc.cfg_uplink_tick()
    clock.t += upmod.UPLINK_DEAD_SECONDS * 2
    assert svc.cfg_uplink_tick() == {"answer": None, "notes": []}
    assert host.uplink_runs == []


# ── сторона ВПС ──────────────────────────────────────────────────────────────

from tests.integration import test_gwcfg_delivery as _srv            # noqa: E402

srv_host = _srv.host


@pytest.fixture()
def real_keys(fake_awg, monkeypatch):
    """Ключи устройств — настоящие X25519: отпечаток пары считается от
    приватного ключа в конфиге."""
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "gen_keypair", keypair)
    monkeypatch.setattr(awg, "pubkey_of", wgconf.pubkey_of)


@pytest.fixture()
def uslot(real_keys, services, fake_awg, fake_routing, make_active_client, monkeypatch, srv_host):
    """Слот 1 (как в тесте доставки документа) с настоящими ключами устройства."""
    return _srv.slot.__wrapped__(services, fake_awg, fake_routing, make_active_client, monkeypatch, srv_host)


def _caps(services, *caps):
    from awgbot.util import gwlink
    services.gwlink_session_opened(1, "3.3.0", gwlink.PROTO, caps=caps)


def test_the_server_sends_the_uplink_only_to_agents_that_can_confirm(services, uslot):
    """Агенту без confirm1 раздела нет: он не спросил бы человека, а молча
    ставить аплинк нельзя. С confirm1 — конфиг устройства в форме для шлюза
    и ключ признания; прежнего ключа вне переезда нет."""
    _caps(services, "cfg1")
    assert services.gwcfg_uplink(uslot) is None
    _caps(services, "cfg1", "confirm1")
    up = services.gwcfg_uplink(uslot)
    dev = services.db.get_device(uslot.device_id)
    assert up == {"conf": services.gateway_uplink_conf(dev), "pubkey": dev.public_key, "prev_pubkey": ""}, up
    conf = wgconf.parse(up["conf"])
    assert conf.get("Interface", "Table").lower() == "off" and not conf.get("Interface", "DNS")
    assert gwcfg.check_uplink(up) is not None, "агент отверг бы аплинк, который шлёт сервер"


def test_during_the_move_the_slot_gets_the_twin_uplink_and_the_old_key(services, uslot, mig):
    """Окно переезда: желаемый аплинк — двойника (новый ключ), прежний ключ
    — отдельно: обвязка признаёт оба, пока шлюз не подтвердил новый. Финал
    до подтверждения — шлюз в списке предупреждения диалога финала."""
    _caps(services, "cfg1", "confirm1")
    services.migration_start()
    dev = services.db.get_device(uslot.device_id)
    twin = services.db.twin_of_device(dev.id)
    assert twin is not None, "у устройства шлюза нет двойника"
    up = services.gwcfg_uplink(uslot)
    assert up["pubkey"] == twin.public_key and up["prev_pubkey"] == dev.public_key, up
    assert wgconf.pubkey_of(wgconf.parse(up["conf"]).get("Interface", "PrivateKey")) == twin.public_key
    old = services.gwcfg_old_uplink()
    assert [name for name, _ in old] == [services._gw_display(uslot)], old
    services.db.update_device_fields(twin.id, last_handshake=int(time.time()))
    assert services.gwcfg_old_uplink() == [], "шлюз уже на двойнике, а финал его пугает"


def test_old_uplink_warning_skips_agents_that_cannot_confirm(services, uslot, mig):
    _caps(services, "cfg1")
    services.migration_start()
    assert services.gwcfg_old_uplink() == []


def test_the_slot_card_shows_the_fingerprint_of_the_waiting_uplink(services, uslot):
    """Карточка слота показывает отпечаток пары ключей аплинка, который ждёт
    «Применить» в чате агента, — тот же, что агент пишет в приглашении."""
    _caps(services, "cfg1", "confirm1")
    doc = services.gwcfg_doc(uslot)
    snap = {"cfg": {"gen": doc["gen"], **doc["prints"]}}
    assert services.gwcfg_card(uslot, snap)["uplink_fp"] == ""
    services.gwcfg_ack_in(1, gwcfg.ack_build(doc["gen"], doc["hash"], wait={"uplink": doc["prints"]["uplink"]}))
    card = services.gwcfg_card(uslot, snap)
    conf = wgconf.parse(doc["sections"]["uplink"]["conf"])
    dev = services.db.get_device(uslot.device_id)
    assert card["confirm1"] is True and card["wait"] == ["uplink"]
    assert card["uplink_fp"] == gwcfg.key_fp(dev.public_key, conf.get("Peer", "PublicKey")) != "", card


def test_an_uplink_the_agent_would_refuse_is_not_sent_and_the_card_says_why(services, uslot, monkeypatch):
    """Раздел, который агент отверг бы проверкой, не едет: отказ одного
    раздела агент считает отказом документа целиком — env, линк, почта
    встали бы тоже. Причина — в карточке слота; починили — причина стёрта."""
    _caps(services, "cfg1", "confirm1")
    good = services.gateway_uplink_conf
    monkeypatch.setattr(services, "gateway_uplink_conf",
                        lambda dev: good(dev).replace("[Peer]", "DNS = 10.8.1.1\n\n[Peer]", 1))
    assert services.gwcfg_uplink(uslot) is None, "сервер шлёт аплинк, который агент отвергнет"
    doc = services.gwcfg_doc(uslot)
    assert "uplink" not in gwcfg.build(doc["gen"], doc["hash"], doc["sections"]), "раздел уехал бы агенту"
    card = services.gwcfg_card(uslot, {"cfg": {"gen": doc["gen"]}})
    assert "DNS" in card["uplink_err"], card["uplink_err"]
    monkeypatch.setattr(services, "gateway_uplink_conf", good)
    assert services.gwcfg_uplink(uslot) is not None
    assert services.gwcfg_card(uslot, {"cfg": {"gen": doc["gen"]}})["uplink_err"] == "", "причина не стёрта"


def test_a_refusal_in_the_gateway_chat_tells_the_admin_once_per_value(services, uslot):
    """Н5: в ответе шлюза впервые появился отказ от аплинка с этим отпечатком —
    одно уведомление; тот же ответ снова (повтор после переподключения) —
    тишина; отказ от нового значения — снова одно."""
    _caps(services, "cfg1", "confirm1")
    doc = services.gwcfg_doc(uslot)
    fp = doc["prints"]["uplink"]
    _, notes = services.gwcfg_ack_take(1, gwcfg.ack_build(doc["gen"], doc["hash"], declined={"uplink": fp}))
    assert [n.text for n in notes] == [
        f"🔐 {services._gw_link(uslot)}: новый конфиг аплинка отклонили в чате бота шлюза"], notes
    _, again = services.gwcfg_ack_take(1, gwcfg.ack_build(doc["gen"], doc["hash"], declined={"uplink": fp}))
    assert again == [], "отказ от того же аплинка объявлен дважды"
    # ответ заменён временным отказом и ответом на другой документ — память о сказанном не теряется
    services.gwcfg_ack_take(1, gwcfg.ack_build(doc["gen"], doc["hash"], retry=True, failed={"env": "занято"}))
    _, after_retry = services.gwcfg_ack_take(1, gwcfg.ack_build(doc["gen"] + 1, "ab" * 8, declined={"uplink": fp}))
    assert after_retry == [], "после retry отказ от того же аплинка объявлен снова"
    _, other = services.gwcfg_ack_take(1, gwcfg.ack_build(doc["gen"], doc["hash"], declined={"uplink": "f" * 16}))
    assert len(other) == 1, other
    _, none = services.gwcfg_ack_take(1, gwcfg.ack_build(doc["gen"], doc["hash"], applied=["uplink"]))
    assert none == [], "уведомление без отказа"


def test_every_obfuscation_key_of_a_client_conf_is_allowed_in_the_uplink():
    """Сторож: конфиг аплинка — клиентский конфиг из configgen; ключ
    обфускации, который configgen печатает, а белый список аплинка не знает,
    — агент и скрипт отвергнут аплинк каждого шлюза, как только сервер AWG
    начнёт его задавать."""
    from awgbot.domain import configgen
    allowed = {line.split()[2] for line in wgconf.ALLOW_LIST.read_text(encoding="utf-8").splitlines()
               if line.split()[:2] == ["uplink", "Interface"]}
    missing = [k for k in configgen._OBF_ORDER if k not in allowed]
    assert missing == [], f"нет в белом списке аплинка: {missing}"


@pytest.mark.parametrize("table", ["Off", "OFF", "auto", "100", None])
def test_the_agent_check_wants_table_off_exactly_as_the_script_does(table):
    """Скрипт обвязки ищет строку `Table = off` буквально; агент, принявший
    «Off», обещал бы серверу то, что скрипт отвергнет («не установлен»), — и
    аварийное правило вечно билось бы в этот отказ."""
    priv, pub = keypair()
    _, srv = keypair()
    text = uplink_conf(priv, srv)
    text = text.replace("Table = off\n", "" if table is None else f"Table = {table}\n")
    with pytest.raises(gwcfg.CfgError, match="Table = off"):
        gwcfg.check_uplink({"conf": text, "pubkey": pub, "prev_pubkey": ""})
    assert gwcfg.check_uplink({"conf": uplink_conf(priv, srv), "pubkey": pub, "prev_pubkey": ""}) is not None
