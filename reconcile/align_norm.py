#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对齐归一化 + 双模型对账引擎  v1.0
=====================================
解决 9/28 暴露的 4 个问题：
  P1 文件名不一致：WB 用 2026-09-28.json / 大海用 2026-09-28_dahai.json
  P2 枚举不一致：WB up/down/neutral / 大海 bearish/bullish/neutral / 早期中文 平
  P3 字段路径不一致：WB t1_prediction.sh_index.direction /
                    大海 market_outlook.direction / 早期 market_overview
  P4 对账靠人肉，无脚本

用法:
  python3 align_norm.py --date 2026-09-28           # 对账指定日期
  python3 align_norm.py --date 2026-09-28 --demo    # 用远端实拉数据
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------- 常量

REPO_RAW = "https://gh-proxy.com/https://raw.githubusercontent.com/livsstock/xgl/main"

# 核心池 v2.2
CORE_POOL = [
    ("601899", "紫金矿业",   "oversold_rebound"),
    ("600111", "北方稀土",   "oversold_rebound"),
    ("603799", "华友钴业",   "oversold_rebound"),
    ("601958", "金钼股份",   "oversold_rebound"),
    ("512480", "半导体ETF",  "slow_bull"),
    ("000858", "五粮液",     "slow_bull"),
]

# 方向归一化表 —— 统一收敛到 up / down / neutral
DIRECTION_MAP = {
    # 英文标准
    "up": "up", "down": "down", "neutral": "neutral",
    # 大海 v12.6 用词
    "bullish": "up", "bearish": "down", "flat": "neutral",
    # 早期中文枚举（含"偏多/偏空/略偏多"等副词形式）
    "看多": "up", "看涨": "up", "多": "up", "涨": "up",
    "偏多": "up", "略偏多": "up", "谨慎看多": "up",
    "看空": "down", "看跌": "down", "空": "down", "跌": "down",
    "偏空": "down", "略偏空": "down", "谨慎看空": "down",
    "平": "neutral", "中性": "neutral", "震荡": "neutral",
    "偏中": "neutral", "观望": "neutral",
    # 动作词兜底
    "buy": "up", "sell": "down", "hold": "neutral", "watch": "neutral",
}


def norm_direction(value):
    """把任意口径的方向字段归一化为 up/down/neutral。识别不了返回 None。"""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in DIRECTION_MAP:
        return DIRECTION_MAP[s]
    # 子串兜底（覆盖 "偏空"、"略偏多"、"谨慎看多" 等未穷举的派生写法）
    # 注意顺序：先判否定的、再判肯定的，避免 "偏空" 命中 "空" 之外的歧义
    for key in ("bearish", "看空", "看跌", "偏空", "略偏空", "跌"):
        if key in s:
            return "down"
    for key in ("bullish", "看多", "看涨", "偏多", "略偏多", "涨"):
        if key in s:
            return "up"
    for key in ("neutral", "中性", "震荡", "观望", "平"):
        if key in s:
            return "neutral"
    return None


# ---------------------------------------------------------------- 抓取

def _curl(url, timeout=20):
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout),
         "-H", "User-Agent: Mozilla/5.0",
         "-H", "Referer: https://gu.qq.com/", url],
        capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def fetch_remote(path, timeout=20):
    """从远端仓库拉一个 json，失败返回 None。"""
    raw = _curl(f"{REPO_RAW}/{path}", timeout)
    if not raw or raw.lstrip().startswith("<"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- 字段抽取

def _extract_market_direction(doc):
    """按优先级抽取大盘方向，返回 (归一化值, 原始值)。

    优先级（2026-09-29 起：顶层平铺字段优先，见《字段映射统一方案》）：
      1. doc['market_direction']            <- 统一规范，双方都该写
      2. doc['market_outlook']['direction'] <- 大海 v12.6
      3. doc['market_overview']['direction']<- 大海 9/21
      4. doc['market_env']['direction']
      5. doc['t1_prediction']['sh_index']['direction']  <- WB 2.5
    """
    # 1. 顶层平铺（推荐）
    if doc.get("market_direction"):
        raw = doc["market_direction"]
        return norm_direction(raw), raw

    # 2-4. 嵌套块
    for key in ("market_outlook", "market_overview", "market_env"):
        blk = doc.get(key) or {}
        if isinstance(blk, dict) and blk.get("direction"):
            raw = blk["direction"]
            return norm_direction(raw), raw

    # 5. WB schema 2.5
    t1 = doc.get("t1_prediction") or {}
    sh = t1.get("sh_index") or {}
    raw = sh.get("direction")
    if raw is None:
        raw = t1.get("direction")
    return norm_direction(raw), raw


def extract_wb(doc):
    """从 WB 预测文件抽取 {market_direction, stocks:{code: {...}}}，兼容多版本 schema。"""
    out = {"market_direction": None, "stocks": {}, "model": None}

    d, raw = _extract_market_direction(doc)
    out["market_direction"] = d
    out["_raw_market_direction"] = raw

    # 标的列表：stock_calls / predictions / stocks 都可能
    for key in ("stock_calls", "predictions", "stocks"):
        lst = doc.get(key)
        if isinstance(lst, list) and lst:
            for s in lst:
                if not isinstance(s, dict):
                    continue
                code = "".join(ch for ch in str(s.get("code") or "") if ch.isdigit())
                if not code:
                    continue
                out["stocks"][code] = {
                    "name": s.get("name"),
                    "direction": norm_direction(s.get("direction")),
                    "_raw_direction": s.get("direction"),
                    "action": s.get("action"),
                    "reason": s.get("reason"),
                    "score": s.get("score"),
                }
            break

    for key in ("model_version", "model", "predictor"):
        v = doc.get(key)
        if isinstance(v, str) and v:
            out["model"] = v
            break
    return out


def extract_dahai(doc):
    """从大海预测文件抽取同构结构。"""
    out = {"market_direction": None, "stocks": {}, "model": None}

    d, raw = _extract_market_direction(doc)
    out["market_direction"] = d
    out["_raw_market_direction"] = raw

    # 大海早期把标的写在一级数组里，方向字段可能是 t1 / direction
    for key in ("stock_calls", "predictions", "stocks"):
        lst = doc.get(key)
        if isinstance(lst, list) and lst:
            for s in lst:
                if not isinstance(s, dict):
                    continue
                code = "".join(ch for ch in str(s.get("code") or "") if ch.isdigit())
                if not code:
                    continue
                dirval = s.get("direction")
                if dirval is None:
                    dirval = s.get("t1")          # 大海 9/18, 9/23 用 t1
                out["stocks"][code] = {
                    "name": s.get("name"),
                    "direction": norm_direction(dirval),
                    "_raw_direction": dirval,
                    "action": s.get("action"),
                    "reason": s.get("reason"),
                }
            break

    out["model"] = doc.get("model_version") or doc.get("model") or "未知"
    return out


# ---------------------------------------------------------------- 对账

def extract_basis_date(doc, side):
    """抽取该预测实际依据的『最后一个交易日』。

    2026-09-29 新增：对账前必须先校验时点。若双方基准日不同，
    比较的是不同时段的判断，结论无效（9/29 的"假分歧"即由此造成）。

    2026-09-29 v1.1 修正：返回值语义收紧
      · 只返回【日期部分】YYYY-MM-DD，且必须是合法交易日语义的日期串
      · 抽不到一律返回 None —— 调用方必须把 None 当作『无法判定』，
        不得默认双方对齐。这是"宁可报无法对账，不报假分歧"的落地。
    """
    def _norm_date(s):
        """从任意字符串里取第一个 YYYY-MM-DD。"""
        if not s:
            return None
        s = str(s).replace("收盘数据", "").replace("收盘", "").strip()
        m = __import__("re").search(r"\d{4}-\d{2}-\d{2}", s)
        return m.group(0) if m else None

    # ---- 优先级 1：双方约定的规范字段
    for key in ("data_basis", "basis_date", "as_of"):
        v = doc.get(key)
        if isinstance(v, str) and v:
            got = _norm_date(v)
            if got:
                return got

    # ---- 优先级 2：WB 的 data_basis 若为 dict（含 as_of）
    db = doc.get("data_basis")
    if isinstance(db, dict):
        for k in ("as_of", "date", "basis_date", "trade_date"):
            got = _norm_date(db.get(k))
            if got:
                return got

    # ---- 优先级 3：大海可能把基准藏在 market_outlook 里
    for holder in ("market_outlook", "market_overview", "market_env"):
        blk = doc.get(holder)
        if isinstance(blk, dict):
            for k in ("data_basis", "basis_date", "as_of"):
                got = _norm_date(blk.get(k))
                if got:
                    return got

    # ---- 优先级 4：WB 的市场环境时间戳
    env = doc.get("_market_environment") or doc.get("market_environment")
    if isinstance(env, dict):
        got = _norm_date(env.get("timestamp"))
        if got:
            return got

    # ---- 兜底：generated_at
    # ⚠️ 注意：generated_at 是【生成时间】而非【数据基准】，两者可能差好几天
    #    （实例：大海 9/23 那份文件 generated_at=2026-09-28，date 却写 2026-09-22）
    #    因此仅在明确没有其它字段时才用，并在结果里标记来源为低置信。
    got = _norm_date(doc.get("generated_at"))
    if got:
        return got

    return None


def reconcile(date_str, wb_doc, dh_doc):
    wb = extract_wb(wb_doc)
    dh = extract_dahai(dh_doc)

    rows = []
    agree = 0
    for code, name, bucket in CORE_POOL:
        w = wb["stocks"].get(code, {})
        d = dh["stocks"].get(code, {})
        wdir, ddir = w.get("direction"), d.get("direction")
        if wdir is None or ddir is None:
            status = "无数据"
        elif wdir == ddir:
            status = "一致"
            agree += 1
        else:
            status = "分歧"
        rows.append({
            "code": code, "name": name, "bucket": bucket,
            "wb_direction": wdir, "wb_raw": w.get("_raw_direction"),
            "wb_action": w.get("action"),
            "dh_direction": ddir, "dh_raw": d.get("_raw_direction"),
            "dh_action": d.get("action"), "dh_reason": d.get("reason"),
            "status": status,
        })

    mkt_wb, mkt_dh = wb["market_direction"], dh["market_direction"]
    mkt_agree = (mkt_wb is not None and mkt_wb == mkt_dh)

    # ---- 时点校验（2026-09-29 新增；v1.1 收紧为硬校验）
    wb_basis = extract_basis_date(wb_doc, "wb")
    dh_basis = extract_basis_date(dh_doc, "dh")

    if wb_basis is None or dh_basis is None:
        basis_state = "UNKNOWN"      # 缺字段 → 无法判定，不算对齐
    elif wb_basis == dh_basis:
        basis_state = "ALIGNED"
    else:
        basis_state = "MISMATCH"
    time_aligned = {"ALIGNED": True, "MISMATCH": False, "UNKNOWN": None}[basis_state]

    buy_wb = [s for s in wb["stocks"].values() if s.get("action") == "buy"]
    buy_dh = [s for s in dh["stocks"].values() if s.get("action") == "buy"]

    # 对齐机制 v2.2 判定
    #   v2.0：方向一致 / 分歧 / 一方无数据
    #   v2.1：新增时点不对齐 → TIME_MISMATCH
    #   v2.2：时点【缺失】单列为 BASIS_UNKNOWN，不再默认对齐
    #         —— 宁可报"无法对账"，也不报一个假的"分歧"或假的"一致"
    if not wb["stocks"] or not dh["stocks"]:
        verdict = "一方无数据 → 未对齐 → 不出手"
        code_verdict = "DATA_MISSING"
    elif basis_state == "MISMATCH":
        verdict = (f"⚠️ 时点不对齐（WB基准 {wb_basis} / 大海基准 {dh_basis}）"
                   f"→ 比较无效 → 不出手")
        code_verdict = "TIME_MISMATCH"
    elif basis_state == "UNKNOWN":
        missing = "双方都缺失" if (wb_basis is None and dh_basis is None) else (
            f"大海缺失" if dh_basis is None else "WB缺失")
        verdict = (f"⚠️ 基准日缺失（{missing}）→ 无法判定可比性 → 不出手")
        code_verdict = "BASIS_UNKNOWN"
    elif buy_wb or buy_dh:
        if buy_wb and buy_dh and mkt_agree:
            verdict = "双方均有买入信号且大盘同向 → 可出手（需阿牛哥确认）"
            code_verdict = "GO"
        else:
            verdict = "仅单方有买入信号 → 分歧 → 不出手"
            code_verdict = "NO_GO"
    else:
        verdict = "双方均无买入信号 → 一致不买"
        code_verdict = "NO_GO"

    return {
        "date": date_str,
        "market": {
            "wb": mkt_wb, "wb_raw": wb["_raw_market_direction"],
            "dh": mkt_dh, "dh_raw": dh["_raw_market_direction"],
            "agree": mkt_agree,
        },
        "time_check": {
            "wb_basis": wb_basis, "dh_basis": dh_basis,
            "basis_state": basis_state,
            "aligned": time_aligned,
            "sample_usable": basis_state == "ALIGNED",
            "note": {
                "ALIGNED": None,
                "MISMATCH": "双方基准日不同，方向比较不可比",
                "UNKNOWN": "至少一方未声明基准日，无法确认可比性",
            }[basis_state],
        },
        "core_pool": rows,
        "core_agree": f"{agree}/{len(CORE_POOL)}",
        "signals": {
            "wb_buy": len(buy_wb), "dh_buy": len(buy_dh),
            "wb_model": wb["model"], "dh_model": dh["model"],
        },
        "verdict": verdict,
        "verdict_code": code_verdict,
    }


def render(result):
    W = 96
    print("=" * W)
    print(f"双模型对账报告   {result['date']}")
    print("=" * W)

    m = result["market"]
    tag = "✅ 一致" if m["agree"] else "❌ 分歧"
    print(f"大盘方向:  WB={m['wb_raw']}→{m['wb']}   大海={m['dh_raw']}→{m['dh']}   {tag}")
    print(f"模型版本:  WB={result['signals']['wb_model']}   大海={result['signals']['dh_model']}")

    tc = result.get("time_check") or {}
    state = tc.get("basis_state")
    if state == "MISMATCH":
        print(f"⚠️ 时点校验: ❌ 不对齐  WB基准={tc['wb_basis']}  大海基准={tc['dh_basis']}")
        print("           两边看的是不同交易日的数据，方向比较不可比")
        # 差几个交易日？这比"差几天"更有意义
        try:
            from trade_calendar import count_trading_days, describe
            n = count_trading_days(tc["wb_basis"], tc["dh_basis"])
            print(f"           相差 {n} 个交易日")
        except Exception:
            pass
    elif state == "UNKNOWN":
        print(f"时点校验:  ❓ 无法判定（WB={tc.get('wb_basis') or '缺失'}  "
              f"大海={tc.get('dh_basis') or '缺失'}）")
        print("           ⚠️ 至少一方未声明 data_basis，无法确认双方看的是同一天")
        print("           ⚠️ 该日对账结论【不可采信】，需对方补基准字段后重跑")
    elif state == "ALIGNED":
        print(f"时点校验:  ✅ 对齐（基准日 {tc['wb_basis']}）")
    print()
    print(f"{'代码':<9}{'名称':<12}{'WB原文':<10}{'大海原文':<11}{'归一':<20}{'结果':<8}{'WB动作':<9}{'大海动作':<9}")
    print("-" * W)
    for r in result["core_pool"]:
        pair = f"{r['wb_direction']} vs {r['dh_direction']}"
        icon = {"一致": "✅", "分歧": "❌", "无数据": "⚠️"}[r["status"]]
        print(f"{r['code']:<9}{r['name']:<12}{str(r['wb_raw']):<10}{str(r['dh_raw']):<11}"
              f"{pair:<20}{icon}{r['status']:<6}{str(r['wb_action']):<9}{str(r['dh_action']):<9}")
    print("-" * W)
    print(f"核心池方向一致: {result['core_agree']}")
    print()

    disagree = [r for r in result["core_pool"] if r["status"] == "分歧"]
    if disagree:
        print("分歧明细:")
        for r in disagree:
            print(f"  {r['name']}({r['code']}): WB={r['wb_raw']} vs 大海={r['dh_raw']}"
                  f"   大海理由: {r['dh_reason']}")
        print()

    s = result["signals"]
    print(f"买入信号:  WB {s['wb_buy']} 个   大海 {s['dh_buy']} 个")
    print()
    print(f"对齐机制 v2.2 判定 -> [{result['verdict_code']}] {result['verdict']}")
    print("=" * W)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="对账日期 YYYY-MM-DD")
    ap.add_argument("--wb-file", help="本地 WB 预测文件路径（默认查 predictions/）")
    ap.add_argument("--dh-file", help="本地大海预测文件路径（默认从远端拉）")
    ap.add_argument("--json-out", help="把结果写成 json")
    args = ap.parse_args()

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # WB 侧：本地优先，回退远端
    wb_doc = None
    wb_path = args.wb_file
    if not wb_path:
        cand = os.path.join(base, "predictions", f"{args.date}.json")
        if os.path.exists(cand):
            wb_path = cand
    if wb_path and os.path.exists(wb_path):
        wb_doc = json.load(open(wb_path, encoding="utf-8"))
        print(f"[WB]   本地 {wb_path}")
    else:
        wb_doc = fetch_remote(f"predictions/{args.date}.json")
        if wb_doc:
            print(f"[WB]   远端 predictions/{args.date}.json")
            # 落盘缓存
            cache = os.path.join(base, "predictions", f"{args.date}.json")
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            if not os.path.exists(cache):
                json.dump(wb_doc, open(cache, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)

    # 大海侧：兼容两种文件名
    dh_doc = None
    if args.dh_file and os.path.exists(args.dh_file):
        dh_doc = json.load(open(args.dh_file, encoding="utf-8"))
        print(f"[大海] 本地 {args.dh_file}")
    else:
        for fname in (f"{args.date}_dahai.json", f"{args.date}.json"):
            dh_doc = fetch_remote(f"predictions-dahai/{fname}")
            if dh_doc:
                print(f"[大海] 远端 predictions-dahai/{fname}")
                break

    if not wb_doc:
        print(f"✗ WB 侧无 {args.date} 数据（本地与远端都没有）", file=sys.stderr)
        sys.exit(2)
    if not dh_doc:
        print(f"✗ 大海侧无 {args.date} 数据", file=sys.stderr)
        sys.exit(3)

    print()
    result = reconcile(args.date, wb_doc, dh_doc)
    render(result)

    if args.json_out:
        json.dump(result, open(args.json_out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {args.json_out}")


if __name__ == "__main__":
    main()
