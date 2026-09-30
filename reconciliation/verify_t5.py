# -*- coding: utf-8 -*-
"""
T5 高位滞涨规则 · 独立验证脚本

规则：20日区间分位 ≥ 70%  且  收盘对 MA20 偏离 < 1%
样本：上证指数 sh000001，1500 交易日（2020-07 ~ 2026-09）

用法：python3 reconcile/verify_t5.py
"""
import importlib.util, os, random, sys
from collections import Counter

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("hg", os.path.join(_here, "backtest_hardgate.py"))
hg = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hg)
except SystemExit:
    pass

POS_TH = 70.0      # 20日区间分位阈值
DEV_TH = 1.0       # 对MA20偏离阈值
HOLD = 5           # 持有期（交易日）


def build(n_days=1500):
    bars = hg.load_kline("sh000001", n_days)
    feats = hg.compute_features(bars)
    for i, f in enumerate(feats):
        w = bars[max(0, i - 19): i + 1]
        lo = min(x["low"] for x in w)
        hi = max(x["high"] for x in w)
        f["pos20"] = (f["close"] - lo) / (hi - lo) * 100 if hi > lo else 50.0
        f["from_hi20"] = (f["close"] / hi - 1) * 100 if hi else 0.0
    return bars, feats


def t5(f):
    return f["pos20"] >= POS_TH and (f["dev_ma20"] if f["dev_ma20"] is not None else -99) < DEV_TH


def main():
    bars, feats = build()
    pairs = [(i, feats[i], (feats[i + HOLD]["close"] / feats[i]["close"] - 1) * 100)
             for i in range(len(feats)) if i + HOLD < len(feats)]

    print("=" * 78)
    print(f"T5 高位滞涨规则验证  |  {feats[0]['date']} ~ {feats[-1]['date']}  ({len(feats)} 日)")
    print("=" * 78)

    # 聚合成独立事件段（间隔 ≤3 日算同一段）
    idxs = sorted(i for i, f, r in pairs if t5(f))
    if not idxs:
        print("无触发")
        return
    cl, cur = [], [idxs[0]]
    for x in idxs[1:]:
        if x - cur[-1] <= 3:
            cur.append(x)
        else:
            cl.append(cur); cur = [x]
    cl.append(cur)

    ev = []
    for c in cl:
        j = c[0] + HOLD
        if j < len(feats):
            ev.append((feats[c[0]]["date"], (feats[j]["close"] / feats[c[0]]["close"] - 1) * 100))

    rs = [r for _, r in ev]
    allr = [p[2] for p in pairs]
    down = sum(1 for r in rs if r < 0) / len(rs) * 100
    base_down = sum(1 for r in allr if r < 0) / len(allr) * 100
    avg = sum(rs) / len(rs)
    base_avg = sum(allr) / len(allr)

    print(f"\n触发日 {len(idxs)} 个  →  独立事件 {len(ev)} 个")
    print(f"\n  T5 事件   下跌率 {down:5.1f}%   均收 {avg:+.2f}%")
    print(f"  全样本     下跌率 {base_down:5.1f}%   均收 {base_avg:+.2f}%")
    print(f"  超额       下跌率 {down-base_down:+.1f}pt   收益 {avg-base_avg:+.2f}pt")

    # 逐年
    print(f"\n{'年份':<8}{'事件':>6}{'下跌':>6}{'下跌率':>9}{'事件均收':>10}{'年度基线':>10}")
    print("-" * 78)
    for y in sorted({d[:4] for d, _ in ev}):
        yy = [r for d, r in ev if d[:4] == y]
        bb = [r for _, f, r in pairs if f["date"][:4] == y]
        print(f"{y:<8}{len(yy):>6}{sum(1 for r in yy if r<0):>6}"
              f"{sum(1 for r in yy if r<0)/len(yy)*100:>8.1f}%"
              f"{sum(yy)/len(yy):>9.2f}%{sum(bb)/len(bb):>9.2f}%")

    # 随机对照
    random.seed(7)
    better, trials = 0, 5000
    starts = list(range(0, len(feats) - HOLD, HOLD))
    for _ in range(trials):
        pick = random.sample(starts, min(len(ev), len(starts)))
        dn = sum(1 for s in pick if feats[s + HOLD]["close"] < feats[s]["close"])
        if dn / len(pick) >= down / 100:
            better += 1
    p = better / trials
    print(f"\n随机对照: 观察下跌率 {down:.1f}%  →  随机≥该值比例 {p*100:.2f}%")
    print(f"判定: {'✅ 显著 (p<0.05)' if p < 0.05 else '⚠️ 不显著'}")

    # 9/21
    f9 = next((f for f in feats if f["date"] == "2026-09-21"), None)
    if f9:
        print(f"\n9/21 检查: pos20={f9['pos20']:.1f}%  dev_ma20={f9['dev_ma20']:+.2f}%"
              f"  → {'🔴 触发' if t5(f9) else '⚪ 未触发'}")


if __name__ == "__main__":
    main()
