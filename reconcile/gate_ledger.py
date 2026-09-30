#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
闸门样本台账  v1.2
====================
从 2026-09-29 起，每日记录「对齐机制的实际输出」，用于将来回测闸门有效性。
同时记录【个股买入信号】，追踪每个信号的实际表现。

设计动机：
  9/29 回测发现双方共同数据仅 3 天，无法评估闸门。
  与其补不了的历史，不如把从今天起的每天记录做扎实 —— 攒到 30+ 样本再回测。

  9/30 复盘发现：9/21 那批核心池推荐平均 -5.17%、跑输大盘 2.54 点，
  但因为没有记录，【一直不知道它错】。故新增个股信号追踪。

【v1.1 变更 · 2026-09-29】
  1. 复用 align_norm 的基准日校验，基准缺失/错配的记录降级为
     verdict_code=BASIS_UNKNOWN / TIME_MISMATCH，【不计入有效闸门样本】。
  2. 新增 --backfill：批量回填历史日期，并自动标注基准状态。
  3. stats 分列「名义闸门日」与「有效闸门日」，避免用不可比的样本自我安慰。

【v1.2 变更 · 2026-09-30】
  1. 新增【个股信号台账】：--signal-add / --signals / --signal-stats
     记录每个 buy 信号的发出日、标的、理由，并自动计算 T+1/T+3/T+5
     实际涨跌与相对大盘超额收益。
  2. signal-stats 输出胜率、平均收益、平均超额 —— 用数字判断对错，
     不靠事后回忆。
  3. --signal-scan：从 predictions/ 目录自动扫描所有 buy 信号并建档。

用法:
  # 闸门台账
  python3 gate_ledger.py --add 2026-09-29
  python3 gate_ledger.py --backfill 2026-09-21,2026-09-24,2026-09-28
  python3 gate_ledger.py --show
  python3 gate_ledger.py --stats

  # 个股信号台账（v1.2 新增）
  python3 gate_ledger.py --signal-scan                    # 从 predictions/ 自动扫描
  python3 gate_ledger.py --signal-add 2026-09-30 601899 buy
  python3 gate_ledger.py --signals                        # 查看所有信号
  python3 gate_ledger.py --signal-stats                   # 胜率统计
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from align_norm import fetch_remote, reconcile, CORE_POOL, extract_basis_date  # noqa
from trade_calendar import is_trading_day, describe, count_trading_days  # noqa

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(BASE, "reconcile", "gate_ledger.json")
SIGNALS = os.path.join(BASE, "reconcile", "signal_ledger.json")
BENCH = "sh000001"          # 超额收益基准


def _curl(url, timeout=25):
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout),
         "-H", "User-Agent: Mozilla/5.0",
         "-H", "Referer: https://gu.qq.com/", url],
        capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def load_closes(code="sh000001", days=400):
    url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
           f"newfqkline/get?param={code},day,,,{days},qfq")
    try:
        k = json.loads(_curl(url))["data"][code]
        kl = k.get("qfqday") or k.get("day")
        return {x[0]: float(x[2]) for x in kl}
    except Exception:
        return {}


def load_ledger():
    if os.path.exists(LEDGER):
        return json.load(open(LEDGER, encoding="utf-8"))
    return {
        "schema_version": "1.0",
        "_说明": ("闸门样本台账。每日记录对齐机制输出，用于回测『双模型一致不买』"
                  "是否真的避开了下跌。目标：攒到 30+ 个闸门触发日再回测。"),
        "records": [],
        "_created": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ---------------------------------------------------------------- 个股信号台账

def load_signals():
    """加载个股信号台账（v1.2 新增）。"""
    if os.path.exists(SIGNALS):
        return json.load(open(SIGNALS, encoding="utf-8"))
    return {
        "schema_version": "1.0",
        "_说明": ("个股买入信号台账。记录每一个 buy 信号的发出日/标的/理由，"
                  "并计算后续 T+1/T+3/T+5 实际表现与相对大盘超额收益。"
                  "设计动机：9/21 那批推荐平均 -5.17% 却无人察觉，"
                  "因为从未记录过。此台账确保每个信号可追溯、可证伪。"),
        "benchmark": BENCH,
        "records": [],
        "_created": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def save_signals(d):
    d["_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    json.dump(d, open(SIGNALS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def prefix_code(code):
    """把 6 位纯数字代码补成腾讯行情格式（sh/sz 前缀）。"""
    code = str(code).strip()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("6", "5", "11", "9")):
        return "sh" + code
    return "sz" + code


def save_ledger(d):
    d["_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    json.dump(d, open(LEDGER, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def fetch_pair(date_str):
    """拉取指定日期双方预测，返回 (wb_doc, dh_doc)。"""
    wb = None
    for p in (os.path.join(BASE, "predictions", f"{date_str}.json"),):
        if os.path.exists(p):
            wb = json.load(open(p, encoding="utf-8"))
            break
    if wb is None:
        wb = fetch_remote(f"predictions/{date_str}.json")

    dh = None
    for fn in (f"{date_str}_dahai.json", f"{date_str}.json"):
        dh = fetch_remote(f"predictions-dahai/{fn}")
        if dh:
            break
    return wb, dh


def cmd_add(date_str):
    wb, dh = fetch_pair(date_str)
    if not wb:
        print(f"✗ WB 侧无 {date_str} 数据", file=sys.stderr)
        return 1
    if not dh:
        print(f"✗ 大海侧无 {date_str} 数据", file=sys.stderr)
        return 1

    res = reconcile(date_str, wb, dh)

    # 时点对账 —— 统一走 align_norm 的抽取器，避免两边各写一套判断
    wb_basis = extract_basis_date(wb, "wb")
    dh_basis = extract_basis_date(dh, "dh")

    if wb_basis is None or dh_basis is None:
        basis_state = "UNKNOWN"        # 缺字段，无法判定
    elif wb_basis == dh_basis:
        basis_state = "ALIGNED"
    else:
        basis_state = "MISMATCH"

    # 基准状态决定样本是否可用
    if basis_state == "ALIGNED":
        verdict_code, verdict = res["verdict_code"], res["verdict"]
    elif basis_state == "MISMATCH":
        verdict_code = "TIME_MISMATCH"
        verdict = (f"⚠️ 时点不对齐（WB基准 {wb_basis} / 大海基准 {dh_basis}）"
                   f"→ 比较无效 → 不出手")
    else:
        verdict_code = "BASIS_UNKNOWN"
        verdict = (f"⚠️ 基准日缺失（WB={wb_basis or '无'} / 大海={dh_basis or '无'}）"
                   f"→ 无法判定可比性 → 不出手")

    rec = {
        "date": date_str,
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "is_trading_day": is_trading_day(date_str),
        "day_label": describe(date_str),
        "wb_model": res["signals"]["wb_model"],
        "dh_model": res["signals"]["dh_model"],
        "wb_market": res["market"]["wb"],
        "wb_market_raw": res["market"]["wb_raw"],
        "dh_market": res["market"]["dh"],
        "dh_market_raw": res["market"]["dh_raw"],
        "market_agree": res["market"]["agree"],
        "core_agree": res["core_agree"],
        "wb_buy": res["signals"]["wb_buy"],
        "dh_buy": res["signals"]["dh_buy"],
        "verdict_code": verdict_code,
        "verdict": verdict,
        # 时点信息 —— 决定对账是否可比
        "wb_basis": wb_basis,
        "dh_basis": dh_basis,
        "basis_state": basis_state,          # ALIGNED / MISMATCH / UNKNOWN
        "time_aligned": (basis_state == "ALIGNED"),
        # 样本可用性：只有基准对齐的才算有效闸门样本
        "sample_usable": basis_state == "ALIGNED",
        # 后续表现留空，将来回填
        "forward": {},
        "core_pool_snapshot": [
            {"code": r["code"], "name": r["name"],
             "wb": r["wb_direction"], "dh": r["dh_direction"], "status": r["status"]}
            for r in res["core_pool"]
        ],
    }

    led = load_ledger()
    led["records"] = [x for x in led["records"] if x["date"] != date_str]
    led["records"].append(rec)
    led["records"].sort(key=lambda x: x["date"])
    save_ledger(led)

    icon = {"ALIGNED": "✅", "MISMATCH": "❌", "UNKNOWN": "❓"}[basis_state]
    print(f"✅ 已记录 {date_str}  ({describe(date_str)})")
    print(f"   大盘: WB={rec['wb_market_raw']}→{rec['wb_market']}  "
          f"大海={rec['dh_market_raw']}→{rec['dh_market']}  "
          f"{'一致' if rec['market_agree'] else '分歧'}")
    print(f"   核心池: {rec['core_agree']}   买入信号: WB {rec['wb_buy']} / 大海 {rec['dh_buy']}")
    print(f"   判定: [{verdict_code}] {verdict}")
    print(f"   基准: {icon} {basis_state}   WB={wb_basis or '缺失'}  "
          f"大海={dh_basis or '缺失'}")
    if not rec["sample_usable"]:
        print(f"   ⚠️ 该记录【不计入】有效闸门样本")
    return 0


def cmd_backfill(dates_str):
    dates = [d.strip() for d in dates_str.split(",") if d.strip()]
    rc = 0
    for d in dates:
        print(f"--- 回填 {d} ---")
        rc |= cmd_add(d)
        print()
    return rc


def cmd_show():
    led = load_ledger()
    recs = led.get("records", [])
    if not recs:
        print("台账为空")
        return
    print(f"{'日期':<12}{'WB模型':<12}{'大海':<9}{'大盘':<14}{'一致':<6}"
          f"{'核心池':<8}{'WB买':<6}{'海买':<6}{'判定':<15}{'基准':<8}{'可用':<6}")
    print("-" * 122)
    for r in recs:
        st = r.get("basis_state") or ("ALIGNED" if r.get("time_aligned") else "MISMATCH")
        icon = {"ALIGNED": "✅", "MISMATCH": "❌", "UNKNOWN": "❓"}.get(st, "?")
        usable = "✅" if r.get("sample_usable", st == "ALIGNED") else "❌"
        print(f"{r['date']:<12}{str(r['wb_model'])[:11]:<12}{str(r['dh_model']):<9}"
              f"{r['wb_market']}/{r['dh_market']:<8}"
              f"{'✅' if r['market_agree'] else '❌':<6}{r['core_agree']:<8}"
              f"{r['wb_buy']:<6}{r['dh_buy']:<6}{r['verdict_code']:<15}{icon:<8}{usable:<6}")


def cmd_stats():
    led = load_ledger()
    recs = led.get("records", [])
    n = len(recs)

    # 名义闸门日（不看基准）
    gate_nominal = [r for r in recs if r["wb_buy"] == 0 and r["dh_buy"] == 0
                    and r["market_agree"]]
    # 有效闸门日（基准必须对齐）
    gate_valid = [r for r in gate_nominal if r.get("sample_usable")]
    unusable = [r for r in recs if not r.get("sample_usable")]

    print("=" * 74)
    print("闸门台账统计  v1.1")
    print("=" * 74)
    print(f"  总记录天数        : {n}")
    print(f"  名义闸门日        : {len(gate_nominal)}   （双方同向 + 双方零买入）")
    print(f"  有效闸门日        : {len(gate_valid)}   （+ 基准日对齐）")
    print(f"  不可用记录        : {len(unusable)}")
    print()

    # 基准状态分布
    from collections import Counter
    dist = Counter(r.get("basis_state", "?") for r in recs)
    print("  基准状态分布:")
    for k in ("ALIGNED", "MISMATCH", "UNKNOWN"):
        if dist.get(k):
            label = {"ALIGNED": "对齐 ✅", "MISMATCH": "错配 ❌",
                     "UNKNOWN": "缺失 ❓"}[k]
            print(f"    {label:<12}{dist[k]:>3} 天")
    print()

    n_need = 30
    print(f"  回测就绪度: {len(gate_valid)}/{n_need} 个【有效】闸门样本")
    pct = len(gate_valid) / n_need
    bar = "█" * int(pct * 40)
    print(f"  [{bar:<40}] {pct*100:.0f}%")
    print(f"  还需 {max(0, n_need-len(gate_valid))} 个交易日的干净样本")
    print()

    if unusable:
        print("  ⚠️ 不可用记录明细（对账结论不可采信）:")
        for r in unusable:
            st = r.get("basis_state", "?")
            note = {"MISMATCH": "基准错配", "UNKNOWN": "基准缺失"}.get(st, st)
            print(f"     {r['date']}  [{note}]  WB基准={r.get('wb_basis') or '缺失'}"
                  f"  大海基准={r.get('dh_basis') or '缺失'}")
        print()
        print("  ➡️ 这些记录不能用来论证『闸门有效』——它们连『双方看的是不是")
        print("     同一天』都没确认。补基准字段后需重新纳入。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--add")
    ap.add_argument("--backfill", help="逗号分隔的日期列表，批量回填")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--stats", action="store_true")
    # ---- v1.2 个股信号台账
    ap.add_argument("--signal-add", nargs=3, metavar=("DATE", "CODE", "ACTION"),
                    help="手动添加一条信号")
    ap.add_argument("--signal-scan", action="store_true",
                    help="从 predictions/ 目录自动扫描全部 buy 信号")
    ap.add_argument("--signals", action="store_true", help="查看信号台账")
    ap.add_argument("--signal-stats", action="store_true", help="信号胜率统计")
    args = ap.parse_args()

    if args.add:
        return cmd_add(args.add)
    if args.backfill:
        return cmd_backfill(args.backfill)
    if args.stats:
        return cmd_stats()
    if args.signal_scan:
        return cmd_signal_scan()
    if args.signal_add:
        return cmd_signal_add(*args.signal_add)
    if args.signals:
        return cmd_signals()
    if args.signal_stats:
        return cmd_signal_stats()
    cmd_show()
    return 0


def cmd_signal_scan():
    """从 predictions/ 目录扫描所有 buy 类信号并建档（v1.2 新增）。"""
    import glob
    pred_dir = os.path.join(BASE, "predictions")
    files = sorted(glob.glob(os.path.join(pred_dir, "2026-*.json")))
    led = load_signals()
    existing = {(r["date"], r["code"]) for r in led["records"]}
    added = 0

    for f in files:
        date = os.path.basename(f).replace(".json", "")
        # 跳过盘中快照文件（形如 2026-09-29_1530.json）
        if len(date) != 10:
            continue
        try:
            doc = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        calls = doc.get("stock_calls") or doc.get("predictions") or []
        for s in calls:
            action = str(s.get("action") or "")
            # 只记买入类信号
            if action not in ("buy", "buy_slow_bull", "buy_probe",
                              "buy_standard", "buy_rebound"):
                continue
            code = "".join(ch for ch in str(s.get("code") or "") if ch.isdigit())
            if not code or (date, code) in existing:
                continue
            led["records"].append({
                "date": date,
                "code": code,
                "name": s.get("name") or "",
                "action": action,
                "direction": s.get("direction"),
                "reason": s.get("reason"),
                "core_pool": bool(s.get("core_pool")),
                "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "forward": {},
            })
            existing.add((date, code))
            added += 1

    led["records"].sort(key=lambda x: (x["date"], x["code"]))
    save_signals(led)
    print(f"✅ 扫描完成，新增 {added} 条信号（总计 {len(led['records'])} 条）")
    return 0


def cmd_signal_add(date, code, action):
    led = load_signals()
    if any(r["date"] == date and r["code"] == code for r in led["records"]):
        print(f"⚠️ {date} {code} 已存在，跳过")
        return 1
    led["records"].append({
        "date": date, "code": code, "name": "", "action": action,
        "direction": None, "reason": "手动添加", "core_pool": False,
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "forward": {},
    })
    led["records"].sort(key=lambda x: (x["date"], x["code"]))
    save_signals(led)
    print(f"✅ 已记录信号 {date} {code} {action}")
    return 0


def cmd_signals():
    led = load_signals()
    recs = led["records"]
    if not recs:
        print("信号台账为空，先跑 --signal-scan")
        return 0

    closes = load_closes(BENCH)
    bdates = sorted(closes)

    print(f"{'发出日':<12}{'代码':<8}{'名称':<12}{'动作':<14}"
          f"{'T+1':>8}{'T+3':>8}{'T+5':>8}{'至今':>8}{'超额':>8}")
    print("-" * 96)
    for r in recs:
        m = load_closes(prefix_code(r["code"]))
        if not m:
            continue
        d = sorted(m)
        if r["date"] not in m:
            continue
        p0 = m[r["date"]]
        i = d.index(r["date"])

        def fwd(n):
            return ((m[d[i + n]] / p0 - 1) * 100) if i + n < len(d) else None

        now = (m[d[-1]] / p0 - 1) * 100
        ex = None
        if r["date"] in closes and bdates:
            b = (closes[bdates[-1]] / closes[r["date"]] - 1) * 100
            ex = now - b

        fmt = lambda v: f"{v:>+8.2f}" if v is not None else f"{'--':>8}"
        print(f"{r['date']:<12}{r['code']:<8}{r['name'][:10]:<12}"
              f"{r['action'][:12]:<14}{fmt(fwd(1))}{fmt(fwd(3))}{fmt(fwd(5))}"
              f"{now:>+8.2f}{fmt(ex)}")
    print("-" * 96)
    return 0


def cmd_signal_stats():
    led = load_signals()
    recs = led["records"]
    if not recs:
        print("信号台账为空，先跑 --signal-scan")
        return 0

    closes = load_closes(BENCH)
    bdates = sorted(closes)

    rows = []
    for r in recs:
        m = load_closes(prefix_code(r["code"]))
        if not m or r["date"] not in m:
            continue
        d = sorted(m)
        i = d.index(r["date"])
        p0 = m[r["date"]]
        if i + 5 >= len(d):
            continue     # 未满 5 个交易日，不计入统计
        r5 = (m[d[i + 5]] / p0 - 1) * 100
        rb = (closes[d[i + 5]] / closes[r["date"]] - 1) * 100 \
            if r["date"] in closes and d[i + 5] in closes else None
        rows.append(dict(date=r["date"], name=r["name"] or r["code"],
                         r5=r5, ex=(r5 - rb) if rb is not None else None,
                         core=r["core_pool"]))

    if not rows:
        print(f"信号共 {len(recs)} 条，但均未满 5 个交易日，暂无法统计")
        return 0

    import statistics
    print("=" * 70)
    print("个股买入信号 · 胜率统计（T+5 口径）")
    print("=" * 70)
    print(f"  可统计样本      : {len(rows)} 条 / 总计 {len(recs)} 条")
    n_win = sum(1 for x in rows if x["r5"] > 0)
    n_ex_win = sum(1 for x in rows if x["ex"] is not None and x["ex"] > 0)
    print(f"  绝对收益胜率    : {n_win}/{len(rows)} = {n_win/len(rows)*100:.1f}%")
    print(f"  跑赢大盘胜率    : {n_ex_win}/{len(rows)} = {n_ex_win/len(rows)*100:.1f}%")
    print()
    print(f"  平均收益（T+5） : {statistics.mean(x['r5'] for x in rows):+.2f}%")
    exs = [x["ex"] for x in rows if x["ex"] is not None]
    if exs:
        print(f"  平均超额收益    : {statistics.mean(exs):+.2f}%")
        print(f"  超额中位数      : {statistics.median(exs):+.2f}%")
    print()

    core = [x for x in rows if x["core"]]
    if core:
        print(f"  〔核心池子样本〕{len(core)} 条")
        print(f"    平均收益      : {statistics.mean(x['r5'] for x in core):+.2f}%")
        cx = [x["ex"] for x in core if x["ex"] is not None]
        if cx:
            print(f"    平均超额      : {statistics.mean(cx):+.2f}%")
    print()
    print("=" * 70)
    print("逐笔明细")
    print("=" * 70)
    for x in sorted(rows, key=lambda y: y["ex"] if y["ex"] is not None else 0):
        flag = "✅" if (x["ex"] or 0) > 0 else "❌"
        ex = f"{x['ex']:+.2f}%" if x["ex"] is not None else "--"
        print(f"  {flag} {x['date']}  {x['name'][:12]:<14} T+5 {x['r5']:+6.2f}%   超额 {ex}")
    print()
    if len(rows) < 30:
        print(f"  ⚠️ 样本仅 {len(rows)} 条，统计上不足以定论，需继续积累")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
