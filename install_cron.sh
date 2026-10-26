#!/bin/bash
# 一键安装 WB 决策时点（2026-10-26 时点统一版）
#   08:36  盘前卖     —— 老板个人操作节奏（T-1 数据，不参与对账）
#   14:30  尾盘买     —— 老板个人操作节奏（T 日实时，不参与对账）
#   15:35  收盘预测   —— ★双模型对齐唯一时点：T 日收盘数据 → T+1 预测
# 用法：./install_cron.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

{
  crontab -l 2>/dev/null | grep -v "cron_runner.py" || true
  echo "36 8  * * 1-5 cd $DIR && python3 cron_runner.py >> $DIR/logs/cron.log 2>&1"
  echo "30 14 * * 1-5 cd $DIR && python3 cron_runner.py >> $DIR/logs/cron.log 2>&1"
  echo "35 15 * * 1-5 cd $DIR && python3 cron_runner.py >> $DIR/logs/cron.log 2>&1"
} | crontab -

echo "[OK] 已安装："
crontab -l | grep cron_runner
echo
echo "★ 对齐时点：15:35（双方都用当日收盘，预测对象 T+1）"
echo "日志：$DIR/logs/cron.log"
