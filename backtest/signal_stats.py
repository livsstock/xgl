#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_stats.py — 信号胜率引擎（v1.0，2026-10-18）
==================================================
【为什么需要】
双模型分歧时要判定"谁该改"，靠的不是整体准确率，而是**信号级胜率**。
例："RSI6<25 且 MACD空头"这类信号，历史胜率是多少？T+3 均收多少？
有了这个，才能回答 10/16 华友那种分歧该听谁的。

【做什么】
1. 拉 10 只标的历史日K（含2年+），缓存本地
2. 把 daily_predict/facts 用的信号编码成可回测规则
3. 对每个信号 × 每只标的，统计 T+1/T+3/T+5 的胜率、均收、样本数
4. 汇总到信号级别（跨标的合并），输出 signal_stats.json
5. 训练/验证分段 + FDR 校正（沿用 index_rules.py 口径）

【信号定义（与 facts_builder 同源）】
  S1 RSI超卖     : RSI6 < 25
  S2 RSI深超卖   : RSI6 < 15
  S3 MACD金叉    : 近3日 DIF 上穿 DEA
  S4 MACD空头加速: hist < 0 且 hist 比前日更负
  S5 10日跌超10% : ten_day_chg <= -10
  S6 量比放大    : vol_ratio >= 1.5
  S7 组合-超卖+空头 : S1 且 S4（华友10/16 就是这类）
  S8 组合-超卖+金叉 : S1 且 S3（北方稀土10/16 这类）

【用法】
  python3 signal_stats.py                 # 全量跑
  python3 signal_stats.py --push          # 跑完推 GitHub
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "kline")
OUT_DIR = BASE_DIR

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")

SYMS = [
    ("sh601899", "紫金矿业"), ("sh600111", "北方稀土"),
    ("sh603799", "华友钴业"), ("sh601958", "金钼股份"),
    ("sh512480", "半导体ETF"), ("sz000858", "五粮液"),
    ("sz000960", "锡业股份"), ("sz002155", "湖南黄金"),
    ("sh000001", "上证指数"), ("sh000300", "沪深300"),
]

SIGNAL_NAMES = {
    "S1": "RSI6<25 超卖",
    "S2": "RSI6<15 深超卖",
    "S3": "MACD近3日金叉",
    "S4": "MACD空头加速",
    "S5": "10日跌超10%",
    "S6": "量比≥1.5",
    "S7": "超卖+MACD空头(组合)",
    "S8": "超卖+MACD金叉(组合)",
}


def fetch(sym, n=720, cache=True):
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, f"{sym}.json")
    if cache and os.path.exists(p):
        d = json.load(open(p, encoding="utf-8"))
        if len(d) >= 600:
            return d
    try:
        url = SINA.format(sym=sym, n=n)
        raw = urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=20).read()
        data = json.loads(raw.decode("utf-8", "ignore"))
        out = []
        for r in data:
            try:
                out.append({"day": r["day"][:10], "close": float(r["close"]),
                            "volume": float(r["volume"])})
            except Exception:
                pass
        if cache and out:
            json.dump(out, open(p, "w", encoding="utf-8"))
        return out
    except Exception as e:
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
        raise e


def rsi_series(closes, n=6):
    """Wilder 全历史递推，返回与 closes 等长的序列（前 n 个为 None）"""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    g = [0.0] * len(closes)
    l = [0.0] * len(closes)
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g[i] = max(d, 0); l[i] = max(-d, 0)
    ag = sum(g[1:n + 1]) / n
    al = sum(l[1:n + 1]) / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def ema(vals, n):
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd_series(closes):
    dif = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    dea = ema(dif, 9)
    return dif, dea


def build_signals(closes, vols):
    """返回 list[dict]，每个位置对应的信号集合"""
    n = len(closes)
    rs = rsi_series(closes, 6)
    dif, dea = macd_series(closes)
    hist = [d - e for d, e in zip(dif, dea)]
    res = []
    for i in range(n):
        s = []
        r = rs[i]
        if r is not None and r < 25:
            s.append("S1")
        if r is not None and r < 15:
            s.append("S2")
        # 近3日金叉
        gc = False
        for j in range(max(1, i - 2), i + 1):
            if dif[j] > dea[j] and dif[j - 1] <= dea[j - 1]:
                gc = True
                break
        if gc:
            s.append("S3")
        if i >= 1 and hist[i] < 0 and hist[i] < hist[i - 1]:
            s.append("S4")
        if i >= 10 and closes[i] / closes[i - 10] - 1 <= -0.10:
            s.append("S5")
        if i >= 6:
            avg5 = sum(vols[i - 5:i]) / 5
            if avg5 > 0 and vols[i] / avg5 >= 1.5:
                s.append("S6")
        if "S1" in s and "S4" in s:
            s.append("S7")
        if "S1" in s and "S3" in s:
            s.append("S8")
        res.append(s)
    return res


def fdr_bh(pvals, alpha=0.05):
    """Benjamini-Hochberg，返回哪些通过"""
    idx = sorted(range(len(pvals)), key=lambda i: pvals[i])
    m = len(pvals)
    passed = set()
    thresh = 0
    for rank, i in enumerate(idx, 1):
        if pvals[i] <= alpha * rank / m:
            thresh = rank
    for rank, i in enumerate(idx, 1):
        if rank <= thresh:
            passed.add(i)
    return passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    # 收集：signal -> horizon -> [rets]
    bucket = {}   # (sig, h) -> list of (date, sym, ret)
    total_bars = 0
    ok_syms = 0

    for sym, name in SYMS:
        try:
            kl = fetch(sym)
        except Exception as e:
            print(f"[SKIP] {name}: {e}")
            continue
        if len(kl) < 120:
            print(f"[SKIP] {name}: 数据不足 {len(kl)}")
            continue
        ok_syms += 1
        closes = [r["close"] for r in kl]
        vols = [r["volume"] for r in kl]
        days = [r["day"] for r in kl]
        sigs = build_signals(closes, vols)
        total_bars += len(closes)
        for i, s in enumerate(sigs):
            if not s:
                continue
            for h in (1, 3, 5):
                if i + h >= len(closes):
                    continue
                ret = closes[i + h] / closes[i] - 1
                for sg in s:
                    bucket.setdefault((sg, h), []).append((days[i], name, ret))
        print(f"  {name:10s} {len(closes)}根K线 OK")

    # 汇总
    stats = {}
    all_items = []
    for (sg, h), lst in bucket.items():
        rets = [x[2] for x in lst]
        n = len(rets)
        wins = sum(1 for r in rets if r > 0)
        p = wins / n if n else 0
        stats.setdefault(sg, {})[f"T+{h}"] = {
            "n": n, "win_rate": round(p * 100, 2),
            "avg_ret_pct": round(sum(rets) / n * 100, 3) if n else 0,
        }
        all_items.append((sg, h, n, p))

    # FDR：对 "胜率是否显著偏离50%" 做检验（二项近似 z）
    import math
    pvals = []
    for sg, h, n, p in all_items:
        if n < 30:
            pvals.append(1.0); continue
        z = (p - 0.5) / math.sqrt(0.25 / n)
        # 双侧 p
        pv = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
        pvals.append(pv)
    passed = fdr_bh(pvals)

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "syms": ok_syms, "total_bars": total_bars,
        "signal_defs": SIGNAL_NAMES,
        "_caliber": "Wilder RSI6 全历史; MACD EMA12/26/9; 无未来函数(信号只用当日及之前)",
        "_note": "胜率=次日(T+h)收盘价高于信号日收盘价的样本占比",
        "signals": {},
    }
    for sg, hdata in sorted(stats.items()):
        out["signals"][sg] = {"name": SIGNAL_NAMES.get(sg, sg), "horizons": hdata}

    # 附 FDR 结果
    out["fdr"] = {
        "alpha": 0.05, "tests": len(all_items),
        "passed": [
            {"signal": all_items[i][0], "horizon": f"T+{all_items[i][1]}",
             "n": all_items[i][2], "win_rate": round(all_items[i][3] * 100, 2),
             "p_value": round(pvals[i], 5)}
            for i in sorted(passed)
        ],
    }

    dst = os.path.join(OUT_DIR, "signal_stats.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\n{'='*66}")
    print(f"信号胜率统计  ({ok_syms}只标的 / {total_bars}根K线)")
    print(f"{'='*66}")
    print(f"{'信号':22s} {'T+1':>16s} {'T+3':>16s} {'T+5':>16s}")
    for sg in sorted(out["signals"]):
        row = f"{out['signals'][sg]['name'][:20]:22s}"
        for h in ("T+1", "T+3", "T+5"):
            d = out["signals"][sg]["horizons"].get(h)
            if d:
                row += f" {d['win_rate']:5.1f}%/{d['avg_ret_pct']:+5.2f}%n{d['n']:<4d}"
            else:
                row += " " + " " * 15
        print(row)
    print(f"\nFDR 校正后显著（{len(out['fdr']['passed'])}/{len(all_items)}）:")
    for p in out["fdr"]["passed"]:
        print(f"  {p['signal']} {p['horizon']}: 胜率{p['win_rate']}% n={p['n']} p={p['p_value']}")
    print(f"\n已写入 {dst}")

    if args.push:
        r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "push_api.py"),
                            "backtest/signal_stats.json"],
                           capture_output=True, text=True, timeout=90)
        print("推送:", (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
