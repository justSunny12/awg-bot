"""util/kvfile.parse — один разбор KEY=VALUE у всех читателей вывода скриптов и
файлов состояния.

Что стережём: `--status`/`--plan` зоны резерва, зондов кворума и линка шлюза к
резерву, `systemctl show` юнита обвязки, статус скрипта списков шлюза, вывод
скрипта линка пары и install/awg.lock кита разбираются одинаково: комментарии,
пустые строки, строки без «=» и строки, где слева от «=» не ключ (пробел внутри —
«ВНИМАНИЕ: …=…» скрипта), пропускаются; «=» в значении сохраняется; повтор
ключа — последнее значение.

Цена ошибки: копии расходились — одна брала «# x=1» ключом «# x», другая
резала строку предупреждения в ключ с пробелами; читатель, полагавшийся на
свою копию, видел поле, которого скрипт не печатал (или терял «=» в пути и
ключе), а правка разбора в одном месте не доходила до остальных.
"""
from __future__ import annotations

import types

import pytest

from awgbot.util import kvfile

pytestmark = pytest.mark.unit

# вывод, в котором есть всё, на чём копии расходились
OUT = ("# шапка=не ключ\n"
       "\n"
       "ВНИМАНИЕ: конфиг awg1 не найден, MTU=1420\n"
       "=пустой ключ\n"
       "строка без знака\n"
       "  link = up  \n"
       "src=10.0.0.1\n"
       "url=https://x/?a=1&b=2\n"
       "open=0\n"
       "open=1\n")
WANT = {"link": "up", "src": "10.0.0.1", "url": "https://x/?a=1&b=2", "open": "1"}


def test_parse_skips_everything_that_is_not_a_key_line():
    assert kvfile.parse(OUT) == WANT
    assert kvfile.parse("") == {} and kvfile.parse(None) == {}
    assert kvfile.parse('Q="в кавычках"', strip_quotes=True) == {"Q": "в кавычках"}


def test_read_is_parse_of_the_file(tmp_path):
    p = tmp_path / "state"
    p.write_text(OUT, encoding="utf-8")
    assert kvfile.read(p) == WANT
    assert kvfile.read(tmp_path / "nope") == {}


# ── читатели: тот же вывод — тот же словарь ─────────────────────────────────

def test_zone_status_and_plan(monkeypatch):
    from awgbot.infra import rsvzone
    monkeypatch.setattr(rsvzone, "run", lambda mode, **kw: OUT)
    assert rsvzone.status() == WANT and rsvzone.plan() == WANT


def test_reserve_link_status_on_the_gateway(monkeypatch):
    from awgbot.infra.gwguard import rsv
    monkeypatch.setattr(rsv, "run", lambda mode, *a, **kw: OUT)
    assert rsv.status() == WANT


def test_quorum_probe_status_ignores_a_warning_with_an_equals_sign(monkeypatch):
    """Строка «ВНИМАНИЕ: … hs_awgq0=…» — не поле: интерфейсы и рукопожатия — только
    из строк-ключей."""
    from awgbot.infra import quoprobe
    out = "ВНИМАНИЕ: старый ns hs_awgq1=5\nns=present\nifaces=awgq0\nhs_awgq0=12\n"
    monkeypatch.setattr(quoprobe, "run", lambda mode, *a, **kw: out)
    assert quoprobe.status() == {"ns": "present", "ifaces": ["awgq0"], "hs": {"awgq0": 12}}


def test_gateway_unit_state(monkeypatch):
    from awgbot.infra import gwguard
    monkeypatch.setattr(gwguard.subprocess, "run",
                        lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout=OUT.encode(), stderr=b""))
    assert gwguard.unit_state() == WANT


def test_gateway_lists_status(tmp_path, monkeypatch):
    from awgbot.infra import gwguard
    p = tmp_path / "lists.status"
    p.write_text(OUT, encoding="utf-8")
    monkeypatch.setattr(gwguard, "LAN_STATUS_FILE", str(p))
    assert gwguard.lists_status() == WANT
    monkeypatch.setattr(gwguard, "LAN_STATUS_FILE", str(tmp_path / "nope"))
    assert gwguard.lists_status() == {}


def test_pair_link_script_output(monkeypatch):
    from awgbot.domain.services.reserve import invite
    monkeypatch.setattr(invite.subprocess, "run",
                        lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout=OUT.encode(), stderr=b""))
    me = types.SimpleNamespace(_pair_script=lambda: "/nonexistent/reserve-pair-setup.sh")
    assert invite.ReserveInviteMixin._run_pair_script(me, "--status") == WANT


def test_the_gateway_kit_lock(tmp_path):
    from tools import gwkit
    (tmp_path / "install").mkdir()
    (tmp_path / "install" / "awg.lock").write_text(OUT, encoding="utf-8")
    assert gwkit._lock(tmp_path) == WANT
    with pytest.raises(gwkit.KitError, match="нет install/awg.lock"):
        gwkit._lock(tmp_path / "nope")


# ── одна строка KEY= в env: get_line / set_line ─────────────────────────────
# Три копии «прочие строки как были, своя — в конец» (токен агента с сервера,
# ADMIN_ID сопряжения, токены ботов шлюзов по слотам) и два читателя. Цена
# ошибки: потерянная соседняя строка env — бот не стартует без BOT_TOKEN;
# env шире 0600 — токен читает любой пользователь хоста; запись поверх
# недочитанного файла — env теряет всё, кроме одной строки.

ENV = "BOT_TOKEN=111:AAA\n# GW_BOT_TOKEN=закомментирован\nADMIN_ID=42\nGW_BOT_TOKEN=old\nGW_BOT_TOKEN=older\n"


def _mode(p) -> int:
    import os
    import stat
    return stat.S_IMODE(os.stat(p).st_mode)


def test_set_line_replaces_every_copy_and_keeps_the_rest(tmp_path):
    import os
    p = tmp_path / "env"
    p.write_text(ENV, encoding="utf-8")
    os.chmod(p, 0o644)
    assert kvfile.set_line(p, "GW_BOT_TOKEN", "new") is True
    assert p.read_text(encoding="utf-8") == ("BOT_TOKEN=111:AAA\n# GW_BOT_TOKEN=закомментирован\nADMIN_ID=42\n"
                                             "GW_BOT_TOKEN=new\n")
    assert _mode(p) == 0o600, "env остался читаемым всем"
    assert kvfile.get_line(p, "GW_BOT_TOKEN") == "new"


def test_set_line_none_removes_the_key_and_a_missing_file_stays_missing(tmp_path):
    p = tmp_path / "env"
    p.write_text(ENV, encoding="utf-8")
    assert kvfile.set_line(p, "GW_BOT_TOKEN", None) is True
    assert "GW_BOT_TOKEN=" not in p.read_text(encoding="utf-8").replace("# GW_BOT_TOKEN=", "")
    assert kvfile.set_line(tmp_path / "nope", "K", None) is False
    assert not (tmp_path / "nope").exists(), "удаление ключа создало пустой env"
    assert kvfile.set_line(tmp_path / "new" / "env", "K", "v") is True
    assert (tmp_path / "new" / "env").read_text(encoding="utf-8") == "K=v\n"


def test_set_line_on_an_unreadable_env_raises_and_writes_nothing(tmp_path):
    """Каталог вместо файла (или нет прав на чтение) — исключение, а не env из
    одной строки поверх прежнего."""
    d = tmp_path / "env"
    d.mkdir()
    with pytest.raises(OSError):
        kvfile.set_line(d, "K", "v")
    assert d.is_dir() and list(d.iterdir()) == []


def test_get_line_takes_the_first_line_from_the_start(tmp_path):
    p = tmp_path / "env"
    p.write_text(ENV, encoding="utf-8")
    assert kvfile.get_line(p, "GW_BOT_TOKEN") == "old", "закомментированная строка или не первая"
    assert kvfile.get_line(p, "NOPE") == "" and kvfile.get_line(tmp_path / "nope", "K") == ""
    assert kvfile.line_value(" ADMIN_ID=1\nADMIN_ID= 7 \n", "ADMIN_ID") == "7"


# ── те, кто пишет и читает env ──────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text(ENV, encoding="utf-8")
    monkeypatch.setenv("AWG_BOT_ENV", str(p))
    return p


def test_the_agent_token_from_the_server_keeps_the_other_lines(env):
    from awgbot.domain.gateway.cfg.token import CfgTokenMixin
    CfgTokenMixin()._cfg_token_write("222:BBB")
    text = env.read_text(encoding="utf-8")
    assert text.endswith("BOT_TOKEN=222:BBB\n") and "BOT_TOKEN=111:AAA" not in text
    assert "ADMIN_ID=42\n" in text and "GW_BOT_TOKEN=old\n" in text and _mode(env) == 0o600


def test_the_join_admin_id_keeps_the_other_lines(env):
    from awgbot.domain.gateway.cfg.token import CfgTokenMixin
    from awgbot.domain.gateway.join import GatewayJoinMixin

    class _Svc(GatewayJoinMixin, CfgTokenMixin):
        pass
    _Svc()._join_env_set("ADMIN_ID", "77")
    text = env.read_text(encoding="utf-8")
    assert text.endswith("ADMIN_ID=77\n") and "ADMIN_ID=42" not in text and "BOT_TOKEN=111:AAA\n" in text


def test_gateway_bot_tokens_by_slot(env, monkeypatch):
    from awgbot.domain.services.gateway_link.agentbot import AgentBotMixin
    from awgbot.runtime import gwbotme
    monkeypatch.setattr(gwbotme, "forget", lambda slot: None)
    svc = AgentBotMixin()
    assert svc.gw_bot_token(1) == "old"
    tok = "333333333:CC-third-token-value-long-enough"
    svc.set_gw_bot_token(tok, 2)
    assert svc.gw_bot_token(2) == tok and svc.gw_bot_token(1) == "old"
    svc.forget_gw_bot_token(1)
    text = env.read_text(encoding="utf-8")
    assert "\nGW_BOT_TOKEN=" not in text and f"GW_BOT_TOKEN_2={tok}\n" in text and "BOT_TOKEN=111:AAA\n" in text
    assert _mode(env) == 0o600


def test_forgetting_a_token_without_env_creates_nothing(tmp_path, monkeypatch):
    from awgbot.domain.services.gateway_link.agentbot import AgentBotMixin
    monkeypatch.setenv("AWG_BOT_ENV", str(tmp_path / "env"))
    AgentBotMixin().forget_gw_bot_token(3)
    assert not (tmp_path / "env").exists()


def test_the_admin_id_of_a_backup_env():
    from awgbot.domain.backup import meta
    assert meta.env_admin_id(b"BOT_TOKEN=1\nADMIN_ID=\"42\"\n") == "42"
    assert meta.env_admin_id("# ADMIN_ID=1\nADMIN_ID='7'\n") == "7"
    assert meta.env_admin_id(None) == "" and meta.env_admin_id("X=1") == ""
