"""Self-update: выбор цели — последний релиз роли с обязательными ступенями
(`#requires_main_X` / `#requires_gw_X`: адресат и минимум одной строкой), сверка
sha256 из digest, «нет в списке → молчим», mute/«ровно один раз», усечение
changelog под лимит, список пропущенных ступеней.
"""
import hashlib
import json

from awgbot.infra import updates
from awgbot.bot import texts
import awgbot.core.config as cfg


def _release_json(tag, body="", asset=True, digest_hex=None, name=None, draft=False,
                  title=None):
    a = []
    if asset:
        a.append({"name": name or cfg.UPDATES_ASSET_NAME,
                  "url": f"https://api/assets/{tag}",
                  "digest": f"sha256:{digest_hex}" if digest_hex else None})
    return {"tag_name": tag, "body": body, "draft": draft, "assets": a,
            "name": title if title is not None else f"{tag} — релиз {tag}"}


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


def test_target_is_the_latest_and_skipped_steps_are_listed(monkeypatch):
    """Между установленной и последней — берём ПОСЛЕДНЮЮ: одна сборка ядра и
    один переезд вместо цепочки. Пропущенные ступени — в skipped, по
    возрастанию, с заголовками без тега."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", digest_hex="a" * 64),
        _release_json("v1.3.0", digest_hex="b" * 64),
        _release_json("v1.2.0", digest_hex="c" * 64, title="v1.2.0 — середина"),   # вне порядка
    ])
    nxt = updates.next_release()
    assert nxt is not None and nxt.tag == "v1.3.0"
    assert [r.tag for r in nxt.skipped] == ["v1.2.0"]
    assert nxt.skipped[0].title == "середина"


def test_requires_lowers_the_target_to_the_mandatory_step(monkeypatch):
    """`#requires_main_X` у любого из пропускаемых релизов: хост ниже X сначала
    едет на X (ближайший релиз для роли не ниже него), потом дальше.
    Достигший X — прыгает сразу в конец."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    rel = [
        _release_json("v1.1.0", digest_hex="a" * 64),
        _release_json("v1.2.0", digest_hex="b" * 64),
        _release_json("v1.3.0", body="#requires_main_1.2.0", digest_hex="c" * 64),
        _release_json("v1.4.0", digest_hex="d" * 64),
    ]
    _patch_releases(monkeypatch, rel)
    nxt = updates.next_release()
    assert nxt.tag == "v1.2.0" and nxt.skipped == ()
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.2.0")
    nxt = updates.next_release()
    assert nxt.tag == "v1.4.0" and [r.tag for r in nxt.skipped] == ["v1.3.0"]


def test_requires_chain_is_followed_one_link_at_a_time(monkeypatch):
    """У обязательной ступени может быть своя обязательная ступень."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.0.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.0.0", digest_hex="a" * 64),
        _release_json("v1.1.0", digest_hex="b" * 64),
        _release_json("v1.2.0", body="#requires_main_v1.1.0", digest_hex="c" * 64),
        _release_json("v1.3.0", body="#requires_main_1.2.0", digest_hex="d" * 64),
    ])
    assert updates.next_release().tag == "v1.1.0"
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    assert updates.next_release().tag == "v1.2.0"
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.2.0")
    assert updates.next_release().tag == "v1.3.0"


def test_requirements_are_per_role(monkeypatch):
    """Одна строка на роль несёт и адресата, и минимум: у основного бота и у
    агента шлюза они разные. Роль без строки релиз не берёт вовсе."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", body="#requires_main_1.0.0\n#requires_gw_1.0.0", digest_hex="a" * 64),
        _release_json("v1.2.0", body="#requires_main_1.0.0", digest_hex="b" * 64),
        _release_json("v1.3.0", body="#requires_main_1.0.0\n#requires_gw_1.0.0", digest_hex="c" * 64),
        _release_json("v1.4.0", body="#requires_main_1.2.0\n#requires_gw_1.3.0", digest_hex="d" * 64),
    ])
    assert updates.next_release("gateway").tag == "v1.3.0", "минимум шлюза — 1.3.0"
    assert updates.next_release("client").tag == "v1.2.0", "минимум основного — 1.2.0"
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.3.0")
    assert updates.next_release("gateway").tag == "v1.4.0"
    assert updates.next_release("client").tag == "v1.4.0"


def test_generation_ceiling_hides_releases_beyond_the_migration_target(monkeypatch):
    """Идёт переезд на поколение 2 — релизы с ядром поколения 3 не цель:
    берём последний под потолком."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", body="#requires_main_1.0.0\n#awg_gen1", digest_hex="a" * 64),
        _release_json("v1.2.0", body="#requires_main_1.0.0\n#awg_gen2", digest_hex="b" * 64),
        _release_json("v1.3.0", body="#requires_main_1.0.0\n#awg_gen3", digest_hex="c" * 64),
    ])
    assert updates.next_release(max_generation=2).tag == "v1.2.0"
    assert updates.next_release(max_generation=1) is None
    assert updates.next_release().tag == "v1.3.0"


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


def test_a_withdrawn_release_still_updates_to_its_hotfixes(monkeypatch):
    """Релиз сняли с GitHub (битый), хост успел его поставить: тега 1.2.0 нет,
    но хотфиксы 1.2.0.N той же базы есть — обновления не глушим, иначе хост
    застрянет ровно на том, что чинит хотфикс."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.2.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", digest_hex="a" * 64),
        _release_json("v1.2.0.2", digest_hex="b" * 64),
        _release_json("v1.2.0.3", digest_hex="c" * 64),
    ])
    assert updates.next_release().tag == "v1.2.0.3"
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.2.0.1")   # снятый хотфикс той же базы
    assert updates.next_release().tag == "v1.2.0.3"
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.3.0")     # чужая база — молчим
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

def test_skipped_steps_are_links_to_their_release_pages():
    """Прыжок через ступени: каждая пропущенная версия — ссылка на свою
    страницу релиза (changelog); ссылки на diff кода нет — админу нужен
    changelog, а не исходники; хвост длиннее десяти — свёрнут в «ещё N». Всё
    вместе — под лимит Telegram."""
    def rel(i):
        return updates.Release(tag=f"v1.{i}.0", version=(1, i, 0), body="", asset_url=None,
                               sha256=None, title=f"шаг {i}")
    text = texts.update_available("v1.3.0", "- x", installed="1.1.0", skipped=(rel(2),))
    assert f'href="https://github.com/{cfg.UPDATES_REPO}/releases/tag/v1.2.0">v1.2.0 — шаг 2</a>' in text
    assert "/compare/" not in text, "ссылка на diff кода пугает, а не помогает"
    assert "Список изменений" in text and "- x" in text

    many = texts.update_available("v1.20.0", "- y" * 10, installed="1.1.0",
                                  skipped=tuple(rel(i) for i in range(2, 20)))
    assert "ещё 8" in many and "v1.19.0" in many and "v1.9.0" not in many
    assert len(many) <= 4096

    assert 'href' not in texts.update_available("v1.2.0", "- x", installed="1.1.0")
    admin = texts.update_admin_available("1.1.0", "v1.3.0", "- x", skipped=(rel(2),))
    assert "releases/tag/v1.2.0" in admin


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


def test_hotfix_version_has_a_fourth_digit():
    """Четвёртая цифра — заплатка поверх выпущенной версии.

    Прежде такой тег не разбирался вовсе, и последствий было два, оба тихих:
    тег выпадал из списка релизов (обновление никому не предлагалось), а бот,
    на который эту версию поставили руками, не находил себя среди тегов и
    выключал обновления навсегда — по правилу «нас нет в списке → не трогаем».
    """
    assert updates.parse_version("v2.2.3.1") == (2, 2, 3, 1)
    assert updates.parse_version("2.2.3.10") == (2, 2, 3, 10)
    assert updates.parse_version("v2.2.3.1.4") is None      # пятая — уже не версия


def test_hotfix_sorts_between_its_base_and_the_next_release():
    """Порядок держится на сравнении кортежей разной длины, поэтому дополнять
    короткий нулём НЕЛЬЗЯ: 2.2.3 и 2.2.3.0 стали бы одним значением, и бот на
    заплатке считал бы себя базовой версией — то есть предложил бы «обновиться»
    на то, что у него уже стоит."""
    p = updates.parse_version
    assert p("2.2.3") < p("2.2.3.1") < p("2.2.4")
    assert p("2.2.3") != p("2.2.3.0")
    assert p("2.2.3.9") < p("2.3.0")


# ── адресаты релиза: #main_bot / #gw_bot / #all_bots ─────────────────────────

def test_release_audience_parsing():
    """Новый формат: строка на роль = адресат + минимум. Прежние хэштеги
    читаются у старых релизов; без хэштегов — общий."""
    r = updates.Release("v1", (1,), "#requires_gw_2.10.0\n#awg_gen1\n- x", None, None)
    assert r.audience() == {"gw_bot"} and r.applies_to("gateway") and not r.applies_to("client")
    assert r.requires("gateway") == (2, 10, 0) and r.requires("client") is None
    both = updates.Release("v1", (1,), "#requires_main_2.10.0\n#requires_gw_2.12.0.1", None, None)
    assert both.applies_to("gateway") and both.applies_to("client")
    assert both.requires("client") == (2, 10, 0) and both.requires("gateway") == (2, 12, 0, 1)
    old = updates.Release("v1", (1,), "#gw_bot\n- x", None, None)
    assert old.audience() == {"gw_bot"} and old.applies_to("gateway") and not old.applies_to("client")
    assert old.requires("gateway") is None
    legacy = updates.Release("v1", (1,), "без хэштегов", None, None)
    assert legacy.applies_to("gateway") and legacy.applies_to("client")


def test_gateway_skips_main_only_releases_to_its_own(monkeypatch):
    """Агент на 1.1.0: 1.2.0 (#main_bot) — не его и в пропущенных не числится; цель — последний общий."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", body="#all_bots", digest_hex="a" * 64),
        _release_json("v1.2.0", body="#main_bot\n- только основной", digest_hex="b" * 64),
        _release_json("v1.3.0", body="#gw_bot\n- агент", digest_hex="c" * 64),
        _release_json("v1.4.0", body="#all_bots", digest_hex="d" * 64),
    ])
    assert updates.next_release("gateway").tag == "v1.4.0"
    assert [r.tag for r in updates.next_release("gateway").skipped] == ["v1.3.0"], \
        "чужой #main_bot релиз в пропущенных не числится"
    assert updates.next_release("client").tag == "v1.4.0"
    assert [r.tag for r in updates.next_release("client").skipped] == ["v1.2.0"]


def test_role_with_nothing_addressed_is_up_to_date(monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", body="#all_bots", digest_hex="a" * 64),
        _release_json("v1.2.0", body="#main_bot", digest_hex="b" * 64),
    ])
    assert updates.next_release("gateway") is None
    assert updates.next_release("client").tag == "v1.2.0"


def test_default_role_comes_from_config(monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.1.0")
    monkeypatch.setattr(cfg, "ROLE", "gateway")
    _patch_releases(monkeypatch, [
        _release_json("v1.1.0", body="#all_bots", digest_hex="a" * 64),
        _release_json("v1.2.0", body="#main_bot", digest_hex="b" * 64),
        _release_json("v1.2.1", body="#gw_bot", digest_hex="c" * 64),
    ])
    assert updates.next_release().tag == "v1.2.1"


# ── что считается релизом ────────────────────────────────────────────────────

def test_draft_releases_are_ignored(monkeypatch):
    """Черновик — это версия, которой ещё нет: у неё не собран ассет. Предложи
    мы обновиться на неё, отказ пришёл бы уже после слов «обновляюсь»."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.0.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.0.0"),
        _release_json("v1.1.0", draft=True),
        _release_json("v1.2.0"),
    ])
    nxt = updates.next_release()
    assert nxt is not None and nxt.tag == "v1.2.0", "черновик просочился в ступени"


def test_release_without_our_asset_has_no_download_url(monkeypatch):
    """Ассета нет или он с чужим именем — скачивать нечего, и это должно быть
    видно ДО попытки: download_asset отказывается с внятной причиной."""
    import pytest as _pytest
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.0.0")
    _patch_releases(monkeypatch, [
        _release_json("v1.0.0"),
        _release_json("v1.1.0", asset=False),
        _release_json("v1.2.0", name="совсем-другой.tgz"),
    ])
    rels = {r.tag: r for r in updates.list_releases()}
    assert rels["v1.1.0"].asset_url is None and rels["v1.2.0"].asset_url is None
    with _pytest.raises(updates.UpdateError, match="нет ассета"):
        updates.download_asset(rels["v1.1.0"])


def test_release_applies_to_the_role_it_names(monkeypatch):
    """Роли обновляются по своим хэштегам: агент шлюза не должен ставить
    поставку, адресованную только основному боту."""
    main = updates.Release(tag="v1.1.0", version=(1, 1, 0), body="#main_bot",
                           asset_url="u", sha256="x")
    gw = updates.Release(tag="v1.2.0", version=(1, 2, 0), body="#gw_bot", asset_url="u", sha256="x")
    both = updates.Release(tag="v1.3.0", version=(1, 3, 0), body="#all_bots", asset_url="u", sha256="x")
    plain = updates.Release(tag="v1.4.0", version=(1, 4, 0), body="без хэштегов",
                            asset_url="u", sha256="x")
    assert main.applies_to("client") and not main.applies_to("gateway")
    assert gw.applies_to("gateway") and not gw.applies_to("client")
    assert both.applies_to("client") and both.applies_to("gateway")
    assert plain.applies_to("client") and plain.applies_to("gateway"), "старые релизы общие"
