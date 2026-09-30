# -*- coding: utf-8 -*-
"""
硬约束回测 · 大盘价格客观信号

目标：找出一条【完全不依赖模型方向判断】的硬约束，
      能在 9/21 这类"双方都判偏多、实际连续下跌"的环境里提前降级。

数据：腾讯 K 线接口，上证指数 sh000001，401 个交易日（2025-02 ~ 2026-09-30）

用法：
    python3 reconcile/backtest_hardgate.py            # 全量回测
    python3 reconcile/backtest_hardgate.py --asof 2026-09-21   # 看某天状态
"""
import json
import subprocess
import sys
from collections import Counter, defaultdict

# ---------------------------------------------------------------- 数据层

CACHE = {}


def _curl(url, timeout=25):
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout),
         "-H", "User-Agent: Mozilla/5.0",
         "-H", "Referer: https://gu.qq.com/", url],
        capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def load_kline(code, days=400):
    """返回 [{date, open, close, high, low, vol}, ...] 按日期升序。"""
    if code in CACHE:
        return CACHE[code]
    url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
           f"newfqkline/get?param={code},day,,,{days},qfq")
    try:
        k = json.loads(_curl(url))["data"][code]
        kl = k.get("qfqday") or k.get("day")
        bars = []
        for x in kl:
            bars.append({
                "date": x[0],
                "open": float(x[1]),
                "close": float(x[2]),
                "high": float(x[3]),
                "low": float(x[4]),
                "vol": float(x[5]) if x[5] else 0.0,
            })
        CACHE[code] = bars
        return bars
    except Exception as e:
        print(f"  [warn] {code} 加载失败: {e}")
        CACHE[code] = []
        return []


# ---------------------------------------------------------------- 指标层

def sma(vals, n):
    """简单移动平均，返回与 vals 等长的列表，不足 n 处为 None。"""
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def compute_features(bars):
    """给每根 K 线算特征。只使用【当日及之前】的数据，无未来函数。"""
    closes = [b["close"] for b in bars]
    vols = [b["vol"] for b in bars]
    n = len(bars)

    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    vma5 = sma(vols, 5)

    feats = []
    for i, b in enumerate(bars):
        c = closes[i]
        f = {
            "date": b["date"],
            "close": c,
            "ma5": ma5[i], "ma10": ma10[i], "ma20": ma20[i], "ma60": ma60[i],
            "dev_ma20": None,     # 收盘对 MA20 偏离 %
            "dev_ma5": None,
            "ret1": None,         # 当日涨跌 %
            "ret5": None,         # 5 日累计 %
            "ret10": None,
            "ret20": None,
            "vol_ratio": None,    # 量 / 5日均量
            "ma5_slope": None,    # MA5 斜率（相对前一日 %）
            "ma20_slope": None,
            "below_ma20_days": 0, # 连续跌破 MA20 天数
        }
        if ma20[i]:
            f["dev_ma20"] = (c / ma20[i] - 1) * 100
        if ma5[i]:
            f["dev_ma5"] = (c / ma5[i] - 1) * 100
        if i >= 1:
            f["ret1"] = (c / closes[i - 1] - 1) * 100
        if i >= 5:
            f["ret5"] = (c / closes[i - 5] - 1) * 100
        if i >= 10:
            f["ret10"] = (c / closes[i - 10] - 1) * 100
        if i >= 20:
            f["ret20"] = (c / closes[i - 20] - 1) * 100
        if vma5[i] and vma5[i] > 0:
            f["vol_ratio"] = b["vol"] / vma5[i]
        if i >= 1 and ma5[i] and ma5[i - 1]:
            f["ma5_slope"] = (ma5[i] / ma5[i - 1] - 1) * 100
        if i >= 1 and ma20[i] and ma20[i - 1]:
            f["ma20_slope"] = (ma20[i] / ma20[i - 1] - 1) * 100
        # 连续跌破 MA20 天数
        if ma20[i] and c < ma20[i]:
            f["below_ma20_days"] = (feats[-1]["below_ma20_days"] + 1) if feats else 1
        feats.append(f)
    return feats


def future_ret(feats, i, k):
    """第 i 日的 k 个交易日之后收益 %。"""
    j = i + k
    if j >= len(feats):
        return None
    return (feats[j]["close"] / feats[i]["close"] - 1) * 100


# ---------------------------------------------------------------- 回测层

def build_samples(feats, horizon=5):
    """构造样本：每个交易日 -> 未来 horizon 日收益。"""
    out = []
    for i in range(len(feats)):
        fr = future_ret(feats, i, horizon)
        if fr is None:
            continue
        out.append((i, feats[i], fr))
    return out


def eval_rule(samples, name, cond, horizon):
    """
    cond(feat) -> True 表示【触发降级/避险】
    评估：触发后未来 horizon 日收益 vs 未触发
    """
    trig = [(f, r) for _, f, r in samples if cond(f)]
    norm = [(f, r) for _, f, r in samples if not cond(f)]
    if not trig:
        return None
    t_rets = [r for _, r in trig]
    n_rets = [r for _, r in norm] if norm else [0.0]
    n = len(t_rets)

    # 触发样本里，未来下跌（<0）比例 —— 这就是"信号有效性"
    down_rate = sum(1 for r in t_rets if r < 0) / n * 100
    avg = sum(t_rets) / n
    avg_n = sum(n_rets) / len(n_rets)
    # 触发时躲开的平均损失
    avoid = avg_n - avg

    return {
        "name": name,
        "n": n,
        "coverage": n / len(samples) * 100,
        "down_rate": down_rate,
        "avg_trig": avg,
        "avg_norm": avg_n,
        "avoid": avoid,
        "horizon": horizon,
    }


def main():
    asof = None
    if "--asof" in sys.argv:
        asof = sys.argv[sys.argv.index("--asof") + 1]

    print("=" * 74)
    print("硬约束回测 · 上证指数 sh000001")
    print("=" * 74)

    bars = load_kline("sh000001", 400)
    if not bars:
        print("❌ 无数据")
        return
    feats = compute_features(bars)
    print(f"\n样本区间: {feats[0]['date']} ~ {feats[-1]['date']}  ({len(feats)} 个交易日)")

    # 覆盖检查：9/21 前后
    print("\n【数据覆盖检查】9/18 ~ 9/30")
    for f in feats:
        if f["date"] >= "2026-09-18":
            d20 = f"{f['dev_ma20']:+.2f}%" if f["dev_ma20"] is not None else "  n/a "
            r1 = f"{f['ret1']:+.2f}%" if f["ret1"] is not None else " n/a "
            print(f"  {f['date']}  close={f['close']:8.2f}  MA20={f['ma20']:8.2f}  "
                  f"dev={d20}  日涨跌={r1}")

    # ------------------------------------------------ 规则候选
    HORIZON = 5
    samples = build_samples(feats, HORIZON)
    print(f"\n可回测样本: {len(samples)} 条（持有期 {HORIZON} 个交易日）")

    rules = [
        ("R1  跌破MA20", lambda f: f["dev_ma20"] is not None and f["dev_ma20"] < 0),
        ("R2  跌破MA20超1%", lambda f: f["dev_ma20"] is not None and f["dev_ma20"] < -1),
        ("R3  跌破MA20超2%", lambda f: f["dev_ma20"] is not None and f["dev_ma20"] < -2),
        ("R4  跌破MA20超3%", lambda f: f["dev_ma20"] is not None and f["dev_ma20"] < -3),
        ("R5  连续跌破MA20≥3日", lambda f: f["below_ma20_days"] >= 3),
        ("R6  连续跌破MA20≥5日", lambda f: f["below_ma20_days"] >= 5),
        ("R7  5日跌超3%", lambda f: f["ret5"] is not None and f["ret5"] < -3),
        ("R8  5日跌超5%", lambda f: f["ret5"] is not None and f["ret5"] < -5),
        ("R9  10日跌超5%", lambda f: f["ret10"] is not None and f["ret10"] < -5),
        ("R10 MA5<MA20 且 MA5下行",
         lambda f: (f["ma5"] and f["ma20"] and f["ma5"] < f["ma20"]
                    and f["ma5_slope"] is not None and f["ma5_slope"] < 0)),
        ("R11 MA20走平/下行 + 收盘在MA20下",
         lambda f: (f["ma20_slope"] is not None and f["ma20_slope"] <= 0
                    and f["dev_ma20"] is not None and f["dev_ma20"] < 0)),
        ("R12 四线全破(MA5/10/20/60)",
         lambda f: all(f[k] is not None and f["close"] < f[k]
                       for k in ("ma5", "ma10", "ma20", "ma60"))),
    ]

    print("\n" + "=" * 74)
    print(f"单规则评估（持有期 {HORIZON} 交易日）")
    print("=" * 74)
    print(f"{'规则':<28}{'样本':>6}{'覆盖率':>8}{'下跌率':>8}{'触发均收':>10}{'未触发':>9}{'躲开':>8}")
    print("-" * 74)

    results = []
    for name, cond in rules:
        r = eval_rule(samples, name, cond, HORIZON)
        if not r:
            print(f"{name:<28}{'—':>6}  (无触发)")
            continue
        results.append(r)
        print(f"{r['name']:<28}{r['n']:>6}{r['coverage']:>7.1f}%"
              f"{r['down_rate']:>7.1f}%{r['avg_trig']:>9.2f}%"
              f"{r['avg_norm']:>8.2f}%{r['avoid']:>7.2f}%")

    # ------------------------------------------------ 多持有期验证
    print("\n" + "=" * 74)
    print("稳健性：换持有期（避免过拟合到单一 horizon）")
    print("=" * 74)
    key_rules = [
        ("R4  跌破MA20超3%", lambda f: f["dev_ma20"] is not None and f["dev_ma20"] < -3),
        ("R5  连续跌破≥3日", lambda f: f["below_ma20_days"] >= 3),
        ("R8  5日跌超5%", lambda f: f["ret5"] is not None and f["ret5"] < -5),
        ("R12 四线全破",
         lambda f: all(f[k] is not None and f["close"] < f[k]
                       for k in ("ma5", "ma10", "ma20", "ma60"))),
    ]
    print(f"{'规则':<24}{'H=1':>14}{'H=3':>14}{'H=5':>14}{'H=10':>14}")
    print(f"{'':<24}{'下跌率/均收':>14}{'下跌率/均收':>14}{'下跌率/均收':>14}{'下跌率/均收':>14}")
    print("-" * 74)
    for name, cond in key_rules:
        cells = []
        for h in (1, 3, 5, 10):
            ss = build_samples(feats, h)
            r = eval_rule(ss, name, cond, h)
            cells.append(f"{r['down_rate']:.0f}%/{r['avg_trig']:+.1f}" if r else "—")
        print(f"{name:<24}" + "".join(f"{c:>14}" for c in cells))

    # ------------------------------------------------ 9/21 专项
    print("\n" + "=" * 74)
    print("9/21 专项：各规则当时的状态")
    print("=" * 74)
    idx = next((i for i, f in enumerate(feats) if f["date"] == "2026-09-21"), None)
    if idx is None:
        print("  ⚠️ 未找到 9/21")
    else:
        f = feats[idx]
        print(f"\n  9/21 收盘 {f['close']:.2f}  MA5={f['ma5']:.2f} "
              f"MA10={f['ma10']:.2f} MA20={f['ma20']:.2f} MA60={f['ma60']:.2f}")
        print(f"  对MA20偏离 {f['dev_ma20']:+.2f}%   MA20斜率 {f['ma20_slope']:+.3f}%")
        print(f"  5日 {f['ret5'] if f['ret5'] is not None else 0:+.2f}%  "
              f"10日 {f['ret10'] if f['ret10'] is not None else 0:+.2f}%  "
              f"20日 {f['ret20'] if f['ret20'] is not None else 0:+.2f}%")
        print(f"  连续跌破MA20天数: {f['below_ma20_days']}")
        print()
        print(f"  {'规则':<28}{'是否触发':>10}")
        print("  " + "-" * 40)
        for name, cond in rules:
            hit = cond(f)
            print(f"  {name:<28}{'🔴 触发' if hit else '⚪ 未触发':>10}")

        # 9/21 之后实际走势
        print(f"\n  9/21 之后实际走势:")
        for k in (1, 2, 3, 5, 7):
            r = future_ret(feats, idx, k)
            if r is not None:
                print(f"    T+{k:<3} {r:+6.2f}%")

    # ------------------------------------------------ 组合规则
    print("\n" + "=" * 74)
    print("组合规则：OR 并联（任一触发即降级）")
    print("=" * 74)
    combos = [
        ("A  跌破MA20超2% OR 连破≥3日",
         lambda f: ((f["dev_ma20"] is not None and f["dev_ma20"] < -2)
                    or f["below_ma20_days"] >= 3)),
        ("B  跌破MA20超3% OR 5日跌超5%",
         lambda f: ((f["dev_ma20"] is not None and f["dev_ma20"] < -3)
                    or (f["ret5"] is not None and f["ret5"] < -5))),
        ("C  四线全破 OR 5日跌超5%",
         lambda f: (all(f[k] is not None and f["close"] < f[k]
                        for k in ("ma5", "ma10", "ma20", "ma60"))
                    or (f["ret5"] is not None and f["ret5"] < -5))),
        ("D  MA5<MA20且下行 OR 连破≥3日",
         lambda f: ((f["ma5"] and f["ma20"] and f["ma5"] < f["ma20"]
                     and f["ma5_slope"] is not None and f["ma5_slope"] < 0)
                    or f["below_ma20_days"] >= 3)),
    ]
    print(f"{'组合规则':<36}{'样本':>6}{'覆盖率':>8}{'下跌率':>8}{'均收':>9}{'躲开':>8}")
    print("-" * 74)
    for name, cond in combos:
        r = eval_rule(samples, name, cond, HORIZON)
        if not r:
            print(f"{name:<36}(无触发)")
            continue
        print(f"{r['name']:<36}{r['n']:>6}{r['coverage']:>7.1f}%"
              f"{r['down_rate']:>7.1f}%{r['avg_trig']:>8.2f}%{r['avoid']:>7.2f}%")

    # 组合在 9/21 的表现
    print("\n组合规则在 9/21:")
    if idx is not None:
        for name, cond in combos:
            print(f"  {name:<36}{'🔴 触发' if cond(feats[idx]) else '⚪ 未触发'}")

    # ------------------------------------------------ 基线
    print("\n" + "=" * 74)
    print("基线：全样本无条件表现（作为对照）")
    print("=" * 74)
    all_rets = [r for _, _, r in samples]
    print(f"  全样本 {len(all_rets)} 条  "
          f"下跌率 {sum(1 for r in all_rets if r < 0)/len(all_rets)*100:.1f}%  "
          f"平均 {sum(all_rets)/len(all_rets):+.2f}%")

    print("\n" + "=" * 74)
    print("完成")
    print("=" * 74)


if __name__ == "__main__":
    main()
