"""
gwownlists.py — свои списки, общие для всех шлюзов.

Свои списки локальной сети без VPN («в туннель» / «напрямую») ведутся на
каждом шлюзе в чате его агента и при этом одни на все шлюзы админа:
правка на одном шлюзе событием уходит каналом линка на сервер AWG, тот
сливает события в канон и раздаёт его остальным шлюзам, а агент-получатель
пишет оба файла dnsmasq одним вызовом `awg-lan-domain.sh sync`.

Модуль общий для обеих ролей по той же причине, что gwservices.py: формат
события, правило домена и отпечаток канона обязаны совпадать на концах, а
сборка файла для `sync` — единственная точка, где данные с чужой машины
превращаются в строки для скрипта. Ни одного сетевого вызова и чтения с
хоста здесь нет — чистые функции.

Канон: {"gen": метка поколения (случайная при создании), "ver": номер
изменения, "items": {домен: [вид, слот, время]}}. Событие: [n, домен,
вид|del, init]. Правило домена — то же, что в awg-lan-domain.sh (зона
буквами или punycode «xn--»): что проходит у агента, проходит и на сервере.
"""
from __future__ import annotations

import hashlib
import json
import re

KINDS = ("vpn", "ru")
DEL = "del"
DOMAIN_RE = re.compile(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})")
MAX_DOMAINS = 500                       # доменов в каноне (у клиента VPN — 100)
MAX_EVENTS = 200                        # событий в одном сообщении
MAX_DOMAIN_LEN = 253
REJ_KEEP = 20                           # отвергнутых в ответе слоту


def valid_domain(d) -> bool:
    return (isinstance(d, str) and 4 <= len(d) <= MAX_DOMAIN_LEN and "\n" not in d
            and DOMAIN_RE.fullmatch(d) is not None)


def norm_domain(d) -> str:
    return str(d or "").strip().lower()


def clean(items) -> dict[str, str]:
    """{домен: вид} из списка пар [домен, вид] (или словаря): мусор выброшен,
    домен в обоих видах — «напрямую», как решает nft; не больше MAX_DOMAINS."""
    pairs = items.items() if isinstance(items, dict) else (
        (r[0], r[1]) for r in (items or []) if isinstance(r, (list, tuple)) and len(r) >= 2)
    out: dict[str, str] = {}
    for d, k in pairs:
        d, k = norm_domain(d), str(k or "").strip().lower()
        if not valid_domain(d) or k not in KINDS:
            continue
        if out.get(d) == "ru":
            continue
        out[d] = k
    if len(out) > MAX_DOMAINS:
        out = dict(sorted(out.items())[:MAX_DOMAINS])
    return out


def digest(gen: str, ver: int, items: dict, upto) -> str:
    """Отпечаток канона для слота: sha256 канонического JSON; upto — [run, n]
    последнего разобранного события этого слота, у каждого слота свой."""
    canon = json.dumps({"gen": str(gen or ""), "ver": int(ver or 0),
                        "items": sorted((d, k) for d, k in clean(items).items()),
                        "upto": [str((upto or ["", 0])[0] or ""), int((upto or ["", 0])[1] or 0)]},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def canon_items(canon: dict) -> dict[str, str]:
    """{домен: вид} из канона {домен: [вид, слот, время]}."""
    raw = (canon or {}).get("items") or {}
    if not isinstance(raw, dict):
        return {}
    return clean({d: (v[0] if isinstance(v, (list, tuple)) and v else v) for d, v in raw.items()})


def _denied(d: str, deny) -> bool:
    for host in deny or ():
        host = norm_domain(host)
        if host and (d == host or d.endswith("." + host)):
            return True
    return False


def merge(canon: dict, slot_id: int, run: str, events, upto, now_iso: str, deny=()) -> tuple:
    """Слияние событий слота в канон — правила слияния.
    (канон, upto, отвергнутые [[домен, причина]], изменился). Повтор того же
    run с n ≤ сохранённого пропускается; новый run — новый счёт."""
    canon = dict(canon or {})
    items = dict(canon.get("items") or {}) if isinstance(canon.get("items"), dict) else {}
    ver = int(canon.get("ver") or 0)
    run = str(run or "")[:32]
    up_run, up_n = (str((upto or ["", 0])[0] or ""), int((upto or ["", 0])[1] or 0))
    changed = False
    rejected: list[list[str]] = []
    for ev in list(events or [])[:MAX_EVENTS]:
        if not isinstance(ev, (list, tuple)) or len(ev) < 3:
            continue
        try:
            n = int(ev[0])
        except (TypeError, ValueError):
            continue
        if run == up_run and n <= up_n:
            continue                                  # повтор после оборванной сессии
        up_run, up_n = run, max(n, up_n if run == up_run else 0)
        d, kind = norm_domain(ev[1]), str(ev[2] or "").strip().lower()
        init = bool(ev[3]) if len(ev) > 3 else False
        if not valid_domain(d) or kind not in (*KINDS, DEL):
            rejected.append([str(ev[1])[:64], "не домен"])
            continue
        if _denied(d, deny):
            rejected.append([d, "хост сервера"])
            continue
        cur = items.get(d)
        cur_kind = cur[0] if isinstance(cur, (list, tuple)) and cur else None
        if kind == DEL:
            if cur is not None:
                del items[d]
                changed = True
            continue
        if init and cur_kind == "ru":
            continue                                  # первое слияние: «напрямую» сильнее
        if cur is None and len(items) >= MAX_DOMAINS:
            rejected.append([d, f"максимум {MAX_DOMAINS} доменов"])
            continue
        if cur_kind != kind:
            items[d] = [kind, int(slot_id), str(now_iso)]
            changed = True
    if changed:
        ver += 1
    canon.update({"items": items, "ver": ver})
    return canon, [up_run, up_n], rejected[:REJ_KEEP], changed


def diff(local: dict, expected: dict) -> list[tuple[str, str]]:
    """Чем файлы (local) отличаются от ожидаемого (база ⊕ pending): появилось —
    вид, пропало — del, сменило вид — новый вид. По алфавиту."""
    out: list[tuple[str, str]] = []
    for d in sorted(set(local) | set(expected)):
        a, b = expected.get(d), local.get(d)
        if a == b:
            continue
        out.append((d, b if b else DEL))
    return out


def apply_events(base: dict, events) -> dict[str, str]:
    """База ⊕ события (в порядке номеров) → ожидаемое содержимое файлов."""
    out = dict(base or {})
    for ev in sorted((e for e in events or [] if isinstance(e, (list, tuple)) and len(e) >= 3),
                     key=lambda e: int(e[0])):
        d, kind = norm_domain(ev[1]), str(ev[2] or "")
        if kind == DEL:
            out.pop(d, None)
        elif kind in KINDS:
            out[d] = kind
    return out


def parse_list(out: str) -> dict[str, str]:
    """Вывод `awg-lan-domain.sh list` («vpn d» / «ru d») → {домен: вид};
    домен в обоих — «напрямую». Строки не по правилу домена отбрасываются."""
    pairs = []
    for line in (out or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in KINDS:
            pairs.append([parts[1].strip(), parts[0]])
    return clean(pairs)


def render_sync(items: dict) -> str:
    """Файл для `sync`: «vpn d» / «ru d», по алфавиту, только чистые строки."""
    rows = [f"{k} {d}" for d, k in sorted(clean(items).items())]
    return "\n".join(rows) + ("\n" if rows else "")


def counts(items: dict) -> tuple[int, int]:
    """(в туннель, напрямую)."""
    vals = list((items or {}).values())
    return vals.count("vpn"), vals.count("ru")
