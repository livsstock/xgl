#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双模型交叉验证 v2（事实/观点分层架构）
================================================================
读取：
  facts/YYYY-MM-DD.json            统一事实层（唯一）
  opinions-dahai/YYYY-MM-DD_dahai.json   大海观点
  opinions/YYYY-MM-DD.json 或 opinions-wb/YYYY-MM-DD_wb.json  WB观点

流程：
  1. 事实门：facts.quality=complete 才继续，否则全天停工
  2. 观点校验：双方文件都过 validate_opinion（强制质量门）
  3. 逐标的比对：基于"同一份事实"，所以此时的方向分歧 = 真分歧
  4. 行动裁决：只有双方都给买入动作才允许"候选买入"，否则观望
  5. 输出中文对齐表 + JSON存档

用法：
  python3 reconcile_v2.py --facts-date 2026-10-16 [--opinion-date 2026-10-19]
"""
import json
import os
import sys
import argparse
import subprocess

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR = os.path.join(BASE, "scripts", "validate_opinion.py")

CORE6 = ["601899", "600111", "603799", "601958", "512480", "000858"]
NAMES = {"601899": "紫金矿业", "600111": "北方稀土", "603799": "华友钴业",
         "601958": "金钼股份", "512480": "半导体ETF", "000858": "五粮液"}

DIR_CN = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}
ACT_CN = {"buy_probe": "试探买入", "buy_standard": "标准买入", "hold": "持有",
          "watch": "观望", "sell": "卖出"}


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_validator(opinion_path, facts_path):
    r = subprocess.run([sys.executable, VALIDATOR, opinion_path,
                        "--facts", facts_path], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def index_calls(op):
    return {str(c["code"]): c for c in op.get("calls", [])}


def compare(a, b):
    """返回 (关系, 中文标记)"""
    if a is None or b is None:
        return "missing", "❓缺观点"
    if a == b:
        return "agree", "✅一致"
    if {a, b} == {"bullish", "bearish"}:
        return "strong", "🔶强分歧"
    return "weak", "⚠️分歧"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-date", required=True)
    ap.add_argument("--opinion-date", default=None)
    args = ap.parse_args()
    op_date = args.opinion_date or args.facts_date

    facts_path = os.path.join(BASE, "facts", f"{args.facts_date}.json")
    dh_path = os.path.join(BASE, "opinions-dahai", f"{op_date}_dahai.json")
    wb_candidates = [
        os.path.join(BASE, "opinions-wb", f"{op_date}_wb.json"),
        os.path.join(BASE, "opinions", f"{op_date}.json"),
    ]

    facts = load_json(facts_path)
    result = {
        "facts_date": args.facts_date, "opinion_date": op_date,
        "schema_version": "reconcile-v2",
        "facts_gate": "unknown", "opinions_valid": {},
        "market": {}, "calls": [], "conclusion": "",
    }

    # 1. 事实门
    if facts is None:
        print(f"🔴 事实门：facts不存在 {facts_path}，全天停工")
        result["facts_gate"] = "missing"
        _save(result, args)
        return 1
    if facts.get("overall_quality") != "complete":
        print(f"🔴 事实门：facts质量={facts.get('overall_quality')}，全天停工")
        print("   问题:", facts.get("quality_errors"))
        result["facts_gate"] = "incomplete"
        _save(result, args)
        return 1
    result["facts_gate"] = "pass"
    print(f"✅ 事实门通过：{args.facts_date} 数据完整")

    # 2. 观点校验
    dh = load_json(dh_path)
    wb = None
    wb_path_used = None
    for p in wb_candidates:
        if os.path.exists(p):
            wb = load_json(p)
            wb_path_used = p
            break

    for who, op, p in [("大海", dh, dh_path), ("WB", wb, wb_path_used)]:
        if op is None:
            result["opinions_valid"][who] = False
            print(f"⚠️ {who}观点缺失: {p}")
            continue
        ok, msg = run_validator(p, facts_path)
        result["opinions_valid"][who] = ok
        if not ok:
            print(f"🔴 {who}观点未过质量门，拒绝纳入对齐:\n{msg}")
        else:
            print(f"✅ {who}观点通过质量门")

    both_valid = result["opinions_valid"].get("大海") and result["opinions_valid"].get("WB")

    # 3. 大盘
    fmk = facts.get("market", {})
    print(f"\n【大盘事实】上证收{fmk.get('close')} {fmk.get('change_pct')}% "
          f"10日{fmk.get('ten_day_chg')}% RSI6={fmk.get('rsi6')} MACD={fmk.get('macd_signal')}")
    dmd = dh.get("market", {}).get("direction") if dh else None
    wmd = wb.get("market", {}).get("direction") if wb else None
    mrel, mtag = compare(dmd, wmd)
    result["market"] = {
        "facts": fmk, "dahai": DIR_CN.get(dmd, "缺失"),
        "wb": DIR_CN.get(wmd, "缺失"), "relation": mrel,
    }
    print(f"【大盘观点】大海={DIR_CN.get(dmd,'缺失')} WB={DIR_CN.get(wmd,'缺失')} → {mtag}")

    # 大盘开关
    market_block = (dmd == "bearish" or wmd == "bearish")
    if market_block:
        print("⛔ 大盘开关：至少一方看跌 → 冻结一切新买入")

    # 4. 逐标的
    dhc = index_calls(dh) if dh else {}
    wbc = index_calls(wb) if wb else {}

    print(f"\n{'标的':8s}{'事实10日':>9s}{'RSI6':>7s}  {'大海':>10s}{'WB':>10s}  方向    裁决")
    print("-" * 78)
    buy_candidates = []
    strong_divs = []
    for code in CORE6:
        f = facts["instruments"].get(code, {})
        d = dhc.get(code)
        w = wbc.get(code)
        dd = d.get("direction") if d else None
        wd = w.get("direction") if w else None
        rel, tag = compare(dd, wd)

        da = d.get("action") if d else None
        wa = w.get("action") if w else None
        both_buy = (da in ("buy_probe", "buy_standard") and
                    wa in ("buy_probe", "buy_standard"))

        # 裁决
        if market_block and both_buy:
            verdict = "⛔大盘冻结"
        elif both_buy and both_valid:
            verdict = "🟢候选买入"
            buy_candidates.append(code)
        elif rel == "strong":
            verdict = "🔶不动(强分歧)"
            strong_divs.append(code)
        elif d is None or w is None:
            verdict = "❓观点不全"
        else:
            verdict = "⚪观望"

        print(f"{NAMES[code]:8s}{str(f.get('ten_day_chg','-')):>9s}"
              f"{str(f.get('rsi6','-')):>7s}  "
              f"{DIR_CN.get(dd,'缺失')+'/'+ACT_CN.get(da,'?'):>10s}"
              f"{DIR_CN.get(wd,'缺失')+'/'+ACT_CN.get(wa,'?'):>10s}  "
              f"{tag:>6s}  {verdict}")

        result["calls"].append({
            "code": code, "name": NAMES[code],
            "facts": {
                "ten_day_chg": f.get("ten_day_chg"),
                "rsi6": f.get("rsi6"),
                "macd_signal": f.get("macd", {}).get("state") if isinstance(f.get("macd"), dict) else None,
            },
            "dahai": {"direction": dd, "action": da,
                      "grade": d.get("kelly_grade") if d else None,
                      "pct": d.get("kelly_pct") if d else None},
            "wb": {"direction": wd, "action": wa,
                   "grade": w.get("kelly_grade") if w else None,
                   "pct": w.get("kelly_pct") if w else None},
            "relation": rel, "verdict": verdict,
        })

    # 5. 总结论
    print("\n" + "=" * 78)
    if not both_valid:
        conclusion = "🔴 观点质量门未全部通过或缺观点，本日不出操作结论，仅展示。"
    elif market_block:
        conclusion = "⛔ 大盘至少一方看跌，冻结一切新买入，已持仓按止损/止盈纪律管理。"
    elif buy_candidates:
        conclusion = f"🟢 双方一致买入候选：{[NAMES[c] for c in buy_candidates]}（仍需凯利分级+阿牛哥确认）"
    elif strong_divs:
        conclusion = f"🔶 存在强分歧{[NAMES[c] for c in strong_divs]}，按纪律不动手；记入真分歧样本。"
    else:
        conclusion = "⚪ 双方无一致买入信号，观望。"
    print(conclusion)
    result["conclusion"] = conclusion
    result["buy_candidates"] = [NAMES[c] for c in buy_candidates]
    result["strong_divergence"] = [NAMES[c] for c in strong_divs]

    _save(result, args)
    return 0


def _save(result, args):
    out_dir = os.path.join(BASE, "reconciliation", "v2")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{args.facts_date}_reconcile.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n💾 对齐结果存档: {out}")


if __name__ == "__main__":
    sys.exit(main())
