# -*- coding: utf-8 -*-
"""
T5 跨指数验证 · 上证 / 深证 / 创业板 / 沪深300 / 中证500

目的：检验 T5 高位滞涨规则是否能从"上证指数"推广到其他指数。
      大海没有这些数据源，由 WB 这边补测。

用法：python3 reconcile/verify_t5_multi.py
"""
import importlib.util
import os
import random

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("hg", os.path.join(_here, "backtest_hardgate.py"))
hg = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hg)
except SystemExit:
    pass

INDEXES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000300", "沪深300"),
    ("sh000905", "中证500"),
]

POS_TH = 70.0
DEV_TH = 1.0
HOLD = 5
DAYS = 1500


def calc(code, pos_th=POS_TH, dev_th=DEV_TH, hold=HOLD, days=DAYS):
    bars = hg.load_kline(code, days)
    if not bars:
        return None
    feats = hg.compute_features(bars)
    for i, f in enumerate(feats):
        w = bars[max(0, i - 19): i + 1]
        lo = min(x["low"] for x in w)
        hi = max(x["high"] for x in w)
        f["pos20"] = (f["close"] - lo) / (hi - lo) * 100 if hi > lo else 50.0

    def trig(f):
        d = f["dev_ma20"] if f["dev_ma20"] is not None else -99
        return f["pos20"] >= pos_th and d < dev_th

    pairs = [(i, feats[i], (feats[i + hold]["close"] / feats[i]["close"] - 1) * 100)
             for i in range(len(feats)) if i + hold < len(feats)]
    idxs = sorted(i for i, f, r in pairs if trig(f))
    if not idxs:
        return None

    # 合并为独立事件（间隔 ≤3 交易日）
    cl, cur = [], [idxs[0]]
    for x in idxs[1:]:
        if x - cur[-1] <= 3:
            cur.append(x)
        else:
            cl.append(cur); cur = [x]
    cl.append(cur)

    ev = []
    for c in cl:
        j = c[0] + hold
        if j < len(feats):
            ev.append((feats[c[0]]["date"], (feats[j]["close"] / feats[c[0]]["close"] - 1) * 100))
    if not ev:
        return None

    rs = [r for _, r in ev]
    allr = [p[2] for p in pairs]
    return {
        "code": code, "bars": bars, "feats": feats,
        "trig_days": len(idxs), "events": ev, "n": len(ev),
        "down": sum(1 for r in rs if r < 0) / len(rs) * 100,
        "avg": sum(rs) / len(rs),
        "base_down": sum(1 for r in allr if r < 0) / len(allr) * 100,
        "base_avg": sum(allr) / len(allr),
        "cov": len(idxs) / len(pairs) * 100,
    }


def rand_test(res, seed=7):
    feats = res["feats"]
    obs = res["down"] / 100
    random.seed(seed)
    better, trials = 0, 3000
    starts = list(range(0, len(feats) - HOLD, HOLD))
    if not starts:
        return None
    for _ in range(trials):
        pick = random.sample(starts, min(res["n"], len(starts)))
        dn = sum(1 for s in pick if feats[s + HOLD]["close"] < feats[s]["close"])
        if dn / len(pick) >= obs:
            better += 1
    return better / trials


def main():
    print("=" * 82)
    print("T5 跨指数验证  |  pos20>=70% 且 dev_ma20<1%  |  持有期 T+5")
    print("=" * 82)

    results = []
    for code, name in INDEXES:
        r = calc(code)
        if not r:
            print(f"\n{name}({code}): 无数据或未触发")
            continue
        results.append((name, code, r))

    print(f"\n{'指数':<12}{'触发日':>7}{'事件':>6}{'覆盖率':>9}{'下跌率':>9}{'均收':>9}"
          f"{'基线跌率':>10}{'vs基线':>10}")
    print("-" * 82)
    for name, code, r in results:
        print(f"{name:<12}{r['trig_days']:>7}{r['n']:>6}{r['cov']:>8.1f}%"
              f"{r['down']:>8.1f}%{r['avg']:>8.2f}%{r['base_down']:>9.1f}%"
              f"{r['down']-r['base_down']:>+9.1f}pt")

    # 随机对照
    print("\n" + "=" * 82)
    print("随机对照检验")
    print("=" * 82)
    print(f"{'指数':<12}{'事件数':>8}{'观察下跌率':>12}{'随机P值':>12}{'判定':>10}")
    print("-" * 82)
    for name, code, r in results:
        p = rand_test(r)
        verdict = "✅ 显著" if p is not None and p < 0.05 else "⚠️ 不显著"
        pv = f"{p*100:.2f}%" if p is not None else "—"
        print(f"{name:<12}{r['n']:>8}{r['down']:>11.1f}%{pv:>12}{verdict:>10}")

    # 9/21 检查
    print("\n" + "=" * 82)
    print("9/21 各指数触发情况")
    print("=" * 82)
    for name, code, r in results:
        f9 = next((f for f in r["feats"] if f["date"] == "2026-09-21"), None)
        if not f9:
            print(f"  {name:<12} 无 9/21 数据")
            continue
        hit = f9["pos20"] >= POS_TH and (f9["dev_ma20"] or -99) < DEV_TH
        print(f"  {name:<12} pos20={f9['pos20']:5.1f}%  dev={f9['dev_ma20']:+6.2f}%  "
              f"{'🔴 触发' if hit else '⚪ 未触发'}")

    # 参数鲁棒性（跨指数一致性）
    print("\n" + "=" * 82)
    print("参数鲁棒性：pos20 阈值扫描（看是否所有指数都平滑）")
    print("=" * 82)
    print(f"{'指数':<12}" + "".join(f"{'pos'+str(p):>13}" for p in (60, 65, 70, 75, 80)))
    print("-" * 82)
    for name, code, _ in results:
        row = f"{name:<12}"
        for p in (60, 65, 70, 75, 80):
            r = calc(code, pos_th=p)
            row += f"{r['down']:.0f}%/{r['avg']:+.1f}".rjust(13) if r else "—".rjust(13)
        print(row)

    print("\n" + "=" * 82)
    print("持有期曲线（T+1 ~ T+10）")
    print("=" * 82)
    print(f"{'指数':<12}" + "".join(f"{'T+'+str(h):>13}" for h in (1, 3, 5, 7, 10)))
    print("-" * 82)
    for name, code, _ in results:
        row = f"{name:<12}"
        for h in (1, 3, 5, 7, 10):
            r = calc(code, hold=h)
            row += f"{r['down']:.0f}%/{r['avg']:+.1f}".rjust(13) if r else "—".rjust(13)
        print(row)


if __name__ == "__main__":
    main()
