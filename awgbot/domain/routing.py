"""
routing.py — чистая логика условной маршрутизации: нормализация пользовательских
доменов и генерация конфига dnsmasq.

Здесь НЕТ обращений к инфраструктуре — только преобразования строк. Команды
(ipset/iptables/systemctl) живут в infra/routing.py. Разделение то же, что у
configgen.py против awg.py: чистое тестируется без сервера, грязное — руками.

Зачем нормализация вообще: человек вставляет не домен, а то, что скопировал из
адресной строки — `https://www.sberbank.ru/ru/person?x=1`. Требовать от него
ручной чистки бессмысленно, он просто не будет пользоваться списком.
"""

from __future__ import annotations

import re

# Метка домена: буквы/цифры/дефис, не начинается и не кончается дефисом.
_RE_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Похоже на IPv4 — отсекаем: принимаем ТОЛЬКО доменные имена (решение концепта,
# §5). CIDR и адреса dnsmasq через `ipset=` не обрабатывает в принципе.
_RE_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

MAX_DOMAIN_LEN = 253
MAX_LABEL_LEN = 63


class DomainRejected(Exception):
    """Домен не принят. Текст — готовая причина для пользователя."""


def normalize(raw: str) -> str:
    """Приводит пользовательский ввод к голому домену в нижнем регистре.

    Срезает схему, креды, порт, путь, `www.` и завершающую точку; IDN переводит
    в punycode (`сбербанк.рф` → `xn--80aesfpebagmfblc0a.xn--p1ai`), потому что
    dnsmasq сравнивает домены байтами и кириллицу как есть не поймёт.

    Поднимает DomainRejected с человекочитаемой причиной — вызывающий показывает
    её пользователю дословно.
    """
    s = (raw or "").strip().strip('.,;"\'<>()[]')
    if not s:
        raise DomainRejected("пусто")

    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)   # схема
    s = s.split("/", 1)[0]                               # путь
    s = s.split("?", 1)[0].split("#", 1)[0]              # query/fragment
    if "@" in s:                                         # user:pass@host или e-mail
        s = s.rsplit("@", 1)[1]
    s = s.rsplit(":", 1)[0] if re.search(r":\d+$", s) else s   # порт
    s = s.strip().rstrip(".").lower()
    if s.startswith("www."):
        s = s[4:]
    if not s:
        raise DomainRejected("пусто")

    # IDN → punycode. Делаем ДО валидации меток: те работают с ASCII.
    if any(ord(ch) > 127 for ch in s):
        try:
            s = s.encode("idna").decode("ascii")
        except UnicodeError:
            raise DomainRejected("не удалось разобрать имя")

    if _RE_IPV4.match(s):
        raise DomainRejected("это IP-адрес, а нужен домен")
    if len(s) > MAX_DOMAIN_LEN:
        raise DomainRejected("слишком длинное имя")

    labels = s.split(".")
    if len(labels) < 2:
        raise DomainRejected("нужно полное имя с зоной, например sberbank.ru")
    for lab in labels:
        if not lab or len(lab) > MAX_LABEL_LEN or not _RE_LABEL.match(lab):
            raise DomainRejected("недопустимые символы в имени")
    if labels[-1].isdigit():
        raise DomainRejected("зона не может быть числом")
    return s


def zone_of(domain: str) -> str:
    """Зона верхнего уровня (последняя метка)."""
    return domain.rsplit(".", 1)[-1]


def covered_by_base(domain: str, base_zones) -> bool:
    """Оставлено для совместимости вызовов: с инверсией логики базовый набор
    наполняется извне готовыми списками, и проверить принадлежность к нему по
    имени домена нельзя — только по адресу, уже после резолва. Пользователю
    добавление лишнего не вредит: правило матчит объединение наборов."""
    return False


def is_denied(domain: str, denylist) -> bool:
    """Домен запрещён к добавлению: он сам или его родитель есть в денай-листе.

    Денай-лист защищает от двух вещей: увода в туннель собственной управляющей
    инфраструктуры (заперев себя) и от `ipset=/ru/...`-подобных записей, которые
    загнали бы туда пол-интернета. Совпадение по суффиксу — чтобы `mail.X` не
    проезжал, когда запрещён `X`.
    """
    for bad in denylist or ():
        bad = (bad or "").strip().lower().lstrip(".")
        if not bad:
            continue
        if domain == bad or domain.endswith("." + bad):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Разбор пользовательского ввода пачкой
# ─────────────────────────────────────────────────────────────────────────────

def parse_batch(text: str, *, base_zones=(), denylist=()) -> tuple[list[str], list[tuple[str, str]]]:
    """Разбирает многострочный/через-запятую ввод.

    Возвращает (принятые, отклонённые) — где отклонённые это (исходная_строка,
    причина). Пользователь вставляет списком, и молча проглотить половину
    введённого нельзя: он должен видеть, что именно не взяли и почему.

    Дубли внутри одной пачки схлопываются молча — это не ошибка ввода.
    """
    chunks = [c for c in re.split(r"[\s,;]+", text or "") if c.strip()]
    accepted: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in chunks:
        try:
            dom = normalize(chunk)
        except DomainRejected as e:
            rejected.append((chunk, str(e)))
            continue
        if dom in seen:
            continue
        if is_denied(dom, denylist):
            rejected.append((chunk, "этот домен добавлять нельзя"))
            continue
        if covered_by_base(dom, base_zones):
            rejected.append((chunk, "уже покрыт базовым списком — добавлять не нужно"))
            continue
        seen.add(dom)
        accepted.append(dom)
    return accepted, rejected


# ─────────────────────────────────────────────────────────────────────────────
# Генерация конфига dnsmasq
# ─────────────────────────────────────────────────────────────────────────────

def build_dnsmasq_conf(
    *,
    base_domains=(),
    domains_by_client: dict[int, list[str]],
    client_ids=(),
    set_user_prefix: str,
) -> str:
    """Конфиг доменов одним файлом: у каждого профиля СВОЙ набор = база + личные.

    Пер-юзерный merge, а не общий набор плюс личный. Причина в поведении dnsmasq:
    для домена применяется ТОЛЬКО ОДНА директива `ipset=`. Общий набор рядом с
    личными означал бы, что домен из базы, добавленный кем-то себе, перестаёт
    попадать в общий — и у остальных уезжает на шлюз. Один человек ломал бы
    маршрутизацию другим.

    Здесь домен получает одну директиву со списком наборов ВСЕХ, кому он нужен:
        ipset=/blocked.com/vpn_u2,vpn_u7      (базовый — всем включённым)
        ipset=/example.com/vpn_u2               (личный — только этому)

    Правило маркировки тогда одно и с одним отрицанием: «не в моём наборе → на
    шлюз». Проверять пересечение двух наборов не нужно.

    Файл генерится целиком: инкрементальные правки означали бы второй источник
    истины (файл против БД), который однажды разойдётся.
    """
    def _s(cid) -> str:
        return f"{set_user_prefix}{cid}"

    live = sorted({int(c) for c in (client_ids or ())})
    per_domain: dict[str, list[str]] = {}
    for dom in base_domains or ():
        dom = (dom or "").strip().lower()
        if dom:
            per_domain.setdefault(dom, []).extend(_s(c) for c in live)
    for client_id, domains in sorted((domains_by_client or {}).items()):
        for dom in domains:
            per_domain.setdefault(dom, []).append(_s(client_id))

    lines = [
        "# Сгенерировано awg-bot. Правки будут перезаписаны.",
        "# Домены, которым нужен ЗАРУБЕЖНЫЙ адрес. У каждого профиля свой набор:",
        "# базовый список + личные исключения. Чего в наборе нет — уходит на шлюз.",
        "",
    ]
    for dom in sorted(per_domain):
        sets, seen = [], set()
        for s in per_domain[dom]:
            if s not in seen:
                seen.add(s)
                sets.append(s)
        if sets:
            lines.append(f"ipset=/{dom}/{','.join(sets)}")
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Разбор внешних списков
# ─────────────────────────────────────────────────────────────────────────────

_RE_CIDR = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")


def parse_networks(text: str) -> list[str]:
    """Вытащить IPv4-подсети из произвольного текста списка.

    Форматы у источников разные — простые построчные списки, JSON Google, — но
    подсеть везде выглядит одинаково, поэтому вытаскиваем регуляркой, а не
    парсим каждый формат отдельно. Мусор отсеивается проверкой октетов и маски:
    один битый CIDR в `ipset restore` роняет всю пачку.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}", text or ""):
        cidr = m.group(0)
        if cidr in seen:
            continue
        addr, mask = cidr.split("/")
        if int(mask) > 32 or any(int(o) > 255 for o in addr.split(".")):
            continue
        seen.add(cidr)
        out.append(cidr)
    return out


def parse_domain_list(text: str) -> list[str]:
    """Вытащить домены из внешнего списка.

    Возвращает ИМЕНА, а не готовые директивы: имя набора подставляется потом, при
    сборке общего файла. Иначе базовые домены пришлось бы писать отдельно от
    личных, а dnsmasq применяет для домена только одну директиву `ipset=` — и
    домен, попавший в оба файла, оказался бы лишь в одном наборе.

    Формат источников — строки `ipset=/домен/чужой_набор`; годится и простой
    построчный список доменов.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("ipset=/") or line.startswith("nftset=/"):
            parts = line.split("/")
            dom = parts[1] if len(parts) > 2 else ""
        else:
            dom = line
        dom = dom.strip().lower().strip(".")
        if not dom or "/" in dom or " " in dom or dom in seen:
            continue
        seen.add(dom)
        out.append(dom)
    return out


__all__ = [
    "DomainRejected", "normalize", "zone_of", "covered_by_base", "is_denied",
    "parse_batch", "build_dnsmasq_conf", "parse_networks", "parse_domain_list",
    "MAX_DOMAIN_LEN", "MAX_LABEL_LEN",
]
