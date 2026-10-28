#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron_runner.py — daily_predict 的 cron 守护包装（v1.0）

目的：让 daily_predict.py 真正成为"无人值守"的预测机制。
  - 时点判定（11:30 / 14:00 / 16:00 三个固定点）
  - 周末/节假日跳过
  - 失败重试 1 次
  - 退出码：0=成功 / 1=数据全挂 / 2=失败过多
  - 日志统一写到 logs/daily_predict-{date}.log

部署：
  crontab -e
  # WB 三个决策时点（与老板实际操作节奏对齐）
  36 8  * * 1-5 cd /workspace/finance_task && python3 cron_runner.py >> logs/cron.log 2>&1   # 盘前卖
  30 14 * * 1-5 cd /workspace/finance_task && python3 cron_runner.py >> logs/cron.log 2>&1   # 尾盘买
  10 15 * * 1-5 cd /workspace/finance_task && python3 cron_runner.py >> logs/cron.log 2>&1   # 收盘全链路
"""

import os
import subprocess
import sys
from datetime import datetime

PROJECT_DIR = "/workspace/finance_task"
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def is_trade_day():
    """委托 trade_calendar：周末 + 法定节假日都算非交易日。"""
    sys.path.insert(0, os.path.join(PROJECT_DIR, "reconcile"))
    from trade_calendar import is_trading_day
    return is_trading_day(datetime.now().strftime("%Y-%m-%d"))


def is_in_trade_window():
    """收盘后时段：15:05-23:59。

    [2026-10-20 改动] 原来 11:30/14:00/16:00 三个点跑 daily_predict，
    盘中跑拿到的是"未收盘的当日K线"，推给大海就是脏数据。
    现在只保留收盘后一次，走 run_daily.sh 全链路（事实层→预测→推送→对账）。
    """
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return 15 * 60 + 5 <= t <= 23 * 60 + 59


def main():
    """时点版（2026-10-29 老板定稿）：

    【盘中双时点 · 双模型对比】
      10:30  morning_sell_scan.py 卖出预测 → dual_compare.py 与大海对比出结论
      14:10  afternoon_scan.py 买入预测   → dual_compare.py 与大海对比出结论

    【收盘主产出 · 唯一对账时点】
      15:35  run_daily.sh 全链路（事实层→T日收盘预测→opinions-wb→简报→对账）
    """
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    if not is_trade_day():
        print(f"[{timestamp}] SKIP: 周末非交易日")
        return 0

    date_str = now.strftime("%Y-%m-%d")
    t = now.hour * 60 + now.minute

    if 10 * 60 + 25 <= t <= 10 * 60 + 45:
        cmd = ["bash", "-c",
               f"cd {PROJECT_DIR} && python3 morning_sell_scan.py && python3 dual_compare.py --point sell --date {date_str}"]
        tag = "1030"
    elif 14 * 60 + 5 <= t <= 14 * 60 + 25:
        cmd = ["bash", "-c",
               f"cd {PROJECT_DIR} && python3 afternoon_scan.py && python3 dual_compare.py --point buy --date {date_str}"]
        tag = "1410"
    elif 15 * 60 + 30 <= t <= 23 * 60 + 59:
        cmd = ["bash", os.path.join(PROJECT_DIR, "run_daily.sh"), date_str]
        tag = "close"
    else:
        print(f"[{timestamp}] SKIP: 非决策时点")
        return 0

    log_path = os.path.join(LOG_DIR, f"decision-{tag}-{date_str}.log")
    print(f"[{timestamp}] RUN[{tag}]: {' '.join(cmd)}")
    print(f"[{timestamp}] LOG: {log_path}")

    for attempt in (1, 2):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== cron_runner {tag} attempt {attempt} @ {timestamp} ===\n")
            rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
        if rc == 0:
            print(f"[{timestamp}] OK[{tag}] attempt {attempt}")
            return 0
        if rc == 2:
            print(f"[{timestamp}] ABORT[{tag}]: 当日无可用数据（非交易日/未收盘/接口异常）")
            return 0
        print(f"[{timestamp}] FAIL[{tag}] attempt {attempt} rc={rc}")

    print(f"[{timestamp}] FINAL FAIL[{tag}] after 2 attempts")
    return 2


if __name__ == "__main__":
    sys.exit(main())