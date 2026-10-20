#!/bin/bash
# 一键安装 WB 每日定时任务（收盘后 15:10 跑全链路）
# 用法：./install_cron.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

LINE="10 15 * * 1-5 cd $DIR && python3 cron_runner.py >> $DIR/logs/cron.log 2>&1"

# 清掉旧的 WB 任务行（避免三条 11:30/14:00/16:00 并存）
( crontab -l 2>/dev/null | grep -v "cron_runner.py" ; echo "$LINE" ) | crontab -

echo "[OK] 已安装："
crontab -l | grep cron_runner
echo
echo "时点：每周一至周五 15:10（收盘后），走 run_daily.sh 全链路"
echo "日志：$DIR/logs/cron.log 与 logs/daily_predict-YYYY-MM-DD.log"
