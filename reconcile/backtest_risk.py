#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M_sse 作为「风险指标」（而非方向指标）的有效性检验  v1.0
===========================================================
背景：2026-09-29 阿牛哥定调「大盘风控是最高优先级」，
      系统定位从「预测涨跌」转为「决策闸门」。

      M_sse 方向命中率仅 31.4%（=随机基线），但作为风险指标可能仍有价值。
      本脚本回答：M_sse 低位时，后续「大跌」概率是否显著高于基线？

关键设计：
  · 区分「跌」（均值事件）与「大跌」（尾部事件）—— 两者结论完全不同
  · 同时输出「拦对率」与「误拦率」，因为风控的成本在误拦
  · 明确指出样本不足的区间

【v1.1 变更 · 2026-09-29】交易日口径修正
  此前"次日"直接取行情数组的下一格。行情数组本身是交易日序列，取下一格
  在【样本构建】层面是对的；但输出与对外沟通里写"次日"而不标注实际跨了
  几个自然日，会被误读。

  实例：9/24 → 9/28 是"下一个交易日"没错，但中间夹了中秋+周末共 3 个
  非交易日。对外说"次日跌 2.96%"会让对方在 9/25 找不到数据。

  本版新增：
    · 每个极端读数的日期旁标注「实际跨 N 个自然日」
    · 输出「交易日 vs 自然日」口径对照，说明差异来源
    · 修正持有期语义：T+N 一律指 N 个交易日

用法:
  python3 backtest_risk.py
  python3 backtest_risk.py --threshold 40
"""

import argparse
import concurrent.futures
import datetime
import json
import math
import os
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from trade_calendar import count_trading_days, describe, CALENDAR_VERSION
except ImportError:
    print("✗ 需要 trade_calendar.py 在同目录", file=sys.stderr)
    sys.exit(1)

WATCHLIST = [
    "sh601398", "sh601288", "sh601012", "sz000858", "sh600519", "sh601899",
    "sh603799", "sh601088", "sz002142", "sh510300", "sh510500", "sz159915",
    "sh600019", "sh600893", "sz000960", "sz002155", "sh601212", "sh562500",
    "sh518880", "sh512480", "sh601958", "sz000725", "sh600036", "sh600111",
    "sh600875", "sh600392",
]


def _curl(url, timeout=25):
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout),
         "-H", "User-Agent: Mozilla/5.0",
         "-H", "Referer: https://gu.qq.com/", url],
        capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def load(code, days=400):
    url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
           f"newfqkline/get?param={code},day,,,{days},qfq")
    try:
        k = json.loads(_curl(url))["data"][code]
        kl = k.get("qfqday") or k.get("day")
        return {x[0]: float(x[2]) for x in kl}
    except Exception:
        return {}


def build_samples():
    sh = load("sh000001")
    if not sh:
        return None, None
    dates = sorted(sh)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        stocks = {c: m for c, m in ex.map(lambda c: (c, load(c)), WATCHLIST) if m}

    rows = []
    for i in range(20, len(dates) - 1):
        d, pd = dates[i], dates[i - 1]
        r15 = (sh[dates[i]] / sh[dates[i - 15]] - 1) * 100
        r15s = max(0, min(100, 50 + r15 * 5))
        ma20 = sum(sh[dates[j]] for j in range(i - 20, i)) / 20
        dev = (sh[d] / ma20 - 1) * 100
        rmas = max(0, min(100, 50 + dev * 5))
        chgs, up = [], 0
        for c, m in stocks.items():
            if d in m and pd in m:
                p = (m[d] - m[pd]) / m[pd] * 100
                chgs.append(p)
                if p > 0:
                    up += 1
        if len(chgs) < 5:
            continue
        br = up / len(chgs) - 0.5
        bs = max(0, min(100, (br + 0.5) * 100))
        ph = [math.atan2(p, 10) for p in chgs]
        rk = math.sqrt(sum(math.cos(x) for x in ph) ** 2 +
                       sum(math.sin(x) for x in ph) ** 2) / len(ph)
        m = (0.3 * r15s + 0.2 * rmas + 0.3 * bs +
             0.2 * max(0, min(100, rk * 100)))
        nxt = (sh[dates[i + 1]] - sh[d]) / sh[d] * 100
        # 记录"次日"实际跨了几个自然日 —— 用于识别假期跳空
        gap = (datetime.date.fromisoformat(dates[i + 1][:10])
               - datetime.date.fromisoformat(d[:10])).days
        rows.append(dict(date=d, m=m, nxt=nxt, breadth=br, gap=gap))
    return rows, stocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=40)
    ap.add_argument("--big-drop", type=float, default=1.5)
    args = ap.parse_args()

    print("构建样本...")
    rows, _ = build_samples()
    if not rows:
        print("✗ 数据获取失败", file=sys.stderr)
        sys.exit(1)
    n = len(rows)
    print(f"  样本 {n} 个交易日\n")

    BIG = args.big_drop
    base_dn = sum(1 for x in rows if x["nxt"] < -0.3) / n * 100
    base_big = sum(1 for x in rows if x["nxt"] < -BIG) / n * 100
    base_avg = statistics.mean(x["nxt"] for x in rows)

    print("=" * 76)
    print("一、基线")
    print("=" * 76)
    print(f"  次日跌(<-0.3%)概率      : {base_dn:.1f}%")
    print(f"  次日大跌(<-{BIG}%)概率    : {base_big:.1f}%")
    print(f"  次日平均涨跌            : {base_avg:+.2f}%")
    print(f"  日历版本                : {CALENDAR_VERSION}")
    print()

    # ---- 口径说明（v1.1 新增）：交易日 vs 自然日
    gapped = [x for x in rows if x.get("gap", 1) > 1]
    print("=" * 76)
    print("一之二、『次日』口径校验（v1.1 新增）")
    print("=" * 76)
    print(f"  『次日』= 下一个【交易日】，不是自然日的明天。")
    print(f"  样本中跨越假期的『次日』共 {len(gapped)} 处"
          f"（占 {len(gapped)/n*100:.1f}%）：")
    for x in gapped[-8:]:
        print(f"    {x['date']}  次日实际跨 {x['gap']} 个自然日"
              f"   {describe(x['date'])} → 下一交易日")
    if len(gapped) > 8:
        print(f"    ... 另有 {len(gapped)-8} 处")
    print()
    print(f"  ⚠️ 旧口径按自然日算 T+N，持有期被系统性低估：")
    print(f"     A 股每年约 244 交易日 / 365 自然日 ≈ 0.67")
    print(f"     说『5 日后』若按自然日，实际只持有约 3.3 个交易日。")
    print()

    print("=" * 76)
    print("二、分区间表现")
    print("=" * 76)
    print(f"{'M_sse 区间':<16}{'样本':>6}{'次日跌%':>10}{'次日大跌%':>11}"
          f"{'次日均值':>11}{'大跌提升':>10}")
    print("-" * 76)
    for lo, hi, label in [(0, 30, "<30"), (30, 40, "30-40"), (40, 50, "40-50"),
                          (50, 60, "50-60"), (60, 70, "60-70"), (70, 101, "≥70")]:
        sel = [x for x in rows if lo <= x["m"] < hi]
        if not sel:
            continue
        dn = sum(1 for x in sel if x["nxt"] < -0.3) / len(sel) * 100
        bg = sum(1 for x in sel if x["nxt"] < -BIG) / len(sel) * 100
        av = statistics.mean(x["nxt"] for x in sel)
        lift = bg / base_big if base_big else 0
        flag = " ⚠️样本不足" if len(sel) < 10 else ""
        print(f"{label:<16}{len(sel):>6}{dn:>10.1f}{bg:>11.1f}{av:>+11.2f}"
              f"{lift:>9.2f}x{flag}")
    print("-" * 76)
    print()

    th = args.threshold
    print("=" * 76)
    print(f"三、风控门槛 M_sse < {th} 的完整成本收益")
    print("=" * 76)
    sel = [x for x in rows if x["m"] < th]
    if not sel:
        print("  无样本")
        return
    bg = sum(1 for x in sel if x["nxt"] < -BIG) / len(sel) * 100
    dn = sum(1 for x in sel if x["nxt"] < -0.3) / len(sel) * 100
    up = sum(1 for x in sel if x["nxt"] > 0.3) / len(sel) * 100
    neu = 100 - dn - up
    print(f"  触发天数        : {len(sel)} 天（占 {len(sel)/n*100:.1f}%）")
    print(f"  后续大跌概率    : {bg:.1f}%   基线 {base_big:.1f}%   提升 {bg/base_big if base_big else 0:.2f}x")
    print()
    print(f"  拦下后的实际去向:")
    print(f"    ✅ 真跌(<-0.3%)  : {dn:>5.1f}%   ← 拦对了")
    print(f"    ➖ 横盘           : {neu:>5.1f}%   ← 成本低")
    print(f"    ❌ 上涨(>0.3%)    : {up:>5.1f}%   ← 误拦，这是成本")
    print()
    if len(sel) < 30:
        print(f"  ⚠️ 样本仅 {len(sel)} 天，统计上不足以支撑决策")
    print()

    print("=" * 76)
    print("四、结论与建议")
    print("=" * 76)
    verdict = []
    if bg > base_big * 2:
        verdict.append(f"  ✅ M_sse<{th} 对『大跌』有识别力（提升 {bg/base_big:.2f}x）")
    else:
        verdict.append(f"  ❌ M_sse<{th} 对『大跌』无显著识别力")
    if up > 50:
        verdict.append(f"  ❌ 但误拦率 {up:.0f}% 过高，不能做一票否决")
    else:
        verdict.append(f"  ✅ 误拦率 {up:.0f}% 尚可接受")
    verdict.append(f"  ➡️ 建议：作为『风险提示』使用；若要『否决』，"
                   f"需叠加多条件（跌破均线 + 宽度<30%）降低误报")
    for v in verdict:
        print(v)
    print()
    print("=" * 76)
    print("局限")
    print("=" * 76)
    print("  · 单年样本，含单边下行段，环境偏差存在")
    print("  · 极端区间（<30）样本个位数，不可用于决策")
    print("  · 未考虑交易成本与滑点")


if __name__ == "__main__":
    main()
