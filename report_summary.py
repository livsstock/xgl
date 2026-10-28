#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_summary.py — WB 简短预测总结报告（2026-10-29 v1.0）

老板要求：按 大盘 / 5个股指 / 6个精选股票 / 26支股票池 格式单独出简短预测总结。
输入: predictions/{date}.json + facts/{date}.json + 腾讯实时（补齐5股指）
输出: docs/WB预测总结-{date}.md，推送 GitHub
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, "docs")
UA = {"User-Agent": "Mozilla/5.0"}

INDEXES = [("sh000001", "上证指数"), ("sh000300", "沪深300"), ("sh000905", "中证500"),
           ("sz399006", "创业板指"), ("sh000016", "上证50")]
CORE6 = ["601899", "600111", "603799", "601958", "512480", "000858"]


def qt_last_close():
    req = urllib.request.Request(
        "https://qt.gtimg.cn/q=" + ",".join(c for c, _ in INDEXES), headers=UA)
    raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "replace")
    out = {}
    for m in re.finditer(r'v_(\w+)="([^"]*)"', raw):
        p = m.group(2).split("~")
        out[m.group(1)] = (p[1], float(p[3]), float(p[32] or 0))
    return out


def main():
    date = sys.argv[sys.argv.index("--date") + 1] if "--date" in sys.argv else None
    import glob
    if not date:
        fs = sorted(f for f in glob.glob(os.path.join(BASE, "predictions", "2???-??-??.json"))
                    if len(os.path.basename(f)) == 15 + 11)
        date = os.path.basename(fs[-1])[:-5]
    pred = json.load(open(os.path.join(BASE, "predictions", f"{date}.json"), encoding="utf-8"))
    facts = json.load(open(os.path.join(BASE, "facts", f"{date}.json"), encoding="utf-8"))
    ins = facts["instruments"]
    target = pred.get("t1_prediction", {}).get("target_date") or "下一交易日"

    L = [f"# 📋 WB 预测总结（{date} 收盘 → 预测 {target}）", "",
         f"生成：{datetime.now().strftime('%m-%d %H:%M')}｜模型 WB-v2.5｜口径：RSI6<25 buy（T+5胜率55.2%）", ""]

    # 一、大盘
    mo = pred.get("market_env") or {}
    md = pred.get("market_direction", "?")
    mc = pred.get("market_confidence", "?")
    sse = ins.get("000001", {})
    L += ["## 一、大盘", "",
          f"- **{md.upper()}**（信心 {mc}）｜ 上证 {sse.get('close')}（{sse.get('change_pct'):+.2f}%）｜ 10日 {sse.get('ten_day_chg'):+.2f}% ｜ RSI6 {sse.get('rsi6')}",
          f"- 大盘门：{'✅ 开（可出手）' if sse.get('change_pct', 0) > -2 else '⛔ 关（全池观望）'}", ""]

    # 二、5 大股指
    qt = qt_last_close()
    L += ["## 二、五大股指", "", "| 指数 | 收盘 | 当日 | 短评 |", "|---|---|---|---|"]
    for code, name in INDEXES:
        key = code.replace("sh", "").replace("sz", "")
        f = ins.get(key)
        if f:
            row = (f.get("close"), f.get("change_pct"))
        elif code in qt:
            row = qt[code][1:]
        else:
            row = (None, None)
        trend = "反弹" if (row[1] or 0) > 1 else ("走强" if (row[1] or 0) > 0 else "偏弱")
        L.append(f"| {name} | {row[0]} | {row[1]:+.2f}% | {trend} |")
    L.append("")

    # 三、6 精选（核心池）
    L += ["## 三、精选 6 只（核心池）", "", "| 标的 | 收盘 | 当日 | RSI6 | MACD | 建议 |", "|---|---|---|---|---|---|"]
    callmap = {c.get("code"): c for c in pred.get("stock_calls", [])}
    for code in CORE6:
        f = ins.get(code) or {}
        c = callmap.get(code) or {}
        act = c.get("action", "hold")
        mark = {"buy": "🟢 buy", "buy_probe": "🟡 probe", "watch": "⚪ watch", "hold": "– hold", "avoid": "🔴 回避"}.get(act, act)
        L.append(f"| {f.get('name')} | {f.get('close')} | {f.get('change_pct', 0):+.2f}% | {f.get('rsi6')} | {f.get('macd', {}).get('state')} | {mark} |")
    L.append("")

    # 四、26 股票池
    buys, watches, holds = [], [], []
    for c in pred.get("stock_calls", []):
        act = c.get("action", "hold")
        tag = f"{c.get('name')}({c.get('code')})"
        (buys if act.startswith("buy") else watches if act == "watch" else holds).append(tag)
    L += ["## 四、26 支股票池汇总", "",
          f"- **买入 {len(buys)}**：{'、'.join(buys) or '无'}",
          f"- **观察 {len(watches)}**：{'、'.join(watches) or '无'}",
          f"- **持有/不动 {len(holds)}**：其余 {len(holds)} 只",
          "", f"> ⚖️ 执行纪律：10:30 卖 / 14:10 买双时点扫描；双模型一致才出手；大海当日缺失则 WB 单方信号仓位 ×0.5"]

    out = os.path.join(DOCS, f"WB预测总结-{date}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"[REPORT] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
