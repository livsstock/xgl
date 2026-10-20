#!/bin/bash
# LIVS-Stock 双时点决策（2026-10-20）
#   08:36  盘前卖 —— 用 T-1 收盘，持仓止盈/止损/到期
#   14:30  尾盘买 —— 用 T 日实时价估算收盘态
# 用法：./run_decision_points.sh [YYYY-MM-DD] [0836|1430]
set -uo pipefail
cd "$(dirname "$0")"

D="${1:-$(date +%F)}"
P="${2:-}"

# 交易日检查
python3 - <<PY
import sys; sys.path.insert(0, "reconcile")
from trade_calendar import is_trading_day, describe
if not is_trading_day("$D"):
    print(f"[SKIP] $D 非交易日（{describe('$D')}）"); sys.exit(1)
PY
[ $? -eq 1 ] && exit 0

python3 decision_points.py --date "$D" --point "${P:-1430}" --push
