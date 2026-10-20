#!/bin/bash
# LIVS-Stock 每日全链路（2026-10-20 重构版）
# 顺序：事实层 → WB预测 → 大海对齐视图 → 推送 → 对账 → 修正表
# 用法：./run_daily.sh [YYYY-MM-DD] [--close]
#   --close  收盘后跑（数据日==目标日）；不带则允许 T-1（盘前跑）
set -uo pipefail
cd "$(dirname "$0")"

DATE="${1:-$(date +%F)}"
MODE="${2:-}"
EXTRA=""
[ "$MODE" = "--close" ] && EXTRA=""

echo "================ LIVS-Stock $DATE 全链路 ================"

# 0. 交易日检查
DOW=$(date -d "$DATE" +%u 2>/dev/null || echo 0)
if [ "$DOW" -ge 6 ]; then
  echo "[SKIP] $DATE 是周末"
  exit 0
fi

# 1. 事实层（唯一数据源）
python3 facts_builder.py --date "$DATE" --allow-t1 || { echo "[FAIL] 事实层构建失败"; exit 1; }
GATE=$(python3 -c "import json;print(json.load(open('facts/$DATE.json'))['quality_gate'])")
[ "$GATE" = "FAIL" ] && { echo "[FAIL] 质量门 FAIL，终止"; exit 1; }

# 2. WB 预测（读事实层，不推）
python3 daily_predict.py --date "$DATE" --allow-t1 --no-push || { echo "[FAIL] 预测失败"; exit 1; }

# 3. 大海对齐视图（独立文件，不覆盖原生）
python3 export_dahai_format.py --date "$DATE" || echo "[WARN] 对齐视图生成失败（不阻断）"

# 4. 推送 facts + 预测 + 对齐视图
python3 - <<PY
import push_api as P
for rel in ["facts/$DATE.json", "predictions/$DATE.json", "predictions/${DATE}_wbview.json"]:
    print(f"[PUSH] {rel}: {P.put_file('/workspace/finance_task/' + rel, rel)}")
PY

# 5. 对账（等大海文件到位）
python3 reconcile_wb_dahai.py --dates "$DATE" || echo "[WARN] 对账失败"

# 6. 修正表
python3 correction_table.py --date "$DATE" 2>/dev/null || echo "[INFO] 修正表跳过"

echo "================ 完成 ================"
