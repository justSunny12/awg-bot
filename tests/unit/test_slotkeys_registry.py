"""
Реестр слотовых ключей server_state (domain/services/gateway_link/slotkeys.py).

Ключи состояния слота раньше перечисляли три ручных списка, и ключ, забытый в
одном из них, переживал снятие слота: новый слот с тем же номером наследовал
хвосты прежнего. Сторож ниже читает код пакетов gwchannel/, gateway_link/ и
routing/ и требует, чтобы каждый слотовый ключ был в реестре; поведенческие
проверки — что снятие слота чистит всё помеченное «слот» и «устройство»,
замена устройства — только «устройство», а копия настроек шлюза (gwkeep)
переживает замену.
"""
from __future__ import annotations

import ast
import base64
import os
import re
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain.services.gateway_link import slotkeys

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SERVICES = REPO / "awgbot" / "domain" / "services"
SCANNED = ("gwchannel", "gateway_link", "routing")

# в реестре, но в коде пакетов не встречаются: ключи доставки, снятой в 3.3.1
# (лежат у слотов, заведённых раньше), и копия настроек шлюза (пишет её канал)
NOT_IN_CODE = {"gwlink_ack", "gwlink_applied", "gwkeep"}

_SLOT_NAMES = {"slot_id", "sid", "slot", "k"}
_STATE_CALLS = {"get_state", "set_state", "get_state_json", "set_state_json"}
_KEY_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
_TEMPLATE_RE = re.compile(r"[a-z][a-z0-9_]*(_\{\}[a-z0-9_]*)?\Z")


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _str_consts() -> dict[str, set[str]]:
    """Строковые константы сервисного слоя: NAME = "…" на уровне модуля и
    класса — по ним разрешается self._X_KEY."""
    out: dict[str, set[str]] = {}
    for path in _py_files(SERVICES):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bodies = [tree.body] + [n.body for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        for body in bodies:
            for node in body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            out.setdefault(t.id, set()).add(node.value.value)
    return out


def _slotish(node) -> bool:
    """Выражение — номер слота: slot_id, sid, gw.id, int(slot_id or 1)…"""
    if isinstance(node, ast.Name):
        return node.id in _SLOT_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr == "id"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "int" and node.args:
        return _slotish(node.args[0])
    if isinstance(node, ast.BoolOp):                    # int(slot_id or 1)
        return any(_slotish(v) for v in node.values)
    return False


def _key_values(node, consts) -> set[str]:
    """Значения имени ключа: литерал, self._X_KEY, getattr(self, "_X", "запас")."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return set(consts.get(node.attr, ()))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
            and len(node.args) == 3:
        return _key_values(node.args[1], consts) | _key_values(node.args[2], consts)
    return set()


def _fstring_templates(node, consts) -> set[str]:
    """f-строка ключа с номером слота → шаблоны с «{}» на месте номера."""
    outs = {""}
    slots = 0
    for part in node.values:
        if isinstance(part, ast.Constant):
            outs = {o + str(part.value) for o in outs}
        elif isinstance(part, ast.FormattedValue) and _slotish(part.value):
            slots += 1
            outs = {o + "{}" for o in outs}
        elif isinstance(part, ast.FormattedValue):
            vals = _key_values(part.value, consts)
            if not vals:
                return set()
            outs = {o + v for o in outs for v in vals}
        else:
            return set()
    if slots != 1:
        return set()
    return {o for o in outs if _TEMPLATE_RE.match(o)}


def _registry_name(template: str) -> str:
    """«gwlink_seen_{}» → «gwlink_seen»; шаблон с номером внутри — как есть."""
    return template[:-3] if template.endswith("_{}") else template


def scan_slot_keys() -> set[str]:
    """Имена слотовых ключей, как их видит код трёх пакетов: первый аргумент
    вызова, вторым идёт номер слота (_gwlink_key, _gw_slot_key, _gwlink_ack_put,
    _gwcfg_error_set, _gw_cached…), и f-строки с номером слота."""
    consts = _str_consts()
    found: set[str] = set()
    for pkg in SCANNED:
        for path in _py_files(SERVICES / pkg):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and len(node.args) >= 2 and _slotish(node.args[1]):
                    fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                    if fn in _STATE_CALLS:
                        continue
                    found.update(v for v in _key_values(node.args[0], consts) if _KEY_RE.match(v))
                elif isinstance(node, ast.JoinedStr):
                    found.update(_registry_name(t) for t in _fstring_templates(node, consts))
    return found


# ── сторож: реестр сверен с кодом ────────────────────────────────────────────

def test_the_scan_sees_slot_keys_of_every_kind():
    """Сторож сам не слепой: находит ключи через помощники, f-строки и шаблоны
    с номером внутри."""
    found = scan_slot_keys()
    for name in ("gwlink_seen", "gwcfg_gen", "gwlink_lists", "gwcfg_link_err", "gw_link_port_hand",
                 "routing_gw_ping", "routing_gw_{}_up_streak", "routing_gw_{}_down_since", "gwlink_snap_at"):
        assert name in found, f"сторож не видит {name}"
    assert len(found) >= 35, sorted(found)


def test_every_slot_key_of_the_code_is_in_the_registry():
    """Новый слотовый ключ без записи в реестре пережил бы снятие слота."""
    missing = scan_slot_keys() - set(slotkeys.KEYS)
    assert not missing, f"слотовые ключи без записи в slotkeys.KEYS: {sorted(missing)}"


def test_the_registry_has_no_stale_names():
    stale = set(slotkeys.KEYS) - scan_slot_keys() - NOT_IN_CODE
    assert not stale, f"в реестре ключи, которых код не знает: {sorted(stale)}"


def test_every_entry_says_when_it_goes():
    for name, k in slotkeys.KEYS.items():
        assert k.when in (slotkeys.SLOT, slotkeys.DEVICE), name
        assert _TEMPLATE_RE.match(name), name
        if k.when == slotkeys.DEVICE:
            # ключи устройства снимает gwlink_forget: документ — через gwcfg_forget, канал — сам
            assert k.part in ("cfg", "link"), name
    assert slotkeys.KEYS["gwkeep"].when == slotkeys.SLOT, "копия настроек нужна новому устройству слота"


def test_on_restore_only_the_settings_copy_comes_from_the_backup():
    """Слотовые ключи описывают стоящее на шлюзе сейчас — при восстановлении
    у текущего слота они текущие; из бэкапа (с пометкой «предложить») — только
    копия настроек шлюза."""
    assert set(slotkeys.names(merge=slotkeys.OFFER)) == {"gwkeep"}
    assert set(slotkeys.names(merge=slotkeys.CURRENT)) == set(slotkeys.KEYS) - {"gwkeep"}
    assert all(k.merge in (slotkeys.CURRENT, slotkeys.OFFER) for k in slotkeys.KEYS.values())


def test_the_key_of_a_slot():
    assert slotkeys.key("gwlink_seen", 2) == "gwlink_seen_2"
    assert slotkeys.key("routing_gw_{}_up_streak", 3) == "routing_gw_3_up_streak"
    assert slotkeys.key("gwcfg_gen", "4") == "gwcfg_gen_4"


# ── поведение: снятие слота и замена устройства ──────────────────────────────

PRIV = base64.b64encode(os.urandom(32)).decode()
VALUE = "1"


@pytest.fixture()
def slots(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Админ: два слота и запасное устройство; скрипт линка — заглушка."""
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    spare = services.add_device(admin.id, "Pi3")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(services, "_link_privkey", lambda gw=None: PRIV)
    for sid in (1, 2):
        for name in slotkeys.KEYS:
            services.db.set_state(slotkeys.key(name, sid), VALUE)
    return spare


def _left(services, sid: int, which) -> set[str]:
    return {n for n in which if services.db.get_state(slotkeys.key(n, sid))}


def test_removing_a_slot_clears_its_slot_and_device_keys(slots, services):
    services.gateway_remove(2)
    assert not _left(services, 2, slotkeys.KEYS), "снятие слота оставило ключи"
    assert _left(services, 1, slotkeys.KEYS) == set(slotkeys.KEYS), "снятие слота 2 задело слот 1"


def test_replacing_the_device_clears_only_device_keys(slots, services):
    res = services.gateway_setup(slots.device_id, slot_id=2)
    assert res["previous"] is not None, "замены не было"
    assert not _left(services, 2, slotkeys.names(when=slotkeys.DEVICE)), \
        "ключи прежнего устройства пережили замену"
    slot_only = set(slotkeys.names(when=slotkeys.SLOT))
    assert _left(services, 2, slot_only) == slot_only, "замена устройства сняла ключи слота"
    assert _left(services, 1, slotkeys.KEYS) == set(slotkeys.KEYS)


def test_the_gateway_settings_copy_survives_a_replacement_and_goes_with_the_slot(slots, services):
    services.gateway_setup(slots.device_id, slot_id=2)
    assert services.db.get_state("gwkeep_2") == VALUE, "копия настроек шлюза снята заменой устройства"
    services.gateway_remove(2)
    assert not services.db.get_state("gwkeep_2"), "копия настроек шлюза пережила снятие слота"


def test_partial_forgets_take_their_part_of_the_registry(slots, services):
    services.gwcfg_forget(2)
    assert not _left(services, 2, slotkeys.names(part="cfg"))
    assert _left(services, 2, slotkeys.names(part="link")) == set(slotkeys.names(part="link"))
    services.gateway_rekey_forget(2)
    assert not _left(services, 2, slotkeys.names(part="rekey"))
    assert _left(services, 2, slotkeys.names(part="slot")) == set(slotkeys.names(part="slot"))
