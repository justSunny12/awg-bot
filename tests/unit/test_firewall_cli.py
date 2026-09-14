"""
CLI файервола: неинтерактивный мастер, которым установщик включает фильтр, и
сообщения в чат вокруг таймера отката.

Раньше всё это проверялось чтением исходника — то есть переживало бы любую
поломку поведения. А путь ровно один и исполняется ровно один раз, на чужом
хосте, в момент, когда человек решает ограничить себе SSH.
"""
from __future__ import annotations

import pytest

from awgbot.infra import nftguard
from tools import firewall as fw


@pytest.fixture()
def cli(monkeypatch):
    """nftguard и настройки — в памяти; возвращает (store, acts)."""
    from awgbot.core import settings
    store = {"app.firewall.enabled": False, "app.firewall.ssh_allow": [],
             "app.firewall.open_tcp": [], "app.firewall.open_udp": []}
    acts: list = []
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(store.get(k, d)))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    monkeypatch.setattr(fw, "_admin_ips", lambda: ["10.8.1.2"])
    monkeypatch.setattr(fw, "_ensure_nft", lambda: True)
    monkeypatch.setattr(nftguard, "build_spec", lambda ips: nftguard.GuardSpec(ssh_port=22))
    monkeypatch.setattr(nftguard, "render", lambda spec: "TABLE")
    monkeypatch.setattr(nftguard, "ensure_persistence", lambda: [])
    monkeypatch.setattr(nftguard, "apply_text", lambda t: acts.append(("apply", t)))
    monkeypatch.setattr(nftguard, "arm_rollback",
                        lambda sec, cmd, env: acts.append(("arm", sec, cmd, env)))
    monkeypatch.setattr(nftguard, "disarm_rollback", lambda: acts.append(("disarm",)) or True)
    monkeypatch.setattr(nftguard, "remove", lambda: acts.append(("remove",)) or ["снято"])
    monkeypatch.setattr(nftguard, "ufw_active", lambda: False)
    # tgsend импортируется лениво внутри функций — подменяем сам модуль
    from awgbot.infra import tgsend
    monkeypatch.setattr(tgsend, "send", lambda *a, **k: acts.append(("chat", a, k)) or True)
    monkeypatch.setattr(fw, "_tty_input", lambda p: pytest.fail(f"спросили в неинтерактиве: {p}"))
    return store, acts


def test_setup_with_allow_and_yes_asks_nothing(cli):
    """Установщик зовёт мастер из трубы: любой вопрос там уходит в никуда."""
    store, acts = cli
    assert fw.cmd_setup(["--allow", "203.0.113.7, home.example.org", "--yes"]) == 0
    # CLI нормализует записи так же, как таблица: адрес → /32, имя как есть
    assert store["app.firewall.ssh_allow"] == ["203.0.113.7/32", "home.example.org"]
    assert store["app.firewall.enabled"] is True
    assert [a[0] for a in acts if a[0] in ("apply", "arm")] == ["apply", "arm"]


def test_rollback_seconds_reaches_the_timer(cli):
    """Во время установки человек ещё занят: три минуты на «проверь вход» мало,
    и перестань ключ доезжать — правила откатятся посреди процесса."""
    store, acts = cli
    assert fw.cmd_setup(["--allow", "203.0.113.7", "--yes", "--rollback-seconds", "900"]) == 0
    arm = next(a for a in acts if a[0] == "arm")
    assert arm[1] == 900
    assert "tools.firewall" in " ".join(arm[2]) and arm[2][-1] == "rollback"


def test_bad_rollback_seconds_is_refused(cli):
    store, acts = cli
    assert fw.cmd_setup(["--allow", "203.0.113.7", "--yes", "--rollback-seconds", "мусор"]) == 1
    assert acts == [] and store["app.firewall.enabled"] is False


def test_empty_allow_is_an_explicit_choice(cli):
    """Пустой список — осознанно открытый SSH (вход только по ключам), а не
    повод отказаться: иначе установка на хосте без статического адреса
    упиралась бы в тупик."""
    store, acts = cli
    assert fw.cmd_setup(["--allow", "", "--yes"]) == 0
    assert store["app.firewall.ssh_allow"] == [] and store["app.firewall.enabled"] is True
    assert any(a[0] == "apply" for a in acts)


def test_bad_address_leaves_the_firewall_off(cli):
    store, acts = cli
    assert fw.cmd_setup(["--allow", "мусор", "--yes"]) == 1
    assert store["app.firewall.enabled"] is False and acts == []


def test_arming_sends_two_buttons_to_the_chat(cli):
    """Правила могли отрезать именно тот SSH, из которого их применяли. Кнопка
    приходит в чат сама — и та же, что рисует бот."""
    from awgbot.bot import keyboards as kb
    store, acts = cli
    fw._arm(300)
    chat = [a for a in acts if a[0] == "chat"]
    assert len(chat) == 1
    text, kwargs = chat[0][1][0], chat[0][2]
    assert "проверка входа" in text.lower() or "проверь" in text.lower()
    buttons = dict((t, d) for t, d in kwargs["buttons"])
    drawn = {b.text: b.callback_data
             for row in kb.settings_firewall({"rollback": True}).inline_keyboard for b in row}
    assert set(buttons.values()) == {drawn[t] for t in buttons}, \
        "кнопка из CLI уйдёт в другой обработчик, чем кнопка из бота"


def test_rollback_removes_the_table_and_tells_the_admin(cli):
    """Молчаливый откат — худший исход: человек уверен, что файервол включён."""
    store, acts = cli
    store["app.firewall.enabled"] = True
    assert fw.cmd_rollback([]) == 0
    kinds = [a[0] for a in acts]
    assert kinds.index("remove") < kinds.index("chat"), "сначала снять, потом рассказывать"
    assert store["app.firewall.enabled"] is False
    said = next(a for a in acts if a[0] == "chat")[1][0]
    assert "откат" in said.lower() and "SSH" in said


def test_off_disarms_before_removing(cli):
    store, acts = cli
    store["app.firewall.enabled"] = True
    assert fw.cmd_off([]) == 0
    assert [a[0] for a in acts][:2] == ["disarm", "remove"]
    assert store["app.firewall.enabled"] is False
