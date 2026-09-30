#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
洞1 参考修复实现 —— 基于 WB 数据真实字段 index.change_percent。

用法：
    python3 reconcile/fix_market_direction_demo.py

设计原则：
    1. 只用真实存在的字段（index.change_percent）
    2. 保留旧字段名兼容（chg_today_pct / eod_quotes），避免上游改结构就崩
    3. 兜底仍返回 unknown —— 不猜，让洞3 fail-closed 接手
    4. 阈值与大海的方案一致（±0.5%），便于对拍
"""
import json
import glob
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEARISH_TH = -0.5
BULLISH_TH = 0.5


def extract_market_direction(data, source="wb"):
    """
    从 WB 数据提取大盘方向（当期状态）。

    搜索顺序：
      1. 顶层 index.change_percent      ← 真实字段（9/17~9/29 全部文件验证）
      2. 顶层 index.chg_today_pct       ← 兼容旧命名
      3. 顶层 index.change_pct          ← 兼容旧命名
      4. 顶层 market_direction          ← 显式方向字段（若未来补充）
      5. 兜底 unknown
    """
    idx = data.get("index")
    if isinstance(idx, dict):
        # 兼容三种可能的涨跌字段名，按优先级取第一个非 None 的
        chg = None
        used = None
        for k in ("change_percent", "chg_today_pct", "change_pct"):
            v = idx.get(k)
            if v is not None:
                chg = v
                used = k
                break
        if chg is not None:
            if chg <= BEARISH_TH:
                d = "bearish"
            elif chg >= BULLISH_TH:
                d = "bullish"
            else:
                d = "neutral"
            return {
                "direction": d,
                "confidence": None,
                "reason": f"上证涨跌{chg:+.2f}%",
                "source": f"index.{used}",
            }
        # index 存在但没有涨跌字段 —— 记录一下，便于排查
        return {
            "direction": "unknown",
            "confidence": None,
            "reason": f"index 存在但无涨跌字段，实际 keys={list(idx.keys())}",
            "source": "index.<missing>",
        }

    # 显式方向字段（WB 若未来补充）
    md = data.get("market_direction")
    if isinstance(md, str) and md.strip():
        return {"direction": md.strip().lower(), "confidence": None,
                "reason": "显式 market_direction", "source": "market_direction"}

    return {"direction": "unknown", "confidence": None,
            "reason": "无法从数据中提取大盘方向", "source": "none"}


def is_market_high_risk(wb_market, dh_market):
    """风控门（与大海版一致，仅用于本次演示）"""
    wb_dir = wb_market.get("direction", "unknown")
    dh_dir = dh_market.get("direction", "unknown")
    if wb_dir == "bearish" and dh_dir == "bearish":
        return True, "CRITICAL"
    if wb_dir == "bearish" or dh_dir == "bearish":
        return True, "HIGH"
    if wb_dir == "unknown" or dh_dir == "unknown":
        return True, "HIGH"
    if wb_dir == "neutral" and dh_dir == "neutral":
        return False, "MODERATE"
    return False, "NORMAL"


def dh_direction_from(pred_path):
    """从大海预测文件读方向"""
    if not os.path.exists(pred_path):
        return {"direction": "unknown", "source": "missing"}
    with open(pred_path, encoding="utf-8") as f:
        d = json.load(f)
    for fld in ("market_outlook", "market_overview"):
        mo = d.get(fld, {})
        if isinstance(mo, dict) and mo.get("direction"):
            return {"direction": mo["direction"], "source": fld}
    return {"direction": "unknown", "source": "none"}


def main():
    print("=" * 82)
    print("洞1 参考修复 —— 用真实 WB 数据验证（对照 align_core6.py 当前恒 unknown）")
    print("=" * 82)
    print(f"{'日期':<10}{'index字段':<16}{'WB方向':<10}{'来源':<24}{'大海':<9}{'风控':<10}")
    print("-" * 82)

    files = sorted(glob.glob(os.path.join(REPO, "output", "stock_data_*.json")))
    n_ok = 0
    for f in files:
        date = os.path.basename(f).replace("stock_data_", "").replace(".json", "")
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        r = extract_market_direction(data)
        idx = data.get("index", {})
        chg = idx.get("change_percent", "—")

        # 大海同日预测
        pred = os.path.join(REPO, "predictions-dahai", f"{date[:4]}-{date[4:6]}-{date[6:]}_dahai.json")
        dh = dh_direction_from(pred)
        risk, level = is_market_high_risk(r, dh)

        if r["source"].startswith("index.") and r["direction"] != "unknown":
            n_ok += 1
        print(f"{date:<10}{str(chg):<16}{r['direction']:<10}{r['source']:<24}"
              f"{dh['direction']:<9}{level:<10}")

    print("-" * 82)
    print(f"命中率: {n_ok}/{len(files)} 个文件成功提取 WB 方向")
    print()
    print("对照：当前 align_core6.py 版本 → 0/%d（全部 unknown）" % len(files))
    print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(main())
