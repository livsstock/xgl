#!/bin/bash
# 一键安装 WB 三个决策时点（与老板实际操作节奏对齐，2026-10-20）
#   08:36  盘前卖     —— T-1 收盘数据，持仓止盈/止损/到期
#   14:30  尾盘买     —— T 日实时价估算收盘态
#   15:10  收盘全链路 —— 事实层 + T+1 预测 + 大海对账
# 用法：./install_cron.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

{
  crontab -l 2>/dev/null | grep -v "cron_runner.py" || true
  echo "36 8  * * 1-5 cd $DIR && python3 cron_runner.py >> $DIR/logs/cron.log 2>&1"
  echo "30 14 * * 1-5 cd $DIR && python3 cron_runner.py >> $DIR/logs/cron.log 2>&1"
  echo "10 15 * * 1-5 cd $DIR && python3 cron_runner.py >> $DIR/logs/cron.log 2>&1"
} | crontab -

echo "[OK] 已安装："
crontab -l | grep cron_runner
echo
echo "日志：$DIR/logs/cron.log"
echo "      $DIR/logs/decision-0836-YYYY-MM-DD.log   盘前卖"
echo "      $DIR/logs/decision-1430-YYYY-MM-DD.log   尾盘买"
echo "      $DIR/logs/daily_predict-YYYY-MM-DD.log   收盘全链路"
