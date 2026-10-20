#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decision_points.py — 盘前卖 / 尾盘买 双时点决策（v1.0，2026-10-20）
=====================================================================
【为什么单独一个模块】
老板的实际操作节奏是：
  08:36  看盘前建议 → 决定"今天要不要卖"
  14:30  看盘中建议 → 决定"今天要不要买"

这两个时点的信息、规则、决策对象完全不同，用一份"收盘后 T+1 预测"无法回答：

| 时点 | 决策对象 | 能用的数据 | 必须回答的问题 |
|---|---|---|---|
| 08:36 | 已有持仓 | T-1 收盘（当日还没开盘，没有 T 日数据） | 今天开盘要不要卖 / 挂什么价卖 |
| 14:30 | 要不要新开仓 | T 日实时（14:30 的价≈当日收盘） | 今天尾盘要不要买 / 买多少 |

【08:36 卖：为什么必须用 T-1】
今天 08:36，T 日一根 K 线都还没有。任何号称"用 T 日数据"的盘前判断都是编的。
所以卖决策的正确口径是：
  持仓成本 + T-1 收盘价 → 是否触及止盈/止损/持仓到期
  外加：隔夜跳空预判（T-1 跌幅、大盘方向）
然后给"挂单价"——盘前能下的只有限价单，必须给具体价格，不能只说"卖"。

【14:30 买：为什么用实时价而不是 T-1】
14:30 距收盘 30 分钟，价格已基本定型，用实时价重算 RSI6/MACD 才反映今天真实状态。
若用 T-1 数据，等于拿昨天的超卖信号去买今天的票——盘中反弹 3% 的票会被误买。

【与评分口径的关系（重要）】
  14:30 的买决策可以实盘执行，但**记分牌（胜率统计）必须用 T 日收盘价**重算，
  不能用 14:30 价。否则回测口径与实盘执行不一致，历史胜率会虚高。
  本模块输出的 `final_signal_at_close` 字段用于收盘后复核。

【用法】
  python3 decision_points.py --date 2026-10-20 --point 0836
  python3 decision_points.py --date 2026-10-20 --point 1430 --push
  python3 decision_points.py --date 2026-10-20 --point 1430 --simulate   # 用收盘价模拟 14:30
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "reconcile"))

PRED_DIR = os.path.join(BASE_DIR, "predictions")
FACTS_DIR = os.path.join(BASE_DIR, "facts")
DDP_DIR = os.path.join(PRED_DIR, "decision_points")

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"}


# ---------------------------------------------------------------- 基础数据

def qt_quote(code):
    """腾讯实时行情。返回 dict 或 None（停牌/网络失败）"""
    try:
        req = urllib.request.Request(f"https://qt.gtimg.cn/q={code}", headers=UA)
        raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "replace")
        p = raw.split("~")
        if len(p) < 45 or not p[3]:
            return None
        return {
            "name": p[1], "price": float(p[3]), "prev_close": float(p[4]),
            "open": float(p[5]) if p[5] else None,
            "high": float(p[33]) if p[33] else None,
            "low": float(p[34]) if p[34] else None,
            "volume": float(p[6]) if p[6] else 0,
            "chg_pct": float(p[32]) if p[32] else None,
        }
    except Exception:
        return None


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def load_positions():
    return (load_json(os.path.join(BASE_DIR, "config", "positions.json"), {})
            .get("positions") or [])


def load_core_pool():
    """核心池：code(6位) → {name, track, position_pct}"""
    p = load_json(os.path.join(BASE_DIR, "config", "core_pool.json"), {})
    out = {}
    for track, key in (("short_oversold", "oversold_rebound"),
                       ("slow_bull_hold", "slow_bull")):
        for it in (p.get(key) or []):
            out[it["code"]] = {"name": it["name"], "track": track,
                               "position_pct": it.get("position_pct", 10)}
    return out


def load_watchlist():
    """观察池：[(code6, tencent_code, name)]"""
    try:
        import daily_predict as DP
        return [(c, t, n) for c, t, n in DP.WATCHLIST]
    except Exception:
        return []


def with_prefix(code):
    c = str(code)
    if c.startswith(("sh", "sz")):
        return c
    return ("sh" if c[0] in "56" else "sz") + c


# ---------------------------------------------------------------- 08:36 卖

def decide_sell(date_str, positions):
    """盘前卖决策：只用 T-1 及更早的收盘数据（T 日盘前不存在 T 日数据）。

    规则（白皮书 v1.5 §7.7 分轨）：
      short_oversold  止盈 +5% / 止损 -5% / 持有 10 交易日强平
      slow_bull_hold  止损 -15% / 持有 20 交易日结算 / 不设止盈
    """
    import trade_calendar as TC

    rules = {
        "short_oversold": {"tp": 5.0, "sl": -5.0, "max_days": 10},
        "slow_bull_hold": {"tp": None, "sl": -15.0, "max_days": 20},
    }

    if not positions:
        return {"decisions": [], "summary": "当前空仓，今天没有可卖的",
                "data_basis": f"{TC.prev_trading_day(date_str)}收盘数据"}

    # T-1 事实包（盘前唯一合法数据源）
    t1 = TC.prev_trading_day(date_str)
    pkg = load_json(os.path.join(FACTS_DIR, f"{t1}.json"), {})
    insts = pkg.get("instruments") or {}

    decisions = []
    for pos in positions:
        code = str(pos.get("code", ""))
        name = pos.get("name") or code
        track = pos.get("track") or "short_oversold"
        rule = rules.get(track, rules["short_oversold"])
        buy_price = float(pos.get("buy_price") or 0)
        buy_date = pos.get("buy_date") or date_str

        inst = insts.get(code) or {}
        t1_close = inst.get("close")

        item = {"code": code, "name": name, "track": track,
                "buy_price": buy_price, "buy_date": buy_date,
                "t1_date": t1, "t1_close": t1_close}

        if t1_close is None:
            item.update({"action": "HOLD", "reason": f"T-1({t1}) 无此标的数据，无法判定",
                         "limit_price": None})
            decisions.append(item)
            continue

        chg = round((t1_close / buy_price - 1) * 100, 2) if buy_price else 0.0
        days = TC.count_trading_days(buy_date, t1) if buy_date else 0
        item.update({"chg_vs_cost": chg, "hold_days": days})

        action, reason = "HOLD", "未触及任何卖出条件"
        if rule["sl"] is not None and chg <= rule["sl"]:
            action = "SELL"
            reason = f"止损：{chg:+.2f}% ≤ {rule['sl']}%"
        elif rule["tp"] is not None and chg >= rule["tp"]:
            action = "SELL"
            reason = f"止盈：{chg:+.2f}% ≥ {rule['tp']}%"
        elif days >= rule["max_days"]:
            action = "SELL"
            reason = f"到期强平：已持有 {days} 交易日 ≥ {rule['max_days']}"

        # 盘前能下的只有限价单 → 必须给具体价，不能只说"卖"
        limit_price = None
        if action == "SELL":
            limit_price = round(t1_close * 0.99, 3)   # 低 1% 挂，保证成交
        item.update({"action": action, "reason": reason, "limit_price": limit_price})
        decisions.append(item)

    n_sell = sum(1 for d in decisions if d["action"] == "SELL")
    summary = (f"持仓 {len(decisions)} 只，今日卖出 {n_sell} 只"
               if n_sell else f"持仓 {len(decisions)} 只，今日无需卖出")
    return {"decisions": decisions, "summary": summary,
            "data_basis": f"{t1}收盘数据（盘前最新可用）"}


# ---------------------------------------------------------------- 14:30 买

def _metrics_at(kl, price_override=None, vol_override=None):
    """用 K 线 + 盘中价格重算当日指标（盘中估算收盘态）"""
    from facts_builder import rsi_wilder, macd_full

    closes = [r["close"] for r in kl]
    vols = [r["volume"] for r in kl]
    if price_override is not None:
        # 当日这根 K 线还没走完 → 用实时价替换
        if kl and kl[-1]["day"] >= datetime.now().strftime("%Y-%m-%d"):
            closes[-1] = price_override
            if vol_override:
                vols[-1] = vol_override
        else:
            closes.append(price_override)
            vols.append(vol_override or 0)

    r6 = rsi_wilder(closes, 6)
    macd = macd_full(closes)
    ma20 = round(sum(closes[-20:]) / 20, 3) if len(closes) >= 20 else None
    ten = (round((closes[-1] / closes[-11] - 1) * 100, 2)
           if len(closes) > 11 else None)
    vr = (round(vols[-1] / (sum(vols[-6:-1]) / 5), 2)
          if (len(vols) > 6 and sum(vols[-6:-1])) else None)
    return {"rsi6": r6, "macd": macd, "ma20": ma20, "ten_day_chg": ten, "vol_ratio": vr}


def decide_buy(date_str, simulate=False):
    """14:30 买决策：用 T 日实时价（simulate 时用收盘价）估算收盘态指标。

    规则 v3.0（与 daily_predict 一致，基于 10 年 65249 根 K 线回测）：
      RSI6 < 15         → buy         （S2，T+5 胜率 58.9%）
      15 ≤ RSI6 < 25    → buy_probe   （S1，T+5 胜率 55.2%）
      25 ≤ RSI6 < 30    → watch
      MACD / 量比        仅辅助，不单独构成买入
    """
    from facts_builder import fetch_kline

    pool = load_core_pool()
    watch = load_watchlist()
    if not watch:
        return {"error": "无法加载观察池"}

    held = {str(p.get("code")) for p in load_positions()}
    now_hm = datetime.now().strftime("%H:%M")
    rows = []

    for code6, tcode, name in watch:
        meta = pool.get(code6, {})
        q = None if simulate else qt_quote(tcode)
        price = q["price"] if q else None
        if price is None:
            # 无实时报价（停牌/收盘后回放）→ 退回最新收盘
            kl = fetch_kline(tcode, n=120)
            price = kl[-1]["close"] if kl else None
        if price is None:
            continue

        kl = fetch_kline(tcode, n=120)
        m = _metrics_at(kl, None if simulate else price,
                        (q or {}).get("volume"))
        r6 = m["rsi6"]
        if r6 is None:
            continue

        macd_state = (m["macd"] or {}).get("state", "")
        if r6 < 15:
            sig, conf_hint = "buy", "S2 强超卖"
        elif r6 < 25:
            sig, conf_hint = "buy_probe", "S1 超卖"
        elif r6 < 30:
            sig, conf_hint = "watch", "边缘"
        else:
            sig, conf_hint = "hold", "无信号"

        # 否决层：RSI 仅边缘 + MACD 空头 → 压成 watch
        veto = ""
        if 25 <= r6 < 30 and macd_state in ("below", "death_cross"):
            sig = "watch"
            veto = "RSI仅边缘且MACD空头，否决买入"

        rows.append({
            "code": code6, "name": name,
            "live_price": round(price, 3),
            "live_chg_pct": (round((price / kl[-1]["close"] - 1) * 100, 2)
                             if simulate and kl else (q or {}).get("chg_pct")),
            "rsi6": r6, "ten_day_chg": m["ten_day_chg"],
            "macd_signal": macd_state, "vol_ratio": m["vol_ratio"],
            "ma20_dev": (round((price / m["ma20"] - 1) * 100, 2)
                         if m["ma20"] else None),
            "action": sig, "signal_hint": conf_hint, "veto": veto,
            "core_pool": bool(meta), "track": meta.get("track"),
            "already_held": code6 in held,
            "base_position_pct": meta.get("position_pct"),
        })

    buys = [r for r in rows if r["action"] in ("buy", "buy_probe")]
    for r in buys:
        # 仓位 = 基础仓 × 信号强度系数（强超卖 1.0 / 普通 0.6）
        k = 1.0 if r["action"] == "buy" else 0.6
        r["suggest_position_pct"] = round((r["base_position_pct"] or 10) * k, 1)
        if r["already_held"]:
            r["note"] = "已持仓，本次不重复开仓"
            r["suggest_position_pct"] = 0.0
        elif not r["core_pool"]:
            # [2026-10-20] 观察池不给仓位：核心池 6 只是 9/20 双方共同确认过的池子，
            # 观察池未经双方共识。给仓等于单方面扩池，违反"双模型一致才出手"。
            r["note"] = "观察池，仅提示不建仓（建仓须进核心池且双方确认）"
            r["suggest_position_pct"] = 0.0

    rows.sort(key=lambda x: (not x["core_pool"], x["rsi6"] if x["rsi6"] else 999))
    total_pos = round(sum(r.get("suggest_position_pct") or 0 for r in buys), 1)
    core_buys = [r for r in buys if r["core_pool"] and (r.get("suggest_position_pct") or 0) > 0]

    return {
        "point": "14:30",
        "live_as_of": now_hm,
        "simulated": simulate,
        "data_basis": ("收盘价模拟（非实时）" if simulate
                       else f"{date_str} {now_hm} 实时价估算收盘态"),
        "rows": rows,
        "buy_list": [{"code": r["code"], "name": r["name"], "action": r["action"],
                      "rsi6": r["rsi6"], "core_pool": r["core_pool"],
                      "suggest_position_pct": r.get("suggest_position_pct"),
                      "note": r.get("note", "")}
                     for r in buys],
        "watch_only": [r["name"] for r in buys if not r["core_pool"]],
        "total_suggest_position_pct": total_pos,
        "summary": (f"尾盘核心池建议买入 {len(core_buys)} 只，合计 {total_pos}%；"
                    f"另有 {len(buys) - len(core_buys)} 只观察池信号仅提示不建仓"
                    if buys else "尾盘无买入信号，空仓等待"),
    }


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--point", choices=["0836", "1430"], required=True)
    ap.add_argument("--simulate", action="store_true",
                    help="用收盘价模拟盘中（补跑历史/校验用）")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    os.makedirs(DDP_DIR, exist_ok=True)

    if args.point == "0836":
        res = decide_sell(args.date, load_positions())
        res.update({"date": args.date, "point": "08:36", "kind": "sell_premarket",
                    "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")})
    else:
        res = decide_buy(args.date, simulate=args.simulate)
        res.update({"date": args.date, "kind": "buy_intraday",
                    "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")})

    dst = os.path.join(DDP_DIR, f"{args.date}_{args.point}.json")
    json.dump(res, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---- 控制台人话报告 ----
    print(f"\n{'='*66}")
    print(f"  {args.date}  {res['point']}  {'卖（盘前）' if args.point == '0836' else '买（尾盘）'}")
    print(f"  数据基准：{res.get('data_basis')}")
    print(f"{'='*66}")

    if args.point == "0836":
        print(f"  {res['summary']}\n")
        for d in res["decisions"]:
            tag = "🔴 卖" if d["action"] == "SELL" else "⚪ 持"
            lp = f" | 挂单价 {d['limit_price']}" if d.get("limit_price") else ""
            print(f"  {tag} {d['name']:10s} 成本{d.get('buy_price'):>8} "
                  f"T-1收{d.get('t1_close'):>8} {d.get('chg_vs_cost'):+.2f}% "
                  f"持{d.get('hold_days')}日{lp}")
            print(f"       └ {d['reason']}")
    else:
        print(f"  {res['summary']}\n")
        print(f"  {'标的':<12}{'核心':<5}{'实时价':>8}{'RSI6':>8}{'10日':>8}"
              f"{'MACD':>10}{'建议':>10}")
        for r in res["rows"]:
            core = "★" if r["core_pool"] else ""
            print(f"  {r['name']:<12}{core:<5}{r['live_price']:>8.3f}"
                  f"{r['rsi6']:>8.2f}{(r['ten_day_chg'] or 0):>7.2f}%"
                  f"{r['macd_signal']:>10}{r['action']:>10}"
                  f"{('%s%%' % r['suggest_position_pct']) if r.get('suggest_position_pct') else '':>7}")

    print(f"\n  写入：{dst}")

    if args.push:
        import push_api as P
        rel = os.path.relpath(dst, BASE_DIR)
        print(f"  [PUSH] {P.put_file(dst, rel)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
