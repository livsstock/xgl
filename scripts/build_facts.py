#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一事实数据包生成器（LIVS-Stock 事实层 · 唯一数据源）
================================================================
架构原则（2026-10-17 阿牛哥拍板）：
  客观数据只能一份，主观判断可以两套。
  本脚本产出唯一的 facts/YYYY-MM-DD.json，大海与WB都只读它、不再各自算指标。

产出指标（全部基于 stockdata kline_v2 日K，统一算法）：
  - close/change_pct/ten_day_chg
  - MA5/MA10/MA20
  - RSI6（Wilder 平滑，周期6）
  - MACD(12,26,9)：DIF/DEA/BAR + signal(golden_cross/death_cross/converging/diverging/above/below)
  - vol_ratio（当日量 / 近5日均量）
  - quality: complete / incomplete（任一关键数据缺失即 incomplete，全天停工）

用法：
  python3 build_facts.py [--date YYYY-MM-DD]
依赖：stock-data-skill 的 bin/_cli_wrapper.py（kline_v2）
"""
import json
import subprocess
import argparse
import os
from datetime import datetime, date, timedelta

# ===== 路径 =====
SKILL_BIN = "/Coze/Drive/大海/所有对话/主对话/.skills/skill_stock-data-skill/bin/_cli_wrapper.py"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "facts")

# ===== 标的池：核心6只 + 大盘指数 =====
# (kline_v2代码, 6位数字, 名称, 类型)
INSTRUMENTS = [
    ("601899.XSHG", "601899", "紫金矿业", "stock"),
    ("600111.XSHG", "600111", "北方稀土", "stock"),
    ("603799.XSHG", "603799", "华友钴业", "stock"),
    ("601958.XSHG", "601958", "金钼股份", "stock"),
    ("512480.XSHG", "512480", "半导体ETF", "etf"),
    ("000858.XSHE", "000858", "五粮液", "stock"),
    ("000001.XSHG", "000001", "上证指数", "index"),
]


def fetch_kline(symbol, start, end):
    """通过 stock-data-skill 拉日K，返回按日期升序的 list[dict]"""
    cmd = ["python3", SKILL_BIN, "call", "kline_v2",
           "--param", f"code={symbol}",
           "--param", f"startdate={start}",
           "--param", f"enddate={end}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"kline失败 {symbol}: {r.stderr[:200]}")
    data = json.loads(r.stdout)
    rows = data.get("data", [])
    out = []
    for row in rows:
        out.append({
            "date": row["date"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        })
    out.sort(key=lambda x: x["date"])
    return out


def sma(closes, n):
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 4)


def rsi_wilder(closes, period=6):
    """标准 Wilder RSI。需要 period+1 个收盘价起步，越多越准。"""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    # 初始平均：前 period 个变动的简单平均
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    # Wilder 递推
    for d in deltas[period:]:
        g = max(d, 0)
        l = max(-d, 0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def ema_series(values, n):
    """标准EMA：前 n 个值的SMA播种，之后全历史递推。
    必须喂足够长的历史（≥250根），否则慢线未收敛会导致DIF漂移（10/17与WB对算确认）。"""
    if len(values) < n:
        return [None] * len(values)
    k = 2 / (n + 1)
    out = [None] * len(values)
    seed = sum(values[:n]) / n
    out[n - 1] = seed
    for i in range(n, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def macd(closes, fast=12, slow=26, signal=9, cross_window=3):
    """标准 MACD（与WB对算一致，2026-10-17）：
       EMA12/EMA26 全历史SMA播种递推；DIF=EMA12-EMA26；DEA=EMA9(DIF)；
       hist=DIF-DEA（1倍柱）。
       状态：近 cross_window 日内发生上穿=golden_cross / 下穿=death_cross（事件），
       否则按 DIF 与 DEA 相对位置 above/below。"""
    if len(closes) < slow + signal + 10:
        return None
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    dif_series = [(ema_fast[i] - ema_slow[i])
                  if ema_fast[i] is not None and ema_slow[i] is not None else None
                  for i in range(len(closes))]
    dif_valid = [x for x in dif_series if x is not None]
    dea_valid = ema_series(dif_valid, signal)
    # 对齐尾部
    n_valid = len(dif_valid)
    dif = dif_valid[-1]
    dea = dea_valid[-1]
    hist = dif - dea

    # 近 cross_window 日交叉事件检测
    state = "above" if dif > dea else "below"
    for j in range(max(1, n_valid - cross_window), n_valid):
        if dea_valid[j - 1] is not None and dea_valid[j] is not None:
            if dif_valid[j - 1] <= dea_valid[j - 1] and dif_valid[j] > dea_valid[j]:
                state = "golden_cross"
            elif dif_valid[j - 1] >= dea_valid[j - 1] and dif_valid[j] < dea_valid[j]:
                state = "death_cross"

    return {"dif": round(dif, 4), "dea": round(dea, 4),
            "hist": round(hist, 4), "state": state}


def pct(a, b):
    if b == 0:
        return None
    return round((a - b) / b * 100, 2)


def build_one(inst, target_date):
    symbol, code, name, itype = inst
    # 拉全历史（约3年自然日≈700根K线），保证EMA26充分收敛（与WB全历史递推对齐）
    start = (datetime.strptime(target_date, "%Y-%m-%d").date() - timedelta(days=1100)).strftime("%Y-%m-%d")
    rows = fetch_kline(symbol, start, target_date)

    result = {
        "code": code, "name": name, "type": itype,
        "quality": "incomplete",
    }
    # 找到目标日（或之前最近交易日）
    if not rows:
        result["error"] = "无K线数据"
        return result

    last = rows[-1]
    if last["date"] != target_date:
        result["error"] = f"目标日{target_date}无数据，最新为{last['date']}"
        # 仍计算但标记，由质量门决定
    closes = [r["close"] for r in rows]

    if len(closes) < 35:
        result["error"] = f"K线不足35根(仅{len(closes)})，MACD不可靠"
        return result

    ten_day_ref = closes[-11] if len(closes) >= 11 else closes[0]
    m = macd(closes)
    if m is None:
        result["error"] = "MACD计算失败"
        return result

    vol_ratio = None
    if len(rows) >= 6:
        avg_vol5 = sum(r["volume"] for r in rows[-6:-1]) / 5
        if avg_vol5 > 0:
            vol_ratio = round(last["volume"] / avg_vol5, 2)

    result.update({
        "date": last["date"],
        "close": last["close"],
        "open": last["open"],
        "high": last["high"],
        "low": last["low"],
        "change_pct": pct(closes[-1], closes[-2]),
        "ten_day_chg": pct(closes[-1], ten_day_ref),
        "ma5": sma(closes, 5),
        "ma10": sma(closes, 10),
        "ma20": sma(closes, 20),
        "rsi6": rsi_wilder(closes, 6),
        "macd": {"dif": m["dif"], "dea": m["dea"],
                 "hist": m["hist"], "state": m["state"]},
        "vol_ratio": vol_ratio,
        "quality": "complete",
    })
    result.pop("error", None)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    facts = {
        "date": args.date,
        "generated_at": datetime.now().astimezone().isoformat(),
        "schema_version": "facts-v1.0",
        "indicator_spec": {
            "rsi": "RSI6, Wilder全历史递推",
            "macd": "EMA12/26全历史SMA播种递推；DEA=EMA9(DIF)；hist=DIF-DEA(1倍柱)；state=近3日交叉事件或above/below",
            "vol_ratio": "当日成交量 / 前5日均量",
            "ten_day_chg": "相对10个交易日前收盘",
            "data_source": "stockdata kline_v2 (不复权日K)",
            "aligned_with": "WB 2026-10-17 算法参数确认件（RSI误差0.00/MACD DIF DEA一致）",
        },
        "instruments": {},
        "market": {},
        "overall_quality": "complete",
        "quality_errors": [],
    }

    for inst in INSTRUMENTS:
        try:
            r = build_one(inst, args.date)
        except Exception as e:
            r = {"code": inst[1], "name": inst[2], "type": inst[3],
                 "quality": "incomplete", "error": str(e)[:200]}
        facts["instruments"][inst[1]] = r
        if r.get("quality") != "complete":
            facts["overall_quality"] = "incomplete"
            facts["quality_errors"].append(f"{inst[2]}({inst[1]}): {r.get('error','未知')}")

    # 大盘单独提一份
    idx = facts["instruments"].get("000001", {})
    if idx.get("quality") == "complete":
        facts["market"] = {
            "close": idx["close"],
            "change_pct": idx["change_pct"],
            "ten_day_chg": idx["ten_day_chg"],
            "rsi6": idx["rsi6"],
            "macd_signal": idx["macd"]["state"],
        }

    out_path = os.path.join(OUT_DIR, f"{args.date}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=2)

    print(f"事实数据包已生成: {out_path}")
    print(f"整体质量: {facts['overall_quality']}")
    if facts["quality_errors"]:
        print("质量问题:")
        for e in facts["quality_errors"]:
            print(f"  ⚠️ {e}")
    # 打印核心6只速览
    print("\n核心标的速览:")
    for inst in INSTRUMENTS:
        code = inst[1]
        r = facts["instruments"].get(code, {})
        if r.get("quality") == "complete":
            print(f"  {r['name']:6s} 收{r['close']:>8} 涨跌{r['change_pct']:>6}% "
                  f"10日{r['ten_day_chg']:>7}% RSI6={r['rsi6']:>6} "
                  f"MACD={r['macd']['state']}")
        else:
            print(f"  {inst[2]:6s} ❌ {r.get('error','缺失')}")


if __name__ == "__main__":
    main()
