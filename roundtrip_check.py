#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
roundtrip_check.py — 事实层往返校验（2026-10-21）
=================================================
【问题】
双方对同一只标的算出不同 RSI，以前只能靠"谁看起来对"来扯。
今天找到根因：**算法版本与数据源可以任意组合，组合出来的数都能"看起来合理"**。

实测 2026-10-20 半导体ETF：
  Wilder 全历史  = 17.01   （WB 事实层）
  Wilder 截断120 = 12.82   （大海）
  SMA6           = 11.69   （他另外几只用的是这个）

三个数都"像"RSI，但对应三个不同口径。差异最大 5.32 点，足以让同一只票
在一边显示"超卖可买"、在另一边显示"严禁抄底"。

【本脚本干什么】
用对方自己发布的算法 + 收盘价，独立复算他的指标，再跟他的文件比。
- 能复现 → 算法一致，分歧是"判断不同"（真分歧，有样本价值）
- 不能复现 → 算法或数据不一致（假分歧，样本作废，必须先修口径）

【输出】
  reconcile/roundtrip/{date}.json
  exit 0 = 全部可复现   1 = 存在不可复现项
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

OUT_DIR = os.path.join(BASE_DIR, "reconcile", "roundtrip")


# ---------------------------------------------------------------- RSI 三种口径

def rsi_wilder_full(closes, n=6):
    """WB 口径：全历史递推（通达信）"""
    if len(closes) < n + 1:
        return None
    d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(max(x, 0) for x in d[:n]) / n
    al = sum(max(-x, 0) for x in d[:n]) / n
    for x in d[n:]:
        ag = (ag * (n - 1) + max(x, 0)) / n
        al = (al * (n - 1) + max(-x, 0)) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def rsi_wilder_window(closes, n=6, window=120):
    """大海口径：截断到最近 window 根再递推"""
    c = closes[-window:]
    return rsi_wilder_full(c, n)


def rsi_sma(closes, n=6):
    """简单均值口径（部分实现用的"RSI"）"""
    if len(closes) < n + 1:
        return None
    g, l = [], []
    for i in range(len(closes) - n, len(closes)):
        x = closes[i] - closes[i - 1]
        g.append(max(x, 0))
        l.append(max(-x, 0))
    ag, al = sum(g) / n, sum(l) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


CALIBERS = {
    "wilder_full": (rsi_wilder_full, "Wilder 全历史（WB 口径/通达信）"),
    "wilder_120": (lambda c: rsi_wilder_window(c, 6, 120), "Wilder 截断120根（大海口径）"),
    "sma6": (rsi_sma, "SMA6 简单均值"),
}


# ---------------------------------------------------------------- 主流程

def load_dahai(path):
    d = json.load(open(path, encoding="utf-8"))
    codes = {}
    for s in d.get("stock_calls") or []:
        c = str(s.get("code", "")).replace("sh", "").replace("sz", "")
        codes[c] = s
    return d, codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--dahai", default=None, help="大海文件路径")
    ap.add_argument("--window", type=int, default=120, help="他的截断窗口，默认120")
    args = ap.parse_args()

    path = args.dahai or os.path.join(BASE_DIR, "predictions",
                                      f"{args.date}_dahai.json")
    if not os.path.exists(path):
        print(f"[ERR] 找不到大海文件：{path}")
        return 1

    from facts_builder import fetch_kline

    d, codes = load_dahai(path)
    rows, fail = [], 0
    # [2026-10-21] 判定口径优先用"-1 收盘"：他 16:40 生成，若盘中跑/有延迟，
    # 可能混到未收盘价。两种都算，取误差最小者，并明确标注用的是哪天的收盘。
    prev_day = None
    try:
        sys.path.insert(0, os.path.join(BASE_DIR, "reconcile"))
        from trade_calendar import prev_trading_day
        prev_day = prev_trading_day(args.date)
    except Exception:
        pass

    # CSV 输出（便于人工核对，不依赖报告）
    csv_dst = os.path.join(OUT_DIR, f"{args.date}.csv")
    csv_lines = ["code,name,dahai_rsi6,wb_rsi6,recomputed,matched,diff,reproducible,basis_day,verdict"]

    # [2026-10-21 修正] 匹配时优先保持 Wilder 族：只有误差 ≤0.05 才接受 SMA 口径。
    # 否则 10-16 那批整数 RSI 会被误判成 Wilder（因为恰好更接近）。
    for code, s in codes.items():
        name = s.get("name")
        his = s.get("rsi6")
        if his is None:
            continue
        sym = ("sh" if code[0] in "56" else "sz") + code
        try:
            kl = fetch_kline(sym, n=200)
        except Exception as e:
            rows.append({"code": code, "name": name, "error": str(e)})
            fail += 1
            continue

        cands = []
        for basis_day in [args.date] + ([prev_day] if prev_day else []):
            closes = [r["close"] for r in kl if r["day"] <= basis_day]
            if len(closes) < 8:
                continue
            for key in CALIBERS:
                if key == "wilder_120":
                    v = rsi_wilder_window(closes, 6, args.window)
                else:
                    v = CALIBERS[key][0](closes)
                if v is None:
                    continue
                cands.append({"basis_day": basis_day, "caliber": key,
                              "value": v, "diff": round(v - his, 2)})

        if not cands:
            continue

        # [2026-10-21 修正] 先判"是否整数"：10-16 那批全是 38/42/22/28/10/68，
        # 一个整数都对不上的概率极低 → 说明那是"人工估值/四舍五入"，不是精确算法输出。
        is_int = float(his).is_integer()
        wilder = [c for c in cands if c["caliber"].startswith("wilder")]
        sma = [c for c in cands if c["caliber"] == "sma6"]
        exact = [c for c in wilder if abs(c["diff"]) <= 0.05]
        if exact:
            best_all = min(exact, key=lambda c: abs(c["diff"]))
        elif sma:
            best_all = min(sma, key=lambda c: abs(c["diff"]))
        else:
            best_all = min(wilder, key=lambda c: abs(c["diff"]))

        ok = abs(best_all["diff"]) <= 0.05
        if is_int and not ok:
            verdict = "整数估值（疑似人工四舍五入，非算法输出）"
        elif ok:
            verdict = "可复现"
        else:
            verdict = "不可复现"
        if not ok:
            fail += 1
        k = best_all["caliber"]
        allvals = {}
        for c in cands:
            if c["basis_day"] == best_all["basis_day"]:
                allvals[c["caliber"]] = c["value"]
        rows.append({
            "code": code, "name": name,
            "dahai_rsi6": his,
            "wb_rsi6": allvals.get("wilder_full"),
            "recomputed": allvals,
            "basis_day": best_all["basis_day"],
            "matched_caliber": k,
            "matched_caliber_desc": CALIBERS[k][1],
            "diff": best_all["diff"],
            "reproducible": ok,
            "verdict": verdict,
        })
        csv_lines.append(f"{code},{name},{his},{allvals.get('wilder_full')},"
                         f"{best_all['value']},{k},{best_all['diff']},{ok},"
                         f"{best_all['basis_day']},{verdict}")

    n = len(rows)
    n_ok = sum(1 for r in rows if r.get("reproducible"))
    calibers_used = {}
    for r in rows:
        c = r.get("matched_caliber")
        if c:
            calibers_used.setdefault(c, []).append(r["name"])

    out = {
        "date": args.date,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "dahai_file": os.path.relpath(path, BASE_DIR),
        "summary": {
            "total": n,
            "reproducible": n_ok,
            "rate": f"{n_ok}/{n}",
            "calibers_detected": {k: v for k, v in calibers_used.items()},
        },
        "rows": rows,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    dst = os.path.join(OUT_DIR, f"{args.date}.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---- 打印 ----
    print(f"\n{'='*72}")
    print(f"  往返校验 {args.date}   （用对方算法复算 → 能否复现他的数）")
    print(f"{'='*72}")
    print(f"  {'标的':<12}{'他报的':>8}{'WB口径':>8}{'基准日':>12}{'匹配口径':>12}{'复算值':>8}{'差':>7}")
    for r in rows:
        if r.get("error"):
            print(f"  {r['name']:<12}  取数失败 {r['error'][:40]}")
            continue
        print(f"  {r['name']:<12}{r['dahai_rsi6']:>8.2f}{r['wb_rsi6']:>8.2f}"
              f"{r['basis_day']:>12}{r['matched_caliber']:>12}"
              f"{r['recomputed'][r['matched_caliber']]:>8.2f}"
              f"{r['diff']:>+7.2f}{'' if r['reproducible'] else '  ✗'}")
    print(f"\n  可复现：{n_ok}/{n}")
    verdicts = {}
    for r in rows:
        v = r.get("verdict")
        if v:
            verdicts[v] = verdicts.get(v, 0) + 1
    for v, cnt in verdicts.items():
        print(f"    {v}: {cnt} 只")
    if calibers_used:
        for k, v in calibers_used.items():
            print(f"    {CALIBERS[k][1]}: {len(v)} 只")
    open(csv_dst, "w", encoding="utf-8").write("\n".join(csv_lines) + "\n")
    print(f"  写入：{dst}")
    print(f"        {csv_dst}\n")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
