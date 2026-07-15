"""Self-update: выбор «следующей ступени» по semver, сверка sha256 из digest,
«нет в списке → молчим», mute/«ровно один раз», усечение changelog под лимит.
"""
import hashlib
import json

from awgbot.infra import updates
from awgbot.bot import texts
import awgbot.core.config as cfg


def _release_json(tag, body="", asset=True, digest_hex=None, name=None):
    a = []
    if asset:
        a.append({"name": name or cfg.UPDATES_ASSET_NAME,
                  "url": f"https://api/assets/{tag}",
                  "digest": f"sha256:{digest_hex}" if digest_hex else None})
    return {"tag_name": tag, "body": body, "draft": False, "assets": a}


def _patch_releases(monkeypatch, releases_json):
    """Подменяет сеть: /releases → releases_json; assets/<url> → по карте blobs."""
    def fake_request(url, accept):
        if url.endswith("/releases"):
            return json.dumps(releases_json).encode()
        raise updates.UpdateError(f"unexpected url {url}")
    monkeypatch.setattr(updates, "_request", fake_request)


# ── semver / следующая ступень ───────────────────────────────────────────────

def test_parse_version():
    assert updates.parse_version("v1.2.3") == (1, 2, 3)
    assert updates.parse_version("1.2.3") == (1, 2, 3)
    assert updates.parse_version("v1.10.0") == (1, 10, 0)
    assert updates.parse_version("latest") is None
    assert updates.parse_version("v1.2") is None


def test_next_is_immediate_step_not_latest(monkeypatch):
    """Между установленной и последней — берём БЛИЖАЙШУЮ большую, не последнюю."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", digest_hex="a" * 64),
        _release_json("v1.3.0", digest_hex="b" * 64),
        _release_json("v1.2.0", digest_hex="c" * 64),   # намеренно вне порядка
    ])
    nxt = updates.next_release()
    assert nxt is not None and nxt.tag == "v1.2.0"       # 1.1.0 → 1.2.0, не 1.3.0


def test_semver_ordering_10_after_9(monkeypatch):
    """v1.10.0 новее v1.9.0 (числовое сравнение, не строковое)."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.9.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.9.0", digest_hex="a" * 64),
        _release_json("v1.10.0", digest_hex="b" * 64),
    ])
    nxt = updates.next_release()
    assert nxt is not None and nxt.tag == "v1.10.0"


def test_installed_is_latest_returns_none(monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.3.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.2.0", digest_hex="a" * 64),
        _release_json("v1.3.0", digest_hex="b" * 64),
    ])
    assert updates.next_release() is None


def test_installed_not_in_releases_returns_none(monkeypatch):
    """Нерелизная сборка (версии нет среди тегов) → молчим навсегда."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.5")   # такого тега нет
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", digest_hex="a" * 64),
        _release_json("v1.2.0", digest_hex="b" * 64),
    ])
    assert updates.next_release() is None


def test_non_semver_tags_ignored(monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", digest_hex="a" * 64),
        _release_json("nightly", digest_hex="c" * 64),      # не semver → мимо
        _release_json("v1.2.0", digest_hex="b" * 64),
    ])
    nxt = updates.next_release()
    assert nxt.tag == "v1.2.0"


# ── скачивание + сверка sha256 из digest ─────────────────────────────────────

def test_download_verifies_sha256(monkeypatch):
    blob = b"delivery-bytes"
    digest = hashlib.sha256(blob).hexdigest()
    rel = updates.Release(tag="v1.2.0", version=(1, 2, 0), body="",
                          asset_url="https://api/assets/x", sha256=digest)
    monkeypatch.setattr(updates, "_request", lambda url, accept: blob)
    assert updates.download_asset(rel) == blob


def test_download_rejects_mismatched_sha256(monkeypatch):
    rel = updates.Release(tag="v1.2.0", version=(1, 2, 0), body="",
                          asset_url="https://api/assets/x", sha256="d" * 64)
    monkeypatch.setattr(updates, "_request", lambda url, accept: b"other-bytes")
    try:
        updates.download_asset(rel)
        assert False, "должно было отклонить по sha256"
    except updates.UpdateError as e:
        assert "sha256" in str(e)


def test_download_rejects_missing_digest(monkeypatch):
    rel = updates.Release(tag="v1.2.0", version=(1, 2, 0), body="",
                          asset_url="https://api/assets/x", sha256=None)
    try:
        updates.download_asset(rel)
        assert False, "без digest должно отклонять"
    except updates.UpdateError as e:
        assert "digest" in str(e)


# ── mute / «ровно один раз на версию» (services) ─────────────────────────────

def test_update_to_notify_once_then_muted(services, monkeypatch):
    rel = updates.Release(tag="v1.2.0", version=(1, 2, 0), body="changelog",
                          asset_url="u", sha256="a" * 64)
    monkeypatch.setattr(services, "update_next", lambda: rel)

    first = services.update_to_notify()
    assert first is not None and first.tag == "v1.2.0"    # первый раз — уведомляем
    assert services.update_to_notify() is None            # второй — уже нет (once)


def test_update_to_notify_respects_mute(services, monkeypatch):
    rel = updates.Release(tag="v1.2.0", version=(1, 2, 0), body="x",
                          asset_url="u", sha256="a" * 64)
    monkeypatch.setattr(services, "update_next", lambda: rel)
    services.mute_updates()
    assert services.updates_muted() is True
    assert services.update_to_notify() is None            # заглушено


def test_new_version_notifies_even_after_previous_notified(services, monkeypatch):
    """Пропущенная (показанная) версия не мешает уведомить о следующей ступени."""
    rel1 = updates.Release("v1.2.0", (1, 2, 0), "a", "u", "a" * 64)
    monkeypatch.setattr(services, "update_next", lambda: rel1)
    assert services.update_to_notify().tag == "v1.2.0"
    assert services.update_to_notify() is None
    rel2 = updates.Release("v1.3.0", (1, 3, 0), "b", "u", "b" * 64)
    monkeypatch.setattr(services, "update_next", lambda: rel2)
    assert services.update_to_notify().tag == "v1.3.0"    # новая ступень — снова да


# ── усечение changelog под лимит Telegram ────────────────────────────────────

def test_changelog_fits_untruncated():
    msg = texts.update_available("v1.2.0", "строка один\nстрока два")
    assert "<blockquote expandable>" in msg
    assert "обрезаны" not in msg
    assert len(msg) <= 4096


def test_changelog_truncated_when_huge():
    body = "\n".join(f"пункт номер {i} с некоторым текстом" for i in range(600))
    msg = texts.update_available("v1.2.0", body)
    assert len(msg) <= 4096
    assert "обрезаны" in msg
    assert msg.count("<blockquote") == 1 and msg.count("</blockquote>") == 1


def test_changelog_empty_body():
    msg = texts.update_available("v1.2.0", "")
    assert "v1.2.0" in msg and "<blockquote" not in msg


# ── подтверждение результата после self-update ───────────────────────────────

def test_confirm_applied_update_success(services, monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.1")
    monkeypatch.setattr(updates, "release_body", lambda tag: "- пункт changelog")
    services.db.set_state("update_pending", "v1.1.1")
    note = services.confirm_applied_update()
    assert note is not None
    assert "успешно обновлен до v1.1.1" in note.text
    assert "<blockquote expandable>" in note.text          # changelog под катом
    assert note.reply_markup is not None                   # кнопка «В меню»
    assert services.confirm_applied_update() is None       # флаг стёрт — однократно


def test_update_wait_roundtrip(services):
    """set/pop «дождись»-сообщения: одноразово, парсится обратно в (chat, msg)."""
    assert services.pop_update_wait() is None
    services.set_update_wait(12345, 678)
    assert services.pop_update_wait() == (12345, 678)
    assert services.pop_update_wait() is None              # одноразово


def test_confirm_applied_update_failure(services, monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")  # версия не сменилась
    services.db.set_state("update_pending", "v1.1.1")
    note = services.confirm_applied_update()
    assert note is not None and "⚠️" in note.text
    assert "v1.1.1" in note.text and "1.1.0" in note.text


def test_confirm_applied_update_no_pending(services):
    assert services.confirm_applied_update() is None


def test_apply_update_sets_pending(services, monkeypatch):
    rel = updates.Release("v9.9.9", (9, 9, 9), "", "u", "a" * 64)
    monkeypatch.setattr(updates, "download_asset", lambda r: b"blob")
    monkeypatch.setattr(updates, "apply", lambda blob: None)
    services.apply_update(rel)
    assert services.db.get_state("update_pending") == "v9.9.9"


def test_update_to_notify_respects_never_schedule(services, monkeypatch, tmp_path):
    """poll_schedule=never глушит уведомления и при ручной правке YAML —
    инвариант в самом update_to_notify, не только в UI."""
    from awgbot.core import settings as st
    (tmp_path / "updates.yaml").write_text('poll_schedule: "never"\n', encoding="utf-8")
    st.init(tmp_path)
    try:
        rel = updates.Release("v9.9.9", (9, 9, 9), "x", "u", "a" * 64)
        monkeypatch.setattr(services, "update_next", lambda: rel)
        assert services.updates_muted() is False        # мьют НЕ включён
        assert services.update_to_notify() is None      # но never глушит
    finally:
        from awgbot.core import config
        st.init(config.CONF_DIR)
