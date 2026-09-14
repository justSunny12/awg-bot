"""
Поколения AmneziaWG: манифест против состояния хоста, блокировка обновления,
хэштег поколения в релизе, протокол по интерфейсу (docs/ROADMAP.md, п.8).

Поколение растёт редко и ломает совместимость: ошибка здесь либо оставляет
клиентов на ядре, которое их больше не обслуживает, либо размазывает профили
по трём интерфейсам, откуда их нечем мигрировать.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.infra import awglock, updates


@pytest.fixture()
def lockdir(tmp_path, monkeypatch):
    """Манифест поставки и файл состояния хоста — во временном каталоге."""
    lock = tmp_path / "awg.lock"
    lock.write_text("AWG_GENERATION=2\nAWG_PROTOCOL_ID=amnezia-awg3\n"
                    "AWG_MODULE_TAG=v9.9.1\nAWG_MODULE_VERSION=9.9.1\n", encoding="utf-8")
    monkeypatch.setattr(awglock, "LOCK_PATH", lock)
    monkeypatch.setattr(awglock, "STATE_PATH", tmp_path / "awg.state")
    return tmp_path


# ── манифест и состояние ─────────────────────────────────────────────────────

def test_missing_state_adopts_the_delivery_generation(lockdir):
    """Файла состояния ещё нет — хост считается стоящим на поколении поставки.
    Верно по построению: обновления идут по одной ступени, значит поставка,
    вводящая файл, приезжает раньше той, что меняет поколение."""
    assert awglock.generation() == 2
    assert awglock.applied_generation() == 2 and not awglock.needs_migration()


def test_needs_migration_when_delivery_moves_ahead(lockdir):
    awglock.write_state(applied=1)
    assert awglock.applied_generation() == 1 and awglock.needs_migration()
    assert awglock.target_generation() == 1, "цели переезда ещё нет — равна применённой"
    awglock.write_state(target=2)
    assert awglock.target_generation() == 2


def test_finishing_migration_clears_the_target(lockdir):
    awglock.write_state(applied=1, target=2)
    awglock.write_state(applied=2, target=0)
    assert awglock.applied_generation() == 2 and awglock.target_generation() == 2
    assert not awglock.needs_migration()
    assert "TARGET" not in (lockdir / "awg.state").read_text(encoding="utf-8")


def test_no_manifest_means_no_generation_management(tmp_path, monkeypatch):
    """Поставки до v2.10.0 манифеста не возят: поколениями не управляем вовсе,
    а не считаем их нулевыми и не требуем переезда на каждом старте."""
    monkeypatch.setattr(awglock, "LOCK_PATH", tmp_path / "нет.lock")
    monkeypatch.setattr(awglock, "STATE_PATH", tmp_path / "awg.state")
    assert awglock.generation() == 0 and not awglock.needs_migration()
    assert not awglock.blocks_update(0, True)


# ── блокировка обновления ────────────────────────────────────────────────────

def test_update_blocked_only_past_the_migration_target(lockdir):
    awglock.write_state(applied=1, target=2)
    assert not awglock.blocks_update(2, migration_running=True), "цель переезда — можно"
    assert not awglock.blocks_update(3, migration_running=False), "переезда нет — можно"
    assert awglock.blocks_update(3, migration_running=True), "дальше цели — нельзя"


def test_release_generation_comes_from_the_body_hashtag():
    """Поколение надо знать ДО скачивания: в ассете оно лежит внутри архива."""
    def rel(body):
        return updates.Release(tag="v3.0.0", version=(3, 0, 0), body=body,
                               asset_url=None, sha256=None)
    assert rel("#all_bots\n#awg_gen2\n- ядро новее").awg_generation() == 2
    assert rel("#main_bot\nобычный релиз").awg_generation() == 0
    assert rel("").awg_generation() == 0


def test_service_refuses_to_apply_a_blocked_update(tmp_path, monkeypatch):
    from awgbot.domain.selfupdate import SelfUpdateMixin

    class Svc(SelfUpdateMixin):
        def __init__(self):
            self.db = None
        def migration_running(self):
            return True

    monkeypatch.setattr(awglock, "LOCK_PATH", tmp_path / "awg.lock")
    monkeypatch.setattr(awglock, "STATE_PATH", tmp_path / "awg.state")
    awglock.write_state(applied=1, target=2)
    svc = Svc()
    rel = updates.Release(tag="v3.0.0", version=(3, 0, 0), body="#awg_gen3",
                          asset_url="u", sha256="x")
    reason = svc.update_block_reason(rel)
    assert "переезд" in reason and "v3.0.0" in reason
    with pytest.raises(updates.UpdateError):
        svc.apply_update(rel)
    ok = updates.Release(tag="v2.9.9", version=(2, 9, 9), body="#awg_gen2",
                         asset_url="u", sha256="x")
    assert svc.update_block_reason(ok) == "", "поставка на цель переезда не блокируется"


# ── протокол по интерфейсу ───────────────────────────────────────────────────

def test_protocol_id_is_per_interface_in_the_migration_window(lockdir, monkeypatch):
    """Идентификатор вморожен в каждую выданную ссылку. Старые обязаны остаться
    со старым (иначе приложение перестанет опознавать импортированные профили),
    двойники — получить новый."""
    from awgbot.domain import configgen
    monkeypatch.setattr(config, "APP_CONTAINER", "amnezia-awg2")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    assert configgen.app_container_for("awg0") == "amnezia-awg2"
    assert configgen.app_container_for("") == "amnezia-awg2"
    assert configgen.app_container_for("awg1") == "amnezia-awg3"
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    assert configgen.app_container_for("awg1") == "amnezia-awg2", "вне окна — общий"


def test_vpn_link_carries_the_interface_protocol(lockdir, monkeypatch):
    """Ссылка двойника несёт протокол НОВОГО поколения, ссылка с основного
    интерфейса — прежний."""
    from awgbot.domain import configgen
    monkeypatch.setattr(config, "APP_CONTAINER", "amnezia-awg2")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "SERVER_HOST", "203.0.113.10")
    params = {"obfuscation": {"Jc": "4"}, "listen_port": 51820,
              "server_pubkey": "SPUB=", "psk": "PSK="}
    twin = configgen.decode_vpn(
        configgen.generate("PRIV=", "PUB=", "10.9.1.5", params, iface="awg1")["vpn"])
    assert twin["defaultContainer"] == "amnezia-awg3"
    assert twin["containers"][0]["container"] == "amnezia-awg3"
    old = configgen.decode_vpn(
        configgen.generate("PRIV=", "PUB=", "10.8.1.5", params, iface="awg0")["vpn"])
    assert old["defaultContainer"] == "amnezia-awg2"


# ── согласование с установщиком ──────────────────────────────────────────────

def test_state_file_and_keys_match_the_installer():
    """Файл состояния пишет установщик (shell), читает бот (python). Разойдись
    путь или имя ключа — установщик пишет в одно место, бот читает пустоту,
    `applied_generation` усыновляет поколение поставки, и переезд не будет
    объявлен НИКОГДА. Тихо, без единой ошибки."""
    import re
    from pathlib import Path as _P
    script = (_P(__file__).resolve().parents[2] / "awg-bot.sh").read_text(encoding="utf-8")
    assert 'AWG_STATE="$ETC_DIR/awg.state"' in script
    assert 'ETC_DIR="/etc/awg-bot"' in script
    # путь бота: рядом с conf, то есть /etc/awg-bot/awg.state
    import awgbot.infra.awglock as al
    # conftest подменяет STATE_PATH на временный — сверяем формулу по исходнику
    import inspect
    assert 'STATE_PATH = Path(config.CONF_DIR).parent / "awg.state"' in inspect.getsource(al), \
        "бот читает состояние рядом с conf — там же, где ETC_DIR установщика"
    for key in (al._KEY_APPLIED, al._KEY_TARGET):
        assert f"awg_state_set {key}" in script or f"awg_state_get {key}" in script, \
            f"{key} не встречается в awg-bot.sh"
    # и наоборот: shell не пишет ключей, которых бот не знает
    written = set(re.findall(r"awg_state_set (AWG_[A-Z_]+)", script))
    assert written <= {al._KEY_APPLIED, al._KEY_TARGET}, written


def test_real_manifest_is_readable_by_the_bot():
    """Манифест поставки и его читатель не должны разъезжаться по формату."""
    import awgbot.infra.awglock as al
    assert al.LOCK_PATH.exists(), "install/awg.lock пропал из репозитория"
    assert al.generation() >= 1 and al.protocol_id()
    assert al.lock()["AWG_MODULE_VERSION"]
    # Тождество ведётся по тегу: version.h у апстримных тегов один и тот же.
    assert al.module_tag().startswith("v")


def test_blocked_release_is_not_marked_as_notified(tmp_path, monkeypatch):
    """Иначе единственное уведомление о версии сгорит во время переезда, и
    после его завершения о ней никто не напомнит."""
    from awgbot.domain.selfupdate import SelfUpdateMixin

    state: dict = {}

    class DB:
        def get_state(self, k):
            return state.get(k, "")
        def set_state(self, k, v):
            state[k] = v

    rel = updates.Release(tag="v9.0.0", version=(9, 0, 0), body="#awg_gen3",
                          asset_url="u", sha256="x")

    class Svc(SelfUpdateMixin):
        def __init__(self):
            self.db = DB()
        def migration_running(self):
            return True
        def update_next(self):
            return rel

    monkeypatch.setattr(awglock, "LOCK_PATH", tmp_path / "awg.lock")
    monkeypatch.setattr(awglock, "STATE_PATH", tmp_path / "awg.state")
    awglock.write_state(applied=1, target=2)
    monkeypatch.setattr("awgbot.core.settings.get", lambda k, d=None: d)
    svc = Svc()
    assert svc.update_to_notify() is None
    assert state.get(svc._NOTIFIED_KEY, "") == "", "версия помечена уведомлённой зря"


def test_built_tag_is_the_only_honest_record(lockdir):
    """Строка версии у v3.1.20260812…0906 одна и та же — «что собрано» знает
    только состояние хоста, куда это записал установщик."""
    assert awglock.built_module_tag() == "", "пустое состояние не должно выдумывать тег"
    (lockdir / "awg.state").write_text("AWG_MODULE_TAG_BUILT=v3.1.20260906\n", encoding="utf-8")
    assert awglock.built_module_tag() == "v3.1.20260906"
