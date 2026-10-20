#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reconcile_v2.py — 大海 ↔ WorkBuddy 观点交叉验证 v2（2026-10-20）
=================================================================
【定位】
基于"事实唯一、观点两套"架构的 v2 版对账工具。
与 v1 的关键区别：
  - 以 facts 层为唯一数据基准
  - 分歧归因更精细：数据分歧 vs 真分歧
  - 输出可操作建议（aligned/divergent/partial）
  - 支持 WB opinions-wb/ 和大海 predictions-dahai/ 双格式

【输入】
  --dahai  大海观点文件路径
  --wb     WB观点文件路径
  --facts  facts文件路径（可选，自动匹配日期）

【输出】
  reconcile/reconcile_v2_{date}.json  机器可读对齐报告

【用法】
  python3 reconcile_v2.py \\
    --dahai predictions-dahai/2026-10-16_dahai.json \\
    --wb opinions-wb/2026-10-16_wb.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECONCILE_DIR = os.path.join(BASE_DIR, "reconcile")

# 动作语义对齐：两边统一映射到「无操作 / 观察 / 买入 / 卖出 / 回避」
ACTION_ALIGN = {
    "hold": "无操作",
    "watch": "观察",
    "avoid": "回避",
    "buy": "买入",
    "buy_probe": "买入",
    "buy_standard": "买入",
    "sell": "卖出",
}

# 名称别名
# [2026-10-20 修正] 原版把 "沪深300ETF" 映射成 "沪深300"（指数），
# 会让 ETF 与指数撞名。ETF 名称本身在事实层与预测文件里都是一致的，无需映射。
NAME_ALIAS = {
    "半导体ETF国联安": "半导体ETF",
}


def canon(name):
    """标准化名称。"""
    return NAME_ALIAS.get(name, name)


def norm(action):
    """动作语义归一化。"""
    return ACTION_ALIGN.get(action, action or "?")


def extract_code(code_str):
    """标准化代码：sh601899 -> 601899"""
    if isinstance(code_str, str):
        return code_str.replace("sh", "").replace("sz", "")
    return str(code_str)


def load_json(path):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def build_facts_map(facts):
    """构建 facts code->data 映射。"""
    if not facts:
        return {}
    fm = {}
    if "instruments" in facts:
        for code, data in facts["instruments"].items():
            fm[code] = data
    elif "stocks" in facts:
        for s in facts["stocks"]:
            code = extract_code(s.get("code", ""))
            fm[code] = s
    if "indices" in facts:
        for idx in facts["indices"]:
            code = extract_code(idx.get("code", ""))
            fm[code] = idx
    return fm


def extract_calls(opinion, side):
    """从观点文件中提取标的观点列表，统一格式。"""
    calls_raw = []
    if "calls" in opinion:
        calls_raw = opinion["calls"]
    elif "stock_calls" in opinion:
        calls_raw = opinion["stock_calls"]

    result = {}
    for c in calls_raw:
        code = extract_code(c.get("code", ""))
        name = canon(c.get("name", ""))

        # 统一提取关键字段（兼容两种格式）
        evidence = c.get("evidence", {})
        entry = {
            "code": code,
            "name": name,
            "action": c.get("action"),
            "action_semantic": norm(c.get("action")),
            "direction": c.get("direction"),
            "confidence": c.get("confidence"),
            "kelly_grade": c.get("kelly_grade"),
            "reason": c.get("reason", ""),
            "close": evidence.get("close") or c.get("close"),
            "rsi6": evidence.get("rsi6") or c.get("rsi6"),
            "ten_day_chg": evidence.get("ten_day_chg") or c.get("ten_day_chg"),
            "macd_signal": evidence.get("macd_signal") or c.get("macd_signal"),
            "vol_ratio": evidence.get("vol_ratio"),
            "core_pool": c.get("core_pool", True),
        }
        result[code] = entry
    return result


def check_data_match(call_data, facts_data, field, tolerance_pct=0.01, tolerance_abs=0.1):
    """检查观点数据与facts数据的偏差。
    tolerance_pct: 百分比容差（默认1%）
    tolerance_abs: 绝对容差（用于ten_day_chg等百分比字段）
    """
    if facts_data is None:
        return {"match": None, "reason": "无facts数据"}
    call_val = call_data.get(field)
    facts_val = facts_data.get(field)
    if call_val is None or facts_val is None:
        return {"match": None, "reason": f"{field}缺失"}

    if not isinstance(facts_val, (int, float)):
        return {"match": call_val == facts_val}

    abs_diff = abs(call_val - facts_val)

    if field == "ten_day_chg":
        # ten_day_chg 用绝对偏差0.5（百分比值，10日涨跌幅偏差）
        return {
            "match": abs_diff <= tolerance_abs,
            "call_val": call_val,
            "facts_val": facts_val,
            "abs_diff": round(abs_diff, 2),
        }
    elif field == "rsi6":
        # RSI6 用绝对偏差3.0（0-100范围，3个点容差）
        return {
            "match": abs_diff <= 3.0,
            "call_val": call_val,
            "facts_val": facts_val,
            "abs_diff": round(abs_diff, 2),
        }
    elif field == "close":
        # 收盘价用1%百分比容差
        if facts_val != 0:
            deviation_pct = abs_diff / abs(facts_val) * 100
        else:
            deviation_pct = 0
        return {
            "match": deviation_pct <= 1.0,
            "call_val": call_val,
            "facts_val": facts_val,
            "deviation_pct": round(deviation_pct, 2),
        }
    else:
        # 通用：百分比容差
        if facts_val != 0:
            deviation_pct = abs_diff / abs(facts_val) * 100
        else:
            deviation_pct = 0
        return {
            "match": deviation_pct <= tolerance_pct * 100,
            "call_val": call_val,
            "facts_val": facts_val,
            "deviation_pct": round(deviation_pct, 2),
        }


def reconcile(dahai_data, wb_data, facts_data):
    """核心对账逻辑。"""
    date_str = (dahai_data or wb_data or {}).get("date", "unknown")
    facts_map = build_facts_map(facts_data)

    dh_calls = extract_calls(dahai_data, "dahai") if dahai_data else {}
    wb_calls = extract_calls(wb_data, "wb") if wb_data else {}

    # 找共同标的（以WB为主，因为WB覆盖更广）
    all_codes = set(list(wb_calls.keys()) + list(dh_calls.keys()))

    rows = []
    agree_count = 0
    total_core = 0

    for code in sorted(all_codes):
        wb = wb_calls.get(code)
        dh = dh_calls.get(code)

        # 只处理核心池标的
        is_core = (wb and wb.get("core_pool")) or (dh and dh.get("core_pool"))
        if not is_core:
            # 非核心池只记录，不参与一致率计算
            if wb and dh:
                rows.append({
                    "code": code,
                    "name": (wb or dh)["name"],
                    "core_pool": False,
                    "status": "non_core",
                    "wb_action": wb["action"] if wb else None,
                    "dahai_action": dh["action"] if dh else None,
                })
            continue

        total_core += 1
        facts_item = facts_map.get(code)

        if not wb or not dh:
            rows.append({
                "code": code,
                "name": (wb or dh)["name"],
                "core_pool": True,
                "status": "missing_side",
                "missing": "wb" if not wb else "dahai",
                "present_action": (wb or dh)["action"],
            })
            continue

        # 语义对齐比较
        same_semantic = norm(wb["action"]) == norm(dh["action"])
        same_raw = wb["action"] == dh["action"]
        same_direction = (wb.get("direction") == dh.get("direction"))

        if same_semantic:
            agree_count += 1

        # 分歧归因
        attribution = "一致"
        data_issues = []

        if not same_semantic:
            # 检查是否是数据分歧
            wb_data_ok = True
            dh_data_ok = True

            if facts_item:
                for side, call in [("wb", wb), ("dahai", dh)]:
                    close_check = check_data_match(call, facts_item, "close")
                    rsi_check = check_data_match(call, facts_item, "rsi6",
                                                  tolerance_pct=0.02)
                    tdc_check = check_data_match(call, facts_item, "ten_day_chg")

                    side_issues = []
                    if close_check.get("match") is False:
                        side_issues.append(
                            f"close偏差{close_check.get('deviation_pct', '?')}%")
                    if rsi_check.get("match") is False:
                        side_issues.append(
                            f"rsi6偏差{rsi_check.get('abs_diff', '?')}")
                    if tdc_check.get("match") is False:
                        side_issues.append(
                            f"ten_day_chg偏差{tdc_check.get('abs_diff', '?')}")

                    if side == "wb" and side_issues:
                        wb_data_ok = False
                        data_issues.append(f"WB: {', '.join(side_issues)}")
                    elif side == "dahai" and side_issues:
                        dh_data_ok = False
                        data_issues.append(f"大海: {', '.join(side_issues)}")

            if not facts_item:
                attribution = "无facts基准，无法归因"
            elif wb_data_ok and dh_data_ok:
                attribution = "真分歧（数据一致，判断不同）"
            else:
                attribution = f"数据分歧（{'; '.join(data_issues)}）"

        # 可操作建议
        if same_semantic:
            if wb["action"] == "buy":
                actionability = "ALIGNED_BUY"  # 双方一致买入
            elif wb["action"] == "sell":
                actionability = "ALIGNED_SELL"
            else:
                actionability = "ALIGNED_HOLD"
        else:
            actionability = "DIVERGENT"

        rows.append({
            "code": code,
            "name": wb["name"],
            "core_pool": True,
            "status": "ok",
            "wb_action": wb["action"],
            "wb_semantic": wb["action_semantic"],
            "wb_direction": wb.get("direction"),
            "wb_confidence": wb.get("confidence"),
            "dahai_action": dh["action"],
            "dahai_semantic": dh["action_semantic"],
            "dahai_direction": dh.get("direction"),
            "dahai_confidence": dh.get("confidence"),
            "same_semantic": same_semantic,
            "same_raw": same_raw,
            "same_direction": same_direction,
            "attribution": attribution,
            "data_issues": data_issues,
            "actionability": actionability,
            "facts_close": facts_item["close"] if facts_item else None,
            "facts_rsi6": facts_item["rsi6"] if facts_item else None,
        })

    # 大盘方向对比
    # [2026-10-20 修复] WB 原生 schema 没有 "market" 字段，方向在
    # 顶层 market_direction / t1_prediction.sh_index.direction；
    # 原版只读 wb["market"]["direction"] → WB 永远 None → 假分歧。
    dh_outlook = (dahai_data or {}).get("market_outlook", {})
    wb_data_ = (wb_data or {})
    wb_market = wb_data_.get("market") or {}
    dh_dir = dh_outlook.get("direction")
    wb_dir = (wb_market.get("direction")
              or wb_data_.get("market_direction")
              or ((wb_data_.get("t1_prediction") or {}).get("sh_index") or {}).get("direction"))

    return {
        "date": date_str,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "schema_version": "reconcile-v2",
        "market_direction": {
            "wb": wb_dir,
            "dahai": dh_dir,
            "match": wb_dir == dh_dir,
        },
        "core_pool_compare": {
            "total": total_core,
            "agree_semantic": agree_count,
            "agree_rate": f"{agree_count}/{total_core}" if total_core else "0/0",
            "agree_pct": round(agree_count / total_core * 100, 1) if total_core else 0,
        },
        "details": rows,
        "divergences": [r for r in rows if r.get("status") == "ok" and not r.get("same_semantic")],
        "actionable": {
            "aligned_buy": [r["name"] for r in rows if r.get("actionability") == "ALIGNED_BUY"],
            "aligned_sell": [r["name"] for r in rows if r.get("actionability") == "ALIGNED_SELL"],
            "divergent": [r["name"] for r in rows if r.get("actionability") == "DIVERGENT"],
        },
        "_note": "核心池标的参与一致率计算，非核心池仅记录",
    }


def main():
    ap = argparse.ArgumentParser(description="大海 ↔ WB 观点交叉验证 v2")
    ap.add_argument("--dahai", required=True, help="大海观点JSON路径")
    ap.add_argument("--wb", required=True, help="WB观点JSON路径")
    ap.add_argument("--facts", default=None, help="facts JSON路径（可选）")
    args = ap.parse_args()

    dahai_data = load_json(args.dahai)
    wb_data = load_json(args.wb)

    if not dahai_data and not wb_data:
        print("❌ 双方观点文件均无法加载")
        return 1

    # 自动匹配facts
    facts_data = None
    if args.facts:
        facts_data = load_json(args.facts)
    else:
        date_str = (dahai_data or wb_data or {}).get("date")
        if not date_str:
            date_str = (dahai_data or wb_data or {}).get("based_on_facts")
        if date_str:
            facts_path = os.path.join(BASE_DIR, "facts", f"{date_str}.json")
            facts_data = load_json(facts_path)

    report = reconcile(dahai_data, wb_data, facts_data)

    # 输出到控制台
    cp = report["core_pool_compare"]
    md = report["market_direction"]
    print(f"\n{'='*60}")
    print(f"对账报告 v2: {report['date']}")
    print(f"{'='*60}")
    print(f"大盘方向: WB={md['wb']} | 大海={md['dahai']} | "
          f"{'✅一致' if md['match'] else '❌分歧'}")
    print(f"核心池一致率: {cp['agree_rate']} ({cp['agree_pct']}%)")
    print(f"{'='*60}")

    for r in report["details"]:
        if r.get("status") != "ok":
            continue
        mk = "✅" if r.get("same_semantic") else "⚠️"
        att = r.get("attribution", "")
        if "真分歧" in att:
            att = "🔴" + att
        elif "数据分歧" in att:
            att = "🟡" + att
        print(f"  {mk} {r['name']:8s} | WB={r.get('wb_action', '?'):12s} | "
              f"大海={r.get('dahai_action', '?'):12s} | {att}")

    if report["divergences"]:
        print(f"\n⚠️ 分歧项 ({len(report['divergences'])}):")
        for d in report["divergences"]:
            print(f"  - {d['name']}: {d['attribution']}")

    if report["actionable"]["aligned_buy"]:
        print(f"\n🟢 一致买入: {', '.join(report['actionable']['aligned_buy'])}")

    # 保存报告
    os.makedirs(RECONCILE_DIR, exist_ok=True)
    date_str = report["date"]
    out_path = os.path.join(RECONCILE_DIR, f"reconcile_v2_{date_str}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 报告已保存: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
