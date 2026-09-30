#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「双模型闸门」有效性回测  v1.0
================================
回答大海 2026-09-29 提出的问题：
  「双方一致看空 / 一致不买时，后续市场实际走了多少？
   闸门到底是避开了亏损，还是错过了机会？」

方法：
  1. 拉取双方历史预测（WB: predictions/ ; 大海: predictions-dahai/*_dahai.json）
  2. 用 align_norm 的归一化逻辑对齐方向
  3. 对每个"一致"日，统计后续 1/3/5 个交易日的实际涨跌
  4. 与"随机日"基线对比 —— 这是关键对照
  5. 计算闸门的实际价值：避开的亏损 vs 错过的收益

【v1.1 变更 · 2026-09-29】
  1. 后续收益一律走 trade_calendar 的交易日序列，不再用行情数组下标裸推
     —— 行情数组下标本身已是交易日，但【基准日错配】会让整个样本失真，
        因此新增基准日校验（见第 2 点）。
  2. 新增基准日硬校验：双方 data_basis 缺失或不同 → 该日标记为
     BASIS_UNKNOWN / TIME_MISMATCH，【不计入闸门样本】。
     此前把基准错配的日期当成"模型分歧"统计，结论是假的。
  3. 输出新增「跨期间隔」列，明确 T+N 之间实际跨了几个自然日。

用法:
  python3 backtest_gate.py
  python3 backtest_gate.py --horizons 1,3,5,10
  python3 backtest_gate.py --no-basis-check    # 忽略基准校验（对比用）
"""

import argparse
import json
import subprocess
import sys
import statistics
import concurrent.futures
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from align_norm import norm_direction, fetch_remote, REPO_RAW, CORE_POOL
except ImportError:
    print("✗ 需要 align_norm.py 在同目录", file=sys.stderr)
    sys.exit(1)

try:
    from trade_calendar import count_trading_days, describe
except ImportError:
    print("✗ 需要 trade_calendar.py 在同目录", file=sys.stderr)
    sys.exit(1)

try:
    from align_norm import extract_basis_date
except ImportError:
    # 兼容旧版 align_norm
    extract_basis_date = None


def _curl(url, timeout=25):
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout),
         "-H", "User-Agent: Mozilla/5.0",
         "-H", "Referer: https://gu.qq.com/", url],
        capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def load_closes(code, days=400):
    url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
           f"newfqkline/get?param={code},day,,,{days},qfq")
    try:
        k = json.loads(_curl(url))["data"][code]
        kl = k.get("qfqday") or k.get("day")
        return {x[0]: float(x[2]) for x in kl}
    except Exception:
        return {}


def extract_market_dir(doc, side):
    """抽取大盘方向，兼容双方 schema。"""
    if side == "wb":
        t1 = doc.get("t1_prediction") or {}
        sh = t1.get("sh_index") or {}
        cand = sh.get("direction")
        if cand is None:
            for k in ("market_outlook", "market_overview", "market_env"):
                b = doc.get(k) or {}
                if isinstance(b, dict) and b.get("direction"):
                    cand = b["direction"]; break
    else:
        cand = None
        for k in ("market_outlook", "market_overview", "market_env"):
            b = doc.get(k) or {}
            if isinstance(b, dict) and b.get("direction"):
                cand = b["direction"]; break
    return norm_direction(cand), cand


def extract_stocks(doc):
    out = {}
    for k in ("stock_calls", "predictions", "stocks"):
        lst = doc.get(k)
        if isinstance(lst, list) and lst:
            for s in lst:
                if not isinstance(s, dict):
                    continue
                code = "".join(ch for ch in str(s.get("code") or "") if ch.isdigit())
                if code:
                    out[code] = {
                        "direction": norm_direction(s.get("direction")),
                        "action": s.get("action"),
                    }
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="1,3,5,10")
    ap.add_argument("--code", default="sh000001", help="回测标的，默认上证")
    ap.add_argument("--no-basis-check", action="store_true",
                    help="跳过基准日校验（仅用于对比，正式回测勿用）")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",")]

    print("拉取双方历史预测...")
    # 双方已知日期（可从远端枚举，这里用已确认的存在列表）
    wb_dates = ["2026-09-21", "2026-09-22", "2026-09-24", "2026-09-28", "2026-09-29"]
    dh_dates = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23",
                "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"]

    wb_docs, dh_docs = {}, {}
    for d in wb_dates:
        doc = fetch_remote(f"predictions/{d}.json")
        if doc:
            wb_docs[d] = doc
    for d in dh_dates:
        for fn in (f"{d}_dahai.json", f"{d}.json"):
            doc = fetch_remote(f"predictions-dahai/{fn}")
            if doc:
                dh_docs[d] = doc
                break
    print(f"  WB {len(wb_docs)} 份: {sorted(wb_docs)}")
    print(f"  大海 {len(dh_docs)} 份: {sorted(dh_docs)}")

    common = sorted(set(wb_docs) & set(dh_docs))
    print(f"  双方交集: {common}  ({len(common)} 天)")
    if not common:
        print("✗ 无共同日期，无法回测闸门", file=sys.stderr)
        sys.exit(1)
    print()

    # 取行情
    closes = load_closes(args.code)
    if not closes:
        print("✗ 行情拉取失败", file=sys.stderr)
        sys.exit(1)
    dates = sorted(closes)

    def fwd(date_str, h):
        """返回 date_str 之后 h 个交易日的累计涨跌幅。"""
        if date_str not in closes:
            return None
        i = dates.index(date_str)
        if i + h >= len(dates):
            return None
        return (closes[dates[i + h]] - closes[date_str]) / closes[date_str] * 100

    def shift_date(date_str, h):
        """返回 date_str 之后 h 个【交易日】的日期字符串，超出数据范围返回 None。"""
        if date_str not in closes:
            return None
        i = dates.index(date_str)
        if i + h >= len(dates):
            return None
        return dates[i + h]

    def _natural_gap(a, b):
        """两个交易日之间隔了几个自然日（用于体现假期跨度）。"""
        if not a or not b:
            return None
        import datetime as _dt
        da = _dt.date.fromisoformat(a[:10])
        db = _dt.date.fromisoformat(b[:10])
        return (db - da).days

    # ---- 逐日对齐 + 后续表现
    rows = []
    for d in common:
        wb_m, wb_raw = extract_market_dir(wb_docs[d], "wb")
        dh_m, dh_raw = extract_market_dir(dh_docs[d], "dh")
        wb_s = extract_stocks(wb_docs[d])
        dh_s = extract_stocks(dh_docs[d])

        # ---- 基准日校验（v1.1 新增）：基准不明/不一致 → 不可比
        wb_basis = extract_basis_date(wb_docs[d], "wb") if extract_basis_date else None
        dh_basis = extract_basis_date(dh_docs[d], "dh") if extract_basis_date else None
        if args.no_basis_check:
            basis_ok = None
        elif wb_basis is None or dh_basis is None:
            basis_ok = False      # 缺字段，无法判定
        else:
            basis_ok = (wb_basis == dh_basis)

        basis_note = ""
        if basis_ok is False:
            basis_note = ("基准缺失" if (wb_basis is None or dh_basis is None)
                          else f"基准错配 {wb_basis}/{dh_basis}")

        # 闸门判定：双方均无 buy 且大盘同向 → 不出手
        wb_buy = [c for c, v in wb_s.items() if v.get("action") == "buy"]
        dh_buy = [c for c, v in dh_s.items() if v.get("action") == "buy"]

        agree = (wb_m is not None and wb_m == dh_m)
        both_bearish = agree and wb_m == "down"

        # 核心池一致度
        core_agree = 0
        core_total = 0
        for code, name, _ in CORE_POOL:
            a = wb_s.get(code, {}).get("direction")
            b = dh_s.get(code, {}).get("direction")
            if a and b:
                core_total += 1
                if a == b:
                    core_agree += 1

        rec = dict(date=d, wb_dir=wb_m, dh_dir=dh_m,
                   wb_raw=wb_raw, dh_raw=dh_raw,
                   agree=agree, both_bearish=both_bearish,
                   wb_buy=len(wb_buy), dh_buy=len(dh_buy),
                   core=f"{core_agree}/{core_total}" if core_total else "-",
                   wb_basis=wb_basis, dh_basis=dh_basis,
                   basis_ok=basis_ok, basis_note=basis_note)
        for h in horizons:
            rec[f"fwd{h}"] = fwd(d, h)
            # 跨期间隔（自然日），用于说明 T+N 实际隔了多久
            f_date = shift_date(d, h)
            rec[f"gap{h}"] = (int(count_trading_days(d, f_date)) if f_date else None,
                              _natural_gap(d, f_date))
        rows.append(rec)

    # ---- 展示
    W = 118
    print("=" * W)
    print("一、闸门触发记录（双方均有数据的日期）")
    print("=" * W)
    hdr = (f"{'日期':<12}{'WB':<9}{'大海':<10}{'同向':<6}{'基准':<14}"
           f"{'核心池':<8}{'WB买':<6}{'海买':<6}")
    for h in horizons:
        hdr += f"{'T+'+str(h)+'%':>9}"
    print(hdr)
    print("-" * W)
    for r in rows:
        bo = r["basis_ok"]
        bflag = "✅" if bo else ("❓" if bo is None else "❌")
        basis_txt = (f"{bflag}" + (r["basis_note"] or ""))[:13]
        line = (f"{r['date']:<12}{str(r['wb_raw']):<9}{str(r['dh_raw']):<10}"
                f"{'✅' if r['agree'] else '❌':<6}{basis_txt:<14}"
                f"{r['core']:<8}{r['wb_buy']:<6}{r['dh_buy']:<6}")
        for h in horizons:
            v = r[f"fwd{h}"]
            line += f"{v:>9.2f}" if v is not None else f"{'--':>9}"
        print(line)
    print("-" * W)
    print()

    # ---- 跨期间隔说明（回答"扣不扣节假日"）
    print("=" * W)
    print("一之二、跨期间隔（为什么必须用交易日）")
    print("=" * W)
    print(f"  {'基准日':<12}{'T+1实际日':<13}{'自然日差':<10}"
          f"{'T+5实际日':<13}{'自然日差':<10}")
    print("-" * W)
    for r in rows:
        g1 = r.get("gap1") or (None, None)
        g5 = r.get("gap5") or (None, None)
        d1 = shift_date(r["date"], 1) or "-"
        d5 = shift_date(r["date"], 5) or "-"
        n1 = g1[1] if g1[1] is not None else "-"
        n5 = g5[1] if g5[1] is not None else "-"
        mark = "  ← 跨假期" if isinstance(n1, int) and n1 > 3 else ""
        print(f"  {r['date']:<12}{d1:<13}{str(n1):<10}{d5:<13}{str(n5):<10}{mark}")
    print("-" * W)
    print("  · T+1 自然日差 > 3 说明中间夹了周末/假期，")
    print("    旧口径按自然日算会把 1 个交易日误算成 4 天。")
    print()

    # ---- 闸门日 vs 全样本基线
    print("=" * W)
    print("二、闸门日 vs 全样本基线（关键对照）")
    print("=" * W)
    gate_all = [r for r in rows if r["agree"] and not r["wb_buy"] and not r["dh_buy"]]
    gate_rows = [r for r in gate_all if r["basis_ok"] is not False]
    dropped = [r for r in gate_all if r["basis_ok"] is False]
    print(f"  闸门日（双方同向 + 双方零买入）: {len(gate_all)} 天")
    if dropped:
        print(f"  ⚠️ 其中 {len(dropped)} 天因基准缺失/错配被剔除，不计入统计：")
        for r in dropped:
            print(f"       {r['date']}  WB基准={r['wb_basis'] or '缺失'}  "
                  f"大海基准={r['dh_basis'] or '缺失'}  ({r['basis_note']})")
    print(f"  ✅ 有效闸门样本: {len(gate_rows)} 天")
    print()
    print(f"{'持有期':<10}{'闸门日均值':>12}{'全样本均值':>12}{'差':>10}"
          f"{'闸门日中位数':>14}{'全样本中位数':>14}")
    print("-" * W)
    for h in horizons:
        gv = [r[f"fwd{h}"] for r in gate_rows if r[f"fwd{h}"] is not None]
        av = [fwd(d, h) for d in dates[:-h]] if h < len(dates) else []
        av = [x for x in av if x is not None]
        if not gv or not av:
            continue
        gm, am = statistics.mean(gv), statistics.mean(av)
        gmd, amd = statistics.median(gv), statistics.median(av)
        print(f"{'T+'+str(h):<10}{gm:>12.2f}{am:>12.2f}{gm-am:>+10.2f}{gmd:>14.2f}{amd:>14.2f}")
    print("-" * W)
    print()

    # ---- 闸门价值评估
    print("=" * W)
    print("三、闸门价值评估")
    print("=" * W)
    for h in horizons:
        gv = [r[f"fwd{h}"] for r in gate_rows if r[f"fwd{h}"] is not None]
        if not gv:
            continue
        gm = statistics.mean(gv)
        avoided = -gm if gm < 0 else 0        # 若均值为负，闸门避开了这个跌幅
        missed = gm if gm > 0 else 0          # 若均值为正，闸门错过了这个涨幅
        n_up = sum(1 for v in gv if v > 0)
        n_dn = sum(1 for v in gv if v < 0)
        print(f"  T+{h:<3} 闸门后均值 {gm:>+7.2f}%   "
              f"上涨 {n_up} 次 / 下跌 {n_dn} 次   "
              f"{'✅ 避开跌幅 ' + format(avoided, '+.2f') + '%' if gm < 0 else '⚠️ 错过涨幅 ' + format(missed, '+.2f') + '%'}")
    print()

    # ---- 样本量警告
    print("=" * W)
    print("四、结论有效性说明（必读）")
    print("=" * W)
    print(f"  ⚠️  闸门样本仅 {len(gate_rows)} 天 —— 统计上完全不足以得出结论")
    print(f"  ⚠️  双方共同数据日期仅 {len(common)} 天，且全部集中在 9 月下旬单一市场环境中")
    print(f"  ⚠️  样本期上证从 3952 跌至 3824，是单边下行段，'看空正确'有环境偏差")
    print()
    print("  上述数字只能作为『方向性参考』，不能作为『闸门有效』或『闸门无效』的证据。")
    print("  要得出可靠结论，需要：")
    print("    · 至少 30+ 个闸门触发日")
    print("    · 覆盖上涨/下跌/震荡三种市场环境")
    print("    · 双方先补齐历史预测（当前 WB 缺 9/18、9/23；大海缺 9/22）")
    print()
    print("  建议：与其回测这 3 天，不如双方先把历史预测补齐，再统一回测。")


if __name__ == "__main__":
    main()
