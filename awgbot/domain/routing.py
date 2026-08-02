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
    """Домен уже покрыт базовой частью — он в одной из национальных зон.

    Добавлять такой в личный список бессмысленно: базовая директива матчит зону
    со всеми поддоменами, и общее правило маркировки сработает и без него.
    Пользователю про это говорим явно, иначе он решит, что кнопка не работает.
    """
    return zone_of(domain) in set(base_zones or ())


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
    base_zones,
    domains_by_client: dict[int, list[str]],
    set_base: str,
    set_user_prefix: str,
) -> str:
    """Собирает ВЕСЬ конфиг списков одним файлом.

    Один домен — одна директива со всеми наборами, куда он входит:
        ipset=/sberbank.ru/ru_u3,ru_u7
    Так единственный резолв наполняет все нужные наборы разом, и вопрос
    «попадёт ли адрес в набор второго клиента при ответе из кэша» не возникает.
    Разносить директивы одного домена по разным файлам нельзя — на слияние в
    этом случае полагаться не стоит.

    Файл генерится целиком и перезаписывается: инкрементальные правки означали бы
    второй источник истины (файл против БД), который однажды разойдётся.
    """
    lines = [
        "# Сгенерировано awg-bot. Правки будут перезаписаны.",
        "# Условная маршрутизация: домены → ipset → маркировка → туннель до шлюза.",
        "",
        "# Базовая часть: национальные зоны целиком (со всеми поддоменами).",
    ]
    for zone in base_zones or ():
        zone = (zone or "").strip().lower()
        if zone:
            lines.append(f"ipset=/{zone}/{set_base}")

    # домен → наборы клиентов, у которых он в личном списке
    per_domain: dict[str, list[str]] = {}
    for client_id, domains in sorted((domains_by_client or {}).items()):
        for dom in domains:
            per_domain.setdefault(dom, []).append(f"{set_user_prefix}{client_id}")

    if per_domain:
        lines += ["", "# Личные списки пользователей."]
        for dom in sorted(per_domain):
            lines.append(f"ipset=/{dom}/{','.join(per_domain[dom])}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "DomainRejected", "normalize", "zone_of", "covered_by_base", "is_denied",
    "parse_batch", "build_dnsmasq_conf", "MAX_DOMAIN_LEN", "MAX_LABEL_LEN",
]
