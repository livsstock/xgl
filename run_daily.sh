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

# 0. 交易日检查（周末 + 法定节假日）
python3 - <<PY
import sys
sys.path.insert(0, "reconcile")
from trade_calendar import is_trading_day, describe
d = "$DATE"
if not is_trading_day(d):
    print(f"[SKIP] {d} 非交易日（{describe(d)}）")
    sys.exit(1)
PY
[ $? -eq 1 ] && exit 0

# 1. 事实层（唯一数据源，v2 格式 —— 大海 build_facts.py 为唯一入口）
python3 build_facts.py --date "$DATE"
RC=$?
if [ $RC -eq 2 ]; then
  echo "[ABORT] $DATE 无可用数据（非交易日 / 未收盘 / 接口异常）→ 今日不出预测"
  echo "        这是主动拦截，不是故障：宁可不跑，不拿空包或旧数据冒充当日判断"
  exit 2
elif [ $RC -ne 0 ]; then
  echo "[FAIL] 事实层构建失败 (exit=$RC)"; exit 1
fi
Q=$(python3 -c "import json;print(json.load(open('facts/$DATE.json'))['overall_quality'])")
N=$(python3 -c "import json;print(len(json.load(open('facts/$DATE.json'))['instruments']))")
echo "[FACTS] 标的数=$N 质量=$Q"
[ "$Q" != "complete" ] && { echo "[WARN] 事实层质量=$Q（继续，但对账会被标记 partial）"; }

# 2. WB 预测（读事实层，不推）
python3 daily_predict.py --date "$DATE" --allow-t1 --no-push || { echo "[FAIL] 预测失败"; exit 1; }

# 3. 备份 WB 原生格式（大海格式会占用 predictions/{date}.json 主路径）
cp "predictions/$DATE.json" "predictions/${DATE}_wb_native.json"

# 4. 导出为大海 schema → 写入 predictions/{date}.json（大海读这个路径）
python3 export_dahai_format.py --date "$DATE" || echo "[WARN] 对齐视图生成失败（不阻断）"

# 5. 推送 facts + 预测 + 对齐视图
python3 - <<PY
import push_api as P
for rel in ["facts/$DATE.json", "predictions/$DATE.json",
            "predictions/${DATE}_wb_native.json", "predictions/${DATE}_wbview.json"]:
    print(f"[PUSH] {rel}: {P.put_file('/workspace/finance_task/' + rel, rel)}")
PY

# 6. 校验（大海 validate_opinion.py R1-R7）
if [ -f "predictions/$DATE.json" ]; then
  python3 validate_opinion.py "predictions/${DATE}_wb_native.json" --facts "facts/$DATE.json" \
    || echo "[WARN] 观点校验未全过（见 validate_reports/）"
fi

# 7. 对账（大海 reconcile_v2.py，双方 file 都在才跑）
if [ -f "predictions/${DATE}_dahai.json" ]; then
  python3 reconcile_v2.py --dahai "predictions/${DATE}_dahai.json" \
    --wb "predictions/${DATE}_wb_native.json" --facts "facts/$DATE.json" || echo "[WARN] 对账失败"
else
  echo "[INFO] 大海文件未到位，跳过对账：predictions/${DATE}_dahai.json"
fi

# 8. 同基准对账（关键：消除 T/T-1 时点错位造成的"假分歧"）
if [ -f "predictions/${DATE}_dahai.json" ]; then
  python3 same_basis_reconcile.py --date "$DATE" || echo "[WARN] 同基准对账失败"
fi

# 8.5 往返校验（关键：先证明口径一致，再谈分歧。
#     不能复现 = 假分歧，样本作废，不能进修正表）
if [ -f "predictions/${DATE}_dahai.json" ]; then
  python3 roundtrip_check.py --date "$DATE" \
    || echo "[WARN] 往返校验存在不可复现项（该日分歧样本不可用）"
fi

# 9. 修正表
python3 correction_table.py --date "$DATE" 2>/dev/null || echo "[INFO] 修正表跳过"

echo "================ 完成 ================"

# 10. 每日简报（老板要求的每日一次预测汇报 + 验证总结）
python3 daily_brief.py --date "$DATE" || echo "[WARN] 简报生成失败"

# 11. 导出 opinions-wb/{T+1}_wb.json（大海 14:45 尾盘复核的输入，修复 10-16 断更）
python3 export_opinions_wb.py --date "$DATE" || echo "[WARN] opinions-wb 导出失败"

# 12. WB 简短预测总结（大盘/5股指/6精选/26池 格式，老板要求）
python3 report_summary.py --date "$DATE" || echo "[WARN] 总结报告生成失败"
