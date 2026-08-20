"""
Проверки routing-host-setup.sh — обвяза ВПС под условную маршрутизацию.

Скрипт правит iptables и /etc, гонять его в CI негде. Но самые дорогие ошибки в
нём текстовые: правило встаёт не в том порядке, откат не снимает то, что ставил
apply, подсказка оператору называет несуществующий набор. Всё это ловится
чтением исходника, а цена — выезд на боевой сервер.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-host-setup.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


# ── обход перехвата DNS ──────────────────────────────────────────────────────

# Имена, которые клиент обязан отрезолвить обычным DNS, прежде чем уйти в DoH.
# Ловить DoH по адресам бесполезно: эндпоинты живут на CDN, а не на анкасте
# резолвера, — поэтому единственная точка перехвата именно здесь.
DOH_ENDPOINTS = [
    "cloudflare-dns.com",
    "chrome.cloudflare-dns.com",
    "dns.google",
    "dns.quad9.net",
]


@pytest.mark.parametrize("host", DOH_ENDPOINTS)
def test_doh_endpoint_is_nxdomained(script, host):
    """DoH-эндпоинты гасятся в dnsmasq.

    Набор наполняет ТОЛЬКО наш резолвер: готовых списков российских подсетей
    не существует, всё держится на директивах ipset= в dnsmasq. Клиент, ушедший
    в DoH, спрашивает мимо нас и не добавляет в набор ничего — режим у него
    просто не работает. Отказ заметен (российские сервисы ругаются на адрес),
    но со стороны пользователя необъясним, поэтому закрываем его на входе.
    """
    assert f"address=/{host}/" in script


def test_dot_is_rejected_not_dropped(script):
    """DoT (853) режем REJECT'ом.

    DROP заставил бы клиента ждать таймаута на каждом запросе — резолв подвисал
    бы вместо того, чтобы откатиться на :53.
    """
    assert "--dport 853" in script
    assert "-j REJECT --reject-with tcp-reset" in script
    assert re.search(r"--dport 853[^\n]*\n?[^\n]*-j DROP", script) is None


def test_dot_reject_is_inserted_after_the_accepts(script):
    """Порядок в файле = порядок в цепочке, наоборот.

    ensure_rule вставляет через -I, то есть КАЖДОЕ следующее правило встаёт выше
    предыдущего. Значит блок с REJECT обязан идти в файле ПОЗЖЕ разрешающих
    ACCEPT — иначе ACCEPT окажется в цепочке над ним и REJECT не сработает
    никогда, молча и без единой ошибки.
    """
    # только часть apply: в откате порядок обратный и к цепочке отношения не имеет
    body = script[script.index("# 4) выход наружу"):]
    accept = body.index('ensure_rule filter FORWARD -s "$CLIENT_SUBNET" -j ACCEPT')
    reject = body.index("--dport 853")
    assert reject > accept, (
        "REJECT 853 стоит в файле раньше ACCEPT — из-за -I он окажется в цепочке "
        "ниже разрешающего правила и не сработает")


def test_rollback_removes_what_apply_installs(script):
    """Откат снимает правило DoT.

    Забытое в откате правило переживает rollback и продолжает резать 853 уже
    после того, как фичу сняли, — отказ, который никто не свяжет с ботом.
    """
    rollback = script.split('if [ "$MODE" = "rollback" ]', 1)[1]
    assert "--dport 853" in rollback


# ── подсказки оператору ──────────────────────────────────────────────────────

def test_hint_does_not_name_a_set_nobody_creates(script):
    """В подсказках не должно быть ru_base/vpn_base.

    Общего базового набора в схеме нет: база вливается в каждый vpn_u<N>.
    Подсказка с несуществующим именем давала ноль на исправном хосте и
    отправляла чинить CAP_NET_ADMIN, который в порядке.
    """
    assert "ru_base" not in script
    assert "vpn_base" not in script


def test_fill_hint_uses_a_domain_that_will_be_in_the_set(script):
    """Домен в проверке наполнения обязан быть тем, что реально попадёт в набор.

    Набор перечисляет РОССИЙСКИЕ сервисы — то, что уходит на шлюз. Подсказка с
    заграничным доменом показывала бы ноль на здоровой системе и толкала чинить
    исправное. Раньше здесь стоял huggingface.co: он был верен для упразднённой
    обратной модели, где набор означал «за границу».

    Сверять со списком нельзя — он скачивается, а не зашит. Проверяем то, что
    проверяемо: домен в российской зоне.
    """
    hints = re.findall(r"""dig \+short @\$DNS_ADDR ([^\s"']+)""", script)
    assert hints, "в скрипте нет подсказки с dig — проверка наполнения потерялась"
    # example.com — проверка живости резолвера, она вне списков
    checked = [h for h in hints if h != "example.com"]
    assert checked, "нет ни одной проверки наполнения набора"
    for host in checked:
        assert host.endswith((".ru", ".рф", ".su")), (
            f"{host} не в российской зоне — в набор он не попадёт, "
            f"и подсказка будет врать")


# ── режим awg: контейнер или хост ────────────────────────────────────────────

def test_runtime_is_read_from_the_same_yaml_as_the_bot(script):
    """Режим берётся из app.yaml, а не из переменной окружения.

    Два источника истины разошлись бы ровно один раз — и обвяз собрался бы под
    другой режим, чем работает бот: маршрут до клиентской подсети ушёл бы в
    никуда, а отказ выглядел бы как «у включённых пропал интернет».
    """
    assert "/^  runtime:/" in script, "runtime не читается из app.yaml"
    assert 'AWG_RUNTIME="${AWG_RUNTIME:-docker}"' in script, "дефолт обязан быть docker"


def test_container_is_not_awaited_in_host_mode(script):
    """Ожидание контейнера обёрнуто в проверку режима.

    Без неё --apply на переехавшем сервере висел бы 30 секунд и падал с
    «не удалось узнать адрес контейнера», хотя контейнера там уже быть не должно.
    """
    body = script[script.index("CONT_IP=\"\""):script.index("say \"Параметры:\"")]
    assert 'if [ "$AWG_RUNTIME" = "docker" ]' in body


def test_rollback_survives_empty_container_ip(script):
    """При set -e несработавший тест в AND-списке завершает скрипт.

    `[ -n "$CONT_IP" ] && run ...` в host-режиме оборвал бы откат на середине,
    не сняв ни dnsmasq, ни правила, — и выглядело бы это как успешный откат.
    """
    rollback = script.split('if [ "$MODE" = "rollback" ]', 1)[1]
    assert '[ -n "$CONT_IP" ] && run' not in rollback
    assert 'if [ -n "$CONT_IP" ]; then' in rollback


def test_unit_does_not_require_docker_in_host_mode(script):
    """Юнит в host-режиме не должен зависеть от docker.service.

    Иначе обвяз остался бы заложником сервиса, который переезд как раз убирает:
    после ребута юнит не поднялся бы, а фича молча не завелась.
    """
    unit = script[script.index('if [ "$MODE" = "unit" ]'):script.index("UNITEOF")]
    assert "UNIT_DEPS" in unit
    assert 'if [ "$AWG_RUNTIME" = "host" ]' in unit


def test_unit_carries_the_runtime_into_execstart(script):
    """ExecStart обязан нести режим.

    Юнит пишется один раз, а срабатывает после каждого ребута — без переменной
    он применил бы docker-ветку и прописал маршрут через контейнер, которого
    больше нет.
    """
    assert "AWG_RUNTIME=$AWG_RUNTIME" in script


# ── маршрут и DNS: два отказа переезда ───────────────────────────────────────

def test_stale_route_is_removed_in_host_mode(script):
    """Мало НЕ ставить свой маршрут — надо снять чужой.

    Via-маршрут, оставшийся от контейнерной схемы, переживает переезд и
    перекрывает connected: наружу всё уходит, а ответы клиентам отправляются в
    мёртвую docker-сеть. Диагностика при этом зелёная — маршрут ведь есть.
    """
    body = script.split('step "5.', 1)[1].split("step ", 1)[0]
    assert "ip route del" in body
    assert "AWG_IF" in body, "чужой маршрут отличается от connected по интерфейсу"


def test_dnatted_dns_is_allowed_into_input(script):
    """После DNAT назначение — НАШ адрес, и пакет идёт в INPUT, а не в FORWARD.

    При политике DROP он там умирает: клиент подключается, ходит по голым
    адресам (телеграм работает), а всё, что требует резолва, мертво. В
    docker-режиме не всплывало — контейнер маскарадил клиентов, и DNAT по
    -s подсети до них не доставал.
    """
    assert 'ensure_rule filter INPUT -s "$CLIENT_SUBNET" -d "$DNS_ADDR" -p udp --dport 53 -j ACCEPT' in script
    dnat = script.index("3. Перехват DNS")
    allow = script.index('step "3a.')
    forward = script.index("# 4) выход наружу")
    assert dnat < allow < forward, "разрешение в INPUT должно идти сразу за DNAT"


def test_input_allow_is_rolled_back(script):
    rollback = script.split('if [ "$MODE" = "rollback" ]', 1)[1].split("exit 0", 1)[0]
    assert "filter INPUT" in rollback


def test_verification_hint_names_a_domain_that_can_actually_appear(script):
    """Подсказка проверки обязана называть РОССИЙСКИЙ домен.

    Набор перечисляет то, чему нужен российский адрес, — значит наполнение
    доказывается доменом из этого списка, и только им. Ровно здесь подсказка
    однажды разошлась с моделью: скрипт предлагал резолвить gosuslugi.ru и тут
    же писал, что домен обязан быть из ЗАРУБЕЖНОГО списка, а sberbank.ru в
    наборе «не окажется никогда». Читается это на финальном шаге установки, то
    есть ровно в момент решения «работает или нет», — и исправная установка
    признавалась сломанной.
    """
    tail = script.split('step "Проверка"', 1)[1]
    assert "gosuslugi.ru" in tail, "нужен пример домена из российского списка"
    assert "ЗАРУБЕЖНОГО списка" not in tail
    assert "Логика инвертирована" not in tail


def test_unit_points_at_a_permanent_path(script):
    """Та же дыра, что нашлась на шлюзе, была и здесь: юнит ссылался на каталог,
    откуда запустили скрипт. На ВПС это не выстрелило только потому, что его
    запускали из постоянного места, — но ничто этого не гарантировало."""
    assert 'SELF="$(install_self)"' in script
    assert 'SELF="$(readlink -f "$0")"' not in script
    assert "/usr/local/sbin" in script
