#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reconcile_wb_dahai.py — WB ↔ 大海 逐日对账（v1.0，2026-10-16）

【解决什么】
此前 WB/大海 对账总显示"分歧"，实际很多是**动作词表口径不同**造成的假分歧：
  大海词表：watch(观望) / hold(持有) / buy(试探) / buy_standard(标准仓)
  WB 词表：watch(关注) / hold(不动) / buy(买入)
→ 语义对齐表见 ACTION_ALIGN。

【输出】
  reconcile/daily_compare.json   机器可读
  reconcile/daily_compare.md     人话版

【用法】
  python3 reconcile_wb_dahai.py                 # 用本地已有文件
  python3 reconcile_wb_dahai.py --pull          # 先从 GitHub 拉双方文件
"""
import argparse
import base64
import json
import os
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRED_DIR = os.path.join(BASE_DIR, "predictions")
OUT_DIR = os.path.join(BASE_DIR, "reconcile")
TOK = os.environ["WB_GITHUB_TOKEN"]
REPO = "livsstock/xgl"
API = f"https://api.github.com/repos/{REPO}/contents"

# 动作语义对齐：两边统一映射到「无操作 / 观察 / 买入」
ACTION_ALIGN = {
    "hold": "无操作", "watch": "观察", "avoid": "回避",
    "buy": "买入", "buy_probe": "买入", "buy_standard": "买入",
    "sell": "卖出",
}

# 名称别名（对齐事实包/大海文件/WB文件三方命名）
NAME_ALIAS = {
    "半导体ETF国联安": "半导体ETF",
    "沪深300ETF": "沪深300",
    "中证500ETF": "中证500",
    "创业板ETF": "创业板指",
}


def canon(name):
    return NAME_ALIAS.get(name, name)


def norm(action):
    return ACTION_ALIGN.get(action, action or "?")


def fetch_remote(path):
    url = f"{API}/{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"token {TOK}"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return json.loads(base64.b64decode(r["content"]).decode())
    except Exception:
        return None


def list_remote_dir(path):
    url = f"{API}/{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"token {TOK}"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return [f["name"] for f in r] if isinstance(r, list) else []
    except Exception:
        return []


def load_wb(d):
    p = os.path.join(PRED_DIR, f"{d}.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return fetch_remote(f"predictions/{d}.json")


def load_dahai(d):
    p = os.path.join(PRED_DIR, f"{d}_dahai.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    j = fetch_remote(f"predictions-dahai/{d}_dahai.json")
    if j:
        json.dump(j, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return j


def load_facts(d):
    """读统一事实包；本地无则拉远端 facts/。"""
    p = os.path.join(BASE_DIR, "facts", f"{d}.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    j = fetch_remote(f"facts/{d}.json")
    if j:
        json.dump(j, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return j


def compare_one(d):
    wb = load_wb(d)
    dh = load_dahai(d)
    if not wb or not dh:
        return {"date": d, "status": "missing",
                "missing": [k for k, v in [("wb", wb), ("dahai", dh)] if not v]}

    # ---- 质量门：补作业文件不进对账统计 ----
    if wb.get("_rebuilt"):
        return {"date": d, "status": "rejected",
                "reason": "WB文件为事后重建(_rebuilt=true)，不作对账统计"}

    wi = wb.get("index_summary", {})
    di = dh.get("index_summary", {})
    wdir = (wb.get("market_outlook") or {}).get("direction")
    ddir = (dh.get("market_outlook") or {}).get("direction")

    dhm = {canon(c["name"]): c for c in dh.get("stock_calls", [])}
    facts = load_facts(d)
    fm = {canon(s["name"]): s for s in (facts or {}).get("stocks", [])}
    rows, agree_sem, total = [], 0, 0
    for c in wb.get("stock_calls", []):
        if not c.get("core_pool"):
            continue
        n = c["name"]
        t = dhm.get(canon(n))
        f = fm.get(canon(n))
        wb_act = c.get("action")
        dh_act = t.get("action") if t else None
        same_sem = (t is not None and norm(wb_act) == norm(dh_act))
        same_raw = (t is not None and wb_act == dh_act)
        total += 1
        if same_sem:
            agree_sem += 1

        # ---- 分歧归因：先比输入，再比观点 ----
        # RSI6 容差 1.0；收盘价容差 0.01。事实包存在才做归因。
        data_row = {}
        if f:
            for side, src in (("wb", c), ("dahai", t)):
                r = src.get("rsi6") if src else None
                cl = src.get("close") if src else None
                data_row[side] = {
                    "rsi6": r, "close": cl,
                    "rsi_match": (r is None or f["rsi6"] is None or
                                  abs(r - f["rsi6"]) <= 1.0),
                    "close_match": (cl is None or f["close"] is None or
                                    abs(cl - f["close"]) <= 0.01),
                }
            data_row["facts_rsi6"] = f["rsi6"]
            data_row["facts_close"] = f["close"]
        if same_sem:
            attribution = "一致"
        elif not data_row:
            attribution = "无事实包，无法归因"
        else:
            wb_ok = data_row["wb"]["rsi_match"] and data_row["wb"]["close_match"]
            dh_ok = data_row["dahai"]["rsi_match"] and data_row["dahai"]["close_match"]
            if wb_ok and dh_ok:
                attribution = "真分歧（输入一致，模型判断不同）"
            else:
                attribution = ("数据分歧（" +
                               ("WB输入偏离事实 " if not wb_ok else "") +
                               ("大海输入偏离事实" if not dh_ok else "") + "）")
        rows.append({
            "name": n,
            "wb_action": wb_act, "wb_semantic": norm(wb_act),
            "dahai_action": dh_act, "dahai_semantic": norm(dh_act),
            "same_semantic": same_sem, "same_raw": same_raw,
            "attribution": attribution,
            "data_check": data_row,
            "wb_close": c.get("close"), "dahai_close": t.get("close") if t else None,
            "wb_reason": c.get("reason"),
            "dahai_reason": (t.get("reason") or "")[:80] if t else None,
            "kelly_pct": (c.get("kelly") or {}).get("f_final_pct"),
        })

    return {
        "date": d, "status": "ok",
        "index": {"wb_close": wi.get("close"), "dahai_close": di.get("close"),
                  "wb_chg": wi.get("change_percent"),
                  "dahai_chg": di.get("change_percent"),
                  "close_match": (wi.get("close") == di.get("close"))},
        "direction": {"wb": wdir, "dahai": ddir, "match": wdir == ddir},
        "stocks": rows,
        "agree_semantic": agree_sem, "agree_raw": len([r for r in rows if r["same_raw"]]),
        "total": total,
        "divergences": [r for r in rows if not r["same_semantic"]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--dates", nargs="*", default=None)
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    if args.dates:
        dates = args.dates
    else:
        wb_names = set(list_remote_dir("predictions"))
        dates = sorted({n[:10] for n in wb_names
                        if n.endswith(".json") and n[0].isdigit() and len(n) == 15})
        # 只对 10 月
        dates = [d for d in dates if d >= "2026-10-01"]

    results = []
    for d in dates:
        r = compare_one(d)
        results.append(r)
        if r["status"] == "ok":
            dir_ok = "✓" if r["direction"]["match"] else "✗"
            print(f"{d} 上证{r['index']['wb_chg']:+.2f}% | "
                  f"方向 {r['direction']['wb']}/{r['direction']['dahai']} {dir_ok} | "
                  f"个股语义一致 {r['agree_semantic']}/{r['total']}")
        else:
            print(f"{d} 缺失: {r.get('missing')}")

    json.dump(results, open(os.path.join(OUT_DIR, "daily_compare.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=2)

    # 人话版
    lines = ["# WB ↔ 大海 逐日对账（自动生成）", "",
             f"生成时间：{datetime.now():%Y-%m-%d %H:%M}", "",
             "> 口径说明：`hold`=无操作 · `watch`=观察 · `buy`=买入",
             "> 两边词表不同（大海 watch≈WB hold），**语义一致即算对齐**", ""]
    for r in results:
        if r["status"] != "ok":
            continue
        lines.append(f"## {r['date']}")
        lines.append("")
        lines.append(f"- 上证收盘：WB {r['index']['wb_close']} vs 大海 {r['index']['dahai_close']} "
                     f"（{'匹配' if r['index']['close_match'] else '不一致'}）")
        lines.append(f"- 大盘方向：WB `{r['direction']['wb']}` vs 大海 `{r['direction']['dahai']}` "
                     f"（{'一致' if r['direction']['match'] else '**分歧**'}）")
        lines.append(f"- 个股语义一致：**{r['agree_semantic']}/{r['total']}**")
        lines.append("")
        lines.append("| 标的 | WB | 大海 | 语义 | 归因 |")
        lines.append("|---|---|---|---|---|")
        for s in r["stocks"]:
            mk = "✅" if s["same_semantic"] else "⚠️"
            att = s.get("attribution", "")
            if att.startswith("真分歧"):
                att = "🔴" + att
            elif att.startswith("数据分歧"):
                att = "🟡" + att
            lines.append(f"| {s['name']} | {s['wb_action']} | {s['dahai_action']} | "
                         f"{mk} | {att} |")
        lines.append("")
    open(os.path.join(OUT_DIR, "daily_compare.md"), "w",
         encoding="utf-8").write("\n".join(lines))
    print(f"\n已生成 reconcile/daily_compare.json + .md")


if __name__ == "__main__":
    main()
