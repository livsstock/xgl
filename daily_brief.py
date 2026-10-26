#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_brief.py — 每日双模型简报生成器（2026-10-26 v1.0）

老板要求：双模型预测结果每天汇报一次，并包括对前面验证结果的总结。

输出: docs/简报-{date}.md
内容:
  1) 最新预测（最新收盘日 T，预测对象 T+1）：WB 买入/观察清单 + 大海对比
  2) 往返校验（口径对齐）结果
  3) 昨日预测的验证：已有事实层数据时自动核对涨跌，否则标注"待收盘验证"
  4) 历史验证总结：信号回测胜率 + 逐日对账结论

用法:
  python3 daily_brief.py                # 自动取最新有预测的日期
  python3 daily_brief.py --date 2026-10-23
"""
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
PRED = os.path.join(BASE, "predictions")
DAHAI = os.path.join(BASE, "predictions-dahai")
FACTS = os.path.join(BASE, "facts")
RT = os.path.join(BASE, "reconcile", "roundtrip")
DOCS = os.path.join(BASE, "docs")

# 信号回测效力（verify_t5_multi / t5_robust 已验证，2026-10 汇总）
BACKTEST = [
    ("S2: RSI6<15 抄底", "T+5 胜率 58.9%"),
    ("S1: RSI6<25 触发", "T+5 胜率 55.2%"),
    ("S5: 10日跌超10% 超跌反弹", "T+5 胜率 56.0%"),
]

IRON_RULE = "双模型一致才出手：一方缺失 / 口径校验不过 → 当日仓位归零"

SEM = {"buy": "出手", "buy_probe": "试探", "watch": "观望", "hold": "不动", "avoid": "回避"}
DAHAI_SEM = {"买入": "buy", "关注": "watch", "观望": "watch", "回避": "avoid", "持有": "hold"}


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def latest_date():
    dates = sorted(f[:-5] for f in os.listdir(PRED)
                   if len(f) == 15 and f.endswith(".json") and f[4] == "-" and f[7] == "-")
    return dates[-1] if dates else None


def fmt_wb_call(c):
    pos = c.get("suggest_position_pct")
    pos_s = f" | 仓位{pos}%" if pos else ""
    return f"- **{c.get('name')}** ({c.get('code')}): {c.get('action')} rsi6={c.get('rsi6')}{pos_s} — {(c.get('reason') or '')[:60]}"


def main():
    date = None
    if "--date" in sys.argv:
        date = sys.argv[sys.argv.index("--date") + 1]
    date = date or latest_date()
    if not date:
        print("[BRIEF] 无任何预测文件")
        return 1

    wb = load(os.path.join(PRED, f"{date}.json"))
    if not wb:
        print(f"[BRIEF] {date} 无 WB 预测")
        return 1
    dahai = load(os.path.join(DAHAI, f"{date}_dahai.json")) or load(os.path.join(PRED, f"{date}_dahai.json"))
    rt = load(os.path.join(RT, f"{date}.json"))

    calls = wb.get("stock_calls") or []
    buys = [c for c in calls if (c.get("action") or "").startswith("buy")]
    sm = wb.get("summary") or {}

    lines = [
        f"# 📊 双模型每日简报 {date}",
        f"",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 预测对象：**T+1（下一交易日）** ｜ 时点：收盘后统一版",
        "",
        "## 一、WB 预测（本轮主产出）",
        "",
        f"- 大盘：{wb.get('market_outlook', {}).get('direction')}（信心 {wb.get('market_outlook', {}).get('confidence')}）｜ 上证 {wb.get('index_summary', {}).get('close')}（{wb.get('index_summary', {}).get('change_percent')}%）",
        f"- 覆盖 {len(calls)} 只：买入 {sm.get('buy', 0)} / 观察 {sm.get('watch', 0)} / 持有 {sm.get('hold', 0)} / 回避 {sm.get('avoid', 0)}",
        "",
    ]
    if buys:
        lines += ["**买入信号：**", ""] + [fmt_wb_call(c) for c in buys] + [""]
    else:
        lines += ["**买入信号：无**（全池观望/持有）", ""]

    # 大海对比
    lines += ["## 二、大海对比", ""]
    if not dahai:
        lines += [f"- ❌ 大海今日未交付 `{date}_dahai.json` → 按**铁律一方缺失=不出手**", ""]
    else:
        dcalls = dahai.get("predictions") or dahai.get("stock_calls") or []
        has_rsi = any("rsi6" in c for c in dcalls)
        lines += [
            f"- 模型：{dahai.get('model_version')} ｜ 覆盖 {len(dcalls)} 只 ｜ rsi6 字段：{'✅ 有' if has_rsi else '❌ 无（往返校验无法进行）'}",
            f"- 词表：{'✅ 已对齐' if dcalls and all((c.get('action') or '') in ('buy','watch','hold','avoid') for c in dcalls) else '❌ 仍为中文词表（关注/观望/回避）'}",
        ]
        if buys and dcalls:
            dmap = {c.get("code"): c for c in dcalls}
            lines += ["", "| 标的 | WB | 大海 | 是否一致 |", "|---|---|---|---|"]
            for c in buys:
                d = dmap.get(c.get("code")) or {}
                da = DAHAI_SEM.get(d.get("action"), d.get("action", "缺"))
                ok = "✅" if da == "buy" else "⚠️ 分歧"
                lines.append(f"| {c.get('name')} | buy | {d.get('action', '缺')} | {ok} |")
        lines.append("")

    # 往返校验
    lines += ["## 三、口径对齐（往返校验）", ""]
    if not rt or not (rt.get("summary") or {}).get("total"):
        lines += ["- ❌ 无法校验（大海文件无 rsi6 或未交付）→ **当日分歧样本作废**", ""]
    else:
        s = rt["summary"]
        ok = s.get("reproducible", 0) >= s.get("total", 0)
        lines += [f"- 结果：{s.get('reproducible')}/{s.get('total')} 可复现 {'✅ 通过' if ok else '⛔ 未通过→当日仓位归零'}",
                  f"- 口径分布：{s.get('calibers_detected')}", ""]

    # 验证：上一预测日（其 T+1 = 本 date）的 buy 信号 vs 本日实际
    lines += ["## 四、上一轮预测验证", ""]
    all_dates = sorted(f[:-5] for f in os.listdir(PRED)
                       if len(f) == 15 and f.endswith(".json") and f[4] == "-" and f[7] == "-")
    prev_dates = [d for d in all_dates if d < date]
    prev_wb = load(os.path.join(PRED, f"{prev_dates[-1]}.json")) if prev_dates else None
    fact_today = load(os.path.join(FACTS, f"{date}.json"))
    ins = (fact_today or {}).get("instruments") or {}
    px = {}
    if isinstance(ins, dict):
        for v in ins.values():
            if isinstance(v, dict) and "change_pct" in v:
                px[v.get("code")] = v["change_pct"]
                px[v.get("name")] = v["change_pct"]
    prev_buys = [c for c in ((prev_wb or {}).get("stock_calls") or [])
                 if (c.get("action") or "").startswith("buy")] if prev_wb else None
    if px and prev_buys is not None:
        lines += [f"- 验证对象：{prev_dates[-1]} 收盘预测 → {date} 实际", ""]
        if prev_buys:
            lines += ["| 标的 | WB预测(T+1) | 实际 | 结果 |", "|---|---|---|---|"]
            for c in prev_buys:
                ch = px.get(c.get("code")) or px.get(c.get("name"))
                if ch is None:
                    lines.append(f"| {c.get('name')} | buy | 无数据 | — |")
                else:
                    lines.append(f"| {c.get('name')} | buy | {ch:+.2f}% | {'✅ 涨' if ch > 0 else '❌ 跌'} |")
        else:
            lines += ["- 上轮无买入信号（观望为主），无需核对买点"]
    else:
        lines += [f"- ⏳ {date} 收盘事实层尚未生成，收盘后 15:35 自动验证本简报中的 buy 信号"]
    lines.append("")

    # 历史总结
    lines += ["## 五、历史验证总结（截至最新）", "", "**信号回测效力（多日多窗口验证）：**", ""]
    lines += [f"- {n}：{r}" for n, r in BACKTEST]
    lines += [
        "",
        "**逐日对账关键结论：**",
        "- 10-19：双方 RSI 口径完全一致（Wilder，6/6 复现）",
        "- 10-20：大海切到 SMA6 口径（5/6 复现，半导体ETF 差 1.13 待确认）",
        "- 10-21~10-23：大海换 v1.2-kuramoto，无 rsi6/macd、覆盖 15 只 → 往返校验 0/0，样本连续作废",
        "- 10-23 实战：WB buy 4 只当日全部上涨；大海全观望踏空，其回避的北方稀土当日 +2.12%",
        "- **大海三项死线（rsi6 字段 / 覆盖26只 / 词表对齐）截至今日均未达标 → 对齐样本持续归零**",
        "",
        f"> ⚖️ 铁律：{IRON_RULE}",
    ]

    out = os.path.join(DOCS, f"简报-{date}.md")
    os.makedirs(DOCS, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[BRIEF] 已生成 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
