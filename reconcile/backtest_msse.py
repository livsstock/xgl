#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M_sse 方向预测力回测  v1.0
============================
用途：验证 M_sse（及任意变体）对大盘次日方向的预测能力，与随机基线对比。

背景：2026-09-28 发现 M_sse 命中率 31.4%，与"永远猜中性"完全一致。
本脚本固化了那次分析，供双方复核。

用法:
  python3 backtest_msse.py               # 全量回测 + 基线对比
  python3 backtest_msse.py --extreme     # 额外验证"极端读数是否有效"
  python3 backtest_msse.py --days 300    # 指定取多少根日K
"""

import argparse
import concurrent.futures
import json
import math
import statistics
import subprocess
import sys

# ---------------------------------------------------------------- 配置

WATCHLIST = [
    "sh601398", "sh601288", "sh601012", "sz000858", "sh600519", "sh601899",
    "sh603799", "sh601088", "sz002142", "sh510300", "sh510500", "sz159915",
    "sh600019", "sh600893", "sz000960", "sz002155", "sh601212", "sh562500",
    "sh518880", "sh512480", "sh601958", "sz000725", "sh600036", "sh600111",
    "sh600875", "sh600392",
]

# 与 daily_predict.py 保持一致
MSSE_BULL = 60
MSSE_BEAR = 40
FLAT_THRESHOLD = 0.3          # 次日涨跌幅绝对值 < 0.3% 视为"平"


# ---------------------------------------------------------------- 取数

def _curl(url, timeout=25):
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout),
         "-H", "User-Agent: Mozilla/5.0",
         "-H", "Referer: https://gu.qq.com/", url],
        capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def load_closes(code, days=300):
    """返回 {日期: 收盘价}。用前复权日线。"""
    url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
           f"newfqkline/get?param={code},day,,,{days},qfq")
    try:
        d = json.loads(_curl(url))["data"][code]
        kl = d.get("qfqday") or d.get("day")
        return {x[0]: float(x[2]) for x in kl}
    except Exception:
        return {}


# ---------------------------------------------------------------- M_sse 复现

def calc_m_sse(sh, codes, i, dates, mode="v25"):
    """
    在第 i 根日K上计算 M_sse，严格按 daily_predict.py 的公式。
    返回 (m_sse, debug_dict) 或 None。
    """
    if i < 20:
        return None
    d, pd = dates[i], dates[i - 1]

    # R_15 = 15 个交易日涨跌幅（不是 15 分钟！）
    r15_raw = (sh[dates[i]] / sh[dates[i - 15]] - 1) * 100
    r15 = max(0, min(100, 50 + r15_raw * 5))

    # R_MA20dev
    ma20 = sum(sh[dates[j]] for j in range(i - 20, i)) / 20
    dev = (sh[d] / ma20 - 1) * 100
    r_ma = max(0, min(100, 50 + dev * 5))

    # breadth + 相位数据
    changes, up, valid = [], 0, 0
    for c in codes:
        m = codes[c]
        if d in m and pd in m:
            p = (m[d] - m[pd]) / m[pd] * 100
            changes.append(p)
            valid += 1
            if p > 0:
                up += 1
    if valid < 5:
        return None
    breadth = up / valid - 0.5
    breadth_score = max(0, min(100, (breadth + 0.5) * 100))

    # Kuramoto（源码口径 atan2(p, 10)）
    phases = [math.atan2(p, 10) for p in changes]
    r_kura = math.sqrt(sum(math.cos(x) for x in phases) ** 2 +
                       sum(math.sin(x) for x in phases) ** 2) / len(phases)

    if mode == "v25":
        kura_score = max(0, min(100, r_kura * 100))
    elif mode == "range3":                     # 修正相位量程
        ph2 = [math.atan2(p, 3) for p in changes]
        rk2 = math.sqrt(sum(math.cos(x) for x in ph2) ** 2 +
                        sum(math.sin(x) for x in ph2) ** 2) / len(ph2)
        kura_score = max(0, min(100, rk2 * 100))
    elif mode == "dirweight":                  # 方向加权
        kura_score = 50 + 50 * r_kura * (1 if (r15_raw + dev) >= 0 else -1)
    elif mode == "noR":                        # 彻底去掉
        return ((0.3 * r15 + 0.2 * r_ma + 0.3 * breadth_score) / 0.8,
                dict(r15=r15_raw, dev=dev, r_kura=r_kura))
    elif mode == "weightB":                    # 权重降到 0.1
        return (0.3 * r15 + 0.1 * r_ma + 0.5 * breadth_score +
                0.1 * max(0, min(100, r_kura * 100)),
                dict(r15=r15_raw, dev=dev, r_kura=r_kura))
    else:
        raise ValueError(mode)

    m = (0.3 * r15 + 0.2 * r_ma + 0.3 * breadth_score + 0.2 * kura_score)
    return m, dict(r15=r15_raw, dev=dev, r_kura=r_kura)


# ---------------------------------------------------------------- 评估

def direction_of(value_pct):
    if value_pct > FLAT_THRESHOLD:
        return "up"
    if value_pct < -FLAT_THRESHOLD:
        return "down"
    return "neutral"


def m_sse_to_dir(m, bull=MSSE_BULL, bear=MSSE_BEAR):
    if m >= bull:
        return "up"
    if m <= bear:
        return "down"
    return "neutral"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--extreme", action="store_true",
                    help="额外验证极端读数假设")
    args = ap.parse_args()

    print(f"取数中（日K {args.days} 根，观察池 {len(WATCHLIST)} 只）...")
    sh = load_closes("sh000001", args.days)
    if len(sh) < 60:
        print("✗ 上证数据不足，检查网络", file=sys.stderr)
        sys.exit(1)
    dates = sorted(sh)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        pairs = ex.map(lambda c: (c, load_closes(c, args.days)), WATCHLIST)
    codes = {c: m for c, m in pairs if m}
    print(f"  上证 {len(dates)} 日   观察池 {len(codes)}/{len(WATCHLIST)} 只\n")

    # 收集样本
    rows = []
    for i in range(20, len(dates) - 1):
        r = calc_m_sse(sh, codes, i, dates, "v25")
        if not r:
            continue
        m, dbg = r
        nxt = (sh[dates[i + 1]] - sh[dates[i]]) / sh[dates[i]] * 100
        today = (sh[dates[i]] - sh[dates[i - 1]]) / sh[dates[i - 1]] * 100
        rows.append(dict(date=dates[i], m=m, nxt=nxt, today=today, **dbg))

    n = len(rows)
    if n < 30:
        print(f"✗ 有效样本仅 {n} 个，不足以回测", file=sys.stderr)
        sys.exit(2)

    # ---- 1. M_sse 分布
    ms = sorted(x["m"] for x in rows)
    def pct(q):
        return ms[min(int(n * q), n - 1)]

    print("=" * 72)
    print("一、M_sse 分布")
    print("=" * 72)
    print(f"  样本 {n}")
    print(f"  min {ms[0]:.1f}  p10 {pct(.10):.1f}  p25 {pct(.25):.1f}  "
          f"p50 {pct(.50):.1f}  p75 {pct(.75):.1f}  p90 {pct(.90):.1f}  max {ms[-1]:.1f}")
    print(f"  均值 {statistics.mean(ms):.1f}   标准差 {statistics.stdev(ms):.1f}")
    up_r = sum(1 for x in ms if x >= MSSE_BULL) / n * 100
    dn_r = sum(1 for x in ms if x <= MSSE_BEAR) / n * 100
    print(f"  ≥{MSSE_BULL} 占比 {up_r:.1f}%   ≤{MSSE_BEAR} 占比 {dn_r:.1f}%   "
          f"中性带占比 {100-up_r-dn_r:.1f}%")
    print()

    # ---- 2. 方向命中率
    def hit(pred, actual):
        return pred == actual

    hit_v25 = sum(1 for x in rows if hit(m_sse_to_dir(x["m"]), direction_of(x["nxt"])))
    print("=" * 72)
    print("二、M_sse 方向命中率")
    print("=" * 72)
    print(f"  M_sse 现状         {hit_v25}/{n}  =  {hit_v25/n*100:.1f}%")
    print()

    # ---- 3. 随机基线
    dist = {"up": 0, "down": 0, "neutral": 0}
    for x in rows:
        dist[direction_of(x["nxt"])] += 1
    print("=" * 72)
    print("三、随机基线对照（关键）")
    print("=" * 72)
    print(f"  实际次日分布:  涨 {dist['up']/n*100:.1f}%   "
          f"跌 {dist['down']/n*100:.1f}%   平 {dist['neutral']/n*100:.1f}%")
    print(f"  永远猜中性        {dist['neutral']/n*100:>5.1f}%")
    print(f"  永远猜涨          {dist['up']/n*100:>5.1f}%")
    print(f"  永远猜跌          {dist['down']/n*100:>5.1f}%")

    mom = sum(1 for x in rows
              if direction_of(x["today"]) == direction_of(x["nxt"]))
    print(f"  今天怎样明天怎样    {mom/n*100:>5.1f}%")
    print()
    verdict = ("⚠️  与随机基线无差异 —— 该信号不含预测信息"
               if abs(hit_v25 - dist["neutral"]) / n < 0.05
               else "信号优于随机基线")
    print(f"  判定: {verdict}")
    print()

    # ---- 4. 各口径对照
    print("=" * 72)
    print("四、各口径命中率对照")
    print("=" * 72)
    for mode, name in [("v25", "v2.5 现状"),
                       ("range3", "量程修正 atan2(p,3)"),
                       ("dirweight", "方向加权"),
                       ("noR", "去掉 Kuramoto"),
                       ("weightB", "Kuramoto 降权 0.1")]:
        h = 0
        for i in range(20, len(dates) - 1):
            r = calc_m_sse(sh, codes, i, dates, mode)
            if not r:
                continue
            m, _ = r
            nxt = (sh[dates[i + 1]] - sh[dates[i]]) / sh[dates[i]] * 100
            if m_sse_to_dir(m) == direction_of(nxt):
                h += 1
        print(f"  {name:<24} {h:>4}/{n}  {h/n*100:>5.1f}%")
    print()

    # ---- 5. 极端读数验证
    if args.extreme:
        print("=" * 72)
        print("五、极端读数是否有效（只在 M_sse 极端时出方向）")
        print("=" * 72)
        for lo, hi in [(35, 65), (30, 70), (25, 75), (20, 80)]:
            sel = [x for x in rows if x["m"] <= lo or x["m"] >= hi]
            if not sel:
                continue
            h = sum(1 for x in sel
                    if m_sse_to_dir(x["m"]) == direction_of(x["nxt"]))
            base = sum(1 for x in sel
                       if direction_of(x["nxt"]) == "neutral") / len(sel) * 100
            flag = "✅ 有效" if h / len(sel) > base / 100 + 0.08 else "❌ 无增益"
            print(f"  阈值 [{lo},{hi}] 外  样本 {len(sel):>3}  "
                  f"命中 {h/len(sel)*100:>5.1f}%  "
                  f"(中性基线 {base:>4.1f}%)  {flag}")
        print()

    # ---- 6. 明细
    print("=" * 72)
    print("六、最近 15 个交易日明细")
    print("=" * 72)
    print(f"{'日期':<12}{'R15%':>8}{'MA20dev':>9}{'R_kura':>8}"
          f"{'M_sse':>8}{'预测':>8}{'次日%':>8}{'实际':>8}{'':>4}")
    print("-" * 72)
    for x in rows[-15:]:
        pred = m_sse_to_dir(x["m"])
        act = direction_of(x["nxt"])
        mark = "✅" if pred == act else "❌"
        print(f"{x['date']:<12}{x['r15']:>8.2f}{x['dev']:>9.2f}{x['r_kura']:>8.4f}"
              f"{x['m']:>8.1f}{pred:>8}{x['nxt']:>8.2f}{act:>8}{mark:>4}")
    print()

    print("=" * 72)
    print("局限说明")
    print("=" * 72)
    print("  · 样本期偏短（约 280 日）且含单边下行段，外推需谨慎")
    print("  · 使用腾讯行情前复权日线，与线上盘中快照存在时点差")
    print("  · 命中率口径：方向三元分类（涨/跌/平），平阈值 ±0.3%")


if __name__ == "__main__":
    main()
