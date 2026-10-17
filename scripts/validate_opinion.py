#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
观点文件校验器（LIVS-Stock 观点层 · 质量门）
================================================================
配合统一事实层 facts/ 使用。大海与WB的观点文件必须通过本校验，
否则对齐脚本拒收、当日不出结论。

校验规则（对应《给WB-20261017》字段字典与质量门）：
  R1 事实门：facts 存在且 overall_quality=complete，否则全天停工
  R2 防重建：观点文件含 _rebuilt=true → 拒收
  R3 日期门：based_on_facts 必须等于 facts 文件日期
  R4 必填字段：model/date/based_on_facts/market/calls
  R5 取值合法：direction∈{bullish,bearish,neutral}
              action∈{buy_probe,buy_standard,hold,watch,sell}
              kelly_grade∈{A,B,C,D}，confidence∈[0,100]
  R6 逻辑自洽（消灭"观望(看涨)"矛盾）：
       buy_* 必须配 bullish
       sell  必须配 bearish
       watch 不得配 bullish（除非显式 counter_trend=true 并写理由）
  R7 证据一致性：evidence 里引用的指标必须能在 facts 查到，且偏差≤0.1
                 （防止有人偷偷自己另算一套数）

用法：
  python3 validate_opinion.py <观点文件.json> [--facts facts/2026-10-16.json]
退出码：0=通过  1=拒收（打印问题清单）
"""
import json
import sys
import os
import argparse

VALID_DIRECTION = {"bullish", "bearish", "neutral"}
VALID_ACTION = {"buy_probe", "buy_standard", "hold", "watch", "sell"}
VALID_GRADE = {"A", "B", "C", "D"}
TOL = 0.1  # 指标引用容差

# 允许在 evidence 中引用、且必须与 facts 一致的标量指标
EVIDENCE_SCALAR = {"close", "change_pct", "ten_day_chg", "rsi6",
                   "ma5", "ma10", "ma20", "vol_ratio"}


def fail(errors, code, msg):
    errors.append(f"[{code}] {msg}")


def validate(opinion_path, facts_path):
    errors = []
    warnings = []

    if not os.path.exists(opinion_path):
        print(f"❌ 观点文件不存在: {opinion_path}")
        return 1
    with open(opinion_path, encoding="utf-8") as f:
        op = json.load(f)

    # R2 防重建
    if op.get("_rebuilt") is True:
        fail(errors, "R2", "观点文件标记 _rebuilt=true（事后重建），拒收")

    # R4 顶层必填
    for key in ("model", "date", "based_on_facts", "market", "calls"):
        if key not in op:
            fail(errors, "R4", f"缺少顶层字段: {key}")
    if errors:
        return _report(errors, warnings)

    # R1 事实门
    if not facts_path:
        facts_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "facts", f"{op['based_on_facts']}.json")
    if not os.path.exists(facts_path):
        fail(errors, "R1", f"事实数据包不存在: {facts_path}，全天停工")
        return _report(errors, warnings)
    with open(facts_path, encoding="utf-8") as f:
        facts = json.load(f)
    if facts.get("overall_quality") != "complete":
        fail(errors, "R1",
             f"facts质量={facts.get('overall_quality')}，问题:{facts.get('quality_errors')}")

    # R3 日期门
    if facts.get("date") != op["based_on_facts"]:
        fail(errors, "R3",
             f"based_on_facts={op['based_on_facts']} 与 facts日期={facts.get('date')} 不符")

    # 大盘观点
    mk = op.get("market", {})
    md = mk.get("direction")
    if md not in VALID_DIRECTION:
        fail(errors, "R5", f"大盘direction非法: {md}")

    finst = facts.get("instruments", {})

    # 个股观点逐条
    for i, c in enumerate(op.get("calls", [])):
        tag = f"第{i+1}条({c.get('name','?')}/{c.get('code','?')})"
        code = c.get("code")
        direction = c.get("direction")
        action = c.get("action")

        if direction not in VALID_DIRECTION:
            fail(errors, "R5", f"{tag} direction非法: {direction}")
        if action not in VALID_ACTION:
            fail(errors, "R5", f"{tag} action非法: {action}")
        if not isinstance(c.get("confidence"), (int, float)) or not (0 <= c["confidence"] <= 100):
            fail(errors, "R5", f"{tag} confidence越界: {c.get('confidence')}")
        grade = c.get("kelly_grade")
        if grade not in VALID_GRADE:
            fail(errors, "R5", f"{tag} kelly_grade非法: {grade}")
        # 凯利仓位与分级匹配（粗校验）
        cap = {"A": 15, "B": 8, "C": 5, "D": 0}
        kp = c.get("kelly_pct")
        if grade in cap and isinstance(kp, (int, float)):
            if kp > cap[grade] + 0.01:
                fail(errors, "R5",
                     f"{tag} {grade}级仓位{kp}%超过上限{cap[grade]}%")
            if grade == "D" and kp > 0:
                fail(errors, "R5", f"{tag} D级不得给仓位")

        # R6 逻辑自洽
        if action in ("buy_probe", "buy_standard") and direction != "bullish":
            fail(errors, "R6", f"{tag} action={action} 必须 direction=bullish")
        if action == "sell" and direction != "bearish":
            fail(errors, "R6", f"{tag} action=sell 必须 direction=bearish")
        if action == "watch" and direction == "bullish" and not c.get("counter_trend"):
            fail(errors, "R6",
                 f"{tag} 出现矛盾组合 watch+看涨；如确为逆势观望须加 counter_trend=true 并说明")

        # R7 证据一致性
        ev = c.get("evidence")
        if ev and code in finst:
            fdata = finst[code]
            for k, v in ev.items():
                if k in EVIDENCE_SCALAR:
                    fv = fdata.get(k)
                    if fv is None:
                        fail(errors, "R7", f"{tag} 引用 {k}，但facts无此字段")
                    elif abs(float(v) - float(fv)) > TOL:
                        fail(errors, "R7",
                             f"{tag} 证据 {k}={v} 与facts={fv} 偏差>{TOL}（禁止另算）")
                elif k == "macd_signal":
                    if v != fdata.get("macd", {}).get("state"):
                        fail(errors, "R7",
                             f"{tag} 证据 macd_signal={v} 与facts={fdata.get('macd',{}).get('state')} 不符")
        # 理由必填
        if not c.get("reason"):
            warnings.append(f"{tag} 未填reason，建议补充判断依据")

    return _report(errors, warnings, op)


def _report(errors, warnings, op=None):
    model = op.get("model", "?") if op else "?"
    date = op.get("date", "?") if op else "?"
    if warnings:
        print("⚠️ 警告（不阻断）:")
        for w in warnings:
            print(f"   {w}")
    if errors:
        print(f"❌ [{model} {date}] 观点校验未通过，共{len(errors)}项:")
        for e in errors:
            print(f"   {e}")
        return 1
    print(f"✅ [{model} {date}] 观点校验通过，允许进入交叉验证")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("opinion")
    ap.add_argument("--facts", default=None)
    args = ap.parse_args()
    sys.exit(validate(args.opinion, args.facts))
