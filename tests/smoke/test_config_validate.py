"""Smoke: config.validate() — падает адресно на нехватке, проходит на валидном."""
import importlib

import pytest

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def _restore_config():
    """Тесты перегружают config с временным conf-каталогом — после каждого
    возвращаем боевой, иначе соседние тесты (render/keyboards) видят огрызок."""
    yield
    import os
    os.environ.pop("AWG_BOT_CONF_DIR", None)
    import awgbot.core.config as c
    importlib.reload(c)


def _write_conf(conf_dir, *, server_host="1.2.3.4", server_port=43125):
    (conf_dir / "app.yaml").write_text(
        f'network: {{server_host: "{server_host}", server_port: {server_port}}}\n',
        encoding="utf-8")


def _reload_config(monkeypatch, conf_dir):
    monkeypatch.setenv("AWG_BOT_CONF_DIR", str(conf_dir))
    monkeypatch.setenv("BOT_TOKEN", "12345:tok")
    monkeypatch.setenv("ADMIN_ID", "1")
    import awgbot.core.config as c
    return importlib.reload(c)


def test_validate_passes_on_valid_conf(monkeypatch, tmp_path):
    _write_conf(tmp_path)
    c = _reload_config(monkeypatch, tmp_path)
    c.validate()   # не должно бросить
    assert c.SERVER_HOST == "1.2.3.4"


def test_validate_missing_server_host(monkeypatch, tmp_path):
    (tmp_path / "app.yaml").write_text(
        "network: {server_port: 43125}\n", encoding="utf-8")
    c = _reload_config(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="server_host"):
        c.validate()



