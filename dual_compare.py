#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dual_compare.py — 双模型盘中对比结论生成器（2026-10-29 v1.0）

老板 2026-10-29 定的时点：上午 10:30 卖出预测 / 下午 14:10 买入预测，
与大海双模型对比出结论。

逻辑：
  --point sell  读 predictions/sell-{date}.json（morning_sell_scan 10:30 产出）
  --point buy   读 predictions/afternoon-{date}.json（afternoon_scan 14:10 产出）
  大海侧：远端拉 opinions-dahai/{date}_dahai.json 或 predictions-dahai/{date}_dahai.json
  输出  docs/对比-{date}-{sell|buy}.md 并推送 GitHub

铁律：大海侧当日缺失 → 只出 WB 单方意见，标注"双模型结论不成立"。
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, "docs")
RAW = "https://ghfast.top/https://raw.githubusercontent.com/livsstock/xgl/main"
UA = {"User-Agent": "Mozilla/5.0"}

DAHAI_SEM = {"买入": "buy", "关注": "watch", "观望": "watch", "回避": "avoid", "持有": "hold"}


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_dahai(date):
    """依次试探大海意见文件路径，返回 (dict|None, 路径)"""
    for path in (f"opinions-dahai/{date}_dahai.json",
                 f"predictions-dahai/{date}_dahai.json"):
        try:
            return json.loads(http_get(f"{RAW}/{path}")), path
        except Exception:
            continue
    # 本地兜底
    for path in (os.path.join(BASE, "predictions-dahai", f"{date}_dahai.json"),):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f), path
    return None, None


def dahai_actions(d):
    """归一大海 calls/predictions → {code: (action_en, action_raw)}"""
    out = {}
    for c in (d.get("calls") or d.get("predictions") or d.get("stock_calls") or []):
        code = str(c.get("code", "")).replace("sh", "").replace("sz", "")
        raw = c.get("action") or ""
        out[code] = (DAHAI_SEM.get(raw, raw), raw)
    return out


def push(files):
    env = os.environ.get("WB_GITHUB_TOKEN")
    if not env:
        print("[COMPARE] 无 WB_GITHUB_TOKEN，仅生成不推送")
        return
    subprocess_files = " ".join(f'"{f}"' for f in files)
    rc = os.system(f'cd "{BASE}" && WB_GITHUB_TOKEN={env} python3 push_files.py {subprocess_files}')
    print(f"[COMPARE] push rc={rc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--point", choices=["sell", "buy"], required=True)
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    date, point = args.date, args.point

    fname = f"sell-{date}.json" if point == "sell" else f"afternoon-{date}.json"
    fp = os.path.join(BASE, "predictions", fname)
    if not os.path.exists(fp):
        print(f"[COMPARE] 缺 {fname}（先跑扫描脚本），退出")
        return 1
    with open(fp, encoding="utf-8") as f:
        wb = json.load(f)

    lines = [f"# 双模型{'卖出' if point == 'sell' else '买入'}对比 {date}"
             f"（{'10:30' if point == 'sell' else '14:10'}）", ""]

    # WB 侧结论
    if point == "sell":
        decs = wb.get("decisions") or []
        if not decs:
            lines += ["## WB 结论", "", "- **无持仓，无卖出决策**"]
            wb_map = {}
        else:
            lines += ["## WB 结论", "", "| 标的 | 建议 | 依据 |", "|---|---|---|"]
            wb_map = {}
            for d in decs:
                lines.append(f"| {d.get('name')} | {d.get('action')} | {(d.get('reason') or '')[:60]} |")
                wb_map[str(d.get("code"))] = d.get("action")
    else:
        res = wb.get("results") or []
        buys = [r for r in res if r.get("verdict") == "buy_today_1430"]
        near = [r for r in res if r.get("verdict") == "watch_confirms"]
        gate = wb.get("market_gate") or {}
        lines += ["## WB 结论", "",
                  f"- 大盘门：{'✅ 通过' if gate.get('ok') else '⛔ 不通过'}（{gate.get('reason', '')}）",
                  f"- 今日尾盘可买（{len(buys)}）：{'、'.join(r.get('name') for r in buys) or '无'}",
                  f"- 尾盘确认名单（{len(near)}）：{'、'.join(r.get('name') for r in near) or '无'}"]
        wb_map = {str(r.get("code")): r.get("verdict") for r in res}

    # 大海侧
    dahai, dpath = fetch_dahai(date)
    lines += ["", "## 大海侧", ""]
    if not dahai:
        lines += ["- ❌ 当日无大海文件 → **双模型对比结论不成立，按铁律不出手，仅记录 WB 单方意见**"]
    else:
        lines += [f"- 来源：`{dpath}`（模型 {dahai.get('model_version', '?')}）"]
        dmap = dahai_actions(dahai)
        if point == "buy":
            lines += ["", "| 标的 | WB(尾盘买) | 大海 | 双模型结论 |", "|---|---|---|---|"]
            for r in buys + near:
                code = str(r.get("code"))
                da, draw = dmap.get(code, ("缺", "缺"))
                wbv = "buy" if r.get("verdict") == "buy_today_1430" else "watch"
                if da == "buy":
                    concl = "✅ 一致→可执行" if wbv == "buy" else "⚠️ WB仅watch"
                elif da in ("watch", "hold"):
                    concl = "⚠️ 分歧（大海观望）" if wbv == "buy" else "✅ 一致观望"
                else:
                    concl = f"⛔ 分歧（大海{draw}）"
                lines.append(f"| {r.get('name')} | {wbv} | {draw} | {concl} |")
            agree = sum(1 for r in buys if dmap.get(str(r.get("code")), ("",))[0] == "buy")
            lines += ["", f"### 最终结论", "",
                      f"- WB 可买 {len(buys)} 只，其中与大海一致 {agree} 只",
                      f"- **执行口径：双模型一致才出手；大海缺失/观望的标的按铁律不下单**"]
        else:
            lines += ["", "| 标的 | WB(卖出) | 大海 | 结论 |", "|---|---|---|---|"]
            for code, act in wb_map.items():
                da, draw = dmap.get(code, ("缺", "缺"))
                concl = "✅ 一致" if da in ("watch", "hold") else "⚠️ 分歧"
                lines.append(f"| {code} | {act} | {draw} | {concl} |")
            lines += ["", "### 最终结论", "", "- 卖出为 WB 持仓纪律单方面执行（大海无持仓视角），大海意见仅作参考"]

    out = os.path.join(DOCS, f"对比-{date}-{point}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[COMPARE] 已生成 {out}")
    if not args.no_push:
        push([out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
