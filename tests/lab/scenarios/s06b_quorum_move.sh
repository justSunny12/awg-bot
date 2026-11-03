#!/bin/bash
# s06b_quorum_move — вторая половина s06_quorum_move (тот же файл, пункты г, и, е и ж, д — см. его шапку):
# пункты независимы, половины идут на разных ВМ стенда параллельно. Случай 3 (д) — на быстром уровне
# (LAB_FAST=1, T_up_rf 2 мин) и при LAB_LONG=1 (на полном уровне ≈ 20 мин); LAB_S06B — свой выбор пунктов.
set -u
parts="${LAB_S06B:-g i e}"
if [ -z "${LAB_S06B:-}" ] && { [ "${LAB_LONG:-}" = 1 ] || [ "${LAB_FAST:-}" = 1 ]; }; then parts="$parts d"; fi
LAB_SCEN=s06b_quorum_move LAB_S06="$parts" exec bash "$(dirname "$0")/s06_quorum_move.sh"
