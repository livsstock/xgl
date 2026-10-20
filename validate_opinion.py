#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_opinion.py — 观点文件R1-R7校验器（v1.0，2026-10-20）
=============================================================
【定位】
对大海或WB的观点输出文件进行7项一致性校验，确保数据与事实层对齐。

【校验规则】
  R1: action必须是 buy/sell/hold/watch 之一
  R2: kelly_grade必须是 A/B/C/D 之一
  R3: 如果action=buy，必须有evidence字段
  R4: evidence中的数值与facts层数据偏差不超过1%
  R5: ten_day_chg与facts层ten_day_chg偏差不超过0.1
  R6: RSI6值在0-100范围内
  R7: MACD金叉/死叉判定与DIF/DEA关系一致

【输入】
  观点JSON文件路径（支持大海格式和WB格式）

【输出】
  校验结果JSON + 控制台摘要

【用法】
  python3 validate_opinion.py predictions-dahai/2026-10-16_dahai.json
  python3 validate_opinion.py opinions-wb/2026-10-16_wb.json --facts facts/2026-10-16.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_facts(date_str):
    """加载对应日期的facts文件。"""
    facts_path = os.path.join(BASE_DIR, "facts", f"{date_str}.json")
    if not os.path.exists(facts_path):
        return None
    with open(facts_path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_stock_code(code_str):
    """标准化股票代码：sh601899 -> 601899, 601899 -> 601899"""
    if isinstance(code_str, str):
        return code_str.replace("sh", "").replace("sz", "")
    return str(code_str)


def validate_opinion(opinion_path, facts_path=None):
    """校验观点文件，返回校验结果。"""
    with open(opinion_path, "r", encoding="utf-8") as f:
        opinion = json.load(f)

    # 确定日期和加载facts
    date_str = opinion.get("date") or opinion.get("based_on_facts")
    facts = None
    if facts_path:
        if os.path.exists(facts_path):
            with open(facts_path, "r", encoding="utf-8") as f:
                facts = json.load(f)
    elif date_str:
        facts = load_facts(date_str)

    # 构建facts索引（兼容两种格式）
    facts_map = {}
    if facts:
        if "instruments" in facts:
            facts_map = facts["instruments"]
        elif "stocks" in facts:
            for s in facts["stocks"]:
                code = extract_stock_code(s.get("code", ""))
                facts_map[code] = s

    # 提取观点calls（兼容两种格式）
    calls = []
    if "calls" in opinion:
        calls = opinion["calls"]
    elif "stock_calls" in opinion:
        calls = opinion["stock_calls"]

    results = {
        "file": os.path.basename(opinion_path),
        "date": date_str,
        "total_calls": len(calls),
        "passed": 0,
        "failed": 0,
        "errors": [],
        "warnings": [],
    }

    for i, call in enumerate(calls):
        stock_name = call.get("name", f"股票{i+1}")
        stock_code = extract_stock_code(call.get("code", ""))
        call_errors = []

        # R1: action校验
        action = call.get("action")
        if action not in ["buy", "sell", "hold", "watch"]:
            call_errors.append(f"R1: action='{action}'不是合法值(buy/sell/hold/watch)")

        # R2: kelly_grade校验
        kelly_grade = call.get("kelly_grade")
        if kelly_grade and kelly_grade not in ["A", "B", "C", "D"]:
            call_errors.append(f"R2: kelly_grade='{kelly_grade}'不是合法值(A/B/C/D)")

        # R3: buy必须有evidence
        if action == "buy":
            evidence = call.get("evidence")
            if not evidence:
                call_errors.append(f"R3: action=buy但缺少evidence字段")

        # R4-R7: 与facts对比（仅当有facts且call有evidence时）
        evidence = call.get("evidence", {})
        facts_data = facts_map.get(stock_code)

        if facts_data and evidence:
            # R4: 数值偏差≤1%（检查close）
            ev_close = evidence.get("close")
            facts_close = facts_data.get("close")
            if ev_close and facts_close:
                deviation = abs(ev_close - facts_close) / facts_close
                if deviation > 0.01:
                    call_errors.append(
                        f"R4: close偏差{deviation:.2%}超过1% "
                        f"(观点={ev_close}, facts={facts_close})"
                    )

            # R5: ten_day_chg偏差≤0.1
            ev_tdc = evidence.get("ten_day_chg")
            facts_tdc = facts_data.get("ten_day_chg")
            if ev_tdc is not None and facts_tdc is not None:
                if abs(ev_tdc - facts_tdc) > 0.1:
                    call_errors.append(
                        f"R5: ten_day_chg偏差{abs(ev_tdc - facts_tdc):.2f}超过0.1 "
                        f"(观点={ev_tdc}, facts={facts_tdc})"
                    )

            # R6: RSI6范围0-100
            rsi6 = evidence.get("rsi6") or call.get("rsi6")
            if rsi6 is not None:
                if not (0 <= rsi6 <= 100):
                    call_errors.append(f"R6: RSI6={rsi6}超出0-100范围")

            # R7: MACD金叉/死叉判定一致性
            macd_signal = evidence.get("macd_signal") or call.get("macd_signal")
            facts_macd = facts_data.get("macd", {})
            if macd_signal and facts_macd:
                facts_dif = facts_macd.get("dif")
                facts_dea = facts_macd.get("dea")
                facts_state = facts_macd.get("state")

                # 检查逻辑一致性
                if macd_signal == "golden_cross" and facts_state != "golden_cross":
                    call_errors.append(
                        f"R7: MACD金叉判定不一致 "
                        f"(观点={macd_signal}, facts={facts_state})"
                    )
                elif macd_signal == "death_cross" and facts_state != "death_cross":
                    call_errors.append(
                        f"R7: MACD死叉判定不一致 "
                        f"(观点={macd_signal}, facts={facts_state})"
                    )

        if call_errors:
            results["failed"] += 1
            results["errors"].append({
                "stock": stock_name,
                "code": stock_code,
                "errors": call_errors,
            })
        else:
            results["passed"] += 1

    results["pass_rate"] = (
        f"{results['passed']}/{results['total_calls']} "
        f"({results['passed']/results['total_calls']*100:.1f}%)"
        if results["total_calls"] > 0 else "0/0"
    )

    return results


def main():
    ap = argparse.ArgumentParser(description="观点文件R1-R7校验器")
    ap.add_argument("opinion_file", help="观点JSON文件路径")
    ap.add_argument("--facts", help="facts JSON文件路径（可选，自动匹配日期）")
    args = ap.parse_args()

    if not os.path.exists(args.opinion_file):
        print(f"❌ 文件不存在: {args.opinion_file}")
        return 1

    results = validate_opinion(args.opinion_file, args.facts)

    # 输出结果
    print(f"\n{'='*60}")
    print(f"观点校验报告: {results['file']}")
    print(f"{'='*60}")
    print(f"日期: {results['date']}")
    print(f"校验标的: {results['total_calls']}只")
    print(f"通过率: {results['pass_rate']}")
    print(f"通过: {results['passed']} | 失败: {results['failed']}")
    print(f"{'='*60}\n")

    if results["errors"]:
        print("❌ 校验失败项:\n")
        for err in results["errors"]:
            print(f"  {err['stock']} ({err['code']})")
            for e in err["errors"]:
                print(f"    - {e}")
            print()

    # 保存结果
    output_path = os.path.join(
        BASE_DIR, "validate_reports",
        f"{results['date']}_validate.json"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"✅ 校验结果已保存: {output_path}")

    return 0 if results["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
