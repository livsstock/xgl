#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
facts_builder.py — 统一事实数据包（v1.0，2026-10-17）
=====================================================
【方法论根子】
客观数据只能有一份，主观判断可以有两套。
此前 WB 和大海各取各的数据、各算各的指标，同一只票 RSI 能差 4 个点
（10/15 华友：大海 18.87 vs WB 14.93）——分歧来源是混的，交叉验证无意义。

本模块：收盘后统一采集 + 统一算指标 + 统一质量门，
产出唯一一份 facts/YYYY-MM-DD.json，WB 和大海都只读它、不再各自算。

【质量门（不合格即停工，谁都不许预测）】
  G1 收盘价合理性: 0.1 < close < 10000，且 |当日涨跌| < 21%（跌停+误差）
  G2 成交量: 当日 volume > 0
  G3 历史充分: K线 >= 40 根（MACD 需 35+）
  G4 新鲜度: 数据最后一天 == 目标交易日（停牌/缺数即 FAIL）
  G5 指标可算: RSI6/MACD/MA20 均非 None
任一 FAIL → 整包标记 quality_gate=FAIL，并列出失败项。

【输出 schema（字段字典）】
  date                事实所属交易日
  generated_at        生成时间
  quality_gate        PASS / FAIL
  gate_failures       失败项列表
  indices{}           大盘指数事实（sh000001 等）
    close/chg_pct/ma20/ma20_dev/r_15/amplitude
  stocks[]            个股/ETF 事实
    code/name/sector/core_pool
    close/chg_pct/ten_day_chg          —— 价格事实
    rsi6/macd{dif,dea,hist,state}/ma20/ma20_dev/dd30/vol_ratio —— 指标事实
  _caliber            计算口径说明（RSI Wilder、MACD EMA12/26/9）
  _prohibit           「观点字段禁入」声明：本包不含 direction/action/kelly

【用法】
  python3 facts_builder.py                     # 今天
  python3 facts_builder.py --date 2026-10-16   # 指定交易日
  python3 facts_builder.py --push              # 生成并推送
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FACTS_DIR = os.path.join(BASE_DIR, "facts")

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")

UNIVERSE = [
    ("sh000001", "上证指数", "大盘", False),
    ("sh000300", "沪深300", "大盘", False),
    ("sh601899", "紫金矿业", "有色金属", True),
    ("sh600111", "北方稀土", "有色金属", True),
    ("sh603799", "华友钴业", "有色金属", True),
    ("sh601958", "金钼股份", "有色金属", True),
    ("sh512480", "半导体ETF", "半导体", True),
    ("sz000858", "五粮液", "白酒消费", True),
    ("sh601398", "工商银行", "银行", False),
    ("sh601288", "农业银行", "银行", False),
    ("sh600036", "招商银行", "银行", False),
    ("sz002142", "宁波银行", "银行", False),
    ("sh600392", "盛和资源", "稀土", False),
    ("sz000960", "锡业股份", "有色金属", False),
    ("sz002155", "湖南黄金", "贵金属", False),
    ("sh601212", "白银有色", "有色金属", False),
    ("sz000725", "京东方A", "面板", False),
    ("sh600875", "东方电气", "电力设备", False),
    ("sh600019", "宝钢股份", "钢铁", False),
    ("sh600893", "航发动力", "军工", False),
    ("sh600519", "贵州茅台", "白酒消费", False),
    ("sh601012", "隆基绿能", "光伏", False),
    ("sh601088", "中国神华", "煤炭", False),
    ("sh510300", "沪深300ETF", "宽基ETF", False),
    ("sh510500", "中证500ETF", "宽基ETF", False),
    ("sz159915", "创业板ETF", "宽基ETF", False),
    ("sh562500", "机器人ETF", "主题ETF", False),
    ("sh518880", "黄金ETF", "商品ETF", False),
]


# ---------- 指标计算（唯一实现，Wilder 口径）----------
def rsi_wilder(closes, n=6):
    """Wilder 平滑 RSI（通达信口径）。
    [校准 2026-10-17] 必须用全历史递推：截断窗口(如18个delta)会造成
    0.1~1.4 的漂移（华友 21.15 vs 标准 20.46）。全历史版与大海侧对算误差 0.00。"""
    if len(closes) < n + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def ema_series(vals, n):
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd_full(closes):
    """MACD(12,26,9) 返回 dif/dea/hist/state。
    [口径对齐 2026-10-17] state 判定看近3日: 3日内 DIF 上穿 DEA 记 golden_cross,
    下穿记 death_cross（与大海侧口径一致——金叉是'事件'，不只看最后一根）。
    全历史 EMA，与大海侧对算 hist 误差 <0.01。"""
    if len(closes) < 35:
        return None
    dif = [a - b for a, b in zip(ema_series(closes, 12), ema_series(closes, 26))]
    dea = ema_series(dif, 9)
    d, e = dif[-1], dea[-1]
    state = "above" if d > e else "below"
    for i in range(max(1, len(dif) - 3), len(dif)):
        if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
            state = "golden_cross"
        elif dif[i] < dea[i] and dif[i - 1] >= dea[i - 1]:
            state = "death_cross"
    return {"dif": round(d, 3), "dea": round(e, 3),
            "hist": round(d - e, 3), "state": state}


# ---------- 数据采集 ----------
def fetch_kline(sym, n=150):
    url = SINA.format(sym=sym, n=n)
    req = urllib.request.Request(url, headers=UA)
    raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
    data = json.loads(raw)
    out = []
    for r in data:
        try:
            out.append({"day": r["day"][:10],
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r["volume"])})
        except Exception:
            pass
    return out


def build_one(sym, name, sector, core, target_date, failures):
    try:
        kl = fetch_kline(sym)
    except Exception as e:
        failures.append(f"{name}({sym}) 取数失败: {e}")
        return None
    hist = [r for r in kl if r["day"] <= target_date]
    if not hist or hist[-1]["day"] != target_date:
        failures.append(f"{name}({sym}) G4新鮮度FAIL: 数据止于"
                        f"{hist[-1]['day'] if hist else '无'} != {target_date}")
        return None
    closes = [r["close"] for r in hist]
    vols = [r["volume"] for r in hist]
    close = closes[-1]
    prev = closes[-2] if len(closes) > 1 else close

    # ---- 质量门 ----
    gate = []
    if not (0.1 < close < 10000) or abs(close / prev - 1) > 0.21:
        gate.append("G1价格合理")
    if vols[-1] <= 0:
        gate.append("G2成交量")
    if len(closes) < 40:
        gate.append("G3历史充分")
    if gate:
        failures.append(f"{name}({sym}) " + "、".join(gate) + " FAIL")
        return None

    r6 = rsi_wilder(closes, 6)
    macd = macd_full(closes)
    ma20 = round(sum(closes[-20:]) / 20, 3)
    if r6 is None or macd is None or len(closes) < 20:
        failures.append(f"{name}({sym}) G5指标可算 FAIL")
        return None

    return {
        "code": sym, "name": name, "sector": sector, "core_pool": core,
        "close": close,
        "chg_pct": round((close / prev - 1) * 100, 2),
        "ten_day_chg": (round((close / closes[-11] - 1) * 100, 2)
                        if len(closes) > 11 else None),
        "rsi6": r6,
        "macd": macd,
        "ma20": ma20,
        "ma20_dev": round((close / ma20 - 1) * 100, 2),
        "dd30": round((close / max(closes[-30:]) - 1) * 100, 2),
        "vol_ratio": (round(vols[-1] / (sum(vols[-6:-1]) / 5), 2)
                      if len(vols) > 6 else None),
        "data_day": hist[-1]["day"],
    }


def build(target_date):
    failures = []
    indices, stocks = [], []
    for sym, name, sector, core in UNIVERSE:
        item = build_one(sym, name, sector, core, target_date, failures)
        if item is None:
            continue
        if sector == "大盘":
            indices.append(item)
        else:
            stocks.append(item)

    passed = not failures or (len(failures) <= 1 and len(stocks) + len(indices) >= len(UNIVERSE) - 1)
    # 容忍单只失败（如 ETF 停牌），但要在包里显式列出
    out = {
        "date": target_date,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "quality_gate": "FAIL" if failures else "PASS",
        "gate_failures": failures,
        "_partial": bool(failures),
        "indices": indices,
        "stocks": stocks,
        "_caliber": {
            "rsi": "Wilder平滑, n=6（通达信口径）",
            "macd": "EMA12/26, DEA=EMA9, hist=dif-dea",
            "ma20": "简单算术均线",
            "vol_ratio": "当日量 / 前5日均量",
            "ten_day_chg": "close / 10个交易日前close - 1",
            "dd30": "close / 30日最高close - 1",
            "source": "新浪日K money.finance.sina.com.cn scale=240 qfq",
        },
        "_prohibit": "本包为事实层，禁止写入 direction/action/kelly 等观点字段；"
                     "观点请写入 predictions/*.json 并引用本包 date+数值",
        "_schema_version": "facts-v1",
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    os.makedirs(FACTS_DIR, exist_ok=True)
    out = build(args.date)
    dst = os.path.join(FACTS_DIR, f"{args.date}.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"[{'FAIL' if out['quality_gate'] == 'FAIL' else 'OK'}] facts/{args.date}.json"
          f" ({os.path.getsize(dst)}B)")
    print(f"     质量门: {out['quality_gate']}"
          + (f" | 失败: {len(out['gate_failures'])}项" if out["gate_failures"] else ""))
    for s in out["stocks"]:
        if s["core_pool"]:
            m = s["macd"]
            print(f"     {s['name']:8s} close={s['close']:8.2f} chg={s['chg_pct']:+6.2f}%"
                  f" rsi6={s['rsi6']:6.2f} macd={m['state']}(hist={m['hist']})")
    if out["gate_failures"]:
        for f in out["gate_failures"]:
            print("     ⚠️", f)

    if args.push:
        r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "push_api.py"),
                            f"facts/{args.date}.json"],
                           capture_output=True, text=True, timeout=90)
        print("     推送:", (r.stdout or "").strip().splitlines()[-1] if r.stdout else "无输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
