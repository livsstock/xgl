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
  # WB 每日全链路（收盘后 15:10，走 run_daily.sh）
  10 15 * * 1-5 cd /workspace/finance_task && python3 cron_runner.py >> logs/cron.log 2>&1
"""

import os
import subprocess
import sys
from datetime import datetime

PROJECT_DIR = "/workspace/finance_task"
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def is_trade_day():
    """工作日才算交易日（A 股节假日由 build_facts 的新鲜度门兜底拦截）。"""
    return datetime.now().weekday() < 5


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
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    if not is_trade_day():
        print(f"[{timestamp}] SKIP: 周末非交易日")
        return 0

    if not is_in_trade_window():
        print(f"[{timestamp}] SKIP: 非收盘后时段")
        return 0

    date_str = now.strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"daily_predict-{date_str}.log")
    # 走全链路，不再单跑 daily_predict：否则预测推上去了事实层没推，大海对不上
    cmd = ["bash", os.path.join(PROJECT_DIR, "run_daily.sh"), date_str]

    print(f"[{timestamp}] RUN: {' '.join(cmd)}")
    print(f"[{timestamp}] LOG: {log_path}")

    # 重试 1 次
    for attempt in (1, 2):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== cron_runner attempt {attempt} @ {timestamp} ===\n")
            rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
        if rc == 0:
            print(f"[{timestamp}] OK attempt {attempt}")
            return 0
        if rc == 2:
            # 无数据日主动拦截，重试无意义
            print(f"[{timestamp}] ABORT: 当日无可用数据（非交易日/未收盘/接口异常）")
            return 0
        print(f"[{timestamp}] FAIL attempt {attempt} rc={rc}")

    print(f"[{timestamp}] FINAL FAIL after 2 attempts")
    return 2


if __name__ == "__main__":
    sys.exit(main())